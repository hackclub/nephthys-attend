import logging
from collections import defaultdict

from nephthys.actions.resolve import resolve
from nephthys.database.enums import TicketStatus
from nephthys.database.tables import Ticket
from nephthys.utils.env import env
from nephthys.utils.github import get_issue_state
from nephthys.utils.github import GitHubApiError
from nephthys.utils.github import GitHubIssue
from nephthys.utils.github import GitHubRateLimitError
from nephthys.utils.logging import send_heartbeat


async def resolve_for_closed_issue(ticket: Ticket, issue: GitHubIssue):
    # Resolve as whoever linked the issue, falling back to the maintainer if
    # they've since been removed
    resolver = ticket.github_issue_linked_by.slack_id or env.slack_maintainer_id
    logging.info(
        f"Resolving ticket_id={ticket.id} because its GitHub issue={issue} was closed"
    )
    await resolve(
        ticket.msg_ts,
        resolver,
        env.slack_client,
        resolved_message=env.transcript.ticket_resolve_github_issue.format(
            issue_url=issue.url, issue_name=str(issue)
        ),
    )
    # The link has done its job, and keeping it would re-close the ticket if
    # someone reopens it
    await Ticket.update(
        {Ticket.github_issue_url: None, Ticket.github_issue_linked_by: None}
    ).where(Ticket.id == ticket.id)


async def close_tickets_for_closed_issues():
    """
    Resolves open tickets whose linked GitHub issue has been closed.

    This task is intended to be run periodically (e.g., every 10 minutes).
    """
    tickets = await Ticket.objects(Ticket.github_issue_linked_by).where(
        (Ticket.status != TicketStatus.CLOSED) & Ticket.github_issue_url.is_not_null()
    )
    # Several tickets are often waiting on the same bug, so check each issue once
    tickets_by_issue: dict[GitHubIssue, list[Ticket]] = defaultdict(list)
    for ticket in tickets:
        if issue := GitHubIssue.from_url(ticket.github_issue_url or ""):
            tickets_by_issue[issue].append(ticket)
        else:
            logging.error(
                f"Invalid GitHub issue URL on ticket_id={ticket.id}: {ticket.github_issue_url}"
            )

    for issue, issue_tickets in tickets_by_issue.items():
        try:
            state = await get_issue_state(issue)
        except GitHubRateLimitError as e:
            logging.warning(f"Checking the rest of the GitHub issues next time: {e}")
            return
        except GitHubApiError as e:
            logging.error(f"Failed to check GitHub issue={issue}: {e}")
            continue

        if state is None:
            logging.warning(
                f"GitHub issue={issue} linked to {len(issue_tickets)} ticket(s) no longer exists or isn't visible"
            )
        if state != "closed":
            continue

        for ticket in issue_tickets:
            try:
                await resolve_for_closed_issue(ticket, issue)
            except Exception as e:
                logging.error(
                    f"Failed to resolve ticket_id={ticket.id} for closed GitHub issue={issue}: {e}"
                )
                await send_heartbeat(
                    f"Failed to resolve ticket {ticket.id} for closed GitHub issue {issue}: {e}"
                )
