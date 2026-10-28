# Linking GitHub issues to tickets.
#
# When an admin posts a GitHub issue link in a help thread, privately ask them
# whether to link it to the ticket. Linked tickets are resolved once their
# issue is closed (see tasks/close_github_issues.py).
import logging
from typing import Any

from slack_bolt.context.respond.async_respond import AsyncRespond

from nephthys.database.enums import TicketStatus
from nephthys.database.tables import Ticket
from nephthys.database.tables import User
from nephthys.utils.env import env
from nephthys.utils.github import find_issue_links
from nephthys.utils.github import get_issue_state
from nephthys.utils.github import GitHubApiError
from nephthys.utils.github import GitHubIssue

LINK_ACTION = "github-issue-link"
DISMISS_ACTION = "github-issue-dismiss"

# Keep the prompt short if someone pastes a whole list of issues
MAX_ISSUES = 5


def issue_link(issue: GitHubIssue) -> str:
    return f"<{issue.url}|{issue}>"


def linked_issue(ticket: Ticket) -> GitHubIssue | None:
    if not ticket.github_issue_url:
        return None
    return GitHubIssue.from_url(ticket.github_issue_url)


def link_prompt_blocks(
    ticket: Ticket, issues: list[GitHubIssue]
) -> list[dict[str, Any]]:
    intro = "Link this to a GitHub issue? I'll resolve the ticket once the issue is closed. Only you can see this."
    if current := linked_issue(ticket):
        intro += f"\n_This ticket is already linked to {issue_link(current)}, which linking another issue will replace._"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": intro}}
    ]
    for issue in issues:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":link: {issue_link(issue)}"},
                "accessory": {
                    "type": "button",
                    "action_id": LINK_ACTION,
                    "text": {"type": "plain_text", "text": "Link"},
                    "style": "primary",
                    # The ticket rides along, since ephemeral messages can't be looked up later
                    "value": f"{ticket.id}|{issue.url}",
                },
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": DISMISS_ACTION,
                    "text": {"type": "plain_text", "text": "No thanks"},
                }
            ],
        }
    )
    return blocks


async def prompt_issue_link(ticket: Ticket, event: dict[str, Any]):
    """Called when an admin sends a message in a ticket's thread."""
    if ticket.status == TicketStatus.CLOSED:
        return
    current = linked_issue(ticket)
    issues = [i for i in find_issue_links(event.get("text", "")) if i != current]
    if not issues:
        return

    await env.slack_client.chat_postEphemeral(
        channel=event["channel"],
        user=event["user"],
        thread_ts=event["thread_ts"],
        text="Link this ticket to a GitHub issue?",
        blocks=link_prompt_blocks(ticket, issues[:MAX_ISSUES]),
    )


async def on_link_clicked(body: dict[str, Any], respond: AsyncRespond):
    slack_id = body["user"]["id"]
    ticket_id, issue_url = body["actions"][0]["value"].split("|", 1)
    if not (issue := GitHubIssue.from_url(issue_url)):
        raise ValueError(f"Invalid GitHub issue URL in button value: {issue_url}")

    user = await User.objects().get(User.slack_id == slack_id)
    if not (user and user.admin):
        await respond(
            replace_original=True,
            text="Only admins can link GitHub issues to tickets.",
        )
        return

    if not (ticket := await Ticket.objects().get(Ticket.id == int(ticket_id))):
        raise ValueError(f"Failed to find ticket ticket_id={ticket_id}")
    if ticket.status == TicketStatus.CLOSED:
        await respond(
            replace_original=True,
            text=f"This ticket has already been resolved, so I haven't linked {issue_link(issue)}.",
        )
        return

    try:
        state = await get_issue_state(issue)
    except GitHubApiError as e:
        logging.error(f"Failed to check GitHub issue={issue}: {e}")
        await respond(
            replace_original=True,
            text=f"I couldn't reach GitHub to check {issue_link(issue)}, so I haven't linked it. Post the link again to retry.",
        )
        return
    if state is None:
        await respond(
            replace_original=True,
            text=f"I couldn't find {issue_link(issue)} on GitHub. If it's in a private repo, set a `GITHUB_TOKEN` that can read it.",
        )
        return
    if state == "closed":
        await respond(
            replace_original=True,
            text=f"{issue_link(issue)} is already closed, so I haven't linked it.",
        )
        return

    await Ticket.update(
        {
            Ticket.github_issue_url: issue.url,
            Ticket.github_issue_linked_by: user.id,
        }
    ).where(Ticket.id == ticket.id)
    logging.info(
        f"Linked GitHub issue={issue} to ticket_id={ticket.id} by user_id={user.id}"
    )
    await respond(
        replace_original=True,
        text=f":link: Linked {issue_link(issue)} to this ticket. I'll resolve it once the issue is closed.",
    )
