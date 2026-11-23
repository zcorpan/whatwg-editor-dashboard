from __future__ import annotations

import http.client
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DashboardConfig
from .models import PullRequestSnapshot, snapshots_from_nodes


LOGGER = logging.getLogger(__name__)
GRAPHQL_ENDPOINT = "https://api.github.com/graphql"

# How long one build may spend waiting between retries, summed over every request it makes.
# A burst of 502s clears in seconds, but a GitHub incident runs for tens of minutes, and the
# scheduled build has nobody waiting for it, so outlasting most incidents is cheaper than
# skipping a day. The build job's `timeout-minutes` in .github/workflows/dashboard.yml has
# to leave room for this budget plus the requests themselves, each bounded by
# `timeout_seconds`, on top of a normal build.
DEFAULT_RETRY_BUDGET_SECONDS = 60 * 60.0
# The delay doubles from the first value to the cap: 37.5 s, 75 s, 150 s, then five minutes
# for the rest of the budget, so a long outage is polled every five minutes instead of once
# an hour. Nobody is waiting on the scheduled build, so the first retry is not in a hurry.
DEFAULT_RETRY_BASE_SECONDS = 37.5
DEFAULT_MAX_RETRY_DELAY_SECONDS = 300.0
# Doubling to the delay cap and then spending a whole budget at the cap takes about this
# many attempts; the count is a safety net against retrying forever when the delays are
# zero, not the working limit.
DEFAULT_MAX_RETRIES = 20

# GitHub also reports a GraphQL timeout inside an HTTP 200 body, as an `errors` entry whose
# message starts like this ("...This may be the result of a timeout, or it could be a GitHub
# bug.") and says nothing else that would identify the query. Only this wording is retried;
# any other GraphQL error is a real answer.
_TRANSIENT_GRAPHQL_ERROR_MARKER = "something went wrong while executing your query"


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
    request_attempts: int = 0
    retry_count: int = 0
    timeout_count: int = 0
    effective_page_size: int | None = None
    drafts_excluded: int = 0
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
            "request_attempts": self.request_attempts,
            "retry_count": self.retry_count,
            "timeout_count": self.timeout_count,
            "effective_page_size": self.effective_page_size,
            "draft_prs_excluded": self.drafts_excluded,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RepositoryData:
    open_pull_requests: tuple[PullRequestSnapshot, ...]
    recently_closed_pull_requests: tuple[PullRequestSnapshot, ...]
    metadata: FetchMetadata


@dataclass(frozen=True)
class _Failure:
    """One failed attempt, classified for the retry loop."""

    error: GitHubAPIError
    # A one-line version for the retry log; ``error`` may carry a response body.
    summary: str
    cause: BaseException | None
    retryable: bool
    # A GitHub-side timeout, which also halves the outer page size.
    timeout: bool
    delay: float


