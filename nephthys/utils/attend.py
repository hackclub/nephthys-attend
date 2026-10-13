# Client for the Attend API (https://attend.hackclub.com/docs/api-introduction)
import logging
from dataclasses import dataclass
from typing import Any

from nephthys.utils.env import env


@dataclass
class AttendEvent:
    name: str
    slug: str

    @property
    def dashboard_url(self) -> str:
        return f"{env.attend_base_url}/admin/{self.slug}"


class AttendApiError(Exception):
    pass


def participant_admin_url(event_slug: str, participant_event_id: str) -> str:
    return f"{env.attend_base_url}/admin/events/{event_slug}/participants/{participant_event_id}"


async def _get(path: str, params: dict[str, str]) -> dict[str, Any] | None:
    """GET an Attend API path, returning None on a 404."""
    async with env.session.get(
        f"{env.attend_base_url}/api/v1{path}",
        params=params,
        headers={"Authorization": f"Bearer {env.attend_api_key}"},
    ) as res:
        if res.status == 404:
            return None
        if res.status != 200:
            raise AttendApiError(
                f"Attend API GET {path} failed: {res.status} {await res.text()}"
            )
        return await res.json()


async def get_admin_events(slack_id: str) -> list[AttendEvent]:
    """Events (that haven't ended) the person with this Slack ID runs."""
    data = await _get("/users/lookup", {"slack_id": slack_id})
    if not data:
        logging.info(f"No Attend admin found for slack_id={slack_id}")
        return []
    return [AttendEvent(name=e["name"], slug=e["slug"]) for e in data["events"]]


async def find_participant_event_id(
    event_slug: str, *, email: str | None = None, slack_id: str | None = None
) -> str | None:
    """The participant_event_id of someone's registration for an event, if any."""
    params = {"email": email} if email else {"slack_id": slack_id or ""}
    data = await _get(f"/events/{event_slug}/participants/lookup", params)
    if not data:
        return None
    return data.get("participant_event_id")
