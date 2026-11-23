"""Who has worked on which topic, from merged PRs, decayed by age.

A merged PR credits its author 3, each reviewer other than the author 2, and its
merger 1 if they did neither. Each credit is spread over the PR's topics and halves
every `expertise.half_life_days`. Everything is computed *as of* a time, from work
before it, so a routing decision made for an item can be replayed later and comes out
the same.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .config import DashboardConfig
from .models import HistoryRecord
from .topics import Topics, item_topics


AUTHOR_WEIGHT = 3.0
REVIEW_WEIGHT = 2.0
MERGE_WEIGHT = 1.0


@dataclass(frozen=True)
class Contribution:
    login: str
    at: datetime
    weight: float
    role: str  # "authored", "reviewed" or "merged"
    topics: Topics


def contributions(
    history: Iterable[HistoryRecord],
    pages_by_oid: Mapping[str, Mapping[str, int]],
    config: DashboardConfig,
) -> list[Contribution]:
    result: list[Contribution] = []
    for record in history:
        topics = item_topics(
            (*record.labels, *record.issue_labels),
            config,
            pages=pages_by_oid.get(record.merge_oid or ""),
        )
        if not topics:
            continue
        credited: set[str] = set()
        if record.author:
            result.append(Contribution(record.author, record.merged_at, AUTHOR_WEIGHT, "authored", topics))
            credited.add(record.author)
        for login, at in record.reviews:
            if login not in credited:
                result.append(Contribution(login, at, REVIEW_WEIGHT, "reviewed", topics))
                credited.add(login)
        if record.merged_by and record.merged_by not in credited:
            result.append(Contribution(record.merged_by, record.merged_at, MERGE_WEIGHT, "merged", topics))
    return sorted(result, key=lambda value: (value.at, value.login, value.role))


class Expertise:
    def __init__(self, contributions: Iterable[Contribution], config: DashboardConfig) -> None:
        self._all = list(contributions)
        self._times = [value.at for value in self._all]
        self.half_life = timedelta(days=config.expertise.half_life_days)
        # Per topic, time-ordered (at, login, credit): routing asks about thousands of
        # items, and each only needs the contributions on its own topics.
        self._by_topic: dict[str, list[tuple[datetime, str, float]]] = defaultdict(list)
        for value in self._all:
            for topic, share in value.topics.items():
                self._by_topic[topic].append((value.at, value.login, value.weight * share))
        self._topic_times = {topic: [entry[0] for entry in entries] for topic, entries in self._by_topic.items()}

    def _before(self, as_of: datetime) -> list[Contribution]:
        return self._all[: bisect.bisect_left(self._times, as_of)]

    def scores(self, topics: Topics, as_of: datetime) -> dict[str, float]:
        """Each person's decayed expertise in ``topics``, from work before ``as_of``."""
        totals: dict[str, float] = defaultdict(float)
        half_life = self.half_life.total_seconds()
        for topic, weight in sorted(topics.items()):
            entries = self._by_topic.get(topic)
            if not entries:
                continue
            for at, login, credit in entries[: bisect.bisect_left(self._topic_times[topic], as_of)]:
                totals[login] += weight * credit * 0.5 ** ((as_of - at).total_seconds() / half_life)
        return dict(totals)

    def affinities(self, topics: Topics, as_of: datetime) -> dict[str, float]:
        """Each person's share of all expertise in ``topics``: between 0 and 1."""
        scores = self.scores(topics, as_of)
        total = sum(scores.values())
        return {login: score / total for login, score in scores.items()} if total else {}

    def evidence(self, login: str, topics: Topics, as_of: datetime, *, years: int = 2) -> dict[str, int]:
        """How many PRs ``login`` authored, reviewed or merged on these topics recently."""
        since = as_of - timedelta(days=365 * years)
        counts: dict[str, int] = defaultdict(int)
        for value in self._before(as_of):
            if value.login == login and value.at >= since and any(topic in value.topics for topic in topics):
                counts[value.role] += 1
        return dict(counts)
