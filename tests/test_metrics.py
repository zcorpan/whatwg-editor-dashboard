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
        self.assertEqual(current["open_prs"], 11)
        self.assertEqual(current["direct_requests"], 2)
        self.assertEqual(current["rereview_owed"], 1)
        self.assertEqual(current["ready_and_bounded"], 5)
        self.assertEqual(current["first_response_unknown_due_to_sampling"], 1)

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
