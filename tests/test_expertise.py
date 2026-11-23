from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from editor_dashboard.config import load_config
from editor_dashboard.evaluate import expertise_profiles, expertise_report
from editor_dashboard.models import HistoryRecord


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def record(number: int, author: str, days_ago: float, labels=(), reviewers=(), oid=None) -> HistoryRecord:
    at = NOW - timedelta(days=days_ago)
    return HistoryRecord(
        number=number,
        title=f"Change {number}",
        author=author,
        merged_at=at,
        merged_by=reviewers[0] if reviewers else None,
        merge_oid=oid,
        labels=tuple(labels),
        issue_labels=(),
        reviews=tuple((login, at - timedelta(hours=1)) for login in reviewers),
    )


class ExpertiseReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = load_config(ROOT / "dashboard.yml")
        cls.config = replace(config, knows=(("zcorpan", frozenset({"label:topic: img"})),))
        cls.history = [
            *(record(100 + n, "contributor", 30 + n, ["topic: parser"], ["zcorpan"]) for n in range(6)),
            *(record(200 + n, "annevk", 40 + n, ["topic: forms"], oid=f"c{n}") for n in range(6)),
            record(300, "annevk", 10, ["topic: parser"]),
        ]
        cls.pages = {f"c{n}": {"forms": 2} for n in range(6)}

    def test_profiles_credit_authors_and_reviewers_and_pages(self) -> None:
        profiles = expertise_profiles(self.history, self.pages, self.config, as_of=NOW)
        annevk = {topic for topic, _score, _share in profiles["annevk"]}
        self.assertEqual(annevk, {"label:topic: forms", "page:forms", "label:topic: parser"})
        self.assertEqual(profiles["zcorpan"][0][0], "label:topic: parser")
        # Only work before the report date counts.
        self.assertNotIn("annevk", expertise_profiles(self.history, self.pages, self.config, as_of=NOW - timedelta(days=90)))

    def test_report_compares_history_with_the_declarations(self) -> None:
        text = "\n".join(expertise_report(self.history, self.pages, self.config, as_of=NOW))
        self.assertIn("History suggests, not declared: topic: parser", text)
        self.assertIn("Declared, not in the history's top topics: topic: img", text)
        # annevk's one parser PR is 9% of the parser credit, under the 10% cut.
        self.assertIn('  - login: annevk\n    knows: ["forms", "topic: forms"]', text)


if __name__ == "__main__":
    unittest.main()
