"""What an item is about, as weighted topic keys, and which topic label it lacks.

A topic key is `label:<name>` for a topic label and `page:<name>` for a multipage
spec page, both lowercased. Labels are what triagers say; pages are what a diff
touches or an issue links to. The `topics:` map in dashboard.yml says which pages each
label covers, so a page is turned into the labels that cover it, and people only have
to declare labels in `knows:`. A page no label covers stays a `page:` key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .config import DashboardConfig


Topics = dict[str, float]


def topic_labels(labels: Iterable[str], config: DashboardConfig) -> list[str]:
    """The labels that say what an item is about: `topic:` ones and the mapped ones."""
    return sorted({label for label in labels if config.is_topic_label(label)})


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
    """Each topic label weighs 1; the spec pages share 1 between them.

    A page's share goes to the labels that cover it, split evenly, or stays on the
    page if none does.
    """
    topics: Topics = {f"label:{label.lower()}": 1.0 for label in topic_labels(labels, config)}
    shares = page_shares(pages, config)
    linked = [page for page in sorted(set(linked_pages)) if page not in config.ignore_pages]
    if not shares and linked:
        shares = {page: 1 / len(linked) for page in linked}
    for page, share in sorted(shares.items()):
        covering = config.labels_for_page(page)
        keys = [f"label:{label}" for label in covering] or [f"page:{page}"]
        for key in keys:
            topics[key] = topics.get(key, 0.0) + share / len(keys)
    return topics


def display_name(topic: str) -> str:
    kind, _, name = topic.partition(":")
    return name.strip() if kind == "label" else f"the {name} page"


def top_page(shares: Mapping[str, float]) -> str | None:
    """The page a change is mostly about: at least half of its hunks."""
    if not shares:
        return None
    page, share = min(shares.items(), key=lambda pair: (-pair[1], pair[0]))
    return page if share >= 0.5 else None


@dataclass(frozen=True)
class LabelSuggester:
    """A topic label for an unlabeled item, from the `topics:` map or its title."""

    label_words: tuple[tuple[re.Pattern[str], str], ...]

    @classmethod
    def from_config(cls, config: DashboardConfig, known_labels: Iterable[str]) -> "LabelSuggester":
        prefix = config.issues.topic_label_prefix
        words = []
        for label in sorted(set(topic_labels(known_labels, config))):
            if not label.lower().startswith(prefix):
                continue
            name = label[len(prefix) :].strip()
            # Short names ("img", "url") match too much prose to be evidence.
            if len(name) >= 4 and re.fullmatch(r"[a-z][a-z -]*", name.lower()):
                words.append((re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE), label))
        return cls(tuple(words))

    def from_pages(self, pages: Mapping[str, int] | None, config: DashboardConfig) -> tuple[str, str] | None:
        """The label covering the page a change is mostly about, if exactly one does."""
        page = top_page(page_shares(pages, config))
        if page is None:
            return None
        covering = config.labels_for_page(page)
        if len(covering) != 1:
            return None
        return covering[0], f"mostly changes the {page} page, which it covers in dashboard.yml"

    def from_title(self, title: str) -> tuple[str, str] | None:
        for pattern, label in self.label_words:
            if pattern.search(title or ""):
                return label, "the title names the topic"
        return None
