from __future__ import annotations

import re
from dataclasses import dataclass


_TASK_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)\[([ xX])\]\s*(.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class ChecklistItem:
    checked: bool
    label: str


@dataclass(frozen=True)
class Checklist:
    items: tuple[ChecklistItem, ...]

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def checked(self) -> int:
        return sum(item.checked for item in self.items)

    @property
    def ratio(self) -> float | None:
        if not self.items:
            return None
        return self.checked / self.total

    def to_public_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "total": self.total,
            "ratio": round(self.ratio, 4) if self.ratio is not None else None,
            "items": [
                {"checked": item.checked, "label": item.label}
                for item in self.items
            ],
        }


def parse_checklist(markdown: str | None) -> Checklist:
    """Parse GitHub-flavoured task-list items, ignoring fenced code blocks."""
    if not markdown:
        return Checklist(())

    items: list[ChecklistItem] = []
    fence_marker: str | None = None

    for line in markdown.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            marker_char = marker[0]
            if fence_marker is None:
                fence_marker = marker_char
            elif fence_marker == marker_char:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue

        match = _TASK_RE.match(line)
        if not match:
            continue
        label = match.group(2).strip()
        items.append(ChecklistItem(checked=match.group(1).lower() == "x", label=label))

    return Checklist(tuple(items))
