from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from editor_dashboard.config import load_config
from editor_dashboard.expertise import Expertise, contributions
from editor_dashboard.models import HistoryRecord
from editor_dashboard.routing import Candidate, route, suggest_teams
from editor_dashboard.topics import LabelSuggester, item_topics


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def record(number: int, author: str, days_ago: float, labels=(), reviewers=(), oid=None) -> HistoryRecord:
    at = NOW - timedelta(days=days_ago)
    return HistoryRecord(
        number=number,
        title=f"Change {number}",
        author=author,
        merged_at=at,
        merged_by=reviewers[0] if reviewers else None,
        merge_oid=oid,
        labels=tuple(labels),
        issue_labels=(),
        reviews=tuple((login, at - timedelta(hours=1)) for login in reviewers),
    )


def candidate(key: str, days_ago: float, labels=(), *, kind="pr", pages=None, unowned=True, routable=True) -> Candidate:
    config = load_config(ROOT / "dashboard.yml")
    return Candidate(
        key=key,
        kind=kind,
        ready_at=NOW - timedelta(days=days_ago),
        topics=item_topics(labels, config, pages=pages),
        labels=tuple(labels),
        mentioned_teams=frozenset(),
        unowned=unowned,
        routable=routable,
    )


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Routing began long before these items, so each is routed at its own ready time.
        config = load_config(ROOT / "dashboard.yml")
        cls.config = replace(config, routing=replace(config.routing, start="2020-01-01"))
        history = [
            # zcorpan knows the parser, annevk forms; foolip reviewed media once, long ago.
            *(record(100 + n, "contributor", 30 + n, ["topic: parser"], ["zcorpan"]) for n in range(6)),
            *(record(200 + n, "annevk", 40 + n, ["topic: forms"]) for n in range(6)),
            record(300, "contributor", 900, ["topic: media"], ["foolip"]),
            # A retired editor's expertise is real, but they are not routed to.
            *(record(400 + n, "domenic", 20 + n, ["topic: navigation"]) for n in range(6)),
        ]
        cls.expertise = Expertise(contributions(history, {}, cls.config), cls.config)

    def test_items_go_to_the_person_who_knows_the_topic(self) -> None:
        decisions = route(
            [candidate("parser", 2, ["topic: parser"]), candidate("forms", 1, ["topic: forms"])],
            self.expertise,
            self.config,
        )
        self.assertEqual(decisions["parser"].owner, "zcorpan")
        self.assertEqual(decisions["forms"].owner, "annevk")
        self.assertIn("reviewed", decisions["parser"].evidence)

    def test_load_spreads_a_run_of_one_topic(self) -> None:
        run = [candidate(f"p{n}", 10 - n, ["topic: parser"]) for n in range(6)]
        owners = [decision.owner for _key, decision in sorted(route(run, self.expertise, self.config).items())]
        self.assertGreater(owners.count("zcorpan"), 1)
        self.assertGreater(len(set(owners)), 1)

    def test_retired_editors_are_never_routed_to_but_are_named_as_knowing(self) -> None:
        decision = route([candidate("nav", 1, ["topic: navigation"])], self.expertise, self.config)["nav"]
        self.assertNotEqual(decision.owner, "domenic")
        self.assertIn("domenic", decision.also_knows)

    def test_helpers_take_prs_but_not_issues(self) -> None:
        issues = [candidate(f"i{n}", 10 - n, kind="issue") for n in range(12)]
        owners = {decision.owner for decision in route(issues, self.expertise, self.config).values()}
        self.assertNotIn("noamr", owners)
        prs = [candidate(f"p{n}", 10 - n) for n in range(12)]
        owners = {decision.owner for decision in route(prs, self.expertise, self.config).values()}
        self.assertIn("noamr", owners)

    def test_a_decision_does_not_move_when_later_items_arrive(self) -> None:
        first = route([candidate("a", 5, ["topic: forms"])], self.expertise, self.config)["a"]
        later = route(
            [candidate("a", 5, ["topic: forms"]), *(candidate(f"b{n}", 4 - n * 0.1, ["topic: forms"]) for n in range(5))],
            self.expertise,
            self.config,
        )["a"]
        self.assertEqual(first, later)

    def test_a_backlog_older_than_routing_is_spread_as_one_batch(self) -> None:
        # Waiting since before routing began, years apart: routed together on the
        # start date, so the load term applies across them rather than never.
        config = replace(self.config, routing=replace(self.config.routing, start=NOW.date().isoformat()))
        backlog = [candidate(f"old{n}", 3000 - n * 200) for n in range(10)]
        owners = [decision.owner for decision in route(backlog, self.expertise, config).values()]
        self.assertGreaterEqual(len(set(owners)), 4)

    def test_items_outside_the_walk_are_not_routed(self) -> None:
        self.assertEqual(route([candidate("x", 1, routable=False)], self.expertise, self.config), {})

    def test_an_answer_does_not_reshuffle_the_others(self) -> None:
        # Answered items stay in the walk, so the load they put on their owner holds.
        items = [candidate(f"i{n}", 10 - n, ["topic: parser"]) for n in range(4)]
        before = route(items, self.expertise, self.config)
        answered = [replace(items[0], unowned=False), *items[1:]]
        self.assertEqual(route(answered, self.expertise, self.config), before)

    def test_expertise_only_counts_work_before_the_item(self) -> None:
        topics = item_topics(["topic: parser"], self.config)
        self.assertEqual(self.expertise.affinities(topics, NOW - timedelta(days=400)), {})
        affinities = self.expertise.affinities(topics, NOW)
        # The contributor who authored them holds more (authoring weighs 3, reviewing 2),
        # but among the people routed to, zcorpan leads.
        self.assertEqual(max(("annevk", "foolip", "zcorpan"), key=lambda login: affinities.get(login, 0)), "zcorpan")

    def test_teams_are_suggested_where_nobody_routed_to_knows_the_topic(self) -> None:
        media = candidate("m", 1, ["topic: media"])
        self.assertEqual([value.team for value in suggest_teams(media, self.expertise, self.config, now=NOW)], ["media"])
        parser = candidate("p", 1, ["topic: parser"])
        self.assertEqual(suggest_teams(parser, self.expertise, self.config, now=NOW), [])
        asked = replace(media, mentioned_teams=frozenset({"media"}))
        self.assertEqual(suggest_teams(asked, self.expertise, self.config, now=NOW), [])
        interest = candidate("q", 1, ["topic: parser", "needs implementer interest"])
        self.assertEqual([value.team for value in suggest_teams(interest, self.expertise, self.config, now=NOW)], ["html-parser"])


class TopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")

    def test_cross_cutting_pages_say_nothing(self) -> None:
        topics = item_topics([], self.config, pages={"acknowledgements": 3, "parsing": 1})
        self.assertEqual(topics, {"page:parsing": 1.0})

    def test_a_page_suggests_the_label_its_history_carries(self) -> None:
        examples = [(["topic: parser"], {"parsing": 2})] * 6 + [(["topic: forms"], {"parsing": 1})]
        suggester = LabelSuggester.learn(examples, self.config, ["topic: parser", "topic: dialog"])
        label, reason = suggester.from_pages({"parsing": 3}, self.config)
        self.assertEqual(label, "topic: parser")
        self.assertIn("6 of 7", reason)
        self.assertIsNone(suggester.from_pages({"forms": 3}, self.config))
        self.assertEqual(suggester.from_title("The <dialog> element closes twice")[0], "topic: dialog")


if __name__ == "__main__":
    unittest.main()
