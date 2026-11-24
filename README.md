# WHATWG HTML editor dashboard

A static dashboard for working through `whatwg/html` PRs and issues, and for seeing whether review keeps up. Rebuilt daily from public GitHub data and deployed to GitHub Pages. No server, no LLM: every classification is a rule, and each card shows its evidence. Personal state stays in the browser ([PRIVACY.md](PRIVACY.md)).

Unofficial: not a WHATWG product, and its rules and targets aren't WHATWG policy.

## The queue

Each card leads with **whose move it is**: the editors (named if asked or already in the conversation), the author, implementers, tests, compat analysis, the WHATNOT meeting, a dependency, or ready to land. Labels decide first, then an open change request, a current approval, then who acted last. Conflicts and failing checks never hand a PR back. A helper's reply counts as an editor's; a helper and the author iterate until the helper approves, which hands the PR to the editors.

The **Editor** control shows **All editors** or one person's queue. Read as one person, their own items and ones they answered last are left out of their attention lanes and Suggested next.

Per-person lanes:

- **Active now**: changed in the last 30 days, you're involved, and it's your move. Newest first.
- **Direct requests**: review requests, assignments, and mentions in the last 30 days.
- **Stale mentions**: older mentions.
- **Re-review owed**: changed, or the author replied, since your latest review.
- **Ready to land**: you approved the current head and the checklist is ticked.
- **Routed to you**: unanswered items nobody was asked about, routed to you (below).

Per-item lanes:

- **Reply window open**: contributor items with no reply yet, inside the 7-day target. Closest to the deadline first.
- **First reply overdue**: the same, past the target.
- **Longest waits**: contributor activity nobody has answered since.
- **Ready**: mergeable, no blocker, a small diff and a ticked checklist.
- **Long wait on author**: the author's move for a long time; ping or close.
- **Needs triage**: recent issues without a `topic:` label.
- **All open**.

**Suggested next** shows up to three Reply window items, then Active now, then interleaves the other lanes so the backlog isn't starved. Recency leads because on a decade-old backlog age says little about what's actionable.

Issues follow the same rules. Only issues opened, or last answered by a contributor, within `issues.window_days` are queued, since most are dormant. An issue an open PR fixes appears on that PR's card.

**Routing** sends each unanswered contributor item to one editor or helper (helpers take PRs only), weighing the topics people declare under `knows:` against recent load. An item's topics are its labels and the spec pages its diff touches, mapped to labels by `topics:`. Cards may suggest a `topic:` label or a GitHub team, with a button that copies the `gh` command; nothing is applied automatically. `python dashboard.py expertise --state .state --spec-repo .spec-repo` compares declarations with history.

## Browser-local state

**You are** says who's using this browser; it starts at **Just browsing**. It's separate from the Editor control, so reading a colleague's queue never touches your state.

- Opening an item marks its attention signal seen.
- **Address until changed** hides an item until somebody else changes it.
- Pins, snoozes and settings are local; export and import as JSON.
- An open tab reloads the data when a newer build is deployed.

`localStorage` is per origin, not per path: use a dedicated origin if other pages on it aren't trusted. The values are workflow metadata, not credentials. "Unseen" isn't GitHub's unread state, which is never fetched.

## Health view

Twelve-week trends asking the same two things of PRs and issues: the share answered by an editor or helper within 7 days (and how many by editors), and how many still wait past it. Each card says whether it's on target. Open and closed counts are left to foolip's [spec-stats](https://foolip.github.io/spec-stats/reviews/html/). Below: merges by week per editor, reviews, first replies, and **Who did what** per person.

Weekly counts are reconstructed from the current sample, not stored. The newest week never counts toward a rate, since its items can't have missed the target yet. Wording describes order, not cause: "merged after a review".

## Running locally

Python 3.12+; the only dependency is PyYAML.

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install --requirement requirements.txt

# Offline demo
python dashboard.py build --fixture fixtures/sample_api_data.json --now 2026-08-03T12:00:00Z --output site
python -m http.server --directory site 8000

# Live data. --state: incremental later builds (--full forces a full fetch);
# --spec-repo: clone whatwg/html (~850 MB) for routing by spec page;
# --evaluate: report how the rules did.
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site --state .state --spec-repo .spec-repo --evaluate

python dashboard.py validate
python -m unittest discover -s tests -v
```

## Deploying

In **Settings → Pages**, select **GitHub Actions**, then push to `main` or run the workflow. It runs on push, daily and on demand; uses only the job's `GITHUB_TOKEN` (`contents: read` to build, `pages: write` and `id-token: write` to deploy); pins actions to SHAs; runs the tests first; and never runs `whatwg/html` code. GitHub disables schedules in public repositories after 60 days without activity.

Builds are incremental, from a snapshot in the Actions cache, with a full fetch weekly. GitHub GraphQL timeouts (HTTP 502/504) are retried with backoff and smaller pages for up to an hour, so ordinary incidents are waited out. They aren't a permissions problem; a PAT won't help.

## Configuration

`dashboard.yml` holds the repository; editors, retired editors and helpers (with routing `share` and `knows:` topics); the `topics:` label-to-page map; response targets; the activity window; Ready thresholds; waiting-on labels; sampling sizes; and the Suggested next lead and cycle. The editor list applies to the whole sample, so changing it changes past metrics.

## Limitations

- Long threads are sampled at both ends; figures that need the middle are dropped when it's missing, and coverage is published.
- Inline review replies aren't read.
- Checklists show what the author ticked, not verified tests, implementer interest or consensus.
- Draft PRs are left out; a PR's clock starts when it was last marked ready.
- Only `whatwg/html`; no cross-device state.

## License

MIT.
