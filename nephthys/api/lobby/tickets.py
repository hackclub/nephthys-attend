import asyncio
import logging
import re
from datetime import datetime
from datetime import timedelta
from datetime import UTC

from slack_sdk.errors import SlackApiError
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.responses import Response

from nephthys.actions.resolve import resolve
from nephthys.api.lobby.lobby import app_context
from nephthys.api.lobby.lobby import bad_request
from nephthys.api.lobby.lobby import get_logged_in_user
from nephthys.api.lobby.lobby import is_same_origin_request
from nephthys.api.lobby.lobby import templates
from nephthys.api.lobby.lobby import you_must_be_logged_in
from nephthys.database.enums import TicketStatus
from nephthys.database.enums import UserType
from nephthys.database.tables import Ticket
from nephthys.database.tables import User
from nephthys.events.attend import EVENT_MESSAGE_PATTERN
from nephthys.events.attend import PARTICIPANT_MESSAGE_PATTERN
from nephthys.events.message_creation import record_thread_message
from nephthys.utils.env import env
from nephthys.utils.slack_user import get_user_profile
from nephthys.utils.ticket_methods import get_question_message_link

QUEUES = {
    "open": "unanswered",
    "in_progress": "in progress",
    "mine": "assigned to me",
    "closed": "recently closed",
}
MAX_TICKETS_SHOWN = 200
MAX_REPLY_LEN = 4000

SLACK_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
SLACK_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>")
SLACK_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
URL = re.compile(r"https?://[^\s<>\"')\]]+")


def helpers_only(req: Request) -> Response:
    return templates.TemplateResponse(
        req, "helpers_only.jinja", context=app_context(), status_code=403
    )


async def get_logged_in_helper(req: Request) -> tuple[User | None, Response | None]:
    """Returns the logged-in helper, or a response to send if they aren't one"""
    user = await get_logged_in_user(req)
    if not user:
        return None, you_must_be_logged_in(req)
    if not user.helper:
        return None, helpers_only(req)
    return user, None


async def ticket_queue(req: Request):
    user, error = await get_logged_in_helper(req)
    if not user:
        return error

    queue = req.query_params.get("queue", "open")
    if queue not in QUEUES:
        queue = "open"

    query = Ticket.objects(Ticket.opened_by, Ticket.assigned_to, Ticket.category_tag)
    match queue:
        case "open":
            query = query.where(Ticket.status == TicketStatus.OPEN).order_by(
                Ticket.created_at
            )
        case "in_progress":
            query = query.where(Ticket.status == TicketStatus.IN_PROGRESS).order_by(
                Ticket.last_msg_at
            )
        case "mine":
            query = query.where(
                (Ticket.assigned_to == user.id) & (Ticket.status != TicketStatus.CLOSED)
            ).order_by(Ticket.last_msg_at)
        case "closed":
            week_ago = datetime.now(UTC) - timedelta(days=7)
            query = query.where(
                (Ticket.status == TicketStatus.CLOSED) & (Ticket.closed_at >= week_ago)
            ).order_by(Ticket.closed_at, ascending=False)
    tickets = await query.limit(MAX_TICKETS_SHOWN)

    counts = {
        "open": await Ticket.count().where(Ticket.status == TicketStatus.OPEN),
        "in_progress": await Ticket.count().where(
            Ticket.status == TicketStatus.IN_PROGRESS
        ),
        "mine": await Ticket.count().where(
            (Ticket.assigned_to == user.id) & (Ticket.status != TicketStatus.CLOSED)
        ),
    }

    context = {
        "user": user,
        "queue": queue,
        "queues": QUEUES,
        "counts": counts,
        "tickets": tickets,
        "max_tickets_shown": MAX_TICKETS_SHOWN,
        "now": datetime.now(UTC),
        "UserType": UserType,
        **app_context(),
    }
    return templates.TemplateResponse(req, "tickets.jinja", context=context)


