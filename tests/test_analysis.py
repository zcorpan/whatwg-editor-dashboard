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
        self.assertIn("active", self.by_number[13001].lanes)
        self.assertIn("stale_direct", self.by_number[13011].lanes)
        self.assertIn("direct", self.by_number[13001].lanes)
        self.assertIn("direct", self.by_number[13008].lanes)
        self.assertIn("rereview", self.by_number[13002].lanes)
        self.assertIn("new", self.by_number[13003].lanes)
        self.assertIn("oldest_wait", self.by_number[12900].lanes)
        self.assertIn("ready_bounded", self.by_number[13004].lanes)
        self.assertNotIn("ready_bounded", self.by_number[13006].lanes)

    def test_attention_lanes_sort_newest_first(self) -> None:
        lanes = build_lanes(self.analyses, self.config.repository.slug)
        self.assertEqual(lanes["direct"], ["whatwg/html#13001", "whatwg/html#13008"])
        self.assertEqual(
            lanes["active"],
            ["whatwg/html#13001", "whatwg/html#13008", "whatwg/html#13002"],
        )

    def test_oldest_wait_still_sorts_longest_wait_first(self) -> None:
        lanes = build_lanes(self.analyses, self.config.repository.slug)
        waits = [
            self.by_number[int(key.split("#")[1])].current_wait_hours
            for key in lanes["oldest_wait"]
        ]
        self.assertEqual(waits, sorted(waits, reverse=True))

    def test_active_lane_needs_both_recent_activity_and_involvement(self) -> None:
        # #13002 has a prior viewer review and recent activity.
        self.assertIn("active", self.by_number[13002].lanes)
        # #12900 has an old contributor wait and no editor involvement.
        self.assertNotIn("active", self.by_number[12900].lanes)

        stale_activity = replace(self.by_number[13002].pr, updated_at=NOW - timedelta(days=90))
        self.assertNotIn("active", analyze_pull_request(stale_activity, self.config, now=NOW).lanes)

    def test_expired_mention_moves_out_of_direct_but_stays_findable(self) -> None:
        analysis = self.by_number[13011]
        self.assertNotIn("direct", analysis.lanes)
        self.assertIn("stale_direct", analysis.lanes)
        self.assertFalse(analysis.has_attention_signal)
        self.assertIn("mentioned-in-discussion", {reason.code for reason in analysis.reasons})

    def test_review_request_does_not_expire_and_wins_over_an_old_mention(self) -> None:
        # A review request is current GitHub state, so it keeps claiming attention
        # even on a PR whose only other signal is a years-old mention.
        requested = replace(self.by_number[13011].pr, review_requests=("zcorpan",))
        analysis = analyze_pull_request(requested, self.config, now=NOW)
        self.assertIn("direct", analysis.lanes)
        self.assertNotIn("stale_direct", analysis.lanes)
        # The freshest signal represents the PR, not the 2026-01-10 mention.
        self.assertEqual(analysis.direct_request_at, requested.updated_at)

    def test_new_lane_keeps_prs_that_missed_the_response_target(self) -> None:
        # #12900 opened 2026-03-01, far beyond the seven-day target, and no editor
        # has ever responded. The old seven-day cap dropped exactly these.
        analysis = self.by_number[12900]
        self.assertIn("new", analysis.lanes)
        self.assertGreater(analysis.age_hours, self.config.response_targets.initial_editor_response_days * 24)
        self.assertIn("first-response-overdue", {reason.code for reason in analysis.reasons})

    def test_first_time_contributor_detected_without_association(self) -> None:
        self.assertTrue(self.by_number[13011].first_time_contributor)
        self.assertEqual(self.by_number[13011].pr.author_association, "NONE")

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
