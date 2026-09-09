from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from editor_dashboard.analysis import analyze_all
from editor_dashboard.config import load_config
from editor_dashboard.github import load_fixture
from editor_dashboard.metrics import build_metrics


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = load_config(ROOT / "dashboard.yml")
        data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        open_analyses = analyze_all(data.open_pull_requests, config, now=NOW)
        closed_analyses = analyze_all(data.recently_closed_pull_requests, config, now=NOW)
        cls.metrics = build_metrics(open_analyses, closed_analyses, config, now=NOW)

    def test_current_repository_metrics(self) -> None:
        # The four attention lanes are per-editor, so these counts are the whole
        # team's view: #13009 is here for @annevk's mention, not for the viewer's.
        current = self.metrics["repository"]["current"]
        self.assertEqual(current["open_prs"], 12)
        self.assertEqual(current["active_now"], 4)
        self.assertEqual(current["direct_requests"], 3)
        self.assertEqual(current["stale_direct_requests"], 1)
        self.assertEqual(current["rereview_owed"], 1)
        self.assertEqual(current["ready_and_bounded"], 5)
        self.assertEqual(current["first_response_unknown_due_to_sampling"], 1)

    def test_missing_editor_response_is_counted_rather_than_called_unknown(self) -> None:
        """Regression: timelineItems.totalCount counts every timeline item kind.

        Comparing it against the comment/review nodes marked nearly every PR as
        incompletely sampled, which turned "no editor has responded" into "unknown"
        and silently reported zero everywhere.
        """
        current = self.metrics["repository"]["current"]
        self.assertEqual(current["known_without_editor_response"], 6)

        coverage = self.metrics["coverage"]
        self.assertEqual(coverage["open_timeline_complete"], 11)
        self.assertEqual(coverage["open_timeline_total"], 12)

        rate = self.metrics["health"]["indicators"]["first_response_rate"]
        self.assertEqual(rate["eligible"], 11)
        self.assertEqual(rate["within_target"], 5)

    def test_backlog_trend_reconstructs_the_open_queue_week_by_week(self) -> None:
        points = self.metrics["trends"]["points"]
        self.assertEqual(len(points), 13)
        self.assertEqual(points[-1]["at"], "2026-08-03T12:00:00Z")
        # The final point is the queue as it stands, so it has to agree with the counts.
        self.assertEqual(points[-1]["open_prs"], self.metrics["repository"]["current"]["open_prs"])
        self.assertEqual(points[0]["open_prs"], 4)
        self.assertGreaterEqual(points[-1]["awaiting_first_response"], points[-1]["overdue_first_response"])

        backlog = self.metrics["health"]["indicators"]["backlog"]
        self.assertEqual(backlog["change"], points[-1]["open_prs"] - points[0]["open_prs"])
        self.assertTrue(backlog["lower_is_better"])
        self.assertEqual(backlog["status"], "off_track")

    def test_the_current_week_cannot_report_a_first_response_rate_yet(self) -> None:
        """A PR opened three days ago has not missed a seven-day target yet.

        Counting the newest week would report every unanswered new PR as a failure
        and drag the trend down on nothing but the calendar.
        """
        buckets = self.metrics["trends"]["buckets"]
        self.assertEqual(len(buckets), 12)
        self.assertFalse(buckets[-1]["first_response_mature"])
        self.assertTrue(all(bucket["first_response_mature"] for bucket in buckets[:-1]))

        rate = self.metrics["health"]["indicators"]["first_response_rate"]
        self.assertEqual(
            rate["eligible"],
            sum(bucket["first_response_eligible"] for bucket in buckets[:-1]),
        )

    def test_editor_impact_uses_non_causal_wording_fields(self) -> None:
        team = self.metrics["editors"]["team"]["window"]
        self.assertEqual(team["days"], 84)
        self.assertEqual(team["merged"], 6)
        self.assertEqual(team["merged_with_review"], 5)
        self.assertEqual(team["merged_with_review_percent"], 83.3)
        self.assertEqual(team["reviews_submitted"], 10)
        self.assertEqual(self.metrics["editors"]["team"]["recent"]["days"], 28)

    def test_every_editor_is_reported_in_a_fixed_alphabetical_order(self) -> None:
        """The section reports on the editors; the viewer is one row among them."""
        members = self.metrics["editors"]["members"]
        self.assertEqual(
            [member["login"] for member in members],
            ["annevk", "domenic", "domfarolino", "foolip", "zcorpan"],
        )
        by_login = {member["login"]: member["window"] for member in members}
        self.assertEqual(by_login["zcorpan"]["merged_with_review"], 4)
        self.assertEqual(by_login["zcorpan"]["merged_with_review_percent"], 80.0)
        self.assertEqual(by_login["zcorpan"]["reviews_submitted"], 5)
        self.assertEqual(by_login["annevk"]["reviews_submitted"], 2)
        self.assertEqual(by_login["domfarolino"]["first_responses"], 0)
        # One shared denominator, so the first-reply shares are comparable.
        self.assertEqual(
            {window["first_responses_total"] for window in by_login.values()},
            {11},
        )
        self.assertEqual(
            sum(window["first_responses"] for window in by_login.values()),
            by_login["zcorpan"]["first_responses_total"],
        )

    def test_a_merge_is_credited_to_one_editor_so_the_columns_add_up(self) -> None:
        """#12890 and #12870 each carry two editor reviews; the first one gets the credit."""
        buckets = self.metrics["trends"]["buckets"]
        credited = {}
        for bucket in buckets:
            self.assertEqual(
                sum(bucket["merged_by_first_reviewer"].values()),
                bucket["merged_with_editor_review"],
            )
            self.assertLessEqual(bucket["merged_with_editor_review"], bucket["merged"])
            for login, count in bucket["merged_by_first_reviewer"].items():
                credited[login] = credited.get(login, 0) + count
        self.assertEqual(credited, {"annevk": 1, "zcorpan": 4})

    def test_merge_share_excludes_prs_the_editor_authored(self) -> None:
        """An author cannot review their own PR, so counting one is a guaranteed miss."""
        team = self.metrics["editors"]["team"]["window"]
        by_login = {member["login"]: member["window"] for member in self.metrics["editors"]["members"]}
        self.assertEqual(by_login["zcorpan"]["authored_prs_merged"], 1)
        self.assertEqual(by_login["zcorpan"]["merged_by_others"], team["merged"] - 1)
        self.assertEqual(team["authored_prs_merged"], 1)
        # The group figure keeps that PR in the denominator, because a colleague
        # reviewed it: only the author is barred from reviewing it.
        self.assertEqual(team["merged"], 6)
        buckets = self.metrics["trends"]["buckets"]
        self.assertEqual(sum(bucket["merged"] for bucket in buckets), team["merged"])
        self.assertEqual(
            sum(bucket["merged_with_editor_review"] for bucket in buckets),
            team["merged_with_review"],
        )


if __name__ == "__main__":
    unittest.main()
