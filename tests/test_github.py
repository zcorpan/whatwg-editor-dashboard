from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from editor_dashboard.config import load_config
from editor_dashboard.github import (
    _MERGEABILITY_RETRY_DELAYS,
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
        self.assertEqual(data.metadata.query_count, 3)
        self.assertEqual(data.metadata.total_cost, 25)
        self.assertEqual(data.metadata.effective_page_size, 10)
        self.assertTrue(all(call["pageSize"] == 10 for call in client.calls))
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


if __name__ == "__main__":
    unittest.main()
