# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A build-time static site generator. A Python CLI fetches public `whatwg/html` PR data from GitHub's GraphQL API, applies deterministic prioritization rules, and emits `site/` (copied `web/` assets plus a single `data.json`). The browser page is a plain, dependency-free ES module that renders that JSON and keeps personal workflow state in `localStorage`. There is no server and no runtime API call.

Read [README.md](README.md) for the product-level lane definitions and [PRIVACY.md](PRIVACY.md) for the public/private boundary.

## Commands

Setup (Python 3.12+, only dependency is PyYAML):

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install --requirement requirements.txt
```

Build the deterministic offline demo (no token needed) and serve it:

```bash
python dashboard.py build --fixture fixtures/sample_api_data.json --now 2026-08-03T12:00:00Z --output site
python -m http.server --directory site 8000
```

Build from live public GitHub data:

```bash
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site
```

Validate `dashboard.yml`, and run tests:

```bash
python dashboard.py validate
python -m unittest discover -s tests -v
python -m unittest tests.test_analysis -v                                     # one module
python -m unittest tests.test_analysis.AnalysisTests.test_expected_mvp_lanes  # one test
```

`--log-level DEBUG` on `dashboard.py` surfaces GraphQL retry detail.

There is no linter, formatter, or JS toolchain. `site/` is build output and gitignored; CI builds on Python 3.12.

## Architecture

The pipeline is a strict one-way chain; keep the layer boundaries intact when extending.

1. [config.py](editor_dashboard/config.py) — parses and validates [dashboard.yml](dashboard.yml) into frozen dataclasses. All thresholds, logins, sampling sizes, and lane cycling live here; validation is eager and raises `ValueError` with a field-qualified message. The viewer is always forced into the editors set.
2. [github.py](editor_dashboard/github.py) — `GraphQLClient` (urllib, no HTTP library) plus `fetch_repository_data`, which paginates two queries from [graphql/](graphql/): open PRs via the repository connection, recently closed PRs via `search`. `load_fixture` produces the same `RepositoryData` from JSON, which is how every test and the demo build run.
3. [models.py](editor_dashboard/models.py) — `PullRequestSnapshot.from_graphql` / `Activity.from_graphql` normalize raw GraphQL nodes: logins lowercased, timestamps parsed to UTC, the `timelineFirst`/`timelineLast` aliases merged and deduplicated. Snapshots are frozen and carry their own sampling-completeness flags.
4. [analysis.py](editor_dashboard/analysis.py) — the heart. `analyze_pull_request` produces one `PRAnalysis` per PR: lane membership, `Reason` evidence chips, blockers, and the two fingerprints. `build_lanes` produces the per-lane ordering as lists of `owner/repo#number` keys.
5. [metrics.py](editor_dashboard/metrics.py) — aggregates `PRAnalysis` values into repository-health, flow-window (7/28/90 day), viewer-impact, and sampling-coverage numbers.
6. [build.py](editor_dashboard/build.py) — assembles the `data.json` payload (`schema_version`, lanes, items, metrics, methodology, build metadata), copies `web/*` into the output, writes `.nojekyll`.
7. [web/app.js](web/app.js) — fetches `data.json`, resolves lane key lists against `items`, and layers browser-local state on top. `checkForFreshData` re-fetches and re-renders in place when a long-lived tab's `generated_at` is older than the 24 h build interval, gated on tab visibility and a throttle; keep the whole render path re-runnable from `applyDashboard`.

[checklist.py](editor_dashboard/checklist.py) sits outside the chain as a leaf called from `analysis.py`: it parses GitHub task-list items out of a PR description while skipping fenced code blocks. PR bodies stay on the in-memory snapshot; `analysis.py` reduces each one to a mention match, a sha256 for the content fingerprint, and a `Checklist`, and only the checklist's counts plus its short labels cross into `data.json`.

