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

## Architecture

The pipeline is a strict one-way chain; keep the layer boundaries intact when extending.

1. [config.py](editor_dashboard/config.py) — parses and validates [dashboard.yml](dashboard.yml) into frozen dataclasses. All thresholds, logins, sampling sizes, and lane cycling live here; validation is eager and raises `ValueError` with a field-qualified message. The viewer is always forced into the editors set.
2. [github.py](editor_dashboard/github.py) — `GraphQLClient` (urllib, no HTTP library) plus `fetch_repository_data`, which paginates two queries from [graphql/](graphql/): open PRs via the repository connection, recently closed PRs via `search`. `load_fixture` produces the same `RepositoryData` from JSON, which is how every test and the demo build run.
3. [models.py](editor_dashboard/models.py) — `PullRequestSnapshot.from_graphql` / `Activity.from_graphql` normalize raw GraphQL nodes: logins lowercased, timestamps parsed to UTC, the `timelineFirst`/`timelineLast` aliases merged and deduplicated. Snapshots are frozen and carry their own sampling-completeness flags.
4. [analysis.py](editor_dashboard/analysis.py) — the heart. `analyze_pull_request` produces one `PRAnalysis` per PR: lane membership, `Reason` evidence chips, blockers, and the two fingerprints. `build_lanes` produces the per-lane ordering as lists of `owner/repo#number` keys.
5. [metrics.py](editor_dashboard/metrics.py) — aggregates `PRAnalysis` values into repository-health, flow-window (7/28/90 day), viewer-impact, and sampling-coverage numbers.
6. [build.py](editor_dashboard/build.py) — assembles the `data.json` payload (`schema_version`, lanes, items, metrics, methodology, build metadata), copies `web/*` into the output, writes `.nojekyll`.
7. [web/app.js](web/app.js) — fetches `data.json`, resolves lane key lists against `items`, and layers browser-local state on top.

Lane identifiers (`direct`, `rereview`, `new`, `oldest_wait`, `ready_bounded`, `all`) are a shared vocabulary across `analysis.py`, `build.py` `LANE_DESCRIPTIONS`, `config.py` `_ALLOWED_SUGGESTED_LANES`, and `web/app.js` `LANE_ORDER`. Adding or renaming one means touching all four.

### The two fingerprints

`analysis.py` computes both, and they drive distinct browser behaviours — do not conflate them:

- `content_fingerprint` hashes the PR's public content (updated time, head OID, body hash, labels, assignees, review requests, decision, status, mergeability, sampled activity ids/timestamps, thread counts). "Address until changed" stores it; the PR reappears when it changes.
- `attention_fingerprint` hashes only the direct-request and re-review reason codes/timestamps. "Seen" stores it; a new attention signal makes the item unseen again.

Changing what goes into either hash silently invalidates users' stored state, so treat the payloads as a compatibility surface.

### Determinism and sampling honesty

Every classification is rule-based; no LLM, no heuristics that cannot be shown as evidence. Two properties the code deliberately maintains:

- **Deterministic output.** `now` is threaded explicitly through analysis, metrics, and build so a fixture build is byte-reproducible. Sorts always break ties on PR number. Never call `datetime.now()` below `dashboard.py`.
- **Incomplete samples are visible, not silent.** Only the first and last `timeline_each_end` comments/reviews are fetched. `timeline_sample_complete`, `first_editor_response_known`, and `review_threads_sample_complete` gate which metrics are reported; the `coverage` block in `data.json` publishes the shortfall. When adding a metric that needs middle history, gate it the same way rather than assuming completeness.

### GraphQL resilience

GitHub returns HTTP 502/504 for GraphQL request timeouts, and this query is nested-connection heavy. `GraphQLClient.execute` retries with exponential backoff and, on 502/504 only, halves `pageSize` and retains the reduced cap for the rest of the build (`_page_size_cap`). 403 with `X-RateLimit-Remaining: 0` fails fast; secondary-rate-limit 403s get at least a 60 s delay. `fetch_repository_data` also deduplicates PRs that appear twice across pages and drops open-PR nodes that also came back in the closed search, appending a human-readable note to `metadata.warnings` (which is published in `data.json`) rather than failing.

## Constraints enforced by tests

- `test_build.py` asserts the published payload contains no `body` or `comments` keys anywhere. Raw PR descriptions and comment/review bodies must never reach `data.json` — only derived signals and short quoted checklist labels.
- `test_build.py` also asserts `web/app.js` contains no `innerHTML` and `web/index.html` retains its `Content-Security-Policy`. Build DOM via the `element()` helper and text nodes.
- `web/` is vanilla ES modules, no build step, no third-party scripts, fonts, or network calls other than same-origin `data.json`. Files are copied verbatim into `site/`.

## Configuration notes

`dashboard.yml` is the only knob surface. The `editors` list is applied retroactively to the whole 90-day sample, so editing it changes historical response-time and waiting-on-editor metrics. `suggested_next.cycle` may only contain the four non-`direct`, non-`all` lanes, with no duplicates; direct requests are always shown first.

## CI

[.github/workflows/dashboard.yml](.github/workflows/dashboard.yml) runs on push to `main`, once every 24 hours, or manual dispatch. It runs the test suite before building, uses only the job `GITHUB_TOKEN` (`contents: read` for build; `pages: write` + `id-token: write` isolated to the deploy job), and pins actions to full commit SHAs. Never add `pull_request_target` or anything that would execute code from a `whatwg/html` PR branch.