def slack_text_to_plain(text: str, names: dict[str, str]) -> str:
    """Turns Slack's message formatting into readable plain text"""
    text = SLACK_MENTION.sub(
        lambda m: f"@{names.get(m.group(1), m.group(1))}",
        text,
    )
    text = SLACK_SPECIAL.sub(lambda m: f"@{m.group(1)}", text)
    text = SLACK_LINK.sub(
        lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1),
        text,
    )
    return unescape_slack_text(text)


def unescape_slack_text(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def linkify(text: str) -> list[tuple[str, bool]]:
    """Splits text into (segment, is_url) parts so the template can link URLs"""
    parts: list[tuple[str, bool]] = []
    last = 0
    for match in URL.finditer(text):
        url = match.group().rstrip(".,;:!?")
        parts.append((text[last : match.start()], False))
        parts.append((url, True))
        last = match.start() + len(url)
    parts.append((text[last:], False))
    return [part for part in parts if part[0]]


def find_attend_links(raw_messages: list[dict]) -> dict | None:
    """Finds the Attend event and participants the bot linked in a thread"""
    event = None
    participants: list[str] = []
    for m in raw_messages:
        if "bot_id" not in m:
            continue
        text = m.get("text", "")
        if match := EVENT_MESSAGE_PATTERN.match(text):
            name, url = unescape_slack_text(match["name"]), match["url"]
            # The latest pick wins, in case the event was picked again
            event = {"name": name, "url": url}
        elif match := PARTICIPANT_MESSAGE_PATTERN.match(text):
            if match["url"] not in participants:
                participants.append(match["url"])

    # Only trust links that actually point at Attend
    if event and not event["url"].startswith(env.attend_base_url + "/"):
        event = None
    participants = [
        url for url in participants if url.startswith(env.attend_base_url + "/")
    ]
    if not event and not participants:
        return None
    return {"event": event, "participants": participants}


async def get_display_names(slack_ids: set[str]) -> dict[str, str]:
    async def get_name(slack_id: str) -> tuple[str, str]:
        try:
            return slack_id, (await get_user_profile(slack_id)).display_name()
        except Exception as e:
            logging.warning(f"Failed to look up Slack user slack_id={slack_id}: {e}")
            return slack_id, slack_id

    return dict(await asyncio.gather(*(get_name(i) for i in slack_ids)))


async def get_thread_messages(ticket: Ticket) -> list[dict]:
    """Fetches the raw messages in a ticket's Slack thread"""
    try:
        res = await env.slack_client.conversations_replies(
            channel=env.slack_help_channel, ts=ticket.msg_ts, limit=200
        )
    except SlackApiError as e:
        logging.warning(f"Failed to fetch thread for ticket_id={ticket.id}: {e}")
        return []
    return res.get("messages", [])


async def format_thread_messages(
    ticket: Ticket, raw_messages: list[dict]
) -> list[dict]:
    """Turns raw Slack messages into a template-friendly format"""
    slack_ids = {m["user"] for m in raw_messages if m.get("user")}
    for m in raw_messages:
        slack_ids.update(SLACK_MENTION.findall(m.get("text", "")))
    names = await get_display_names(slack_ids)

    opener_slack_id = ticket.opened_by.slack_id if ticket.opened_by else None
    messages = []
    for m in raw_messages:
        is_bot = "bot_id" in m
        author_id = m.get("user")
        if is_bot:
            # Replies sent from the web UI use a customised bot username
            author = m.get("username") or m.get("bot_profile", {}).get("name", "bot")
        else:
            author = names.get(author_id or "", author_id or "unknown")
        messages.append(
            {
                "author": author,
                "is_bot": is_bot,
                "is_opener": bool(author_id and author_id == opener_slack_id),
                "parts": linkify(slack_text_to_plain(m.get("text", ""), names)),
                "files": [f.get("name") or "file" for f in m.get("files", [])],
                "sent_at": datetime.fromtimestamp(float(m["ts"]), UTC),
            }
        )
    return messages


async def get_ticket_from_path(req: Request) -> Ticket | None:
    try:
        ticket_id = int(req.path_params["id"])
    except (KeyError, ValueError):
        return None
    return (
        await Ticket.objects(Ticket.opened_by, Ticket.assigned_to, Ticket.category_tag)
        .where(Ticket.id == ticket_id)
        .first()
    )


async def ticket_detail(req: Request):
    user, error = await get_logged_in_helper(req)
    if not user:
        return error
    ticket = await get_ticket_from_path(req)
    if not ticket:
        return Response("no ticket with that id.", status_code=404)

    raw_messages = await get_thread_messages(ticket)
    context = {
        "user": user,
        "ticket": ticket,
        "messages": await format_thread_messages(ticket, raw_messages),
        "attend": find_attend_links(raw_messages),
        "slack_link": get_question_message_link(ticket),
        "error": req.query_params.get("error"),
        "max_reply_len": MAX_REPLY_LEN,
        **app_context(),
    }
    return templates.TemplateResponse(req, "ticket.jinja", context=context)


def escape_slack_text(text: str) -> str:
    """Escapes control characters so the text shows up in Slack as typed"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def back_to_ticket(ticket: Ticket, error: str | None = None) -> RedirectResponse:
    url = f"/lobby/tickets/{ticket.id}"
    if error:
        url += f"?error={error}"
    return RedirectResponse(url=url, status_code=303)


async def post_reply(ticket: Ticket, helper: User, text: str) -> bool:
    """Sends a reply in the ticket's thread on behalf of a helper.

    Returns False if the thread no longer exists.
    """
    profile = await get_user_profile(helper.slack_id)
    response = await env.slack_client.chat_postMessage(
        channel=env.slack_help_channel,
        thread_ts=ticket.msg_ts,
        text=escape_slack_text(text),
        username=profile.display_name(),
        icon_url=profile.profile_pic_512x(),
    )
    msg: dict = response["message"]  # type: ignore (assuming message exists)
    if not msg.get("thread_ts"):
        # The top-level message got deleted, so this landed in the channel instead
        logging.warning(
            f"Web reply was sent outside of thread for ticket_id={ticket.id}. Unsending."
        )
        await env.slack_client.chat_delete(channel=env.slack_help_channel, ts=msg["ts"])
        return False

    # Bot messages are ignored by the message handler, so do its bookkeeping here
    await record_thread_message(ticket, helper, helper.slack_id)
    logging.info(f"user_id={helper.id} replied to ticket_id={ticket.id} from the web")
    return True


async def ticket_reply(req: Request):
    user, error = await get_logged_in_helper(req)
    if not user:
        return error
    if not is_same_origin_request(req):
        logging.warning(f"user_id={user.id} attempted web reply; not same-origin")
        return bad_request("CSRF detected :O (request must be same-origin)")
    ticket = await get_ticket_from_path(req)
    if not ticket:
        return Response("no ticket with that id.", status_code=404)

    form_data = await req.form()
    text = form_data.get("text")
    action = form_data.get("action")
    if type(text) is not str or action not in ("reply", "reply_resolve", "resolve"):
        return bad_request("that form doesn't look right...")
    text = text.strip()

    if action != "resolve":
        if not text:
            return back_to_ticket(ticket, "empty_reply")
        if len(text) > MAX_REPLY_LEN:
            return back_to_ticket(ticket, "reply_too_long")
        if not await post_reply(ticket, user, text):
            return back_to_ticket(ticket, "thread_gone")

    if action != "reply":
        if ticket.status == TicketStatus.CLOSED:
            return back_to_ticket(ticket, "already_closed")
        await resolve(ticket.msg_ts, user.slack_id, env.slack_client)
        logging.info(f"user_id={user.id} resolved ticket_id={ticket.id} from the web")

    return back_to_ticket(ticket)
