from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from editor_dashboard.config import load_config
from editor_dashboard.github import GitHubAPIError, fetch_repository_data


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, open_responses: list[dict[str, Any]], closed_responses: list[dict[str, Any]]) -> None:
        self.open_responses = iter(open_responses)
        self.closed_responses = iter(closed_responses)

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
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
        self.assertTrue(any("Deduplicated open" in warning for warning in data.metadata.warnings))
        self.assertTrue(any("changed from open to closed" in warning for warning in data.metadata.warnings))


if __name__ == "__main__":
    unittest.main()
