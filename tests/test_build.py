from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from editor_dashboard.analysis import PERSPECTIVE_LANES, SHARED_LANES
from editor_dashboard.build import build_site
from editor_dashboard.config import load_config
from editor_dashboard.github import load_fixture


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_keys(child)


class BuildTests(unittest.TestCase):
    def test_builds_static_public_site(self) -> None:
        config = load_config(ROOT / "dashboard.yml")
        repository_data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            payload = build_site(config, repository_data, output_dir=output, now=NOW)
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "app.js").is_file())
            self.assertTrue((output / "style.css").is_file())
            self.assertTrue((output / "data.json").is_file())
            self.assertTrue((output / ".nojekyll").is_file())

            loaded = json.loads((output / "data.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded["privacy"]["generated_data"], "public-only")
            self.assertFalse(loaded["privacy"]["github_notifications_fetched"])
            self.assertNotIn("body", set(all_keys(payload)))
            self.assertNotIn("comments", set(all_keys(payload)))
            # Nothing in the published data names anybody as the reader.
            self.assertNotIn("viewer", loaded)
            self.assertIn("identity", loaded["privacy"]["local_state"])

    def test_publishes_the_queue_lead_and_both_first_response_lanes(self) -> None:
        config = load_config(ROOT / "dashboard.yml")
        repository_data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            payload = build_site(config, repository_data, output_dir=output, now=NOW)

        self.assertEqual(
            payload["suggested_next"],
            {
                "active": "all",
                "cycle": ["rereview", "overdue", "oldest_wait", "ready_bounded", "stale_direct"],
                "first_response_lead": 3,
            },
        )
        self.assertIn("reply_window", payload["lane_descriptions"])
        self.assertIn("overdue", payload["lane_descriptions"])

        # The browser resolves the lead against `items`, so every key has to be there.
        item_keys = {item["key"] for item in payload["items"]}
        self.assertEqual(payload["lanes"]["reply_window"], ["whatwg/html#13001", "whatwg/html#13003"])
        self.assertTrue(set(payload["lanes"]["reply_window"]) <= item_keys)

        principles = " ".join(payload["methodology"]["principles"])
        self.assertIn("still inside the 7-day target", principles)

    def test_publishes_a_queue_perspective_for_every_editor(self) -> None:
        config = load_config(ROOT / "dashboard.yml")
        repository_data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        with tempfile.TemporaryDirectory() as temporary:
            payload = build_site(config, repository_data, output_dir=Path(temporary), now=NOW)

        self.assertEqual(payload["perspectives"]["default"], "all-editors")
        self.assertEqual(
            payload["perspectives"]["options"],
            [
                {"key": "all-editors", "label": "All editors", "editor": False},
                {"key": "annevk", "label": "@annevk", "editor": True},
                {"key": "domenic", "label": "@domenic", "editor": True},
                {"key": "domfarolino", "label": "@domfarolino", "editor": True},
                {"key": "foolip", "label": "@foolip", "editor": True},
                {"key": "zcorpan", "label": "@zcorpan", "editor": True},
            ],
        )

        # The attention lanes are per-perspective; the other four are published once.
        keys = {option["key"] for option in payload["perspectives"]["options"]}
        self.assertEqual(set(payload["perspective_lanes"]), keys)
        for lanes in payload["perspective_lanes"].values():
            self.assertEqual(set(lanes), set(PERSPECTIVE_LANES))
        self.assertEqual(set(payload["lanes"]), set(SHARED_LANES))

        # The browser resolves every lane key against `items`, whichever editor is
        # selected, and reads each item's perspective for its evidence chips.
        item_keys = {item["key"] for item in payload["items"]}
        for lanes in (*payload["perspective_lanes"].values(), payload["lanes"]):
            for lane in lanes.values():
                self.assertTrue(set(lane) <= item_keys)
        for item in payload["items"]:
            self.assertEqual(set(item["perspectives"]), keys)
            # Every editor gets their own pair of hashes; the union gets neither,
            # because a fingerprint has to leave one person's footprint out.
            for key, view in item["perspectives"].items():
                editor = key != payload["perspectives"]["default"]
                self.assertEqual(bool(view["fingerprint"]), editor, key)
                self.assertEqual(bool(view["attention_fingerprint"]), editor, key)
            self.assertNotIn("fingerprint", item)

        annevk = next(item for item in payload["items"] if item["number"] == 13009)
        self.assertIn("direct", annevk["perspectives"]["annevk"]["lanes"])
        self.assertNotIn("direct", annevk["perspectives"]["zcorpan"]["lanes"])
        self.assertEqual(payload["perspective_lanes"]["annevk"]["direct"], ["whatwg/html#13009"])

    def test_frontend_avoids_dynamic_inner_html(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", javascript)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", html)
        # The two selects the payload drives: which editor's lanes to show, and
        # which editor's fingerprints the browser measures its own state against.
        self.assertIn('id="editor-perspective"', html)
        self.assertIn('id="editor-identity"', html)


if __name__ == "__main__":
    unittest.main()
