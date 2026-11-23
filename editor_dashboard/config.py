from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# `active`, `direct`, `reply_window` and `all` are deliberately absent: each has a
# fixed position in the suggested queue, so letting the cycle claim one too would
# give a single concept two competing mechanisms.
_ALLOWED_SUGGESTED_LANES = {"rereview", "overdue", "oldest_wait", "ready_bounded", "stale_direct", "stale_author", "land", "triage"}

@dataclass(frozen=True)
class RepositoryConfig:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class ResponseTargets:
    initial_editor_response_days: int = 7
    highlight_new_hours: int = 48
    # The share of contributor PRs that should get their first editor response inside
    # the target. The health view reads a rate below this as off target.
    first_response_within_target_percent: int = 80


@dataclass(frozen=True)
class AttentionConfig:
    # How recently a PR must have moved to count as currently active, and how long a
    # public mention keeps claiming attention. Review requests and assignments are
    # current API state that GitHub clears on review, so they never expire.
    activity_window_days: int = 30


@dataclass(frozen=True)
class ReadyBoundedConfig:
    max_changed_files: int = 8
    max_changed_lines: int = 800
    minimum_checklist_ratio: float = 1.0


@dataclass(frozen=True)
class SamplingConfig:
    # Keep outer pages small because each PR includes several nested connections.
    # GitHub terminates GraphQL requests that take too long, usually as HTTP 502/504.
    graphql_page_size: int = 10
    timeline_each_end: int = 25
    reviews_per_pr: int = 25
    review_threads: int = 50
    history_days: int = 90
    # Issues carry fewer nested connections than PRs, so their pages can be larger.
    issue_page_size: int = 25


@dataclass(frozen=True)
class SuggestedNextConfig:
    cycle: tuple[str, ...] = ("rereview", "overdue", "oldest_wait", "ready_bounded")
    # How many `reply_window` PRs lead the queue, ahead of the active lane. These are
    # the only PRs where a first reply can still land inside the response target, so
    # they get the top slots; the bound keeps incoming work from burying live reviews.
    # Zero disables the lead without hiding the lane.
    first_response_lead: int = 3


@dataclass(frozen=True)
class WaitingOnConfig:
    """Labels that say whose move a PR is, ahead of what the conversation says.

    A label is an editor's deliberate statement, so it outranks "who spoke last".
    """

    # Waiting for the WHATNOT meeting.
    meeting_labels: frozenset[str] = frozenset({"agenda+", "agenda+ f2f"})
    # "Must not merge per rationale in comment": usually a dependency that has to land
    # first, so on its own it does not decide who is next. See analysis.py.
    hold_labels: frozenset[str] = frozenset({"do not merge yet", "blocked"})
    # Label -> the party it says the PR waits on.
    party_labels: tuple[tuple[str, str], ...] = (
        ("needs implementer interest", "implementers"),
        ("needs compat analysis", "compat"),
        ("needs tests", "tests"),
    )
    # Waiting on the author for longer than this: a candidate to ping or close.
    stale_author_days: int = 180


_PARTIES = {"editors", "author", "implementers", "tests", "compat", "meeting", "dependency", "merge", "proposal"}


@dataclass(frozen=True)
class IssuesConfig:
    # Only issues opened (for the first-response lanes and triage) or last answered by
    # a contributor (for the longest waits) inside this window are queued. Most of
    # whatwg/html's open issues are dormant feature requests; without a bound, the
    # queue would be a decade of backlog.
    window_days: int = 90
    # Labels starting with this say what an issue is about; an issue without one is
    # in the triage lane.
    topic_label_prefix: str = "topic:"


@dataclass(frozen=True)
class ExpertiseConfig:
    # How far back full builds fetch merged PRs: for the label suggester and the
    # manual `dashboard.py expertise` report. Routing reads `knows:` instead.
    since: str = "2016-01-01"
    # Older work counts for less: a contribution this many days old counts half.
    half_life_days: int = 365


@dataclass(frozen=True)
class RoutingConfig:
    # Added to everybody's affinity, so an item nobody has expertise for is still
    # spread by load, and a small expertise edge cannot pile everything on one person.
    prior: float = 0.1
    # Routing counts how much each person was routed in this many days before an
    # item, to balance the load.
    load_window_days: int = 28
    # Accountability figures count routed items from this date: earlier ones were
    # never routed to anybody at the time.
    start: str = "2026-11-23"


