from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from editor_dashboard.config import load_config


ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """
repository:
  owner: whatwg
  name: html
editors:
  - zcorpan
"""


def config_from(body: str):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "dashboard.yml"
        path.write_text(MINIMAL + body, encoding="utf-8")
        return load_config(path)


class SuggestedNextConfigTests(unittest.TestCase):
    def test_defaults_to_a_three_item_lead(self) -> None:
        self.assertEqual(config_from("").suggested_next.first_response_lead, 3)

    def test_zero_disables_the_lead(self) -> None:
        config = config_from("suggested_next:\n  first_response_lead: 0\n")
        self.assertEqual(config.suggested_next.first_response_lead, 0)

    def test_rejects_a_negative_lead(self) -> None:
        with self.assertRaises(ValueError) as caught:
            config_from("suggested_next:\n  first_response_lead: -1\n")
        self.assertIn("suggested_next.first_response_lead", str(caught.exception))

    def test_rejects_a_lead_past_the_cap(self) -> None:
        # The lead exists to be small; an unbounded one recreates the problem that
        # putting the active lane first solved.
        with self.assertRaises(ValueError) as caught:
            config_from("suggested_next:\n  first_response_lead: 11\n")
        self.assertIn("suggested_next.first_response_lead", str(caught.exception))

    def test_reply_window_is_not_an_allowed_cycle_lane(self) -> None:
        # It is a real lane, but it has a fixed position ahead of the cycle.
        with self.assertRaises(ValueError) as caught:
            config_from("suggested_next:\n  cycle:\n    - reply_window\n")
        self.assertIn("reply_window", str(caught.exception))

    def test_rejects_a_duplicated_cycle_lane(self) -> None:
        with self.assertRaises(ValueError) as caught:
            config_from("suggested_next:\n  cycle:\n    - overdue\n    - overdue\n")
        self.assertIn("duplicate", str(caught.exception))

    def test_at_least_one_editor_is_required(self) -> None:
        # There is no configured viewer to fall back on, so the editor list is the
        # only thing naming who the queue can be read as.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dashboard.yml"
            path.write_text("repository:\n  owner: whatwg\n  name: html\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_config(path)
        self.assertIn("editors", str(caught.exception))

    def test_the_shipped_configuration_parses(self) -> None:
        config = load_config(ROOT / "dashboard.yml")
        self.assertEqual(config.suggested_next.first_response_lead, 3)
        self.assertIn("overdue", config.suggested_next.cycle)


if __name__ == "__main__":
    unittest.main()


def config_with_people(people: str):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "dashboard.yml"
        path.write_text("repository:\n  owner: whatwg\n  name: html\n" + people, encoding="utf-8")
        return load_config(path)


class KnowsConfigTests(unittest.TestCase):
    def test_labels_and_pages(self) -> None:
        config = config_with_people(
            "editors:\n"
            "  - login: zcorpan\n"
            "    knows: [\"topic: img\", Images]\n"
            "  - annevk\n"
            "helpers:\n"
            "  - login: noamr\n"
            "    knows: [parsing]\n"
        )
        self.assertEqual(config.knows_of("zcorpan"), frozenset({"label:topic: img", "page:images"}))
        self.assertEqual(config.knows_of("noamr"), frozenset({"page:parsing"}))
        self.assertEqual(config.knows_of("annevk"), frozenset())

    def test_rejects_pages_that_say_nothing_and_empty_entries(self) -> None:
        with self.assertRaisesRegex(ValueError, "ignore_pages"):
            config_with_people("editors:\n  - login: zcorpan\n    knows: [acknowledgements]\n")
        with self.assertRaisesRegex(ValueError, "empty entry"):
            config_with_people("editors:\n  - login: zcorpan\n    knows: [\"\"]\n")
        with self.assertRaisesRegex(ValueError, "must be a list"):
            config_with_people("editors:\n  - login: zcorpan\n    knows: images\n")


class TopicPagesConfigTests(unittest.TestCase):
    def test_a_mapped_label_is_declarable_and_covers_its_pages(self) -> None:
        config = config_with_people(
            "editors:\n  - login: zcorpan\n    knows: [\"removal/deprecation\", accessibility]\n"
            "topics:\n  \"topic: img\": [images, Embedded-Content]\n  removal/deprecation: [obsolete]\n  accessibility: []\n"
        )
        self.assertEqual(config.labels_for_page("embedded-content"), ("topic: img",))
        self.assertTrue(config.is_topic_label("Accessibility"))
        self.assertEqual(config.knows_of("zcorpan"), frozenset({"label:removal/deprecation", "label:accessibility"}))

    def test_rejects_ignored_pages(self) -> None:
        with self.assertRaisesRegex(ValueError, "ignore_pages"):
            config_with_people("editors: [zcorpan]\ntopics:\n  \"topic: x\": [indices]\n")
