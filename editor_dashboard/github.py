from __future__ import annotations

import http.client
import json
import re
import logging
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DashboardConfig, label_key
from .models import (
    DependencyState,
    HistoryRecord,
    IssueSnapshot,
    PullRequestSnapshot,
    history_from_nodes,
    isoformat,
    issues_from_nodes,
    snapshots_from_nodes,
)
from .references import dependency_references
from .state import Snapshot, fetch_fingerprint


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


class BatchTooLarge(GitHubAPIError):
    """A by-id query timed out; the caller splits its ``ids`` instead of waiting.

    The by-id queries have no ``pageSize`` to halve, so retrying the same batch
    after a backoff would most likely time out again.
    """


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
    # "full" or "incremental" for a GitHub fetch; None for a fixture.
    fetch_mode: str | None = None
    full_fetch_reason: str | None = None
    last_full_fetch_at: str | None = None
    refetched_prs: int | None = None
    refetched_issues: int | None = None
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
            "fetch_mode": self.fetch_mode,
            "full_fetch_reason": self.full_fetch_reason,
            "last_full_fetch_at": self.last_full_fetch_at,
            "refetched_prs": self.refetched_prs,
            "refetched_issues": self.refetched_issues,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RepositoryData:
    open_pull_requests: tuple[PullRequestSnapshot, ...]
    recently_closed_pull_requests: tuple[PullRequestSnapshot, ...]
    metadata: FetchMetadata
    open_issues: tuple[IssueSnapshot, ...] = ()
    recently_closed_issues: tuple[IssueSnapshot, ...] = ()
    # Every PR merged since `expertise.since`, for routing by expertise.
    history: tuple[HistoryRecord, ...] = ()
    # What held open PRs say they depend on, by canonical URL (see references.py).
    # Fetched fresh on every build: it is other repositories' state.
    dependency_states: dict[str, DependencyState] = field(default_factory=dict)
    # Every label the repository has, fetched fresh on every build. None when not known
    # (a fixture without them, or a failed fetch): then the labels items carry stand in.
    labels: tuple[str, ...] | None = None


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
        # Per operation: a timeout on the heavy PR query says nothing about how large
        # a page of the light history query can be.
        self._page_size_caps: dict[str, int] = {}

        self.request_attempts = 0
        self.retry_count = 0
        self.timeout_count = 0
        self.retry_seconds_spent = 0.0

    @property
    def page_size_cap(self) -> int | None:
        """The smallest page size any query has been reduced to, if any."""
        return min(self._page_size_caps.values(), default=None)

    def cap_for(self, operation: str) -> int | None:
        return self._page_size_caps.get(operation)

    @staticmethod
    def _operation(query: str) -> str:
        match = re.search(r"\bquery\s+(\w+)", query)
        return match.group(1) if match else ""

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

    def _reduce_page_size_after_timeout(self, operation: str, variables: dict[str, Any]) -> tuple[int, int] | None:
        value = variables.get("pageSize")
        try:
            current = int(value)
        except (TypeError, ValueError):
            return None
        if current <= 1:
            return None

        reduced = max(1, current // 2)
        variables["pageSize"] = reduced
        if operation not in self._page_size_caps or reduced < self._page_size_caps[operation]:
            self._page_size_caps[operation] = reduced
        return current, reduced

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request_variables = dict(variables)
        operation = self._operation(query)
        page_size = request_variables.get("pageSize")
        cap = self._page_size_caps.get(operation)
        if cap is not None and page_size is not None:
            request_variables["pageSize"] = min(int(page_size), cap)

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
                ids = request_variables.get("ids")
                if isinstance(ids, list) and len(ids) > 1:
                    LOGGER.warning("%s for %d node ids; splitting the batch", outcome.summary, len(ids))
                    raise BatchTooLarge(str(outcome.error)) from outcome.cause
                page_change = self._reduce_page_size_after_timeout(operation, request_variables)

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

        errors = result.get("errors") or []
        if errors and "ids" in variables and isinstance(result.get("data"), dict) and all(
            isinstance(error, dict) and error.get("type") == "NOT_FOUND" for error in errors
        ):
            # A by-id query names a node that no longer exists (a deleted issue or a
            # transferred one). GitHub answers null for it alongside the others, which
            # the callers treat as "gone"; it is not a failed request.
            errors = []
        if errors:
            messages = "; ".join(str(error.get("message", error)) for error in errors)
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


def _cap(graphql: Any, operation: str) -> int | None:
    """The reduced page size of one query, for clients that track it."""
    cap_for = getattr(graphql, "cap_for", None)
    return cap_for(operation) if cap_for else None


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
        answers = {
            value["id"]: value
            for value in _fetch_nodes_by_id(
                graphql, query, pending, {}, metadata, batch_size=_MERGEABILITY_BATCH_SIZE
            )
            if value.get("id")
        }
        for identifier in pending:
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


class FullFetchRequired(GitHubAPIError):
    """An incremental fetch found something it cannot apply, so the build fetches everything."""


def _common_variables(config: DashboardConfig) -> dict[str, Any]:
    return {
        "owner": config.repository.owner,
        "name": config.repository.name,
        "pageSize": config.sampling.graphql_page_size,
        "timelineEachEnd": config.sampling.timeline_each_end,
        "reviewCount": config.sampling.reviews_per_pr,
        "threadCount": config.sampling.review_threads,
    }


def _closed_cutoff(config: DashboardConfig, now: datetime) -> str:
    """The first day of recent history, as the closed-PR search states it.

    The search is date-granular, so pruning a stored snapshot uses the same date rather
    than the exact instant; otherwise an incremental build would drop PRs a full build
    on the same day keeps.
    """
    return (now - timedelta(days=config.sampling.history_days)).date().isoformat()


def _fetch_open_nodes(
    config: DashboardConfig, graphql: GraphQLClient, metadata: FetchMetadata
) -> list[dict[str, Any]]:
    open_nodes: list[dict[str, Any]] = []
    open_query = _read_query("open_pull_requests.graphql")
    cursor: str | None = None
    expected_total: int | None = None
    while True:
        variables = {**_common_variables(config), "cursor": cursor}
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
    return _deduplicate_pr_nodes(open_nodes, label="Open", metadata=metadata)


def _fetch_closed_nodes(
    config: DashboardConfig, graphql: GraphQLClient, metadata: FetchMetadata, now: datetime
) -> list[dict[str, Any]]:
    search_query = (
        f"repo:{config.repository.slug} is:pr is:closed "
        f"closed:>={_closed_cutoff(config, now)} sort:updated-desc"
    )
    closed_nodes: list[dict[str, Any]] = []
    closed_query = _closed_pr_query("recent_closed_pull_requests.graphql")
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
    return _deduplicate_pr_nodes(closed_nodes, label="Recently closed", metadata=metadata)


def _issue_query(name: str) -> str:
    """An issue query, with the shared fragments appended unless it defines them."""
    if name == "open_issues.graphql":
        return _read_query(name)
    return _query_with_fragments(name, "open_issues.graphql")


def _paginate(
    graphql: GraphQLClient,
    query: str,
    variables: dict[str, Any],
    metadata: FetchMetadata,
    connection_of: Callable[[dict[str, Any]], Any],
    label: str,
) -> tuple[list[dict[str, Any]], int]:
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    total = 0
    while True:
        data = graphql.execute(query, {**variables, "cursor": cursor})
        metadata.record_rate_limit(data.get("rateLimit"))
        connection = connection_of(data)
        if not isinstance(connection, dict):
            raise GitHubAPIError(f"GitHub GraphQL response omitted the {label} connection")
        total = int(connection.get("totalCount") or connection.get("issueCount") or 0)
        nodes.extend(node for node in (connection.get("nodes") or []) if node)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return nodes, total
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubAPIError(f"{label} pagination reported another page without an end cursor")


def _deduplicate_by_number(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[int, dict[str, Any]] = {}
    for node in nodes:
        unique.setdefault(int(node["number"]), node)
    return list(unique.values())


def _fetch_open_issue_nodes(
    config: DashboardConfig, graphql: GraphQLClient, metadata: FetchMetadata
) -> list[dict[str, Any]]:
    nodes, total = _paginate(
        graphql,
        _issue_query("open_issues.graphql"),
        {
            "owner": config.repository.owner,
            "name": config.repository.name,
            "pageSize": config.sampling.issue_page_size,
            "timelineEachEnd": config.sampling.timeline_each_end,
        },
        metadata,
        lambda data: (data.get("repository") or {}).get("issues"),
        "open issue",
    )
    nodes = _deduplicate_by_number(nodes)
    if total and len(nodes) != total:
        metadata.warnings.append(f"Expected {total} open issues but fetched {len(nodes)}.")
    return nodes


def _fetch_closed_issue_nodes(
    config: DashboardConfig, graphql: GraphQLClient, metadata: FetchMetadata, now: datetime
) -> list[dict[str, Any]]:
    nodes, total = _paginate(
        graphql,
        _issue_query("recent_closed_issues.graphql"),
        {
            "query": (
                f"repo:{config.repository.slug} is:issue is:closed "
                f"closed:>={_closed_cutoff(config, now)} sort:updated-desc"
            ),
            "pageSize": config.sampling.issue_page_size,
            "timelineEachEnd": config.sampling.timeline_each_end,
        },
        metadata,
        lambda data: data.get("search"),
        "recent closed-issue search",
    )
    if total > 1000:
        metadata.warnings.append(
            "GitHub search is capped at 1,000 results; recently closed issues may be incomplete."
        )
    return _deduplicate_by_number(nodes)


def _apply_incremental_issue_changes(
    config: DashboardConfig,
    graphql: GraphQLClient,
    metadata: FetchMetadata,
    snapshot: Snapshot,
    updated: list[dict[str, Any]],
    now: datetime,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], int]:
    open_nodes = {number: dict(node) for number, node in snapshot.open_issue_nodes.items()}
    closed_nodes = {number: dict(node) for number, node in snapshot.closed_issue_nodes.items()}
    stale = {node["id"] for node in updated if node.get("__typename") == "Issue"}

    # As for PRs, a direct check of every open issue backs up a lagging search index.
    answers = {
        node["id"]: node
        for node in _fetch_nodes_by_id(
            graphql,
            _read_query("issue_freshness.graphql"),
            sorted(node["id"] for node in open_nodes.values() if node.get("id")),
            {},
            metadata,
            batch_size=100,
        )
        if node.get("id")
    }
    for number, node in list(open_nodes.items()):
        answer = answers.get(node.get("id"))
        if answer is None:
            del open_nodes[number]
        elif answer.get("state") != "OPEN" or answer.get("updatedAt") != node.get("updatedAt"):
            stale.add(node["id"])

    refreshed = _fetch_nodes_by_id(
        graphql,
        _issue_query("issues_by_id.graphql"),
        sorted(stale),
        {"timelineEachEnd": config.sampling.timeline_each_end},
        metadata,
        batch_size=int(_cap(graphql, "OpenIssues") or config.sampling.issue_page_size),
    )
    for node in refreshed:
        number = int(node["number"])
        if node.get("state") == "OPEN":
            closed_nodes.pop(number, None)
            open_nodes[number] = node
        else:
            open_nodes.pop(number, None)
            closed_nodes[number] = node

    cutoff = _closed_cutoff(config, now)
    closed_nodes = {
        number: node for number, node in closed_nodes.items() if (node.get("closedAt") or "")[:10] >= cutoff
    }
    return open_nodes, closed_nodes, len(refreshed)


def _fragments(name: str) -> str:
    source = _read_query(name)
    return source[source.index("\nfragment ") :]


def _closed_pr_query(name: str) -> str:
    """A closed-PR query with its shared fragments, including the history record's."""
    query = _read_query(name)
    if name != "recent_closed_pull_requests.graphql":
        query += "\n" + _fragments("recent_closed_pull_requests.graphql")
    return query + "\n" + _fragments("history_pull_requests.graphql")


def _fetch_history_nodes(
    config: DashboardConfig, graphql: GraphQLClient, metadata: FetchMetadata, now: datetime
) -> list[dict[str, Any]]:
    """Every PR merged since `expertise.since`, walked in date slices.

    Search returns at most 1,000 results, so a slice that reports more is halved.
    """
    query = _read_query("history_pull_requests.graphql")
    nodes: list[dict[str, Any]] = []
    pending = [(date.fromisoformat(config.expertise.since), now.date())]
    while pending:
        start, end = pending.pop(0)
        search = f"repo:{config.repository.slug} is:pr is:merged merged:{start.isoformat()}..{end.isoformat()}"
        found, total = _paginate(
            graphql,
            query,
            {"query": search, "pageSize": 50},
            metadata,
            lambda data: data.get("search"),
            "merged-PR history search",
        )
        if total > 1000 and end > start:
            middle = start + (end - start) / 2
            pending[:0] = [(start, middle), (middle + timedelta(days=1), end)]
            continue
        nodes.extend(found)
    return _deduplicate_by_number(nodes)


def _query_with_fragments(query_name: str, fragments_from: str) -> str:
    """A by-id query plus the fragments of the connection query it mirrors.

    Appending the fragments, rather than copying them, is what guarantees that a PR
    refetched by id has exactly the fields the full fetch would have given it.
    """
    source = _read_query(fragments_from)
    return _read_query(query_name) + "\n" + source[source.index("\nfragment ") :]


def _fetch_nodes_by_id(
    graphql: GraphQLClient,
    query: str,
    ids: list[str],
    variables: dict[str, Any],
    metadata: FetchMetadata,
    *,
    batch_size: int,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    pending = [ids[start : start + batch_size] for start in range(0, len(ids), batch_size)]
    while pending:
        batch = pending.pop(0)
        try:
            data = graphql.execute(query, {**variables, "ids": batch})
        except BatchTooLarge:
            middle = len(batch) // 2
            pending[:0] = [batch[:middle], batch[middle:]]
            continue
        metadata.record_rate_limit(data.get("rateLimit"))
        nodes.extend(node for node in (data.get("nodes") or []) if node)
    return nodes


def _fetch_updated_since(
    config: DashboardConfig,
    graphql: GraphQLClient,
    metadata: FetchMetadata,
    since: datetime,
) -> list[dict[str, Any]]:
    """Every PR and issue whose ``updatedAt`` is at or after ``since``: type, id, number, state."""
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    search_query = f"repo:{config.repository.slug} updated:>={stamp}"
    query = _read_query("updated_since.graphql")
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = graphql.execute(query, {"query": search_query, "cursor": cursor, "pageSize": 100})
        metadata.record_rate_limit(data.get("rateLimit"))
        connection = data.get("search")
        if not isinstance(connection, dict):
            raise GitHubAPIError("GitHub GraphQL response omitted the updated-PR search connection")
        if int(connection.get("issueCount") or 0) > 1000:
            # Search stops at 1,000 results, so the rest would be silently missed.
            raise FullFetchRequired("more than 1,000 issues and pull requests changed since the last build")
        found.extend(node for node in (connection.get("nodes") or []) if node and node.get("id"))
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise GitHubAPIError("Updated PR pagination reported another page without an end cursor")
    return found


_VOLATILE_FIELDS = ("mergeable", "mergeStateStatus", "reviewDecision", "statusCheckRollup")
# The check rollup is expensive for GitHub to compute: 100 PRs per request timed out
# on whatwg/html. A timed-out batch is split anyway; this just avoids the first one.
_VOLATILE_BATCH_SIZE = 25


def _apply_incremental_changes(
    config: DashboardConfig,
    graphql: GraphQLClient,
    metadata: FetchMetadata,
    snapshot: Snapshot,
    now: datetime,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], int, list[dict[str, Any]]]:
    """The stored PRs brought up to date: open, closed, the refetch count, and the search hits."""
    open_nodes = {number: dict(node) for number, node in snapshot.open_nodes.items()}
    closed_nodes = {number: dict(node) for number, node in snapshot.closed_nodes.items()}

    since = snapshot.fetched_at - timedelta(hours=config.incremental.overlap_hours)
    updated = _fetch_updated_since(config, graphql, metadata, since)
    stale: dict[str, str] = {
        node["id"]: str(node.get("state") or "")
        for node in updated
        if node.get("__typename", "PullRequest") == "PullRequest"
    }

    missing = [number for number, node in open_nodes.items() if not node.get("id")]
    if missing:
        raise FullFetchRequired("the stored snapshot has open pull requests without node ids")

    # Search can lag, and some fields change without moving `updatedAt`, so every open
    # PR is checked directly. Anything whose `updatedAt` or state moved is refetched;
    # the rest only has its volatile fields patched.
    volatile_query = _read_query("pull_request_volatile_fields.graphql")
    by_id = {node["id"]: node for node in open_nodes.values()}
    answers = {
        node["id"]: node
        for node in _fetch_nodes_by_id(
            graphql, volatile_query, sorted(by_id), {}, metadata, batch_size=_VOLATILE_BATCH_SIZE
        )
        if node.get("id")
    }
    for identifier, node in by_id.items():
        answer = answers.get(identifier)
        if answer is None:
            # Deleted, or no longer visible: it cannot be shown either way.
            open_nodes.pop(int(node["number"]), None)
            continue
        if answer.get("state") != "OPEN" or answer.get("updatedAt") != node.get("updatedAt"):
            stale.setdefault(identifier, str(answer.get("state") or ""))
            continue
        for name in _VOLATILE_FIELDS:
            node[name] = answer.get(name)

    variables = {
        "timelineEachEnd": config.sampling.timeline_each_end,
        "reviewCount": config.sampling.reviews_per_pr,
    }
    batch_size = int(_cap(graphql, "OpenPullRequests") or config.sampling.graphql_page_size)
    open_ids = sorted(identifier for identifier, state in stale.items() if state == "OPEN")
    closed_ids = sorted(identifier for identifier, state in stale.items() if state != "OPEN")
    refreshed_open = _fetch_nodes_by_id(
        graphql,
        _query_with_fragments("open_pull_requests_by_id.graphql", "open_pull_requests.graphql"),
        open_ids,
        {**variables, "threadCount": config.sampling.review_threads},
        metadata,
        batch_size=batch_size,
    )
    refreshed_closed = _fetch_nodes_by_id(
        graphql,
        _closed_pr_query("closed_pull_requests_by_id.graphql"),
        closed_ids,
        variables,
        metadata,
        batch_size=batch_size,
    )
    for node in refreshed_open:
        number = int(node["number"])
        closed_nodes.pop(number, None)
        open_nodes[number] = node
    for node in refreshed_closed:
        number = int(node["number"])
        open_nodes.pop(number, None)
        closed_nodes[number] = node

    cutoff = _closed_cutoff(config, now)
    closed_nodes = {
        number: node
        for number, node in closed_nodes.items()
        if (node.get("closedAt") or "")[:10] >= cutoff
    }
    return open_nodes, closed_nodes, len(refreshed_open) + len(refreshed_closed), updated


def _finish(
    config: DashboardConfig,
    graphql: GraphQLClient,
    metadata: FetchMetadata,
    open_nodes: list[dict[str, Any]],
    closed_nodes: list[dict[str, Any]],
    *,
    sleep: Callable[[float], None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, DependencyState]]:
    """What both fetch paths do once they have nodes: reconcile, re-ask, report."""
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
    # that PRs which closed during the build are no longer in the list. Drafts are not
    # worth asking about: they are in no lane.
    try:
        _resolve_unknown_mergeability(
            graphql, [node for node in open_nodes if not node.get("isDraft")], metadata, sleep=sleep
        )
    except GitHubAPIError as error:
        # Mergeability is a refinement of an otherwise complete build; a failure here
        # leaves the affected PRs marked unknown instead of discarding everything.
        metadata.warnings.append(f"Could not re-check unknown mergeability: {error}")

    dependency_states = _fetch_dependency_states(graphql, held_dependency_urls(config, open_nodes), metadata)

    metadata.request_attempts = int(getattr(graphql, "request_attempts", metadata.query_count))
    metadata.retry_count = int(getattr(graphql, "retry_count", 0))
    metadata.timeout_count = int(getattr(graphql, "timeout_count", 0))
    metadata.effective_page_size = int(_cap(graphql, "OpenPullRequests") or config.sampling.graphql_page_size)
    if metadata.timeout_count:
        metadata.warnings.append(
            f"GitHub timed out {metadata.timeout_count} GraphQL request"
            f"{'s' if metadata.timeout_count != 1 else ''}; the outer page size was automatically "
            f"reduced to {metadata.effective_page_size}."
        )
    return open_nodes, closed_nodes, dependency_states


def held_dependency_urls(config: DashboardConfig, open_nodes: Iterable[dict[str, Any]]) -> list[str]:
    """Every dependency named by an open, ready, held PR, sorted and deduplicated."""
    urls: set[str] = set()
    for node in open_nodes:
        if node.get("isDraft"):
            continue
        labels = {
            str(label.get("name") or "").lower()
            for label in ((node.get("labels") or {}).get("nodes") or [])
            if label
        }
        if labels & config.waiting_on.hold_labels:
            urls.update(dependency_references(node.get("body"), config.repository.slug, int(node["number"])))
    return sorted(urls)


_DEPENDENCY_BATCH_SIZE = 50


def _fetch_dependency_states(
    graphql: GraphQLClient,
    urls: list[str],
    metadata: FetchMetadata,
) -> dict[str, DependencyState]:
    """Resolve each URL with one aliased `resource(url:)` per dependency.

    A failure here only loses the dependency detail, so it is a warning, not a
    failed build: the affected PRs fall back to their conversation state.
    """
    states: dict[str, DependencyState] = {}
    for start in range(0, len(urls), _DEPENDENCY_BATCH_SIZE):
        batch = urls[start : start + _DEPENDENCY_BATCH_SIZE]
        fields = "\n".join(
            f"  d{index}: resource(url: {json.dumps(url)}) {{\n"
            "    ... on PullRequest { state title }\n"
            "    ... on Issue { state title }\n"
            "  }"
            for index, url in enumerate(batch)
        )
        query = (
            "query DependencyStates {\n"
            f"{fields}\n"
            "  rateLimit { cost limit remaining resetAt }\n"
            "}"
        )
        try:
            data = graphql.execute(query, {})
        except GitHubAPIError as error:
            metadata.warnings.append(f"Could not resolve what held PRs depend on: {error}")
            return {}
        metadata.record_rate_limit(data.get("rateLimit"))
        for index, url in enumerate(batch):
            value = data.get(f"d{index}") or {}
            states[url] = DependencyState(
                url=url,
                state=str(value.get("state") or "MISSING"),
                title=value.get("title"),
            )
    return states


def _fetch_labels(config: DashboardConfig, graphql: GraphQLClient, metadata: FetchMetadata) -> tuple[str, ...] | None:
    """The repository's labels. Only suggestions and a config check depend on them,
    so a failure is a warning."""
    try:
        nodes, _total = _paginate(
            graphql,
            _read_query("repository_labels.graphql"),
            {"owner": config.repository.owner, "name": config.repository.name},
            metadata,
            lambda data: ((data.get("repository") or {}).get("labels")),
            "labels",
        )
    except GitHubAPIError as error:
        metadata.warnings.append(f"Could not fetch the repository's labels: {error}")
        return None
    labels = tuple(sorted({str(node["name"]) for node in nodes if node.get("name")}))
    missing = sorted(config.named_labels() - {label_key(label) for label in labels})
    if missing:
        # Most likely renamed or deleted on GitHub since dashboard.yml was written.
        metadata.warnings.append(
            f"dashboard.yml names labels {config.repository.slug} doesn't have: {', '.join(missing)}."
        )
    return labels


def _repository_data(
    open_nodes: Iterable[dict[str, Any]],
    closed_nodes: Iterable[dict[str, Any]],
    metadata: FetchMetadata,
    dependency_states: dict[str, DependencyState] | None = None,
    open_issue_nodes: Iterable[dict[str, Any]] = (),
    closed_issue_nodes: Iterable[dict[str, Any]] = (),
    history_nodes: Iterable[dict[str, Any]] = (),
    labels: Iterable[str] | None = None,
) -> RepositoryData:
    """Snapshots in PR-number order, so a build's output never depends on fetch order."""
    ordered_open = sorted(open_nodes, key=lambda node: int(node["number"]))
    ordered_closed = sorted(closed_nodes, key=lambda node: int(node["number"]))
    # The stored history, then recent merges, which carry the same fields and win.
    history = history_from_nodes([*sorted(history_nodes, key=lambda node: int(node["number"])), *ordered_closed])
    return RepositoryData(
        open_pull_requests=tuple(snapshots_from_nodes(_drop_drafts(ordered_open, metadata))),
        recently_closed_pull_requests=tuple(snapshots_from_nodes(_drop_drafts(ordered_closed, metadata))),
        metadata=metadata,
        dependency_states=dict(dependency_states or {}),
        open_issues=tuple(issues_from_nodes(sorted(open_issue_nodes, key=lambda node: int(node["number"])))),
        recently_closed_issues=tuple(
            issues_from_nodes(sorted(closed_issue_nodes, key=lambda node: int(node["number"])))
        ),
        history=tuple(history),
        labels=None if labels is None else tuple(sorted(set(labels))),
    )


def fetch_with_snapshot(
    config: DashboardConfig,
    token: str,
    *,
    now: datetime | None = None,
    previous: Snapshot | None = None,
    full_reason: str | None = None,
    client: GraphQLClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[RepositoryData, Snapshot]:
    """Fetch the repository, incrementally from ``previous`` unless ``full_reason`` says why not.

    Returns the data for this build and the snapshot to store for the next one. An
    incremental fetch that cannot be applied falls back to a full one on its own.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    graphql = client or GraphQLClient(token)
    fingerprint = fetch_fingerprint(config)

    if previous is not None and full_reason is None:
        metadata = FetchMetadata(source="github-graphql", fetch_mode="incremental")
        try:
            open_map, closed_map, refetched, updated = _apply_incremental_changes(
                config, graphql, metadata, previous, now
            )
            open_issue_map, closed_issue_map, refetched_issues = _apply_incremental_issue_changes(
                config, graphql, metadata, previous, updated, now
            )
        except FullFetchRequired as error:
            full_reason = str(error)
        else:
            metadata.refetched_prs = refetched
            metadata.refetched_issues = refetched_issues
            labels = _fetch_labels(config, graphql, metadata)
            open_nodes, closed_nodes, dependencies = _finish(
                config, graphql, metadata, list(open_map.values()), list(closed_map.values()), sleep=sleep
            )
            LOGGER.info(
                "Incremental fetch since %s: refetched %d PRs in %d GraphQL queries (cost %d)",
                isoformat(previous.fetched_at),
                refetched,
                metadata.query_count,
                metadata.total_cost,
            )
            snapshot = Snapshot(
                fingerprint=fingerprint,
                fetched_at=now,
                last_full_at=previous.last_full_at,
                open_nodes={int(node["number"]): node for node in open_nodes},
                closed_nodes={int(node["number"]): node for node in closed_nodes},
                open_issue_nodes=open_issue_map,
                closed_issue_nodes=closed_issue_map,
                history_nodes=dict(previous.history_nodes),
                # Page maps are per head commit and per merge commit, so they stay
                # valid; a full fetch starts them afresh against the current headings.
                spec_pages=dict(previous.spec_pages),
            )
            metadata.last_full_fetch_at = isoformat(previous.last_full_at)
            return (
                _repository_data(
                    open_nodes,
                    closed_nodes,
                    metadata,
                    dependencies,
                    open_issue_map.values(),
                    closed_issue_map.values(),
                    previous.history_nodes.values(),
                    labels,
                ),
                snapshot,
            )

    metadata = FetchMetadata(source="github-graphql", fetch_mode="full", full_fetch_reason=full_reason)
    open_nodes = _fetch_open_nodes(config, graphql, metadata)
    closed_nodes = _fetch_closed_nodes(config, graphql, metadata, now)
    open_issue_nodes = _fetch_open_issue_nodes(config, graphql, metadata)
    closed_issue_nodes = _fetch_closed_issue_nodes(config, graphql, metadata, now)
    history_nodes = _fetch_history_nodes(config, graphql, metadata, now)
    # An issue can close between the two fetches; the later snapshot wins, as for PRs.
    closed_issue_numbers = {int(node["number"]) for node in closed_issue_nodes}
    open_issue_nodes = [node for node in open_issue_nodes if int(node["number"]) not in closed_issue_numbers]
    labels = _fetch_labels(config, graphql, metadata)
    open_nodes, closed_nodes, dependencies = _finish(config, graphql, metadata, open_nodes, closed_nodes, sleep=sleep)
    metadata.last_full_fetch_at = isoformat(now)

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
    snapshot = Snapshot(
        fingerprint=fingerprint,
        fetched_at=now,
        last_full_at=now,
        open_nodes={int(node["number"]): node for node in open_nodes},
        closed_nodes={int(node["number"]): node for node in closed_nodes},
        open_issue_nodes={int(node["number"]): node for node in open_issue_nodes},
        closed_issue_nodes={int(node["number"]): node for node in closed_issue_nodes},
        history_nodes={int(node["number"]): node for node in history_nodes},
    )
    return (
        _repository_data(
            open_nodes,
            closed_nodes,
            metadata,
            dependencies,
            open_issue_nodes,
            closed_issue_nodes,
            history_nodes,
            labels,
        ),
        snapshot,
    )


def fetch_repository_data(
    config: DashboardConfig,
    token: str,
    *,
    now: datetime | None = None,
    client: GraphQLClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RepositoryData:
    """A full fetch with nothing stored for next time."""
    data, _snapshot = fetch_with_snapshot(
        config, token, now=now, full_reason="no state directory", client=client, sleep=sleep
    )
    return data


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
    return _repository_data(
        raw.get("open_pull_requests") or [],
        raw.get("recently_closed_pull_requests") or [],
        metadata,
        {
            state.url: state
            for state in (DependencyState.from_json(value) for value in raw.get("dependency_states") or [])
        },
        raw.get("open_issues") or [],
        raw.get("recently_closed_issues") or [],
        raw.get("history_pull_requests") or [],
        raw.get("labels"),
    )