@dataclass(frozen=True)
class IncrementalConfig:
    # A build with a stored snapshot only asks GitHub for what changed since it. Some
    # changes never move a PR's `updatedAt` (an edited comment, a deleted one), so a
    # full fetch still runs at least this often to put the snapshot right.
    full_every_days: int = 7
    # How far before the previous build's start the next one looks for updates, so
    # that GitHub's search index lagging behind a write cannot lose it.
    overlap_hours: int = 2


@dataclass(frozen=True)
class DashboardConfig:
    repository: RepositoryConfig
    # The editors the queue can be read as. There is deliberately no configured
    # "viewer": which of them is *you* is a property of the browser, not of the
    # repository, and a public deployment has no business guessing it.
    editors: frozenset[str]
    response_targets: ResponseTargets = field(default_factory=ResponseTargets)
    attention: AttentionConfig = field(default_factory=AttentionConfig)
    ready_bounded: ReadyBoundedConfig = field(default_factory=ReadyBoundedConfig)
    blocking_labels: frozenset[str] = field(default_factory=frozenset)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    suggested_next: SuggestedNextConfig = field(default_factory=SuggestedNextConfig)
    incremental: IncrementalConfig = field(default_factory=IncrementalConfig)
    waiting_on: WaitingOnConfig = field(default_factory=WaitingOnConfig)
    issues: IssuesConfig = field(default_factory=IssuesConfig)
    expertise: ExpertiseConfig = field(default_factory=ExpertiseConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    # Spec pages that say nothing about a change's topic: everybody touches them.
    ignore_pages: frozenset[str] = frozenset({"acknowledgements", "indices", "references", "infrastructure", "index", "introduction", "iana"})
    # A non-editor, non-helper who reviewed at least this many other people's PRs in
    # the health window gets their own row in the team section.
    min_reviews_of_others: int = 10
    # Team slug -> labels whose items the team is a good audience for.
    teams: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Editors who no longer take routed work. They keep a queue perspective, their
    # replies still count as editor responses, and only direct requests reach them.
    retired_editors: frozenset[str] = frozenset()
    # Non-editors who take routed review work and get their own queue perspective,
    # but are not editors for any metric.
    helpers: frozenset[str] = frozenset()
    # Relative share of routed work, by login; 0 means never routed.
    shares: tuple[tuple[str, float], ...] = ()
    # What each person says they know, as lowercased topic keys: `label:topic: img`
    # for a topic label, `page:images` for a multipage spec page. Routing reads only
    # these; computed expertise is a manual report (`dashboard.py expertise`).
    knows: tuple[tuple[str, frozenset[str]], ...] = ()
    # Label -> the multipage spec pages it covers, both lowercased. A change to a page
    # counts as being about the labels that cover it, so `knows:` only needs labels.
    # Every key is a topic label, `topic:` prefix or not.
    topic_pages: tuple[tuple[str, frozenset[str]], ...] = ()
    # Old label -> the name that replaces it, both lowercased, so a renamed or merged
    # label can be used under its new name before GitHub has it, and the old one keeps
    # counting afterwards.
    topic_aliases: tuple[tuple[str, str], ...] = ()

    @property
    def people(self) -> frozenset[str]:
        """Everyone with a queue perspective: the editors and the helpers."""
        return self.editors | self.helpers

    def share(self, login: str) -> float:
        return dict(self.shares).get(login, 0.0)

    def knows_of(self, login: str) -> frozenset[str]:
        return dict(self.knows).get(login, frozenset())

    def canonical_label(self, label: str) -> str:
        """A label's lowercased current name: the one it was renamed or merged into, if any."""
        lowered = label.strip().lower()
        return dict(self.topic_aliases).get(lowered, lowered)

    def is_topic_label(self, label: str) -> bool:
        lowered = self.canonical_label(label)
        return lowered.startswith(self.issues.topic_label_prefix) or lowered in dict(self.topic_pages)

    def labels_for_page(self, page: str) -> tuple[str, ...]:
        return tuple(sorted(label for label, pages in self.topic_pages if page in pages))

    def labels_for_location(self, page: str, sections: tuple[str, ...] = ()) -> tuple[str, ...]:
        """The labels covering a place in the spec: those naming its innermost listed
        section (`semantics#the-link-element`), else those covering the whole page."""
        for section in reversed(sections):
            entry = f"{page}#{section}"
            found = tuple(sorted(label for label, pages in self.topic_pages if entry in pages))
            if found:
                return found
        return self.labels_for_page(page)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _non_negative_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} must not be negative")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _iso_date(value: Any, name: str) -> str:
    text = str(value).strip()
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} must be a YYYY-MM-DD date") from None
    return text


