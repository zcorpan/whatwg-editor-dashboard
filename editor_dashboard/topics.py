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

from .config import DashboardConfig, label_key
from .spec_pages import split_location


Topics = dict[str, float]


def topic_labels(labels: Iterable[str], config: DashboardConfig) -> list[str]:
    """The labels that say what an item is about, lowercased: `topic:` ones and the mapped ones."""
    return sorted({label_key(label) for label in labels if config.is_topic_label(label)})


def page_shares(pages: Mapping[str, int] | None, config: DashboardConfig) -> dict[str, float]:
    """Hunk counts per location (`page` or `page#section/...`) as shares, without the
    pages every change touches."""
    kept = {
        location: count
        for location, count in (pages or {}).items()
        if split_location(location)[0] not in config.ignore_pages and count > 0
    }
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

    A location's share goes to the labels that cover it (its innermost mapped section,
    else its page), split evenly, or stays on the page if none does.
    """
    topics: Topics = {f"label:{label}": 1.0 for label in topic_labels(labels, config)}
    shares = page_shares(pages, config)
    linked = [location for location in sorted(set(linked_pages)) if split_location(location)[0] not in config.ignore_pages]
    if not shares and linked:
        shares = {location: 1 / len(linked) for location in linked}
    for location, share in sorted(shares.items()):
        page, sections = split_location(location)
        covering = config.labels_for_location(page, sections)
        keys = [f"label:{label}" for label in covering] or [f"page:{page}"]
        for key in keys:
            topics[key] = topics.get(key, 0.0) + share / len(keys)
    return topics


def display_name(topic: str) -> str:
    kind, _, name = topic.partition(":")
    return name.strip() if kind == "label" else f"the {name} page"


def top_page(shares: Mapping[str, float]) -> str | None:
    """The page a change is mostly about: at least half of its hunks."""
    by_page: dict[str, float] = {}
    for location, share in shares.items():
        page = split_location(location)[0]
        by_page[page] = by_page.get(page, 0.0) + share
    if not by_page:
        return None
    page, share = min(by_page.items(), key=lambda pair: (-pair[1], pair[0]))
    return page if share >= 0.5 else None


@dataclass(frozen=True)
class LabelSuggester:
    """A topic label for an unlabeled item, from the `topics:` map or its title.

    Only labels that exist are suggested, under the name GitHub has, so a key of
    `topics:` that is not a GitHub label is never suggested.
    """

    label_words: tuple[tuple[re.Pattern[str], str], ...]
    # Lowercased current name -> the label as it exists on GitHub.
    existing: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_config(cls, config: DashboardConfig, known_labels: Iterable[str]) -> "LabelSuggester":
        prefix = config.issues.topic_label_prefix
        existing: dict[str, str] = {}
        for label in sorted(set(known_labels)):
            if config.is_topic_label(label):
                existing.setdefault(label_key(label), label)
        words = []
        for label in sorted(set(existing.values())):
            if not label.lower().startswith(prefix):
                continue
            name = label[len(prefix) :].strip()
            # Short names ("img", "url") match too much prose to be evidence.
            if len(name) >= 4 and re.fullmatch(r"[a-z][a-z -]*", name.lower()):
                words.append((re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE), label))
        return cls(tuple(words), tuple(sorted(existing.items())))

    def from_pages(self, pages: Mapping[str, int] | None, config: DashboardConfig) -> tuple[str, str] | None:
        """The label at least half of a change is about, through the `topics:` map."""
        topics = item_topics([], config, pages=pages)
        labels = [(weight, key[len("label:") :]) for key, weight in topics.items() if key.startswith("label:")]
        if not labels:
            return None
        weight, best = max(labels, key=lambda pair: (pair[0], pair[1]))
        label = dict(self.existing).get(best)
        if weight < 0.5 or label is None:
            return None
        page = top_page(page_shares(pages, config))
        where = f"the {page} page" if page else "the parts of the spec it touches"
        return label, f"mostly changes {where}, which dashboard.yml files under it"

    def from_title(self, title: str) -> tuple[str, str] | None:
        for pattern, label in self.label_words:
            if pattern.search(title or ""):
                return label, "the title names the topic"
        return None