Lane identifiers (`active`, `direct`, `stale_direct`, `rereview`, `new`, `oldest_wait`, `ready_bounded`, `all`) are a shared vocabulary across `analysis.py`, `build.py` `LANE_DESCRIPTIONS`, `config.py` `_ALLOWED_SUGGESTED_LANES`, and `web/app.js` `LANE_ORDER`. Adding or renaming one means touching all four.

Two smaller vocabularies cross the Python/browser boundary the same way. Sort orders (`queue`, `checklist`, `unchecked`, `wait`, `updated`, `created`) must agree between `web/app.js` `SORT_ORDERS` and the `<option value>` list in [web/index.html](web/index.html); `test_build.py` pins two of them. A `Reason`'s `tone` becomes a `chip <tone>` class, so it has to be one of the `.chip.*` rules in [web/style.css](web/style.css) (`urgent`, `attention`, `positive`, `warning`, `muted`) — `neutral` is the JS default and is deliberately unstyled.

### The two fingerprints

`analysis.py` computes both, and they drive distinct browser behaviours — do not conflate them:

- `content_fingerprint` hashes the PR's public content *excluding the viewer's own footprint* (title, body hash, draft state, head OID, labels, other people's assignments and review requests, status, mergeability, the newest non-viewer comment/review, and the counts of review threads somebody else started). "Address until changed" stores it; the PR reappears when it changes.
- `attention_fingerprint` hashes only the direct-request and re-review reason codes/timestamps, and drops the timestamps of the two current-state codes in `_STATE_ATTENTION_CODES`, which carry `pr.updated_at` as a stand-in. "Seen" stores it; a new attention signal makes the item unseen again.

Changing what goes into either hash silently invalidates users' stored state, so treat the payloads as a compatibility surface.

**Viewer-independence is the point of the content fingerprint, not an optimization.** "Address until changed" has to mean "until somebody else changes it". Reviewing a PR moves `pr.updated_at`, appends a timeline item, clears the viewer's own review request, sets `review_decision` and opens review threads, so hashing any of those made every addressed item reappear at the next build — the editor's own reply was read as a change. `pr.updated_at` is therefore not hashed at all (it cannot be attributed to anyone); the public state it stood proxy for is hashed field by field instead. Two consequences are deliberate: a renewed review request with no other change does not resurface an addressed PR, and only the *newest* non-viewer comment is fingerprinted, because a viewer comment on a PR with more than `2 * timeline_each_end` comments shifts the sampling window and would move a list-based hash. What is still unattributable — labels, the PR body, the head commit — stays in the hash, so the viewer editing a description or adding a label does bring the PR back.

### Browser-local state

`localStorage` holds the only user-owned data in the system, and there is no migration path, so its shape is as much a compatibility surface as the fingerprints. The key is `whatwg-editor-dashboard:v${STATE_VERSION}:${location.pathname}` — path-namespaced against other projects on the same origin (see the caveat in [README.md](README.md)), and version-gated: `loadLocalState` discards any payload whose `version` differs, so bumping `STATE_VERSION` silently throws away every user's seen/addressed/pinned/snoozed state. Per-item state is keyed by `owner/repo#number`. Renaming a direct-request or re-review `Reason` code has the same effect on a smaller scale, because those codes are hashed into `attention_fingerprint` and every affected item becomes unseen again.

### Determinism and sampling honesty

Every classification is rule-based; no LLM, no heuristics that cannot be shown as evidence (see [Design intent](#design-intent-and-deliberate-non-goals)). Two properties the code deliberately maintains:

- **Deterministic output.** `now` is threaded explicitly through analysis, metrics, and build so a fixture build is byte-reproducible. Sorts always break ties on PR number. Never call `datetime.now()` below `dashboard.py`.
- **Incomplete samples are visible, not silent.** Only the first and last `timeline_each_end` comments/reviews are fetched. `timeline_sample_complete`, `first_editor_response_known`, and `review_threads_sample_complete` gate which metrics are reported; the `coverage` block in `data.json` publishes the shortfall. When adding a metric that needs middle history, gate it the same way rather than assuming completeness.

Two GitHub API traps have already cost this codebase a silent wrong answer each. Both produced plausible zeros rather than errors, so assert on real values in tests instead of trusting that a number exists:

- **`timelineItems.totalCount` ignores the `itemTypes` filter.** It counts commits, labels and assignments too, so it can never be compared against the filtered `nodes`. Completeness comes from `pageInfo.hasNextPage` ([models.py](editor_dashboard/models.py) `_timeline_sample_complete`). Comparing against `totalCount` marked 283 of 283 open PRs incompletely sampled, which turned every "no editor has responded" into "unknown" and reported `known_without_editor_response: 0`.
- **`authorAssociation` never returns `FIRST_TIME*` on this repo.** It returns `CONTRIBUTOR`, `MEMBER`, or `NONE`, so first-time-contributor detection keys off `NONE`. Checking only for `FIRST_TIMER`/`FIRST_TIME_CONTRIBUTOR` left the flag false on all 283 PRs and every first-time-contributor metric at zero.

### GraphQL resilience

GitHub returns HTTP 502/504 for GraphQL request timeouts, and this query is nested-connection heavy. `GraphQLClient.execute` retries with exponential backoff and, on 502/504 only, halves `pageSize` and retains the reduced cap for the rest of the build (`_page_size_cap`). 403 with `X-RateLimit-Remaining: 0` fails fast; secondary-rate-limit 403s get at least a 60 s delay. `fetch_repository_data` also deduplicates PRs that appear twice across pages and drops open-PR nodes that also came back in the closed search, appending a human-readable note to `metadata.warnings` (which is published in `data.json`) rather than failing.

## Design intent and deliberate non-goals

This codebase is milestones 1–2 of a longer plan, and several apparent gaps are decisions rather than omissions. Don't "fix" these without a deliberate change of direction.

**Lanes, not a score.** A single numeric ranking across all open PRs was considered and rejected: buckets plus visible `Reason` chips are the product. Any scoring may only order items *within* a lane, and the reasons must always be shown. The `active` lane outranks everything else.

**Recency is the primary axis for "what needs me now".** The top of the queue answers "which reviews am I currently in the middle of", not "which claim on my attention is oldest". The `active` lane holds PRs with public activity inside `activity_window_days` (30) where the editor is involved — a direct signal, a re-review owed, or a review previously submitted. It sorts newest activity first, and `suggested_next` emits all of it before the cycle.

This inverts the original design, which put every direct request first, oldest first. On real `whatwg/html` data that buried the live work: of 283 open PRs only 39 had any activity within 30 days, yet 35 direct requests preceded the cycle, led by a mention from 2016 on a PR untouched for 953 days. Age is a poor proxy for actionability on a decade-old backlog.

Consequences to preserve:

- Newest-first is the default for the attention lanes (`active`, `direct`, `rereview`). `oldest_wait` is the **only** lane sorted oldest-first, because fairness to waiting contributors is exactly what it exists to measure.
- When a PR carries several direct signals, the **freshest** one represents it. Taking the oldest meant one stale mention outranked a review request filed the same week.

**Direct signals expire; current state does not.** A review request or assignment is current API state — GitHub clears it when the editor reviews — so it persists as a direct request indefinitely. A mention is a past *event* with no clearing mechanism; once `@zcorpan` appeared in a 2016 comment, that PR claimed a direct request forever. Mentions therefore count only inside the activity window. Expired ones move to a separate stale lane rather than being discarded, so an old mention stays findable without leading the queue.

**`oldest_wait` measures the current wait, not PR age.** It is time since the latest non-editor human activity that no editor answered. A five-year-old PR whose author replied yesterday has a one-day wait; a three-week-old PR with no editor response has a three-week wait. Bot activity must never reset this clock — hence `is_bot()` filtering throughout [analysis.py](editor_dashboard/analysis.py).

**The `suggested_next` interleave resolves a specific tension.** Active work first, then a cycle, so that incoming work cannot permanently starve long-waiting contributors while new PRs still get a fast turnaround. Changing the cycle changes that balance. The cycle is what keeps the backlog from being abandoned now that the top of the queue is recency-driven.

**`new` means "never got a first response", with no age cap.** It previously required `age <= initial_editor_response_days`, so a PR left the lane on the very day it missed the target — the lane could only show successes in progress, never failures. It is now unbounded and sorted oldest-first, and past target the reason chip escalates to `first-response-overdue`. It stays distinct from `oldest_wait`, which covers stalled conversations an editor *did* join at some point.

**No GitHub notification / unread state, on purpose.** The REST notifications endpoint requires a *classic* PAT (fine-grained PATs and GitHub App tokens are unsupported), and putting such a token in a workflow that publishes public output is an unacceptable leakage risk. "Unseen" therefore means "this browser has not opened this public attention signal". Proper unread sync is deferred to a future OAuth-backed service, not to a secret in this workflow.

**No LLM.** If advisory summaries are ever added, they must stay advisory: they may never decide whether an item disappears, becomes "ready", or outranks a direct request, and they must be cached by content fingerprint.

**Wording is a requirement, not style.** Impact metrics describe event order, never causation — "PRs merged after your review", never "PRs you caused to merge" (`test_metrics.py` guards this). Likewise the task list is a *description checklist* and never "requirements complete": it says the author ticked boxes, nothing about test sufficiency, implementer interest, or merge readiness.

**All GitHub-derived text is untrusted.** PR titles, bodies, logins, and comments can contain deliberate HTML or script-like content; they reach the page only as text nodes.

**The workflow is read-only apart from the Pages deploy.** Rolling 90-day metrics are recomputed from the API each run rather than kept in a database. If daily aggregates are added later they belong on a separate data branch, which is the only thing that should ever need `contents: write`.

**Deferred by plan:** issues (PRs only for now), other WHATWG repositories (`dashboard.yml` is single-repo, though the layering is meant to allow more), cross-device state, OAuth/multi-user, and assessment of actual WPT coverage, implementer interest, web-compat risk, or consensus quality.

## Constraints enforced by tests

- `test_build.py` asserts the published payload contains no `body` or `comments` keys anywhere. Raw PR descriptions and comment/review bodies must never reach `data.json` — only derived signals and short quoted checklist labels.
- `test_build.py` also asserts `web/app.js` contains no `innerHTML` and `web/index.html` retains its `Content-Security-Policy`. Build DOM via the `element()` helper and text nodes.
- Four of the six test modules load the real [dashboard.yml](dashboard.yml) and [fixtures/sample_api_data.json](fixtures/sample_api_data.json) at `NOW = 2026-08-03T12:00:00Z` (the same `--now` as the demo build) and assert on named PR numbers and exact lane orderings. So a threshold change in `dashboard.yml`, or a new fixture PR, is expected to break tests in modules that look unrelated to the change; re-derive the expectations rather than loosening the assertions.
- `web/` is vanilla ES modules, no build step, no third-party scripts, fonts, or network calls other than same-origin `data.json`. Files are copied verbatim into `site/`.

## Configuration notes

`dashboard.yml` is the only knob surface. The `editors` list is applied retroactively to the whole 90-day sample, so editing it changes historical response-time and waiting-on-editor metrics. `suggested_next.cycle` may only contain the four non-`direct`, non-`all` lanes, with no duplicates; direct requests are always shown first.

## CI

[.github/workflows/dashboard.yml](.github/workflows/dashboard.yml) runs on push to `main`, once every 24 hours, or manual dispatch. It runs the test suite before building, uses only the job `GITHUB_TOKEN` (`contents: read` for build; `pages: write` + `id-token: write` isolated to the deploy job), and pins actions to full commit SHAs. Never add `pull_request_target` or anything that would execute code from a `whatwg/html` PR branch.
