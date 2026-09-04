from __future__ import annotations

import unittest
from typing import Any

from editor_dashboard.models import PullRequestSnapshot


def comment(identifier: str) -> dict[str, Any]:
    return {
        "__typename": "IssueComment",
        "id": identifier,
        "author": {"login": "alice"},
        "authorAssociation": "CONTRIBUTOR",
        "body": "",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-01T00:00:00Z",
    }


def node(
    *,
    total_count: int,
    first: list[dict[str, Any]],
    last: list[dict[str, Any]],
    page_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    first_connection: dict[str, Any] = {"totalCount": total_count, "nodes": first}
    if page_info is not None:
        first_connection["pageInfo"] = page_info
    return {
        "number": 1,
        "title": "Example",
        "url": "https://github.com/whatwg/html/pull/1",
        "author": {"login": "alice"},
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-02T00:00:00Z",
        "timelineFirst": first_connection,
        "timelineLast": {"totalCount": total_count, "nodes": last},
    }


class ReviewThreadTests(unittest.TestCase):
    def test_thread_author_comes_from_the_first_comment(self) -> None:
        payload = node(total_count=0, first=[], last=[])
        payload["reviewThreads"] = {
            "totalCount": 2,
            "nodes": [
                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"author": {"login": "ZCorpan"}}]}},
                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"author": None}]}},
            ],
        }
        snapshot = PullRequestSnapshot.from_graphql(payload)
        self.assertEqual([thread.author for thread in snapshot.review_threads], ["zcorpan", None])
        self.assertEqual(snapshot.unresolved_review_threads, 2)
        # The published counts stay unfiltered; only the fingerprint drops the
        # viewer's own threads.
        self.assertEqual(snapshot.review_threads_started_by_others("zcorpan"), (1, 1))


class TimelineSampleCompletenessTests(unittest.TestCase):
    def test_unfiltered_total_count_does_not_mark_a_full_sample_incomplete(self) -> None:
        """`timelineItems.totalCount` ignores the itemTypes filter.

        A PR with one comment can report a totalCount of 12 because commits, labels
        and assignments are timeline items too. Trusting that number reported almost
        every PR as incompletely sampled.
        """
        snapshot = PullRequestSnapshot.from_graphql(
            node(
                total_count=12,
                first=[comment("c1")],
                last=[comment("c1")],
                page_info={"hasNextPage": False},
            )
        )
        self.assertTrue(snapshot.timeline_sample_complete)
        self.assertEqual(snapshot.timeline_sampled_count, 1)

    def test_page_info_reports_a_truncated_sample(self) -> None:
        snapshot = PullRequestSnapshot.from_graphql(
            node(
                total_count=80,
                first=[comment("c1")],
                last=[comment("c2")],
                page_info={"hasNextPage": True},
            )
        )
        self.assertFalse(snapshot.timeline_sample_complete)

    def test_falls_back_to_comparing_the_two_windows_without_page_info(self) -> None:
        # `first: n` and `last: n` return the same items exactly when the filtered
        # total is at most n, so identical windows mean nothing was truncated.
        complete = PullRequestSnapshot.from_graphql(
            node(total_count=9, first=[comment("c1")], last=[comment("c1")])
        )
        self.assertTrue(complete.timeline_sample_complete)

        truncated = PullRequestSnapshot.from_graphql(
            node(total_count=9, first=[comment("c1")], last=[comment("c2")])
        )
        self.assertFalse(truncated.timeline_sample_complete)

    def test_empty_timeline_is_a_complete_sample(self) -> None:
        snapshot = PullRequestSnapshot.from_graphql(
            node(total_count=3, first=[], last=[], page_info={"hasNextPage": False})
        )
        self.assertTrue(snapshot.timeline_sample_complete)
        self.assertEqual(snapshot.timeline_sampled_count, 0)


if __name__ == "__main__":
    unittest.main()
