from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from editor_dashboard.analysis import analyze_all, analyze_pull_request, build_lanes
from editor_dashboard.config import load_config
from editor_dashboard.github import load_fixture


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")
        cls.repository_data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        cls.analyses = analyze_all(cls.repository_data.open_pull_requests, cls.config, now=NOW)
        cls.by_number = {analysis.pr.number: analysis for analysis in cls.analyses}

    def test_expected_mvp_lanes(self) -> None:
        self.assertIn("direct", self.by_number[13001].lanes)
        self.assertIn("direct", self.by_number[13008].lanes)
        self.assertIn("rereview", self.by_number[13002].lanes)
        self.assertIn("new", self.by_number[13003].lanes)
        self.assertIn("oldest_wait", self.by_number[12900].lanes)
        self.assertIn("ready_bounded", self.by_number[13004].lanes)
        self.assertNotIn("ready_bounded", self.by_number[13006].lanes)

    def test_direct_requests_sort_oldest_first(self) -> None:
        lanes = build_lanes(self.analyses, self.config.repository.slug)
        self.assertEqual(
            lanes["direct"],
            ["whatwg/html#13008", "whatwg/html#13001"],
        )

    def test_first_editor_response_is_known_from_initial_sample(self) -> None:
        analysis = self.by_number[13007]
        self.assertTrue(analysis.first_editor_response_known)
        self.assertEqual(analysis.first_editor_response.author, "annevk")
        self.assertEqual(analysis.first_editor_response_hours, 49.0)

    def test_incomplete_timeline_marks_absent_first_response_unknown(self) -> None:
        analysis = self.by_number[13010]
        self.assertFalse(analysis.first_editor_response_known)
        self.assertIsNone(analysis.first_editor_response)
        self.assertNotIn("new", analysis.lanes)

    def test_ready_bounded_requires_a_description_checklist(self) -> None:
        original = self.by_number[13004]
        without_checklist = replace(original.pr, body="No task list here.")
        analysis = analyze_pull_request(without_checklist, self.config, now=NOW)
        self.assertNotIn("ready_bounded", analysis.lanes)
        self.assertIn("checklist-missing", {blocker.code for blocker in analysis.blockers})

    def test_address_fingerprint_changes_with_public_pr_change(self) -> None:
        original = self.by_number[13003]
        changed_pr = replace(
            original.pr,
            updated_at=original.pr.updated_at + timedelta(minutes=1),
            head_oid="different-head",
        )
        changed = analyze_pull_request(changed_pr, self.config, now=NOW)
        self.assertNotEqual(original.content_fingerprint, changed.content_fingerprint)


if __name__ == "__main__":
    unittest.main()