def load_config(path: str | Path) -> DashboardConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = _mapping(raw, "configuration")

    repo_raw = _mapping(raw.get("repository", {}), "repository")
    owner = str(repo_raw.get("owner", "")).strip()
    name = str(repo_raw.get("name", "")).strip()
    if not owner or not name:
        raise ValueError("repository.owner and repository.name are required")

    editors_raw = raw.get("editors", [])
    if not isinstance(editors_raw, list):
        raise ValueError("editors must be a list")
    shares: dict[str, float] = {}
    retired: set[str] = set()
    knows_raw: dict[str, list[Any]] = {}

    def person(value: Any, name: str) -> tuple[str, float, str]:
        """A bare login, or a mapping with `login`, optional `share` and `status`."""
        if isinstance(value, dict):
            login = str(value.get("login", "")).strip().lstrip("@").lower()
            status = str(value.get("status", "active")).strip()
            if status not in {"active", "retired"}:
                raise ValueError(f"{name}.status must be active or retired")
            share = float(value.get("share", 0 if status == "retired" else 1))
            declared = value.get("knows", [])
            if not isinstance(declared, list):
                raise ValueError(f"{name}.knows must be a list")
            knows_raw[str(value.get("login", "")).strip().lstrip("@").lower()] = declared
        else:
            login, status, share = str(value).strip().lstrip("@").lower(), "active", 1.0
        if not login:
            raise ValueError(f"{name} entries need a login")
        if share < 0:
            raise ValueError(f"{name}.share must not be negative")
        return login, share, status

    editor_list: list[str] = []
    for value in editors_raw:
        if not isinstance(value, dict) and not str(value).strip():
            continue
        login, share, status = person(value, "editors")
        editor_list.append(login)
        shares[login] = 0.0 if status == "retired" else share
        if status == "retired":
            retired.add(login)
    editors = frozenset(editor_list)

    helpers_raw = raw.get("helpers", [])
    if not isinstance(helpers_raw, list):
        raise ValueError("helpers must be a list")
    helpers: set[str] = set()
    for value in helpers_raw:
        login, share, _status = person(value, "helpers")
        if login in editors:
            raise ValueError(f"helpers: @{login} is already an editor")
        helpers.add(login)
        shares[login] = share
    if not editors:
        raise ValueError("editors must list at least one login")

    targets_raw = _mapping(raw.get("response_targets", {}), "response_targets")
    targets = ResponseTargets(
        initial_editor_response_days=_positive_int(
            targets_raw.get("initial_editor_response_days", 7),
            "response_targets.initial_editor_response_days",
        ),
        highlight_new_hours=_positive_int(
            targets_raw.get("highlight_new_hours", 48),
            "response_targets.highlight_new_hours",
        ),
        first_response_within_target_percent=_positive_int(
            targets_raw.get("first_response_within_target_percent", 80),
            "response_targets.first_response_within_target_percent",
            maximum=100,
        ),
    )

    attention_raw = _mapping(raw.get("attention", {}), "attention")
    attention = AttentionConfig(
        activity_window_days=_positive_int(
            attention_raw.get("activity_window_days", 30),
            "attention.activity_window_days",
        ),
    )

    bounded_raw = _mapping(raw.get("ready_bounded", {}), "ready_bounded")
    ratio = float(bounded_raw.get("minimum_checklist_ratio", 1.0))
    if not 0 <= ratio <= 1:
        raise ValueError("ready_bounded.minimum_checklist_ratio must be between 0 and 1")
    bounded = ReadyBoundedConfig(
        max_changed_files=_positive_int(
            bounded_raw.get("max_changed_files", 8),
            "ready_bounded.max_changed_files",
        ),
        max_changed_lines=_positive_int(
            bounded_raw.get("max_changed_lines", 800),
            "ready_bounded.max_changed_lines",
        ),
        minimum_checklist_ratio=ratio,
    )

    sampling_raw = _mapping(raw.get("sampling", {}), "sampling")
    sampling = SamplingConfig(
        graphql_page_size=_positive_int(
            sampling_raw.get("graphql_page_size", 10),
            "sampling.graphql_page_size",
            maximum=100,
        ),
        timeline_each_end=_positive_int(
            sampling_raw.get("timeline_each_end", 25),
            "sampling.timeline_each_end",
            maximum=100,
        ),
        reviews_per_pr=_positive_int(
            sampling_raw.get("reviews_per_pr", 25),
            "sampling.reviews_per_pr",
            maximum=100,
        ),
        review_threads=_positive_int(
            sampling_raw.get("review_threads", 50),
            "sampling.review_threads",
            maximum=100,
        ),
        history_days=_positive_int(
            sampling_raw.get("history_days", 90),
            "sampling.history_days",
        ),
        issue_page_size=_positive_int(
            sampling_raw.get("issue_page_size", 25),
            "sampling.issue_page_size",
            maximum=100,
        ),
    )

    suggested_raw = _mapping(raw.get("suggested_next", {}), "suggested_next")
    cycle_raw = suggested_raw.get("cycle", ["rereview", "overdue", "oldest_wait", "ready_bounded"])
    if not isinstance(cycle_raw, list) or not cycle_raw:
        raise ValueError("suggested_next.cycle must be a non-empty list")
    cycle = tuple(str(value).strip() for value in cycle_raw)
    unknown_lanes = sorted(set(cycle) - _ALLOWED_SUGGESTED_LANES)
    if unknown_lanes:
        raise ValueError(f"suggested_next.cycle contains unknown lanes: {', '.join(unknown_lanes)}")
    if len(cycle) != len(set(cycle)):
        raise ValueError("suggested_next.cycle must not contain duplicate lanes")
    first_response_lead = _non_negative_int(
        suggested_raw.get("first_response_lead", 3),
        "suggested_next.first_response_lead",
        maximum=10,
    )

    incremental_raw = _mapping(raw.get("incremental", {}), "incremental")
    incremental = IncrementalConfig(
        full_every_days=_positive_int(
            incremental_raw.get("full_every_days", 7),
            "incremental.full_every_days",
            # The closed-PR search a full fetch runs is what bounds recent history, so
            # an incremental chain must not outlive it.
            maximum=sampling.history_days,
        ),
        overlap_hours=_non_negative_int(
            incremental_raw.get("overlap_hours", 2),
            "incremental.overlap_hours",
            maximum=48,
        ),
    )

    waiting_raw = _mapping(raw.get("waiting_on", {}), "waiting_on")
    defaults = WaitingOnConfig()

    def label_set(name: str, default: frozenset[str]) -> frozenset[str]:
        value = waiting_raw.get(name)
        if value is None:
            return default
        if not isinstance(value, list):
            raise ValueError(f"waiting_on.{name} must be a list")
        return frozenset(str(item).strip().lower() for item in value if str(item).strip())

    party_raw = waiting_raw.get("party_labels")
    if party_raw is None:
        party_labels = defaults.party_labels
    else:
        party_raw = _mapping(party_raw, "waiting_on.party_labels")
        party_labels = tuple((str(label).strip().lower(), str(party).strip()) for label, party in party_raw.items())
        bad = sorted(party for _label, party in party_labels if party not in _PARTIES)
        if bad:
            raise ValueError(f"waiting_on.party_labels names unknown parties: {', '.join(bad)}")
    waiting_on = WaitingOnConfig(
        meeting_labels=label_set("meeting_labels", defaults.meeting_labels),
        hold_labels=label_set("hold_labels", defaults.hold_labels),
        party_labels=party_labels,
        stale_author_days=_positive_int(
            waiting_raw.get("stale_author_days", defaults.stale_author_days),
            "waiting_on.stale_author_days",
        ),
    )

    issues_raw = _mapping(raw.get("issues", {}), "issues")
    issues = IssuesConfig(
        window_days=_positive_int(issues_raw.get("window_days", 90), "issues.window_days"),
        topic_label_prefix=str(issues_raw.get("topic_label_prefix", "topic:")).strip().lower(),
    )
    if not issues.topic_label_prefix:
        raise ValueError("issues.topic_label_prefix must not be empty")

    expertise_raw = _mapping(raw.get("expertise", {}), "expertise")
    expertise = ExpertiseConfig(
        since=_iso_date(expertise_raw.get("since", "2016-01-01"), "expertise.since"),
        half_life_days=_positive_int(expertise_raw.get("half_life_days", 365), "expertise.half_life_days"),
    )
    routing_raw = _mapping(raw.get("routing", {}), "routing")
    prior = float(routing_raw.get("prior", 0.1))
    if not 0 < prior <= 1:
        raise ValueError("routing.prior must be greater than 0 and at most 1")
    routing = RoutingConfig(
        prior=prior,
        load_window_days=_positive_int(routing_raw.get("load_window_days", 28), "routing.load_window_days"),
        start=_iso_date(routing_raw.get("start", "2026-11-23"), "routing.start"),
    )
    ignore_raw = raw.get("ignore_pages")
    if ignore_raw is not None and not isinstance(ignore_raw, list):
        raise ValueError("ignore_pages must be a list")
    teams_raw = _mapping(raw.get("teams", {}), "teams")
    teams = []
    for slug, labels in teams_raw.items():
        if not isinstance(labels, list) or not labels:
            raise ValueError(f"teams.{slug} must be a non-empty list of labels")
        teams.append((str(slug).strip(), tuple(sorted(str(label).strip().lower() for label in labels))))

    ignore_pages = (
        frozenset(str(value).strip() for value in ignore_raw)
        if ignore_raw is not None
        else DashboardConfig.__dataclass_fields__["ignore_pages"].default
    )
    topics_raw = _mapping(raw.get("topics", {}), "topics")
    topic_pages: dict[str, frozenset[str]] = {}
    for label, pages in topics_raw.items():
        topic = str(label).strip().lower()
        if not topic:
            raise ValueError("topics: a label must not be empty")
        # An empty list is a label that covers no page but can still be declared.
        if not isinstance(pages, list):
            raise ValueError(f"topics.{label} must be a list of spec pages")
        values = frozenset(str(page).strip().lower() for page in pages if str(page).strip())
        ignored = sorted(value for value in values if value.partition("#")[0] in ignore_pages)
        if ignored:
            raise ValueError(f"topics.{label}: {', '.join(ignored)} in ignore_pages, so they say nothing")
        topic_pages[topic] = values

    aliases_raw = _mapping(raw.get("topic_aliases", {}), "topic_aliases")
    aliases: dict[str, str] = {}
    for new, old_labels in aliases_raw.items():
        canonical = str(new).strip().lower()
        if not isinstance(old_labels, list) or not old_labels:
            raise ValueError(f"topic_aliases.{new} must be a non-empty list of the labels it replaces")
        for old in old_labels:
            alias = str(old).strip().lower()
            if alias in aliases and aliases[alias] != canonical:
                raise ValueError(f"topic_aliases: {old} is replaced by both {aliases[alias]} and {new}")
            aliases[alias] = canonical
    stale = sorted(set(aliases) & set(topic_pages))
    if stale:
        raise ValueError(f"topics: list {', '.join(stale)} under the name that replaces it, not the old one")

    knows: dict[str, frozenset[str]] = {}
    for login, declared in knows_raw.items():
        keys = set()
        for entry in declared:
            text = str(entry).strip().lower()
            if not text:
                raise ValueError(f"knows of @{login} has an empty entry")
            text = aliases.get(text, text)
            if text.startswith(issues.topic_label_prefix) or text in topic_pages:
                keys.add(f"label:{text}")
            else:
                if text in ignore_pages:
                    raise ValueError(f"knows of @{login}: {text} is in ignore_pages, so it says nothing")
                keys.add(f"page:{text}")
        knows[login] = frozenset(keys)

    blocking_labels_raw = raw.get("blocking_labels", [])
    if not isinstance(blocking_labels_raw, list):
        raise ValueError("blocking_labels must be a list")

    return DashboardConfig(
        repository=RepositoryConfig(owner=owner, name=name),
        editors=editors,
        retired_editors=frozenset(retired),
        helpers=frozenset(helpers),
        shares=tuple(sorted(shares.items())),
        knows=tuple(sorted(knows.items())),
        topic_pages=tuple(sorted(topic_pages.items())),
        # In file order: before a merge, the first old label listed is the one suggested.
        topic_aliases=tuple(aliases.items()),
        response_targets=targets,
        attention=attention,
        ready_bounded=bounded,
        blocking_labels=frozenset(str(value).strip().lower() for value in blocking_labels_raw if str(value).strip()),
        sampling=sampling,
        suggested_next=SuggestedNextConfig(cycle, first_response_lead),
        incremental=incremental,
        waiting_on=waiting_on,
        issues=issues,
        expertise=expertise,
        routing=routing,
        ignore_pages=ignore_pages,
        teams=tuple(sorted(teams)),
        min_reviews_of_others=_positive_int(
            _mapping(raw.get("contributors", {}), "contributors").get("min_reviews_of_others", 10),
            "contributors.min_reviews_of_others",
        ),
    )