class GraphQLClient:
    """Small resilient client for GitHub's GraphQL endpoint.

    GitHub documents HTTP 502 and 504 responses as GraphQL timeouts. This
    dashboard asks for several nested connections for every PR, so a timed-out
    page is retried with a smaller outer ``pageSize``. The smaller size is then
    retained for subsequent pages in the same build.

    Retries are bounded by a waiting budget shared across the whole build rather
    than by a count per request: the delay doubles from ``retry_base_seconds`` up
    to ``max_retry_delay_seconds``, and the build gives up once it has slept
    ``retry_budget_seconds`` in total. ``max_retries`` is a per-request safety net
    that only matters when the delays are zero.
    """

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = GRAPHQL_ENDPOINT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS,
        retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS,
        timeout_seconds: float = 30.0,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not token:
            raise ValueError("A GitHub token is required")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must not be negative")
        if max_retry_delay_seconds < 0:
            raise ValueError("max_retry_delay_seconds must not be negative")
        if retry_budget_seconds < 0:
            raise ValueError("retry_budget_seconds must not be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.token = token
        self.endpoint = endpoint
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.retry_budget_seconds = retry_budget_seconds
        self.timeout_seconds = timeout_seconds
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._page_size_cap: int | None = None

        self.request_attempts = 0
        self.retry_count = 0
        self.timeout_count = 0
        self.retry_seconds_spent = 0.0

    @property
    def page_size_cap(self) -> int | None:
        return self._page_size_cap

    def _make_request(self, query: str, variables: dict[str, Any]) -> urllib.request.Request:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        return urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "whatwg-editor-dashboard/0.1.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _retry_delay(self, attempt: int, error: urllib.error.HTTPError | None = None) -> float:
        if error is not None and error.headers is not None:
            retry_after = error.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass
        return min(self.max_retry_delay_seconds, self.retry_base_seconds * (2**attempt))

    @property
    def retry_budget_remaining(self) -> float:
        return max(0.0, self.retry_budget_seconds - self.retry_seconds_spent)

    @staticmethod
    def _request_id(error: urllib.error.HTTPError) -> str | None:
        if error.headers is None:
            return None
        return error.headers.get("X-GitHub-Request-Id")

    def _reduce_page_size_after_timeout(self, variables: dict[str, Any]) -> tuple[int, int] | None:
        value = variables.get("pageSize")
        try:
            current = int(value)
        except (TypeError, ValueError):
            return None
        if current <= 1:
            return None

        reduced = max(1, current // 2)
        variables["pageSize"] = reduced
        if self._page_size_cap is None or reduced < self._page_size_cap:
            self._page_size_cap = reduced
        return current, reduced

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request_variables = dict(variables)
        page_size = request_variables.get("pageSize")
        if self._page_size_cap is not None and page_size is not None:
            request_variables["pageSize"] = min(int(page_size), self._page_size_cap)

        attempt = 0
        while True:
            outcome = self._attempt(query, request_variables, attempt)
            if not isinstance(outcome, _Failure):
                return outcome
            if not outcome.retryable:
                raise outcome.error from outcome.cause

            page_change: tuple[int, int] | None = None
            if outcome.timeout:
                self.timeout_count += 1
                page_change = self._reduce_page_size_after_timeout(request_variables)

            remaining = self.retry_budget_remaining
            if attempt >= self.max_retries or remaining <= 0:
                raise outcome.error from outcome.cause
            delay = min(outcome.delay, remaining)
            self.retry_count += 1
            self.retry_seconds_spent += delay
            page_text = (
                f"; reducing pageSize from {page_change[0]} to {page_change[1]}"
                if page_change
                else ""
            )
            LOGGER.warning(
                "%s on attempt %d%s; retrying in %.1f seconds (%.0f seconds of retry budget left)",
                outcome.summary,
                attempt + 1,
                page_text,
                delay,
                remaining - delay,
            )
            self._sleep(delay)
            attempt += 1

    def _attempt(
        self, query: str, variables: dict[str, Any], attempt: int
    ) -> dict[str, Any] | _Failure:
        """Make one request and return its ``data`` object, or the classified failure."""
        request = self._make_request(query, variables)
        self.request_attempts += 1
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read().decode("utf-8")
            result = json.loads(raw_response)
        except urllib.error.HTTPError as error:
            return self._classify_http_error(error, attempt)
        except json.JSONDecodeError as error:
            return _Failure(
                error=GitHubAPIError(f"GitHub returned invalid JSON: {error.doc[:500]}"),
                summary="GitHub returned invalid JSON",
                cause=error,
                retryable=True,
                timeout=False,
                delay=self._retry_delay(attempt),
            )
        except (OSError, http.client.HTTPException) as error:
            # Everything the socket, TLS and HTTP layers can raise: connection refused or
            # reset, DNS failure, a read timeout, and a response torn off mid-body
            # (`http.client.IncompleteRead`), which is what a GitHub outage looks like once
            # the load balancer stops answering with a clean 502. A certificate that does
            # not verify is this machine's problem, not GitHub's, and waiting will not
            # change it.
            reason = getattr(error, "reason", None)
            certificate_problem = isinstance(
                reason if reason is not None else error, ssl.SSLCertVerificationError
            )
            return _Failure(
                error=GitHubAPIError(f"GitHub request failed: {error!r}"),
                summary=f"GitHub request failed: {error!r}",
                cause=error,
                retryable=not certificate_problem,
                timeout=False,
                delay=self._retry_delay(attempt),
            )

        if result.get("errors"):
            messages = "; ".join(str(error.get("message", error)) for error in result["errors"])
            transient = _TRANSIENT_GRAPHQL_ERROR_MARKER in messages.lower()
            return _Failure(
                error=GitHubAPIError(f"GitHub GraphQL error: {messages}"),
                summary=f"GitHub GraphQL error: {messages[:200]}",
                cause=None,
                retryable=transient,
                timeout=transient,
                delay=self._retry_delay(attempt),
            )
        data = result.get("data")
        if not isinstance(data, dict):
            return _Failure(
                error=GitHubAPIError("GitHub GraphQL response did not contain a data object"),
                summary="GitHub GraphQL response did not contain a data object",
                cause=None,
                retryable=False,
                timeout=False,
                delay=0.0,
            )
        return data

    def _classify_http_error(self, error: urllib.error.HTTPError, attempt: int) -> _Failure:
        body = error.read().decode("utf-8", errors="replace")
        request_id = self._request_id(error)
        request_id_text = f" (request {request_id})" if request_id else ""
        headers = error.headers

        remaining = headers.get("X-RateLimit-Remaining") if headers else None
        if error.code == 403 and remaining == "0":
            reset = headers.get("X-RateLimit-Reset") if headers else None
            reset_text = f"; reset epoch {reset}" if reset else ""
            return _Failure(
                error=GitHubAPIError(f"GitHub GraphQL rate limit exhausted{reset_text}"),
                summary="GitHub GraphQL rate limit exhausted",
                cause=error,
                retryable=False,
                timeout=False,
                delay=0.0,
            )

        body_lower = body.lower()
        secondary_limit = error.code == 403 and (
            "secondary rate limit" in body_lower or "abuse detection" in body_lower
        )
        timeout = error.code in {502, 504}
        retryable = timeout or error.code in {429, 500, 503} or secondary_limit
        delay = self._retry_delay(attempt, error)
        if secondary_limit and not (headers and headers.get("Retry-After")):
            delay = max(60.0, delay)
        return _Failure(
            error=GitHubAPIError(
                f"GitHub returned HTTP {error.code}{request_id_text}: {body[:1000]}"
            ),
            summary=f"GitHub returned HTTP {error.code}{request_id_text}",
            cause=error,
            retryable=retryable,
            timeout=timeout,
            delay=delay,
        )


def _drop_drafts(nodes: list[dict[str, Any]], metadata: FetchMetadata) -> list[dict[str, Any]]:
    """Leave draft pull requests out of the dashboard entirely.

    A draft is not asking anybody for a review yet, so it belongs in no lane and no
    health figure. It enters the data when it is marked ready for review, and its
    age counts from then (models.py ``ready_for_review_at``).
    """
    kept = [node for node in nodes if not node.get("isDraft")]
    metadata.drafts_excluded += len(nodes) - len(kept)
    return kept


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


# GitHub computes mergeability lazily: the first query for a pull request answers UNKNOWN
# and only schedules the real value. A cold build read UNKNOWN for 259 of its 277 open PRs,
# which left `ready_bounded` — it requires MERGEABLE — reporting 15 candidates where 133
# were actually mergeable; the next build 55 minutes later, reading GitHub's now-warm
# answers, saw only one still unknown. The closed-PR search runs between the first read and
# these retries, so GitHub has already had several seconds to compute by the first one.
_MERGEABILITY_BATCH_SIZE = 100
# Asking is what schedules the computation, so the rounds get further apart: after a cold
# build queued a few hundred of them, a later query an hour on had every answer. A daily
# build can afford the worst case of just under a minute of waiting; whatever is still
# unknown after that is reported rather than waited on.
_MERGEABILITY_RETRY_DELAYS = (0.0, 3.0, 8.0, 15.0, 30.0)


def _resolve_unknown_mergeability(
    graphql: GraphQLClient,
    nodes: list[dict[str, Any]],
    metadata: FetchMetadata,
    *,
    sleep: Callable[[float], None],
) -> None:
    """Re-ask for the pull requests whose mergeability GitHub has not computed yet.

    Nodes are patched in place. A pull request that stays UNKNOWN keeps its
    'Mergeability unknown' blocker and is reported in ``metadata.warnings`` rather
    than being guessed at.
    """
    by_id = {
        node["id"]: node
        for node in nodes
        if node.get("id") and (node.get("mergeable") or "UNKNOWN") == "UNKNOWN"
    }
    if not by_id:
        return

    query = _read_query("pull_request_mergeability.graphql")
    pending = list(by_id)
    unknown_at_first_read = len(pending)
    for delay in _MERGEABILITY_RETRY_DELAYS:
        if not pending:
            break
        if delay:
            sleep(delay)
        still_unknown: list[str] = []
        for start in range(0, len(pending), _MERGEABILITY_BATCH_SIZE):
            batch = pending[start : start + _MERGEABILITY_BATCH_SIZE]
            data = graphql.execute(query, {"ids": batch})
            metadata.record_rate_limit(data.get("rateLimit"))
            answers = {
                value["id"]: value
                for value in (data.get("nodes") or [])
                if value and value.get("id")
            }
            for identifier in batch:
                value = answers.get(identifier)
                if value is None:
                    still_unknown.append(identifier)
                    continue
                node = by_id[identifier]
                node["mergeable"] = value.get("mergeable")
                node["mergeStateStatus"] = value.get("mergeStateStatus")
                if (node.get("mergeable") or "UNKNOWN") == "UNKNOWN":
                    still_unknown.append(identifier)
        pending = still_unknown

    LOGGER.info(
        "Resolved mergeability for %d of %d pull requests GitHub first answered UNKNOWN for",
        unknown_at_first_read - len(pending),
        unknown_at_first_read,
    )
    if pending:
        metadata.warnings.append(
            f"GitHub still answered UNKNOWN for the mergeability of {len(pending)} pull request"
            f"{'s' if len(pending) != 1 else ''} after retrying; they cannot enter the ready lane."
        )


def fetch_repository_data(
    config: DashboardConfig,
    token: str,
    *,
    now: datetime | None = None,
    client: GraphQLClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RepositoryData:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    graphql = client or GraphQLClient(token)
    metadata = FetchMetadata(source="github-graphql")

    common_variables = {
        "owner": config.repository.owner,
        "name": config.repository.name,
        "pageSize": config.sampling.graphql_page_size,
        "timelineEachEnd": config.sampling.timeline_each_end,
        "reviewCount": config.sampling.reviews_per_pr,
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
    open_nodes = _drop_drafts(
        _deduplicate_pr_nodes(open_nodes, label="Open", metadata=metadata), metadata
    )

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
            "pageSize": config.sampling.graphql_page_size,
            "timelineEachEnd": config.sampling.timeline_each_end,
            "reviewCount": config.sampling.reviews_per_pr,
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
    closed_nodes = _drop_drafts(
        _deduplicate_pr_nodes(closed_nodes, label="Recently closed", metadata=metadata), metadata
    )

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

    # Last, so that the closed-PR search has already given GitHub time to compute, and so
    # that PRs which closed during the build are no longer in the list.
    try:
        _resolve_unknown_mergeability(graphql, open_nodes, metadata, sleep=sleep)
    except GitHubAPIError as error:
        # Mergeability is a refinement of an otherwise complete build; a failure here
        # leaves the affected PRs marked unknown instead of discarding everything.
        metadata.warnings.append(f"Could not re-check unknown mergeability: {error}")

    metadata.request_attempts = int(getattr(graphql, "request_attempts", metadata.query_count))
    metadata.retry_count = int(getattr(graphql, "retry_count", 0))
    metadata.timeout_count = int(getattr(graphql, "timeout_count", 0))
    page_size_cap = getattr(graphql, "page_size_cap", None)
    metadata.effective_page_size = int(page_size_cap or config.sampling.graphql_page_size)
    if metadata.timeout_count:
        metadata.warnings.append(
            f"GitHub timed out {metadata.timeout_count} GraphQL request"
            f"{'s' if metadata.timeout_count != 1 else ''}; the outer page size was automatically "
            f"reduced to {metadata.effective_page_size}."
        )

    LOGGER.info(
        "Fetched %d open and %d recently closed PRs in %d successful GraphQL queries "
        "(%d request attempts, %d retries, cost %d, remaining %s, page size %d)",
        len(open_nodes),
        len(closed_nodes),
        metadata.query_count,
        metadata.request_attempts,
        metadata.retry_count,
        metadata.total_cost,
        metadata.rate_remaining,
        metadata.effective_page_size,
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
    metadata.request_attempts = int(metadata_raw.get("request_attempts") or metadata.query_count)
    metadata.retry_count = int(metadata_raw.get("retry_count") or 0)
    metadata.timeout_count = int(metadata_raw.get("timeout_count") or 0)
    effective_page_size = metadata_raw.get("effective_page_size")
    metadata.effective_page_size = int(effective_page_size) if effective_page_size else None
    metadata.warnings.extend(str(value) for value in (metadata_raw.get("warnings") or []))
    open_nodes = _drop_drafts(raw.get("open_pull_requests") or [], metadata)
    closed_nodes = _drop_drafts(raw.get("recently_closed_pull_requests") or [], metadata)
    return RepositoryData(
        open_pull_requests=tuple(snapshots_from_nodes(open_nodes)),
        recently_closed_pull_requests=tuple(snapshots_from_nodes(closed_nodes)),
        metadata=metadata,
    )
