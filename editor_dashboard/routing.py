"""Who should pick up each unanswered item, so that new work is spread rather than
left to whoever looks first.

Every recent contributor item is walked, oldest ready first, whether or not it has
been answered since: an answer must not reshuffle everybody else's routing. The card
only shows the routing while nobody has answered and nobody was asked directly. Each goes to the person in the pool with the best

    (affinity + prior) x share / (1 + load)

where affinity is their share of the topic's expertise *as of the item's ready time*
(or of `routing.start`, for items that were already waiting when routing began),
share is their configured share of routed work, and load is how many items the walk
already routed to them in the `routing.load_window_days` before it. Ties go by a
stable hash of the item and the login.

Every input to a decision predates the item, and the walk only looks backwards, so a
decision never moves when later items arrive. It moves only when the configuration
changes, or when the item's topics do: labeling an item is triage, and is allowed to
reroute it. Helpers take PRs only; issue triage stays with the editors.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import DashboardConfig
from .expertise import Expertise
from .topics import Topics, display_name


# Others with at least this share of a topic's expertise are named as also knowing it.
EXPERTISE_FLOOR = 0.15
# Below this much decayed expertise (one review today scores 2), nobody routed to
# really knows a topic, which is when suggesting a GitHub team is worth something.
# A share would not do: whoever touched a topic once, years ago, holds all of it.
TEAM_SCORE_FLOOR = 2.0
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


def _evidence(expertise: Expertise, login: str, topics: Topics, as_of: datetime, affinity: float) -> str:
    if affinity <= 0:
        return "Spread by load: nobody routed to has recent work on these topics"
    counts = expertise.evidence(login, topics, as_of)
    parts = [f"{counts[role]} {role}" for role in ("authored", "reviewed", "merged") if counts.get(role)]
    main = max(topics.items(), key=lambda pair: (pair[1], pair[0]))[0]
    summary = ", ".join(parts) if parts else "older work"
    return f"{summary} on {display_name(main)} and related topics in two years"


def _tie_break(key: str, login: str) -> str:
    # Stable but not alphabetical: ordering ties by login handed every tie to the
    # same person.
    return hashlib.sha256(f"{key}\n{login}".encode("utf-8")).hexdigest()


def route(candidates: Iterable[Candidate], expertise: Expertise, config: DashboardConfig) -> dict[str, Routing]:
    # An item that was waiting before routing began is routed as of that day: with
    # the expertise there was then, and as one batch with the rest of that backlog,
    # so that the load term spreads it. Routing each at its own, often years-old,
    # ready time left most of them with no expertise and no load to go on.
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
        affinities = expertise.affinities(candidate.topics, as_of)

        def score(login: str) -> float:
            return (affinities.get(login, 0.0) + config.routing.prior) * config.share(login) / (1 + load[login])

        owner = min(people, key=lambda login: (-score(login), _tie_break(candidate.key, login)))
        recent.append((as_of, owner))
        others = sorted(
            (login for login, value in affinities.items() if login != owner and value >= EXPERTISE_FLOOR),
            key=lambda login: (-affinities[login], login),
        )[:2]
        decisions[candidate.key] = Routing(
            owner=owner,
            evidence=_evidence(expertise, owner, candidate.topics, as_of, affinities.get(owner, 0.0)),
            also_knows=tuple(others),
        )
    return decisions


def suggest_teams(
    candidate: Candidate,
    expertise: Expertise,
    config: DashboardConfig,
    *,
    now: datetime,
) -> list[TeamSuggestion]:
    """GitHub teams worth asking, never mentioned automatically.

    A team is suggested when its labels apply and either implementers are what the
    item waits for (teams are where implementers are), the label is a horizontal
    review one (the maintainer guidelines ask for the documentation team to be made
    aware), or nobody routed to has much expertise in the topic.
    """
    labels = {label.lower() for label in candidate.labels}
    scores = expertise.scores(candidate.topics, now)
    best = max((scores.get(login, 0.0) for login in pool(config, candidate.kind)), default=0.0)
    suggestions: list[TeamSuggestion] = []
    for team, team_labels in config.teams:
        matching = sorted(labels & set(team_labels))
        if not matching or team in candidate.mentioned_teams:
            continue
        if "needs implementer interest" in labels:
            reason = "needs implementer interest; implementers follow this team"
        elif any(label.startswith(_HORIZONTAL_TEAM_LABEL_PREFIXES) for label in matching):
            reason = f"labeled {matching[0]}"
        elif best < TEAM_SCORE_FLOOR:
            reason = f"little recent editor expertise in {matching[0]}"
        else:
            continue
        suggestions.append(TeamSuggestion(team, reason))
    return suggestions
