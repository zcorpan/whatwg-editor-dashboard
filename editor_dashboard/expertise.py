"""Who has worked on which topic, from merged PRs: input to the manual
`dashboard.py expertise` report, never to routing (which reads `knows:`).

A merged PR credits its author 3, each reviewer other than the author 2, and its
merger 1 if they did neither, each spread over the PR's topics. The report decays
the credit by `expertise.half_life_days`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
