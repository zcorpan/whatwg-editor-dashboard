from __future__ import annotations

import re
from dataclasses import dataclass


_TASK_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)\[([ xX])\]\s*(.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# A task-list label is Markdown, and it reaches the page as a text node — no HTML
# is ever generated from GitHub-derived strings — so it is reduced to its text
# content here. Only the constructs that actually occur in whatwg/html pull request
# descriptions are handled. Emphasis with underscores deliberately is not: these
# labels are full of URLs, and `_` is common inside them, while a true `_italic_`
# has never appeared in one.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Link destinations here never contain parentheses; a nested one would not match.
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^()]*\)")
_REFERENCE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\[[^\]]*\]")
# A label is one line of the description, so a link whose destination the author
# wrapped onto the next line arrives here with the destination — sometimes even the
# closing bracket — missing. Five of whatwg/html's open PRs are written that way.
_SHORTCUT_LINK_RE = re.compile(r"!?\[([^\]]*)\]")
_DANGLING_LINK_RE = re.compile(r"!?\[([^\]]*)$")
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_STRONG_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_EMPHASIS_RE = re.compile(r"(?<![\w*])\*([^*]+)\*(?![\w*])")
_CODE_RE = re.compile(r"`+([^`]*)`+")

# whatwg/html's template writes each item as "<question>: <answer>", where the
# answer is a link, an "N/A", or a paragraph the author typed. Only the question is
# shown, so the list reads as the checklist it is rather than as a wall of prose.
# The colon has to be followed by space or end the label, or the `:` in "https://"
# would split it; and a one-word head is a heading like "Chromium: ..." rather than
# a question, so it is left alone.
_ANSWER_SEPARATOR_RE = re.compile(r":(?=\s|$)")


def _plain_text(label: str) -> str:
    """Reduce one Markdown label to the text a reader would see."""
    label = _COMMENT_RE.sub("", label)
    # Links first, so emphasis inside link text is still unwrapped afterwards, and
    # in this order, so that a reference link does not look like two shortcut ones.
    label = _LINK_RE.sub(r"\1", label)
    label = _REFERENCE_LINK_RE.sub(r"\1", label)
    label = _SHORTCUT_LINK_RE.sub(r"\1", label)
    label = _DANGLING_LINK_RE.sub(r"\1", label)
    label = _AUTOLINK_RE.sub(r"\1", label)
    label = _STRIKE_RE.sub(r"\1", label)
    label = _STRONG_RE.sub(lambda match: match.group(1) or match.group(2), label)
    label = _EMPHASIS_RE.sub(r"\1", label)
    # Code spans are only unwrapped, not protected: brackets inside one are treated
    # as link syntax by the passes above. No label in whatwg/html has ever had any.
    label = _CODE_RE.sub(r"\1", label)
    return re.sub(r"\s+", " ", label).strip()


def _question(label: str) -> str:
    """Drop the author's answer, keeping the template's question."""
    match = _ANSWER_SEPARATOR_RE.search(label)
    if not match:
        return label
    head = label[: match.start()].strip()
    return head if len(head.split()) > 1 else label


def clean_label(label: str) -> str:
    return _question(_plain_text(label))


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
        label = clean_label(match.group(2))
        items.append(ChecklistItem(checked=match.group(1).lower() == "x", label=label))

    return Checklist(tuple(items))
