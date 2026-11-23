from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from editor_dashboard.spec_pages import PageIndex, SpecRepository, commit_pages, pull_request_pages


SOURCE = """<!DOCTYPE html>
  <h2 class="no-num no-toc" w-nosplit>Table of contents</h2>
  <p>Contents.</p>
  <h2 split-filename="forms" id="forms">Forms</h2>
  <h4 id="the-form-element">The <code>form</code> element</h4>
{forms}
  <h4 id="the-input-element">The <code>input</code> element</h4>
{inputs}
  <h2 split-filename="parsing" id="parsing">The HTML syntax</h2>
  <h3>Parsing HTML documents</h3>
  <h4><dfn id="tokenization">Tokenization</dfn></h4>
{tokens}
"""


def lines(prefix: str, count: int = 30) -> str:
    return "\n".join(f"  <p>{prefix} {index}.</p>" for index in range(count))


class PageIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = PageIndex.from_source(SOURCE.format(forms=lines("f"), inputs=lines("i"), tokens=lines("t")))

    def test_headings_belong_to_the_page_their_split_starts(self) -> None:
        self.assertEqual(self.index.by_heading['<h4 id="the-input-element">The <code>input</code> element</h4>'], "forms")
        # git truncates hunk headers, so long headings are keyed by their first 80 bytes.
        long_heading = '  <h4 id="x">' + "A very long heading " * 6 + "</h4>"
        index = PageIndex.from_source('<h2 split-filename="p">P</h2>\n' + long_heading)
        self.assertEqual(index.hunk_pages("@@ -1 +1 @@ " + long_heading.encode()[:80].decode()), {"p": 1})
        self.assertEqual(self.index.by_heading['<h4><dfn id="tokenization">Tokenization</dfn></h4>'], "parsing")
        self.assertNotIn('<h2 class="no-num no-toc" w-nosplit>Table of contents</h2>', self.index.by_heading)

    def test_spec_links_resolve_to_pages(self) -> None:
        text = (
            "See https://html.spec.whatwg.org/multipage/forms.html#x and "
            "https://html.spec.whatwg.org/#tokenization, not https://html.spec.whatwg.org/#nowhere."
        )
        self.assertEqual(self.index.link_pages(text), frozenset({"forms", "parsing"}))


class SpecRepositoryTests(unittest.TestCase):
    """Against a real, throwaway git repository, since the attribution is git's."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.write(forms=lines("f"), inputs=lines("i"), tokens=lines("t"))
        self.git("commit", "-qam", "Start", "--allow-empty")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, **parts: str) -> None:
        (self.root / "source").write_text(SOURCE.format(**parts), encoding="utf-8")
        self.git("add", "source")

    def test_a_pr_is_attributed_to_the_pages_its_hunks_sit_in(self) -> None:
        self.git("switch", "-qc", "feature")
        self.write(forms=lines("f"), inputs=lines("i").replace("i 10.", "i ten."), tokens=lines("t").replace("t 3.", "t three."))
        self.git("commit", "-qm", "Change")
        head = self.git("rev-parse", "HEAD")
        # main moves on afterwards; that must not count towards the PR.
        self.git("switch", "-q", "main")
        self.write(forms=lines("f").replace("f 5.", "f five."), inputs=lines("i"), tokens=lines("t"))
        self.git("commit", "-qm", "Unrelated")

        repository = SpecRepository(self.root / ".git")
        index = PageIndex.from_source(repository.source_at("main"))
        try:
            pages = pull_request_pages(repository, index, head_oid=head, main_ref="main")
            self.assertEqual(pages, {"forms": 1, "parsing": 1})
            self.assertEqual(commit_pages(repository, index, self.git("rev-parse", "main")), {"forms": 1})
            self.assertIsNone(pull_request_pages(repository, index, head_oid="0" * 40, main_ref="main"))
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
