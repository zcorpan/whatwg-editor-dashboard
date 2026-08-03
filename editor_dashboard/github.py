from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DashboardConfig
from .models import PullRequestSnapshot, snapshots_from_nodes


LOGGER = logging.getLogger(__name__)
GRAPHQL_ENDPOINT = "https://api.github.com/graphql"


class GitHubAPIError(RuntimeError):
    pass


@dataclass
class FetchMetadata:
    source: str
    query_count: int = 0
    total_cost: int = 0
    rate_limit: int | None = None
    rate_remaining: int | None = None
    rate_reset_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    def record_rate_limit(self, value: dict[str, Any] | None) -> None:
        if not value:
            return
        self.query_count += 1
        self.total_cost += int(value.get("cost") or 0)
        self.rate_limit = int(value.get("limit") or 0) or self.rate_limit
        self.rate_remaining = int(value.get("remaining") or 0)
        self.rate_reset_at = value.get("resetAt")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "query_count": self.query_count,
            "graphql_cost": self.total_cost,
            "rate_limit": self.rate_limit,
            "rate_remaining": self.rate_remaining,
            "rate_reset_at": self.rate_reset_at,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RepositoryData:
    open_pull_requests: tuple[PullRequestSnapshot, ...]
    recently_closed_pull_requests: tuple[PullRequestSnapshot, ...]
    metadata: FetchMetadata


class GraphQLClient:
    def __init__(self, token: str, *, endpoint: str = GRAPHQL_ENDPOINT, max_retries: int = 3) -> None:
        if not token:
            raise ValueError("A GitHub token is required")
        self.token = token
        self.endpoint = endpoint
        self.max_retries = max_retries

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "whatwg-editor-dashboard/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if result.get("errors"):
                    messages = "; ".join(str(error.get("message", error)) for error in result["errors"])
                    raise GitHubAPIError(f"GitHub GraphQL error: {messages}")
                data = result.get("data")
                if not isinstance(data, dict):
                    raise GitHubAPIError("GitHub GraphQL response did not contain a data object")
                return data
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                last_error = GitHubAPIError(f"GitHub returned HTTP {error.code}: {body[:1000]}")
                retryable = error.code in {429, 500, 502, 503, 504}
                if not retryable or attempt >= self.max_retries:
                    raise last_error from error
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt >= self.max_retries:
                    raise GitHubAPIError(f"GitHub request failed: {error}") from error
            time.sleep(2**attempt)

        raise GitHubAPIError(f"GitHub request failed: {last_error}")


def _deduplicate_pr_nodes(
    nodes: list[dict[str, Any]],
    *,
    label: str,
    metadata: FetchMetadata,
) -> list[dict[str, Any]]:
    """Preserve the first snapshot for each PR and report pagination drift.

    Connections ordered by mutable fields can change while a multi-page fetch is
    running. A duplicate is harmless for the dashboard as long as we surface it
    and avoid counting the PR twice.
    """
    unique: dict[int, dict[str, Any]] = {}
    duplicates: set[int] = set()
    for node in nodes:
        number = node.get("number")
        if number is None:
            raise GitHubAPIError(f"{label} query returned a pull request without a number")
        normalized = int(number)
        if normalized in unique:
            duplicates.add(normalized)
            continue
        unique[normalized] = node

    if duplicates:
        joined = ", ".join(f"#{number}" for number in sorted(duplicates))
        metadata.warnings.append(
            f"Deduplicated {label.lower()} pull requests returned more than once during pagination: {joined}."
        )
    return list(unique.values())


def _read_query(name: str) -> str:
    path = Path(__file__).resolve().parent.parent / "graphql" / name
    return path.read_text(encoding="utf-8")


