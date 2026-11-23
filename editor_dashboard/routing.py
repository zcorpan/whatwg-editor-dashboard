"""Who should pick up each unanswered item, so that new work is spread rather than
left to whoever looks first.

Every recent contributor item is walked, oldest ready first, whether or not it has
been answered since: an answer must not reshuffle everybody else's routing. The card
only shows the routing while nobody has answered and nobody was asked directly. Each
item goes to the person in the pool with the best

    (affinity + prior) x share / (1 + load)

where affinity is the part of the item's topics the person declares in
`dashboard.yml` (`knows:`), share is their configured share of routed work, and load
is how many items the walk already routed to them in the `routing.load_window_days`
before it. Items that were already waiting when routing began are walked as one batch
on `routing.start`, so the load term spreads that backlog. Ties go by a stable hash of
the item and the login.

Expertise is declared rather than computed: merged-PR history misses design done
elsewhere (srcset was designed in another spec and landed here by others) and follows
volume. `dashboard.py expertise` computes the history view, as an aid to editing the
declarations, and the build never runs it.

The walk only looks backwards, so a decision never moves when later items arrive. It
moves only when the configuration changes, or when the item's topics do: labeling an
item is triage, and is allowed to reroute it. Helpers take PRs only; issue triage
stays with the editors.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import DashboardConfig
from .topics import Topics, display_name


_HORIZONTAL_TEAM_LABEL_PREFIXES = ("a11y", "accessibility", "i18n", "security", "privacy", "impacts documentation")


@dataclass(frozen=True)
class Candidate:
    key: str
    kind: str  # "pr" or "issue"
    ready_at: datetime
    topics: Topics
    labels: tuple[str, ...]
    # Teams already mentioned in the item's text, as `@whatwg/<slug>`.
    mentioned_teams: frozenset[str]
    # Whether the item is waiting for somebody to pick it up now: never answered, and
    # nobody asked directly. Only these show their routing.
    unowned: bool
    # Whether the item takes part in the walk at all: every recent contributor item,
    # answered or not, so that answering one never reshuffles the others.
    routable: bool = True


@dataclass(frozen=True)
class Routing:
    owner: str
    evidence: str
    also_knows: tuple[str, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {"owner": self.owner, "evidence": self.evidence, "also_knows": list(self.also_knows)}


@dataclass(frozen=True)
class TeamSuggestion:
    team: str
    reason: str

    def to_public_dict(self) -> dict[str, str]:
        return {"team": self.team, "reason": self.reason}


def pool(config: DashboardConfig, kind: str) -> list[str]:
    people = [login for login in sorted(config.editors) if config.share(login) > 0]
    if kind == "pr":
        people += [login for login in sorted(config.helpers) if config.share(login) > 0]
    return people


def declared_overlap(config: DashboardConfig, login: str, topics: Topics) -> tuple[float, list[str]]:
    """The part of ``topics`` (by weight) that ``login`` declares, and which topics those are."""
    total = sum(topics.values())
    if not total:
        return 0.0, []
    declared = config.knows_of(login)
    matched = sorted((topic for topic in topics if topic.lower() in declared), key=lambda topic: (-topics[topic], topic))
    return sum(topics[topic] for topic in matched) / total, matched


def _evidence(matched: list[str]) -> str:
    if not matched:
        return "Spread by load: nobody routed to declares these topics"
    return "Declared in dashboard.yml: " + ", ".join(display_name(topic) for topic in matched)


def _tie_break(key: str, login: str) -> str:
    # Stable but not alphabetical: ordering ties by login handed every tie to the
    # same person.
    return hashlib.sha256(f"{key}\n{login}".encode("utf-8")).hexdigest()


def route(candidates: Iterable[Candidate], config: DashboardConfig) -> dict[str, Routing]:
    # An item that was waiting before routing began is routed as of that day, as one
    # batch with the rest of that backlog, so that the load term spreads it. Walked
    # at their own, often years-old, ready times, they never shared a load window.
    start = datetime.fromisoformat(config.routing.start).replace(tzinfo=timezone.utc)

    def effective(candidate: Candidate) -> datetime:
        return max(candidate.ready_at, start)

    ordered = sorted(
        (value for value in candidates if value.routable),
        key=lambda value: (effective(value), value.ready_at, value.key),
    )
    window = timedelta(days=config.routing.load_window_days)
    recent: deque[tuple[datetime, str]] = deque()
    decisions: dict[str, Routing] = {}
    for candidate in ordered:
        as_of = effective(candidate)
        while recent and recent[0][0] < as_of - window:
            recent.popleft()
        people = pool(config, candidate.kind)
        if not people:
            continue
        load = {login: 0 for login in people}
        for _at, login in recent:
            if login in load:
                load[login] += 1
        overlap = {login: declared_overlap(config, login, candidate.topics) for login in config.people}

        def score(login: str) -> float:
            return (overlap[login][0] + config.routing.prior) * config.share(login) / (1 + load[login])

        owner = min(people, key=lambda login: (-score(login), _tie_break(candidate.key, login)))
        recent.append((as_of, owner))
        # Anyone else who declares one of the topics, retired editors included: they
        # are never routed to, but are worth knowing about.
        others = sorted(
            (login for login, (share, _matched) in overlap.items() if login != owner and share > 0),
            key=lambda login: (-overlap[login][0], login),
        )[:2]
        decisions[candidate.key] = Routing(
            owner=owner,
            evidence=_evidence(overlap[owner][1]),
            also_knows=tuple(others),
        )
    return decisions


def suggest_teams(candidate: Candidate, config: DashboardConfig) -> list[TeamSuggestion]:
    """GitHub teams worth asking, never mentioned automatically.

    A team is suggested when its labels apply and either implementers are what the
    item waits for (teams are where implementers are), the label is a horizontal
    review one (the maintainer guidelines ask for the documentation team to be made
    aware), or nobody routed to declares the topic.
    """
    labels = {config.canonical_label(label) for label in candidate.labels}
    declared = any(declared_overlap(config, login, candidate.topics)[0] > 0 for login in pool(config, candidate.kind))
    suggestions: list[TeamSuggestion] = []
    for team, team_labels in config.teams:
        matching = sorted(labels & {config.canonical_label(label) for label in team_labels})
        if not matching or team in candidate.mentioned_teams:
            continue
        if "needs implementer interest" in labels:
            reason = "needs implementer interest; implementers follow this team"
        elif any(label.startswith(_HORIZONTAL_TEAM_LABEL_PREFIXES) for label in matching):
            reason = f"labeled {matching[0]}"
        elif not declared:
            reason = f"nobody routed to declares {matching[0]}"
        else:
            continue
        suggestions.append(TeamSuggestion(team, reason))
    return suggestions
