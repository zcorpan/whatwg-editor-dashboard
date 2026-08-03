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

    def test_empty_body(self) -> None:
        checklist = parse_checklist(None)
        self.assertEqual(checklist.total, 0)
        self.assertIsNone(checklist.ratio)


if __name__ == "__main__":
    unittest.main()
