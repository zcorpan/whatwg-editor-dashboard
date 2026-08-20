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
        current = self.metrics["repository"]["current"]
        self.assertEqual(current["open_prs"], 12)
        self.assertEqual(current["active_now"], 3)
        self.assertEqual(current["direct_requests"], 2)
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

        week = self.metrics["repository"]["windows"]["7"]["first_editor_response"]
        self.assertEqual(week["known_no_response"], 2)
        self.assertEqual(week["unknown_due_to_sampling"], 0)
        self.assertEqual(week["first_time_contributors"]["eligible_prs"], 2)

    def test_flow_windows(self) -> None:
        week = self.metrics["repository"]["windows"]["7"]
        self.assertEqual(week["opened"], 4)
        self.assertEqual(week["closed"], 2)
        self.assertEqual(week["merged"], 1)
        self.assertEqual(week["net_backlog_change"], 2)

    def test_public_viewer_impact_uses_non_causal_wording_fields(self) -> None:
        week = self.metrics["viewer"]["windows"]["7"]
        self.assertEqual(week["reviews_submitted"], 2)
        self.assertEqual(week["prs_merged_after_review"], 1)
        self.assertEqual(week["authored_prs_merged"], 0)
        self.assertGreater(week["sampled_contributor_waiting_days_ended"], 0)


if __name__ == "__main__":
    unittest.main()
