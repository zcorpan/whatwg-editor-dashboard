from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from editor_dashboard.build import build_site
from editor_dashboard.config import load_config
from editor_dashboard.github import load_fixture
from editor_dashboard.issue_analysis import analyze_issues


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class IssueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")
        cls.data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        open_prs = frozenset(pr.number for pr in cls.data.open_pull_requests)
        cls.by_number = {
            analysis.number: analysis
            for analysis in analyze_issues(cls.data.open_issues, cls.config, now=NOW, open_pull_requests=open_prs)
        }
        with tempfile.TemporaryDirectory() as directory:
            cls.payload = build_site(cls.config, cls.data, output_dir=directory, now=NOW)
        cls.items = {item["number"]: item for item in cls.payload["items"]}

    def test_a_new_unanswered_issue_is_in_the_reply_window_and_needs_triage(self) -> None:
        analysis = self.by_number[14001]
        self.assertIn("reply_window", analysis.shared_lanes)
        self.assertIn("triage", analysis.shared_lanes)
        self.assertEqual(analysis.waiting_on.party, "editors")

    def test_a_label_by_an_editor_is_a_first_response(self) -> None:
        analysis = self.by_number[14002]
        self.assertEqual(analysis.first_response.author, "annevk")
        self.assertEqual(analysis.first_response.kind, "label")
        self.assertNotIn("reply_window", analysis.shared_lanes)
        self.assertNotIn("overdue", analysis.shared_lanes)
        self.assertNotIn("triage", analysis.shared_lanes)

    def test_mentions_on_issues_are_direct_requests(self) -> None:
        self.assertIn("direct", self.by_number[14002].perspectives["zcorpan"].lanes)
        self.assertIn("whatwg/html#14002", self.payload["perspective_lanes"]["zcorpan"]["direct"])

    def test_an_issue_an_open_pr_fixes_is_folded_into_the_pr(self) -> None:
        self.assertEqual(self.by_number[14003].waiting_on.party, "fix")
        self.assertNotIn(14003, self.items)
        pr = self.items[13001]
        self.assertEqual([issue["number"] for issue in pr["linked_issues"]], [14003])
        codes = {reason["code"] for reason in pr["perspectives"]["annevk"]["reasons"]}
        self.assertIn("mentioned-in-discussion-on-linked-issue", codes)

    def test_dormant_issues_are_not_published(self) -> None:
        self.assertEqual(self.by_number[14004].waiting_on.party, "implementers")
        self.assertNotIn(14004, self.items)

    def test_an_editor_replying_last_is_not_an_editor_wait(self) -> None:
        analysis = self.by_number[14005]
        self.assertEqual(analysis.waiting_on.party, "author")
        self.assertNotIn("oldest_wait", analysis.shared_lanes)

    def test_every_lane_key_resolves(self) -> None:
        keys = {item["key"] for item in self.payload["items"]}
        for lane, values in self.payload["lanes"].items():
            self.assertTrue(set(values) <= keys, lane)
        for perspective, lanes in self.payload["perspective_lanes"].items():
            for lane, values in lanes.items():
                self.assertTrue(set(values) <= keys, (perspective, lane))

    def test_routing_only_names_people_who_take_routed_work(self) -> None:
        routed = [item for item in self.payload["items"] if item.get("routing")]
        self.assertTrue(routed)
        for item in routed:
            owner = item["routing"]["owner"]
            self.assertNotEqual(owner, "domenic")
            if item["kind"] == "issue":
                self.assertNotEqual(owner, "noamr")
            self.assertIn(item["key"], self.payload["perspective_lanes"][owner]["routed"])
            self.assertIn(item["key"], self.payload["perspective_lanes"]["all-editors"]["routed"])
            self.assertEqual(item["waiting_on"]["who"], [owner])
        # Only unanswered, unnamed items show a routing.
        self.assertTrue(all("reply_window" in item["lanes"] or "overdue" in item["lanes"] for item in routed))

    def test_people_rows_cover_the_team(self) -> None:
        rows = {row["login"]: row for row in self.payload["metrics"]["people"]["rows"]}
        self.assertEqual(rows["noamr"]["role"], "helper")
        self.assertTrue({"annevk", "domfarolino", "foolip", "zcorpan"} <= set(rows))
        for row in rows.values():
            self.assertEqual(len(row["weekly"]["reviews"]), 12)

    def test_issue_items_publish_no_bodies(self) -> None:
        serialized = json.dumps([item for item in self.payload["items"] if item.get("kind") == "issue"])
        self.assertNotIn("would you like to send a PR", serialized)
        self.assertNotIn("The spec doesn't say", serialized)


if __name__ == "__main__":
    unittest.main()
