from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
            self.assertEqual(loaded["viewer"]["login"], "zcorpan")

    def test_frontend_avoids_dynamic_inner_html(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", javascript)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", html)
        self.assertIn('value="checklist"', html)
        self.assertIn('value="unchecked"', html)


if __name__ == "__main__":
    unittest.main()
