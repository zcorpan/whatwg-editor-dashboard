"""What an item is about, as weighted topic keys, and which topic label it lacks.

A topic key is `label:<name>` for a `topic:` label and `page:<name>` for a multipage
spec page. Labels are what triagers say; pages are what a diff touches or an issue
links to. Both count, because labels are sparse on PRs (a fifth of open whatwg/html
PRs carry one) and pages need a diff or a link.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from .config import DashboardConfig


Topics = dict[str, float]

# A suggestion from a page needs this many labeled precedents, and the label has to
# be this large a share of the page's labeled history.
_MIN_PRECEDENTS = 5
_MIN_SHARE = 0.5


def topic_labels(labels: Iterable[str], config: DashboardConfig) -> list[str]:
    prefix = config.issues.topic_label_prefix
    return sorted({label for label in labels if label.lower().startswith(prefix)})


def page_shares(pages: Mapping[str, int] | None, config: DashboardConfig) -> dict[str, float]:
    """Hunk counts as shares, without the pages every change touches."""
    kept = {page: count for page, count in (pages or {}).items() if page not in config.ignore_pages and count > 0}
    total = sum(kept.values())
    return {page: count / total for page, count in kept.items()} if total else {}


def item_topics(
    labels: Iterable[str],
    config: DashboardConfig,
    *,
    pages: Mapping[str, int] | None = None,
    linked_pages: Iterable[str] = (),
) -> Topics:
    """Each topic label weighs 1; the spec pages share 1 between them."""
    topics: Topics = {f"label:{label}": 1.0 for label in topic_labels(labels, config)}
    shares = page_shares(pages, config)
    linked = [page for page in sorted(set(linked_pages)) if page not in config.ignore_pages]
    if not shares and linked:
        shares = {page: 1 / len(linked) for page in linked}
    for page, share in shares.items():
        topics[f"page:{page}"] = topics.get(f"page:{page}", 0.0) + share
    return topics


def display_name(topic: str) -> str:
    kind, _, name = topic.partition(":")
    return name.strip() if kind == "label" else f"the {name} page"


@dataclass(frozen=True)
class LabelSuggester:
    """Which `topic:` label goes with which spec page, learned from labeled history."""

    by_page: dict[str, Counter[str]]
    label_words: tuple[tuple[re.Pattern[str], str], ...]

    @classmethod
    def learn(
        cls,
        examples: Iterable[tuple[Iterable[str], Mapping[str, int] | None]],
        config: DashboardConfig,
        known_labels: Iterable[str],
    ) -> "LabelSuggester":
        """``examples`` are (labels, pages) pairs; only labeled ones teach anything."""
        by_page: dict[str, Counter[str]] = defaultdict(Counter)
        for labels, pages in examples:
            names = topic_labels(labels, config)
            top = _top_page(page_shares(pages, config))
            if top and names:
                for name in names:
                    by_page[top][name] += 1
        prefix = config.issues.topic_label_prefix
        words = []
        for label in sorted(set(topic_labels(known_labels, config))):
            name = label[len(prefix) :].strip()
            # Short names ("img", "url") match too much prose to be evidence.
            if len(name) >= 4 and re.fullmatch(r"[a-z][a-z -]*", name.lower()):
                words.append((re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE), label))
        return cls(dict(by_page), tuple(words))

    def from_pages(self, pages: Mapping[str, int] | None, config: DashboardConfig) -> tuple[str, str] | None:
        """A label and its evidence, when the item's main page has a clear label."""
        top = _top_page(page_shares(pages, config))
        if top is None:
            return None
        counts = self.by_page.get(top)
        if not counts:
            return None
        label, count = min(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        total = sum(counts.values())
        if count >= _MIN_PRECEDENTS and count / total >= _MIN_SHARE:
            return label, f"{count} of {total} labeled changes to the {top} page had it"
        return None

    def from_title(self, title: str) -> tuple[str, str] | None:
        for pattern, label in self.label_words:
            if pattern.search(title or ""):
                return label, "the title names the topic"
        return None


def _top_page(shares: Mapping[str, float]) -> str | None:
    if not shares:
        return None
    page, share = min(shares.items(), key=lambda pair: (-pair[1], pair[0]))
    return page if share >= 0.5 else None
