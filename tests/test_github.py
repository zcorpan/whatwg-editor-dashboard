from __future__ import annotations

import http.client
import io
import json
import ssl
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from editor_dashboard.config import load_config
from editor_dashboard.github import (
    _MERGEABILITY_RETRY_DELAYS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_RETRY_BASE_SECONDS,
    DEFAULT_RETRY_BUDGET_SECONDS,
    GraphQLClient,
    GitHubAPIError,
    fetch_repository_data,
)


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class FakeClient:
    def __init__(
        self,
        open_responses: list[dict[str, Any]],
        closed_responses: list[dict[str, Any]],
        mergeability_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.open_responses = iter(open_responses)
        self.closed_responses = iter(closed_responses)
        self.mergeability_responses = iter(mergeability_responses or [])
        self.calls: list[dict[str, Any]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(variables))
        if "query OpenPullRequests" in query:
            return next(self.open_responses)
        if "query RecentClosedPullRequests" in query:
            return next(self.closed_responses)
        if "query PullRequestMergeability" in query:
            return next(self.mergeability_responses)
        if "query OpenIssues" in query:
            return {
                "repository": {
                    "issues": {"totalCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
                },
                "rateLimit": rate_limit(1),
            }
        if "query RecentClosedIssues" in query or "query HistoryPullRequests" in query:
            return {
                "search": {"issueCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}},
                "rateLimit": rate_limit(1),
            }
        raise AssertionError("Unexpected GraphQL query")


def rate_limit(cost: int = 1) -> dict[str, Any]:
    return {
        "cost": cost,
        "limit": 1000,
        "remaining": 999,
        "resetAt": "2026-08-03T13:00:00Z",
    }


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def http_error(code: int, body: str, *, request_id: str = "TEST:123") -> urllib.error.HTTPError:
    headers = {"X-GitHub-Request-Id": request_id}
    return urllib.error.HTTPError(
        "https://api.github.com/graphql",
        code,
        "test error",
        headers,
        io.BytesIO(body.encode("utf-8")),
    )


class GraphQLClientTests(unittest.TestCase):
    def test_timeout_reduces_page_size_and_retains_the_smaller_cap(self) -> None:
        responses: list[FakeHTTPResponse | Exception] = [
            http_error(502, "Bad Gateway"),
            FakeHTTPResponse({"data": {"ok": True}}),
            FakeHTTPResponse({"data": {"ok": True}}),
        ]
        page_sizes: list[int] = []
        delays: list[float] = []

        def urlopen(request, *, timeout):
            self.assertEqual(timeout, 30.0)
            payload = json.loads(request.data.decode("utf-8"))
            page_sizes.append(int(payload["variables"]["pageSize"]))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client = GraphQLClient(
            "token",
            max_retries=2,
            retry_base_seconds=0,
            urlopen=urlopen,
            sleep=delays.append,
        )

        self.assertEqual(client.execute("query Test { viewer { login } }", {"pageSize": 10}), {"ok": True})
        self.assertEqual(client.execute("query Test { viewer { login } }", {"pageSize": 10}), {"ok": True})
        self.assertEqual(page_sizes, [10, 5, 5])
        self.assertEqual(client.page_size_cap, 5)
        self.assertEqual(client.request_attempts, 3)
        self.assertEqual(client.retry_count, 1)
        self.assertEqual(client.timeout_count, 1)
        self.assertEqual(delays, [0.0])

    def test_a_timeout_only_caps_the_query_that_timed_out(self) -> None:
        responses = [http_error(502, "Bad Gateway"), *[FakeHTTPResponse({"data": {"ok": True}})] * 3]
        page_sizes: list[tuple[str, int]] = []

        def urlopen(request, *, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            page_sizes.append((payload["query"].split()[1], int(payload["variables"]["pageSize"])))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client = GraphQLClient("token", retry_base_seconds=0, urlopen=urlopen, sleep=lambda _: None)
        client.execute("query Heavy { x }", {"pageSize": 10})
        client.execute("query Light { x }", {"pageSize": 50})
        client.execute("query Heavy { x }", {"pageSize": 10})
        self.assertEqual(page_sizes, [("Heavy", 10), ("Heavy", 5), ("Light", 50), ("Heavy", 5)])
        self.assertEqual(client.cap_for("Heavy"), 5)
        self.assertIsNone(client.cap_for("Light"))

    def test_nonretryable_http_error_fails_without_sleeping(self) -> None:
        delays: list[float] = []

        def urlopen(request, *, timeout):
            raise http_error(401, "Bad credentials")

        client = GraphQLClient(
            "token",
            max_retries=5,
            retry_base_seconds=0,
            urlopen=urlopen,
            sleep=delays.append,
        )
        with self.assertRaisesRegex(GitHubAPIError, "HTTP 401"):
            client.execute("query Test { viewer { login } }", {"pageSize": 10})
        self.assertEqual(client.request_attempts, 1)
        self.assertEqual(client.retry_count, 0)
        self.assertEqual(delays, [])

    def _client_with_failures(
        self,
        failures: list[Exception | FakeHTTPResponse],
        *,
        then: dict[str, Any] | None = {"data": {"ok": True}},
        **options: Any,
    ) -> tuple[GraphQLClient, list[float], list[int]]:
        responses = list(failures)
        if then is not None:
            responses.append(FakeHTTPResponse(then))
        delays: list[float] = []
        page_sizes: list[int] = []

        def urlopen(request, *, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            page_sizes.append(int(payload["variables"]["pageSize"]))
            if not responses:
                raise AssertionError("More requests than responses")
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client = GraphQLClient("token", urlopen=urlopen, sleep=delays.append, **options)
        return client, delays, page_sizes

    def test_a_response_torn_off_mid_body_is_retried(self) -> None:
        """Regression: a build died on `IncompleteRead` with retries left.

        Three 502s in a row were retried, then the fourth attempt's connection
        dropped mid-body, which the retry loop did not catch.
        """
        client, delays, _ = self._client_with_failures(
            [
                http_error(502, "Bad Gateway"),
                http.client.IncompleteRead(b"", 9845),
                http.client.RemoteDisconnected("Remote end closed connection without response"),
                ConnectionResetError(104, "Connection reset by peer"),
            ]
        )

        self.assertEqual(client.execute("query Test { viewer { login } }", {"pageSize": 10}), {"ok": True})
        self.assertEqual(client.request_attempts, 5)
        self.assertEqual(client.retry_count, 4)
        self.assertEqual(client.timeout_count, 1)
        self.assertEqual(delays, [37.5, 75.0, 150.0, 300.0])

    def test_backoff_doubles_to_the_cap_and_stops_at_the_budget(self) -> None:
        """An outage is polled every few minutes until the build-wide budget is spent."""
        client, delays, _ = self._client_with_failures(
            [http_error(503, "Service Unavailable")] * 30,
            then=None,
            retry_budget_seconds=1000.0,
        )

        with self.assertRaisesRegex(GitHubAPIError, "HTTP 503"):
            client.execute("query Test { viewer { login } }", {"pageSize": 10})
        self.assertEqual(delays, [37.5, 75.0, 150.0, 300.0, 300.0, 137.5])
        self.assertEqual(sum(delays), 1000.0)
        self.assertEqual(client.retry_seconds_spent, 1000.0)
        self.assertEqual(client.request_attempts, 7)

        # The budget is shared by the build: once it is gone, the next request fails on
        # its first error instead of sleeping again.
        with self.assertRaisesRegex(GitHubAPIError, "HTTP 503"):
            client.execute("query Test { viewer { login } }", {"pageSize": 10})
        self.assertEqual(client.request_attempts, 8)
        self.assertEqual(len(delays), 6)

    def test_default_retry_budget_outlasts_an_incident_within_the_job_timeout(self) -> None:
        """The defaults and the workflow's `timeout-minutes` are one decision."""
        delay = 0.0
        delays: list[float] = []
        for attempt in range(DEFAULT_MAX_RETRIES):
            delay = min(DEFAULT_MAX_RETRY_DELAY_SECONDS, DEFAULT_RETRY_BASE_SECONDS * 2**attempt)
            delays.append(delay)
        # The per-request attempt cap alone can spend the whole budget, so the budget,
        # not the count, is what ends a long outage.
        self.assertGreaterEqual(sum(delays), DEFAULT_RETRY_BUDGET_SECONDS)
        # A spent budget plus every attempt hanging for the full request timeout still
        # fits the build job's timeout with room for a normal build.
        client = GraphQLClient("token")
        worst_case_minutes = (
            DEFAULT_RETRY_BUDGET_SECONDS + (DEFAULT_MAX_RETRIES + 1) * client.timeout_seconds
        ) / 60
        workflow = (ROOT / ".github" / "workflows" / "dashboard.yml").read_text(encoding="utf-8")
        build_job = workflow.split("  build:", 1)[1].split("  deploy:", 1)[0]
        timeout_minutes = int(build_job.split("timeout-minutes:", 1)[1].split()[0])
        # A weekly full build, cold clone included, measured at about 25 minutes.
        full_build_minutes = 30
        self.assertLessEqual(worst_case_minutes + full_build_minutes, timeout_minutes)

    def test_graphql_timeout_reported_in_a_200_is_retried_like_a_502(self) -> None:
        client, delays, page_sizes = self._client_with_failures(
            [
                FakeHTTPResponse(
                    {
                        "data": None,
                        "errors": [
                            {
                                "message": "Something went wrong while executing your query. "
                                "This may be the result of a timeout, or it could be a GitHub bug."
                            }
                        ],
                    }
                )
            ]
        )

        self.assertEqual(client.execute("query Test { viewer { login } }", {"pageSize": 10}), {"ok": True})
        self.assertEqual(page_sizes, [10, 5])
        self.assertEqual(client.timeout_count, 1)
        self.assertEqual(delays, [37.5])

    def test_a_timed_out_batch_of_node_ids_is_split_instead_of_retried(self) -> None:
        """By-id queries have no page size to halve, so the batch itself is halved."""
        from editor_dashboard.github import FetchMetadata, _fetch_nodes_by_id

        sent: list[list[str]] = []
        delays: list[float] = []

        def urlopen(request, *, timeout):
            ids = json.loads(request.data.decode("utf-8"))["variables"]["ids"]
            sent.append(ids)
            if len(ids) > 2:
                raise http_error(502, "timeout")
            return FakeHTTPResponse({"data": {"nodes": [{"id": value} for value in ids]}})

        client = GraphQLClient("token", urlopen=urlopen, sleep=delays.append)
        nodes = _fetch_nodes_by_id(client, "query Q { x }", list("abcdef"), {}, FetchMetadata("test"), batch_size=6)

        self.assertEqual([node["id"] for node in nodes], list("abcdef"))
        self.assertEqual(sent, [list("abcdef"), list("abc"), ["a"], ["b", "c"], list("def"), ["d"], ["e", "f"]])
        self.assertEqual(delays, [])

    def test_a_node_that_no_longer_exists_is_null_not_a_failure(self) -> None:
        client, delays, _page_sizes = self._client_with_failures(
            [],
            then={
                "data": {"nodes": [None, {"id": "b"}]},
                "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a node"}],
            },
        )
        self.assertEqual(client.execute("query Q { x }", {"pageSize": 1, "ids": ["a", "b"]}), {"nodes": [None, {"id": "b"}]})
        self.assertEqual(delays, [])

    def test_other_graphql_errors_fail_without_retrying(self) -> None:
        client, delays, _ = self._client_with_failures(
            [
                FakeHTTPResponse(
                    {
                        "data": {"repository": None},
                        "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
                    }
                )
            ],
            then=None,
        )

        with self.assertRaisesRegex(GitHubAPIError, "Could not resolve"):
            client.execute("query Test { viewer { login } }", {"pageSize": 10})
        self.assertEqual(client.request_attempts, 1)
        self.assertEqual(delays, [])

    def test_a_certificate_that_does_not_verify_is_not_retried(self) -> None:
        """A local Python without a certificate store would otherwise wait out the budget."""
        certificate_error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        client, delays, _ = self._client_with_failures(
            [urllib.error.URLError(certificate_error)], then=None
        )

        with self.assertRaisesRegex(GitHubAPIError, "certificate verify failed"):
            client.execute("query Test { viewer { login } }", {"pageSize": 10})
        self.assertEqual(client.request_attempts, 1)
        self.assertEqual(delays, [])

    def test_retry_after_header_overrides_the_backoff(self) -> None:
        error = http_error(429, "Too many requests")
        error.headers["Retry-After"] = "45"
        client, delays, _ = self._client_with_failures([error])

        self.assertEqual(client.execute("query Test { viewer { login } }", {"pageSize": 10}), {"ok": True})
        self.assertEqual(delays, [45.0])


class GitHubFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")
        cls.fixture = json.loads((ROOT / "fixtures" / "sample_api_data.json").read_text(encoding="utf-8"))

    def test_missing_repository_fails_instead_of_publishing_an_empty_dashboard(self) -> None:
        client = FakeClient(
            open_responses=[{"repository": None, "rateLimit": rate_limit()}],
            closed_responses=[],
        )
        with self.assertRaisesRegex(GitHubAPIError, "not found or is not accessible"):
            fetch_repository_data(self.config, "unused", now=NOW, client=client)

    def test_deduplicates_pagination_and_prefers_later_closed_snapshot(self) -> None:
        first = self.fixture["open_pull_requests"][0]
        closes_during_build = self.fixture["open_pull_requests"][1].copy()
        closes_during_build["closedAt"] = "2026-08-03T11:30:00Z"
        closes_during_build["mergedAt"] = "2026-08-03T11:30:00Z"
        already_closed = self.fixture["recently_closed_pull_requests"][0]

        client = FakeClient(
            open_responses=[
                {
                    "repository": {
                        "pullRequests": {
                            "totalCount": 2,
                            "nodes": [first],
                            "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                        }
                    },
                    "rateLimit": rate_limit(10),
                },
                {
                    "repository": {
                        "pullRequests": {
                            "totalCount": 2,
                            "nodes": [first, closes_during_build],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    },
                    "rateLimit": rate_limit(10),
                },
            ],
            closed_responses=[
                {
                    "search": {
                        "issueCount": 2,
                        "nodes": [closes_during_build, already_closed],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "rateLimit": rate_limit(5),
                }
            ],
        )

        data = fetch_repository_data(self.config, "unused", now=NOW, client=client)

        self.assertEqual([pr.number for pr in data.open_pull_requests], [int(first["number"])])
        self.assertEqual(
            {pr.number for pr in data.recently_closed_pull_requests},
            {int(closes_during_build["number"]), int(already_closed["number"])},
        )
        # Three PR pages, one page each of open and recently closed issues, and one of
        # merged-PR history.
        self.assertEqual(data.metadata.query_count, 6)
        self.assertEqual(data.metadata.total_cost, 28)
        self.assertEqual(data.metadata.effective_page_size, 10)
        self.assertTrue(all(call["pageSize"] == 10 for call in client.calls if "reviewCount" in call))
        self.assertTrue(any("Deduplicated open" in warning for warning in data.metadata.warnings))
        self.assertTrue(any("changed from open to closed" in warning for warning in data.metadata.warnings))


    def _open_page(self, nodes: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "repository": {
                "pullRequests": {
                    "totalCount": len(nodes),
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            },
            "rateLimit": rate_limit(10),
        }

    def _empty_closed_page(self) -> dict[str, Any]:
        return {
            "search": {
                "issueCount": 0,
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "rateLimit": rate_limit(1),
        }

    def test_unknown_mergeability_is_re_asked_rather_than_published(self) -> None:
        """Regression: GitHub answers UNKNOWN to a cold mergeability query.

        A build that trusts the first answer reports almost every PR as unmergeable,
        which empties the ready lane.
        """
        unknown = {**self.fixture["open_pull_requests"][0], "id": "PR_1", "mergeable": "UNKNOWN"}
        settled = {**self.fixture["open_pull_requests"][1], "id": "PR_2", "mergeable": "CONFLICTING"}
        delays: list[float] = []

        client = FakeClient(
            open_responses=[self._open_page([unknown, settled])],
            closed_responses=[self._empty_closed_page()],
            mergeability_responses=[
                {
                    "nodes": [{"id": "PR_1", "number": unknown["number"], "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}],
                    "rateLimit": rate_limit(1),
                }
            ],
        )

        data = fetch_repository_data(self.config, "unused", now=NOW, client=client, sleep=delays.append)

        by_number = {pr.number: pr for pr in data.open_pull_requests}
        self.assertEqual(by_number[int(unknown["number"])].mergeable, "MERGEABLE")
        # Only the unknown one is re-asked, and no retry delay is needed once it answers.
        self.assertEqual([call.get("ids") for call in client.calls if "ids" in call], [["PR_1"]])
        self.assertEqual(delays, [])
        self.assertEqual(data.metadata.warnings, [])

    def test_mergeability_that_stays_unknown_is_reported_not_guessed(self) -> None:
        unknown = {**self.fixture["open_pull_requests"][0], "id": "PR_1", "mergeable": "UNKNOWN"}
        delays: list[float] = []
        answer = {
            "nodes": [{"id": "PR_1", "number": unknown["number"], "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}],
            "rateLimit": rate_limit(1),
        }

        client = FakeClient(
            open_responses=[self._open_page([unknown])],
            closed_responses=[self._empty_closed_page()],
            mergeability_responses=[answer] * len(_MERGEABILITY_RETRY_DELAYS),
        )

        data = fetch_repository_data(self.config, "unused", now=NOW, client=client, sleep=delays.append)

        self.assertEqual(data.open_pull_requests[0].mergeable, "UNKNOWN")
        self.assertEqual(delays, [delay for delay in _MERGEABILITY_RETRY_DELAYS if delay])
        self.assertTrue(any("still answered UNKNOWN" in warning for warning in data.metadata.warnings))

    def test_drafts_are_dropped_before_anything_else_asks_about_them(self) -> None:
        # A draft is in no lane and no health figure, so it is not worth a
        # mergeability re-check either.
        ready = self.fixture["open_pull_requests"][0]
        draft = {**self.fixture["open_pull_requests"][1], "id": "PR_2", "isDraft": True, "mergeable": "UNKNOWN"}
        closed_draft = {**self.fixture["recently_closed_pull_requests"][0], "isDraft": True}

        client = FakeClient(
            open_responses=[self._open_page([ready, draft])],
            closed_responses=[
                {
                    "search": {
                        "issueCount": 1,
                        "nodes": [closed_draft],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "rateLimit": rate_limit(1),
                }
            ],
        )

        data = fetch_repository_data(self.config, "unused", now=NOW, client=client, sleep=lambda _: None)

        self.assertEqual([pr.number for pr in data.open_pull_requests], [int(ready["number"])])
        self.assertEqual(data.recently_closed_pull_requests, ())
        self.assertEqual(data.metadata.drafts_excluded, 2)
        self.assertFalse(any("ids" in call for call in client.calls))


if __name__ == "__main__":
    unittest.main()
