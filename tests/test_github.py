from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from editor_dashboard.config import load_config
from editor_dashboard.github import GraphQLClient, GitHubAPIError, fetch_repository_data


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, open_responses: list[dict[str, Any]], closed_responses: list[dict[str, Any]]) -> None:
        self.open_responses = iter(open_responses)
        self.closed_responses = iter(closed_responses)
        self.calls: list[dict[str, Any]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(variables))
        if "query OpenPullRequests" in query:
            return next(self.open_responses)
        if "query RecentClosedPullRequests" in query:
            return next(self.closed_responses)
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


if __name__ == "__main__":
    unittest.main()
