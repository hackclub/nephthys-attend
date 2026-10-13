# Attend context for questions in the admin help channel.
#
# When an event admin asks a question, work out which of their events it's
# about (asking them if they run more than one), link its dashboard in the
# thread, and let them privately name the participant they need help with so
# the thread gets a link to that participant's admin page.
import logging
import re
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from nephthys.database.tables import User
from nephthys.utils.attend import AttendApiError
from nephthys.utils.attend import AttendEvent
from nephthys.utils.attend import find_participant_event_id
from nephthys.utils.attend import get_admin_events
from nephthys.utils.attend import participant_admin_url
from nephthys.utils.env import env

EVENT_SELECT_ACTION = "attend-event-select"
PARTICIPANT_INPUT_ACTION = "attend-participant-lookup"

# Slack caps static selects at 100 options of 75 characters each
MAX_EVENT_OPTIONS = 100
MAX_OPTION_TEXT = 75

# A bare ID or a mention; case-sensitive so names like "williams" aren't IDs
SLACK_ID_PATTERN = re.compile(r"^(?:<@)?([UW][A-Z0-9]{6,})(?:\|[^>]*)?>?$")


def escape_mrkdwn(text: str) -> str:
    """Escape text from Attend (event names are set by event admins) so it
    can't break out of a link or turn into a mention like <!channel>."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def event_message_text(event: AttendEvent) -> str:
    return f"This question is about {escape_mrkdwn(event.name)}: {event.dashboard_url}"


def event_message_blocks(event: AttendEvent) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":calendar: This question is about *<{event.dashboard_url}|{escape_mrkdwn(event.name)}>*",
            },
        }
    ]


def participant_prompt_blocks(
    event_slug: str, thread_ts: str, error: str | None = None
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "input",
            # The event and thread ride along in the block ID, since ephemeral
            # messages can't be looked up later
            "block_id": f"attend-participant|{event_slug}|{thread_ts}",
            "dispatch_action": True,
            "label": {
                "type": "plain_text",
                "text": "Is this about a specific participant?",
            },
            "hint": {
                "type": "plain_text",
                "text": "Enter their email or Slack ID and press enter to link their admin page in this thread. Only you can see this.",
            },
            "element": {
                "type": "plain_text_input",
                "action_id": PARTICIPANT_INPUT_ACTION,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Their email or Slack ID",
                },
                "dispatch_action_config": {"trigger_actions_on": ["on_enter_pressed"]},
            },
        }
    ]
    if error:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":warning: {error}"}],
            }
        )
    return blocks


async def post_event_context(
    event: AttendEvent, author_id: str, channel: str, thread_ts: str
):
    """Link the event in the thread, then privately ask the author which participant it's about."""
    await env.slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=event_message_text(event),
        blocks=event_message_blocks(event),
        unfurl_links=False,
    )
    await post_participant_prompt(event.slug, author_id, channel, thread_ts)


async def post_participant_prompt(
    event_slug: str, author_id: str, channel: str, thread_ts: str
):
    await env.slack_client.chat_postEphemeral(
        channel=channel,
        user=author_id,
        thread_ts=thread_ts,
        text="Is this about a specific participant? Enter their email or Slack ID.",
        blocks=participant_prompt_blocks(event_slug, thread_ts),
    )


async def start_attend_flow(event: dict[str, Any]):
    """Called once a new question's ticket has been created."""
    author_id = event["user"]
    channel = event["channel"]
    thread_ts = event["ts"]

    events = await get_admin_events(author_id)
    if not events:
        return
    if len(events) == 1:
        await post_event_context(events[0], author_id, channel, thread_ts)
        return

    await env.slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text="Which event is this question about?",
        blocks=[
            {
                "type": "section",
                # The author rides along so only they (or a helper) can answer
                "block_id": f"attend-event|{author_id}",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<@{author_id}>, which event is this question about?",
                },
                "accessory": {
                    "type": "static_select",
                    "action_id": EVENT_SELECT_ACTION,
                    "placeholder": {"type": "plain_text", "text": "Select an event"},
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": e.name[:MAX_OPTION_TEXT],
                            },
                            "value": e.slug,
                        }
                        for e in events[:MAX_EVENT_OPTIONS]
                    ],
                },
            }
        ],
    )


async def on_event_selected(body: dict[str, Any], client: AsyncWebClient):
    action = body["actions"][0]
    selector_id = body["user"]["id"]
    author_id = action["block_id"].split("|", 1)[1]
    channel = body["channel"]["id"]
    message = body["message"]
    thread_ts = message.get("thread_ts", message["ts"])

    if selector_id != author_id:
        selector = await User.objects().get(User.slack_id == selector_id)
        if not (selector and selector.helper):
            await client.chat_postEphemeral(
                channel=channel,
                user=selector_id,
                thread_ts=thread_ts,
                text="Only the person who asked this question can pick its event.",
            )
            return

    option = action["selected_option"]
    selected = AttendEvent(name=option["text"]["text"], slug=option["value"])
    logging.info(
        f"Attend event selected event={selected.slug} thread_ts={thread_ts} by={selector_id}"
    )
    await client.chat_update(
        channel=channel,
        ts=message["ts"],
        text=event_message_text(selected),
        blocks=event_message_blocks(selected),
    )
    await post_participant_prompt(selected.slug, author_id, channel, thread_ts)


def parse_participant_query(query: str) -> dict[str, str] | None:
    """Turn what someone typed into lookup params: an email or a Slack ID."""
    query = query.strip()
    if match := SLACK_ID_PATTERN.match(query):
        return {"slack_id": match.group(1)}
    if "@" in query and " " not in query:
        return {"email": query.lower().removeprefix("mailto:")}
    return None


async def on_participant_submitted(body: dict[str, Any], client: AsyncWebClient):
    action = body["actions"][0]
    _, event_slug, thread_ts = action["block_id"].split("|", 2)
    channel = body["channel"]["id"]
    user_id = body["user"]["id"]
    response_url = body["response_url"]

    async def show_error(error: str):
        await respond(
            response_url,
            {
                "replace_original": True,
                "text": error,
                "blocks": participant_prompt_blocks(event_slug, thread_ts, error),
            },
        )

    params = parse_participant_query(action.get("value") or "")
    if not params:
        await show_error("That doesn't look like an email address or a Slack ID.")
        return

    try:
        participant_event_id = await find_participant_event_id(event_slug, **params)
    except AttendApiError as e:
        logging.error(f"Attend participant lookup failed event={event_slug}: {e}")
        await show_error("Attend couldn't look that up. Try their email instead.")
        return
    if not participant_event_id:
        await show_error(
            "No one is registered for this event with that email or Slack ID."
        )
        return

    url = participant_admin_url(event_slug, participant_event_id)
    await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"Participant: {url}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":bust_in_silhouette: <@{user_id}> is asking about <{url}|this participant>",
                },
            }
        ],
        unfurl_links=False,
    )
    await respond(response_url, {"delete_original": True})


async def respond(response_url: str, payload: dict[str, Any]):
    """Edit or delete an ephemeral message, which only its response URL can do."""
    async with env.session.post(response_url, json=payload) as res:
        if res.status != 200:
            logging.error(
                f"Failed to respond to Slack: {res.status} {await res.text()}"
            )