def fetch_repository_data(
    config: DashboardConfig,
    token: str,
    *,
    now: datetime | None = None,
    client: GraphQLClient | None = None,
) -> RepositoryData:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    graphql = client or GraphQLClient(token)
    metadata = FetchMetadata(source="github-graphql")

    common_variables = {
        "owner": config.repository.owner,
        "name": config.repository.name,
        "viewer": config.viewer,
        "pageSize": 50,
        "timelineEachEnd": config.sampling.timeline_each_end,
        "viewerReviewCount": config.sampling.viewer_reviews,
        "threadCount": config.sampling.review_threads,
    }

    open_nodes: list[dict[str, Any]] = []
    open_query = _read_query("open_pull_requests.graphql")
    cursor: str | None = None
    expected_total: int | None = None
    while True:
        variables = {**common_variables, "cursor": cursor}
        data = graphql.execute(open_query, variables)
        metadata.record_rate_limit(data.get("rateLimit"))
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise GitHubAPIError(
                f"Repository {config.repository.slug} was not found or is not accessible to the token"
            )
        connection = repository.get("pullRequests")
        if not isinstance(connection, dict):
            raise GitHubAPIError("GitHub GraphQL response omitted the open pull-request connection")
        expected_total = int(connection.get("totalCount") or 0)
        open_nodes.extend(node for node in (connection.get("nodes") or []) if node)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubAPIError("Open PR pagination reported another page without an end cursor")

    if expected_total is not None and len(open_nodes) != expected_total:
        metadata.warnings.append(
            f"Expected {expected_total} open pull requests but fetched {len(open_nodes)}."
        )
    open_nodes = _deduplicate_pr_nodes(open_nodes, label="Open", metadata=metadata)

    cutoff = now - timedelta(days=config.sampling.history_days)
    search_query = (
        f"repo:{config.repository.slug} is:pr is:closed "
        f"closed:>={cutoff.date().isoformat()} sort:updated-desc"
    )
    closed_nodes: list[dict[str, Any]] = []
    closed_query = _read_query("recent_closed_pull_requests.graphql")
    cursor = None
    issue_count: int | None = None
    while True:
        variables = {
            "query": search_query,
            "cursor": cursor,
            "pageSize": 50,
            "viewer": config.viewer,
            "timelineEachEnd": config.sampling.timeline_each_end,
            "viewerReviewCount": config.sampling.viewer_reviews,
        }
        data = graphql.execute(closed_query, variables)
        metadata.record_rate_limit(data.get("rateLimit"))
        connection = data.get("search")
        if not isinstance(connection, dict):
            raise GitHubAPIError("GitHub GraphQL response omitted the recent closed-PR search connection")
        issue_count = int(connection.get("issueCount") or 0)
        closed_nodes.extend(node for node in (connection.get("nodes") or []) if node)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubAPIError("Closed PR pagination reported another page without an end cursor")

    if issue_count is not None and issue_count > 1000:
        metadata.warnings.append(
            "GitHub search is capped at 1,000 results; historical metrics may be incomplete."
        )
    if issue_count is not None and len(closed_nodes) < min(issue_count, 1000):
        metadata.warnings.append(
            f"Search reported {issue_count} recently closed PRs but fetched {len(closed_nodes)}."
        )
    closed_nodes = _deduplicate_pr_nodes(closed_nodes, label="Recently closed", metadata=metadata)

    # A PR can close between the open-connection fetch and the later closed search.
    # Prefer the later closed snapshot so the public dashboard does not show the
    # same PR as both open and closed or count it twice in flow metrics.
    closed_numbers = {int(node["number"]) for node in closed_nodes}
    overlap = sorted(int(node["number"]) for node in open_nodes if int(node["number"]) in closed_numbers)
    if overlap:
        joined = ", ".join(f"#{number}" for number in overlap)
        metadata.warnings.append(
            f"PRs changed from open to closed during the build; using the later closed snapshot: {joined}."
        )
        open_nodes = [node for node in open_nodes if int(node["number"]) not in closed_numbers]

    LOGGER.info(
        "Fetched %d open and %d recently closed PRs in %d GraphQL queries (cost %d, remaining %s)",
        len(open_nodes),
        len(closed_nodes),
        metadata.query_count,
        metadata.total_cost,
        metadata.rate_remaining,
    )

    return RepositoryData(
        open_pull_requests=tuple(snapshots_from_nodes(open_nodes)),
        recently_closed_pull_requests=tuple(snapshots_from_nodes(closed_nodes)),
        metadata=metadata,
    )


def load_fixture(path: str | Path) -> RepositoryData:
    fixture_path = Path(path)
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    metadata_raw = raw.get("metadata") or {}
    metadata = FetchMetadata(source="fixture")
    metadata.query_count = int(metadata_raw.get("query_count") or 0)
    metadata.total_cost = int(metadata_raw.get("graphql_cost") or 0)
    metadata.rate_limit = metadata_raw.get("rate_limit")
    metadata.rate_remaining = metadata_raw.get("rate_remaining")
    metadata.rate_reset_at = metadata_raw.get("rate_reset_at")
    metadata.warnings.extend(str(value) for value in (metadata_raw.get("warnings") or []))
    return RepositoryData(
        open_pull_requests=tuple(snapshots_from_nodes(raw.get("open_pull_requests") or [])),
        recently_closed_pull_requests=tuple(snapshots_from_nodes(raw.get("recently_closed_pull_requests") or [])),
        metadata=metadata,
    )
