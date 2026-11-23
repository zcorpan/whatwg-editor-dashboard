from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from editor_dashboard.config import load_config
from editor_dashboard.routing import Candidate, route, suggest_teams
from editor_dashboard.topics import LabelSuggester, item_topics


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


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
        config = load_config(ROOT / "dashboard.yml")
        # Routing began long before these items, so each is routed at its own ready
        # time; and these declarations, not the configured draft, are what counts.
        cls.config = replace(
            config,
            routing=replace(config.routing, start="2020-01-01"),
            knows=(
                ("annevk", frozenset({"label:topic: forms"})),
                ("domenic", frozenset({"label:topic: navigation"})),
                ("noamr", frozenset({"label:topic: parser"})),
                ("zcorpan", frozenset({"label:topic: parser", "label:topic: img", "page:image-maps"})),
            ),
        )

    def route(self, candidates, config=None):
        return route(candidates, config or self.config)

    def test_items_go_to_the_person_who_declares_the_topic(self) -> None:
        decisions = self.route([candidate("img", 2, ["topic: img"]), candidate("forms", 1, ["topic: forms"])])
        self.assertEqual(decisions["img"].owner, "zcorpan")
        self.assertEqual(decisions["forms"].owner, "annevk")
        self.assertEqual(decisions["img"].evidence, "Declared in dashboard.yml: topic: img")

    def test_a_diff_matches_the_labels_that_cover_its_pages(self) -> None:
        # images.html is covered by topic: img, which zcorpan declares.
        decision = self.route([candidate("srcset", 1, pages={"images": 3})])["srcset"]
        self.assertEqual(decision.owner, "zcorpan")
        self.assertEqual(decision.evidence, "Declared in dashboard.yml: topic: img")

    def test_a_page_no_label_covers_can_be_declared_directly(self) -> None:
        decision = self.route([candidate("usemap", 1, pages={"image-maps": 2})])["usemap"]
        self.assertEqual(decision.owner, "zcorpan")
        self.assertIn("the image-maps page", decision.evidence)

    def test_load_spreads_a_run_among_those_who_declare_it(self) -> None:
        # zcorpan and noamr both declare the parser; a run of parser PRs goes to both.
        run = [candidate(f"p{n}", 10 - n, ["topic: parser"]) for n in range(6)]
        owners = {decision.owner for decision in self.route(run).values()}
        self.assertEqual(owners, {"noamr", "zcorpan"})

    def test_retired_editors_are_never_routed_to_but_are_named_as_knowing(self) -> None:
        decision = self.route([candidate("nav", 1, ["topic: navigation"])])["nav"]
        self.assertNotEqual(decision.owner, "domenic")
        self.assertIn("domenic", decision.also_knows)
        self.assertTrue(decision.evidence.startswith("Spread by load"))

    def test_helpers_take_prs_but_not_issues(self) -> None:
        issues = [candidate(f"i{n}", 10 - n, ["topic: parser"], kind="issue") for n in range(6)]
        self.assertNotIn("noamr", {decision.owner for decision in self.route(issues).values()})
        prs = [candidate(f"p{n}", 10 - n, ["topic: parser"]) for n in range(6)]
        self.assertIn("noamr", {decision.owner for decision in self.route(prs).values()})

    def test_a_decision_does_not_move_when_later_items_arrive(self) -> None:
        first = self.route([candidate("a", 5, ["topic: forms"])])["a"]
        later = self.route(
            [candidate("a", 5, ["topic: forms"]), *(candidate(f"b{n}", 4 - n * 0.1, ["topic: forms"]) for n in range(5))]
        )["a"]
        self.assertEqual(first, later)

    def test_a_backlog_older_than_routing_is_spread_as_one_batch(self) -> None:
        # Waiting since before routing began, years apart: routed together on the
        # start date, so the load term applies across them rather than never.
        config = replace(self.config, routing=replace(self.config.routing, start=NOW.date().isoformat()))
        backlog = [candidate(f"old{n}", 3000 - n * 200) for n in range(10)]
        owners = [decision.owner for decision in self.route(backlog, config).values()]
        self.assertGreaterEqual(len(set(owners)), 4)

    def test_items_outside_the_walk_are_not_routed(self) -> None:
        self.assertEqual(self.route([candidate("x", 1, routable=False)]), {})

    def test_an_answer_does_not_reshuffle_the_others(self) -> None:
        # Answered items stay in the walk, so the load they put on their owner holds.
        items = [candidate(f"i{n}", 10 - n, ["topic: parser"]) for n in range(4)]
        before = self.route(items)
        answered = [replace(items[0], unowned=False), *items[1:]]
        self.assertEqual(self.route(answered), before)

    def test_teams_are_suggested_where_nobody_routed_to_declares_the_topic(self) -> None:
        media = candidate("m", 1, ["topic: media"])
        self.assertEqual([value.team for value in suggest_teams(media, self.config)], ["media"])
        self.assertEqual(suggest_teams(candidate("p", 1, ["topic: parser"]), self.config), [])
        asked = replace(media, mentioned_teams=frozenset({"media"}))
        self.assertEqual(suggest_teams(asked, self.config), [])
        interest = candidate("q", 1, ["topic: parser", "needs implementer interest"])
        self.assertEqual([value.team for value in suggest_teams(interest, self.config)], ["html-parser"])


class TopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")

    def test_pages_become_the_labels_that_cover_them(self) -> None:
        # Cross-cutting pages say nothing; parsing is covered by topic: parser.
        topics = item_topics([], self.config, pages={"acknowledgements": 3, "parsing": 1})
        self.assertEqual(topics, {"label:topic: parser": 1.0})
        # A page two labels cover splits its share; one no label covers stays a page.
        topics = item_topics([], self.config, pages={"iframe-embed-object": 1, "image-maps": 1})
        self.assertEqual(
            topics,
            {
                "label:topic: embed and object": 0.25,
                "label:topic: frames/navigables/browsing contexts": 0.25,
                "page:image-maps": 0.5,
            },
        )

    def test_old_labels_count_as_the_names_that_replace_them(self) -> None:
        topics = item_topics(["topic: timing", "topic: link"], self.config)
        self.assertEqual(set(topics), {"label:topic: timers and user prompts", "label:topic: links"})
        # Before the rename, the existing label is what gets suggested.
        suggester = LabelSuggester.from_config(self.config, ["topic: timing"])
        self.assertEqual(suggester.from_pages({"timers-and-user-prompts": 2}, self.config)[0], "topic: timing")
        # Of two old labels being merged, the first listed in topic_aliases.
        suggester = LabelSuggester.from_config(
            self.config, ["topic: cross-origin-embedder-policy", "topic: cross-origin-opener-policy"]
        )
        self.assertEqual(suggester.from_pages({"browsers": 2}, self.config)[0], "topic: cross-origin-opener-policy")
        # Afterwards, the new one.
        suggester = LabelSuggester.from_config(self.config, ["topic: timing", "topic: timers and user prompts"])
        self.assertEqual(
            suggester.from_pages({"timers-and-user-prompts": 2}, self.config)[0], "topic: timers and user prompts"
        )

    def test_mapped_labels_without_the_prefix_are_topic_labels(self) -> None:
        self.assertIn("label:accessibility", item_topics(["accessibility", "clarification"], self.config))
        self.assertNotIn("label:clarification", item_topics(["accessibility", "clarification"], self.config))

    def test_the_label_covering_the_main_page_is_suggested(self) -> None:
        suggester = LabelSuggester.from_config(self.config, ["topic: parser", "topic: dialog", "removal/deprecation"])
        label, reason = suggester.from_pages({"parsing": 3, "rendering": 1}, self.config)
        self.assertEqual(label, "topic: parser")
        self.assertIn("parsing page", reason)
        self.assertEqual(suggester.from_pages({"obsolete": 1}, self.config)[0], "removal/deprecation")
        # Two labels cover it, or none does: no suggestion.
        self.assertIsNone(suggester.from_pages({"iframe-embed-object": 3}, self.config))
        self.assertIsNone(suggester.from_pages({"image-maps": 3}, self.config))
        # topic: semantics covers sections, but does not exist on GitHub yet.
        self.assertIsNone(suggester.from_pages({"sections": 3}, self.config))
        self.assertEqual(suggester.from_title("The <dialog> element closes twice")[0], "topic: dialog")


if __name__ == "__main__":
    unittest.main()
