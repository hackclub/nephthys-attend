# Client for the GitHub REST API (https://docs.github.com/en/rest/issues/issues)
import re
from dataclasses import dataclass
from typing import Literal

from nephthys.utils.env import env

# Slack wraps links as <url> or <url|label>, which the character classes stop at
ISSUE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/issues/(\d+)"
)


class GitHubApiError(Exception):
    pass


class GitHubRateLimitError(GitHubApiError):
    pass


@dataclass(frozen=True)
class GitHubIssue:
    owner: str
    repo: str
    number: int

    @classmethod
    def from_match(cls, owner: str, repo: str, number: str) -> "GitHubIssue":
        # GitHub names are case-insensitive, so normalise them for comparisons
        return cls(owner.lower(), repo.lower(), int(number))

    @classmethod
    def from_url(cls, url: str) -> "GitHubIssue | None":
        if not (match := ISSUE_URL_PATTERN.fullmatch(url)):
            return None
        return cls.from_match(*match.groups())

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def find_issue_links(text: str) -> list[GitHubIssue]:
    """Every distinct GitHub issue linked in a message, in order."""
    issues = [GitHubIssue.from_match(*m) for m in ISSUE_URL_PATTERN.findall(text)]
    return list(dict.fromkeys(issues))


async def get_issue_state(issue: GitHubIssue) -> Literal["open", "closed"] | None:
    """Whether an issue is open or closed, or None if it doesn't exist or we can't see it."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nephthys",
    }
    if env.github_token:
        headers["Authorization"] = f"Bearer {env.github_token}"
    async with env.session.get(
        f"https://api.github.com/repos/{issue.owner}/{issue.repo}/issues/{issue.number}",
        headers=headers,
    ) as res:
        if res.status in (404, 410):
            return None
        if res.status == 429 or (
            res.status == 403 and res.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise GitHubRateLimitError(
                f"GitHub API rate limit hit, resets at {res.headers.get('x-ratelimit-reset')}"
            )
        if res.status != 200:
            raise GitHubApiError(
                f"GitHub API GET {issue} failed: {res.status} {await res.text()}"
            )
        return (await res.json())["state"]
