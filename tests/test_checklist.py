from __future__ import annotations

import unittest

from editor_dashboard.checklist import parse_checklist


class ChecklistTests(unittest.TestCase):
    def test_parses_github_task_lists(self) -> None:
        checklist = parse_checklist(
            """
- [x] Done
* [ ] Not done
1. [X] Numbered and done
+ [ ] Another
"""
        )
        self.assertEqual(checklist.total, 4)
        self.assertEqual(checklist.checked, 2)
        self.assertEqual(checklist.ratio, 0.5)
        self.assertEqual(checklist.items[1].label, "Not done")

    def test_ignores_fenced_code_blocks(self) -> None:
        checklist = parse_checklist(
            """
- [x] Real
```md
- [ ] Example only
```
~~~
- [x] Also example only
~~~
"""
        )
        self.assertEqual(checklist.total, 1)
        self.assertEqual(checklist.checked, 1)

    def test_reduces_markdown_labels_to_their_text(self) -> None:
        # The labels reaching the page are Markdown, and it can only be shown as
        # text. Every case below is taken from an open whatwg/html PR.
        checklist = parse_checklist(
            """
- [x] [Tests](https://github.com/web-platform-tests/wpt) are written and can be reviewed and commented upon at:
- [x] Corresponding [HTML AAM](https://w3c.github.io/html-aam/) & [ARIA in HTML](https://w3c.github.io/html-aria/) issues & PRs: **Not needed**
- [x] ~~[MDN issue](https://example.test/m) is filed:~~
- [x] The top of this comment includes a [clear commit message](https://example.test/c) to use. <!-- Github copied its message. -->
- [x] [MDN issue](https://example.test/m) is filed: Not necessary (`<` is not valid)
"""
        )
        self.assertEqual(
            [item.label for item in checklist.items],
            [
                "Tests are written and can be reviewed and commented upon at",
                "Corresponding HTML AAM & ARIA in HTML issues & PRs",
                "MDN issue is filed",
                "The top of this comment includes a clear commit message to use.",
                "MDN issue is filed",
            ],
        )

    def test_keeps_a_label_that_is_only_an_answer(self) -> None:
        # A one-word head is a heading rather than a template question, and the
        # colon inside a URL must not split the label either.
        checklist = parse_checklist(
            """
- [x] Chromium: [Chrome Status entry](https://example.test/s) & [bug](https://example.test/b)
- [ ] https://github.com/mozilla/standards-positions/issues/1045
- [x] Gecko: summoning @bzbarsky and @smaug---- as our go-to people
"""
        )
        self.assertEqual(
            [item.label for item in checklist.items],
            [
                "Chromium: Chrome Status entry & bug",
                "https://github.com/mozilla/standards-positions/issues/1045",
                "Gecko: summoning @bzbarsky and @smaug---- as our go-to people",
            ],
        )

    def test_recovers_a_link_the_author_wrapped_onto_the_next_line(self) -> None:
        """A label is one line, so such a link arrives with its destination gone."""
        checklist = parse_checklist(
            """
- [x] The top of this comment includes a [clear commit message]
- [ ] The top of this comment includes a [clear commit message
- [x] [Tests] custom-elements/element-internals.html (as stated below)
"""
        )
        self.assertEqual(
            [item.label for item in checklist.items],
            [
                "The top of this comment includes a clear commit message",
                "The top of this comment includes a clear commit message",
                "Tests custom-elements/element-internals.html (as stated below)",
            ],
        )

    def test_underscores_in_urls_are_not_emphasis(self) -> None:
        checklist = parse_checklist("- [x] See https://example.test/a_b_c and *this*\n")
        self.assertEqual(checklist.items[0].label, "See https://example.test/a_b_c and this")

    def test_empty_body(self) -> None:
        checklist = parse_checklist(None)
        self.assertEqual(checklist.total, 0)
        self.assertIsNone(checklist.ratio)


if __name__ == "__main__":
    unittest.main()
