# WHATWG HTML editor dashboard

A static dashboard for working through `whatwg/html` pull requests and issues, and for seeing whether review is keeping up. It is rebuilt from public GitHub data once a day and deployed to GitHub Pages. There is no server and no LLM: every classification is a deterministic rule, and every card shows the evidence for it. Personal workflow state stays in the browser. See [PRIVACY.md](PRIVACY.md) for the public/private boundary.

## The queue

Every card leads with **whose move it is**: the editors (named when someone was asked or is already in the conversation), the author, implementers, tests, compat analysis, the WHATNOT meeting, a dependency, or "approved, ready to land". Labels decide first, then an unaddressed change request, a current approval, and finally who acted last. A merge conflict or failing checks never hands a PR back to its author. A helper's reply counts as an editor's, and a helper and the author iterate without the editors: until the helper approves, the PR waits on the helper, and the approval hands it to the editors to review and land.

The **Editor** control picks whose queue is on screen: **All editors** (the union) or one person. Read as one person, an item that waits on the editors is still somebody else's move when it is their own, or when they replied after the author last moved, and their attention lanes and Suggested next leave it out.

Lanes that depend on who is asking:

- **Active now**: PRs and issues that changed in the activity window (30 days), that the person is involved in, and that are their move. Newest first.
- **Direct requests**: review requests and assignments they hold, and mentions inside the activity window.
- **Stale mentions**: older mentions, findable without leading the queue.
- **Re-review owed**: the PR changed or the author replied after their latest review.
- **Ready to land**: they approved the current head and nothing in the checklist is unticked.
- **Routed to you**: unanswered contributor items nobody was asked about, routed to one person (below).

Lanes that are properties of the item:

- **Reply window open**: contributor items with no editor response yet, still inside the seven-day target, closest to the deadline first.
- **First reply overdue**: the same, past the target.
- **Longest waits**: contributor activity no editor or helper has answered, longest first.
- **Ready**: mergeable, no blocker, a bounded diff, and a complete description checklist.
- **Long wait on author**: the author has had the move for a long time; candidates to ping or close.
- **Needs triage**: recent issues without a `topic:` label.
- **All open**.

**Suggested next** shows up to three Reply window PRs, then all of Active now, then interleaves the other lanes so the backlog isn't starved by new work. Recency leads because on a decade-old backlog age is a poor proxy for what is actionable. The reply-window lead is the exception, bounded, because only there can a reply still meet the target.

Issues use the same rules where the meaning carries over. Only issues opened, or last answered by a contributor, inside `issues.window_days` are queued, since most open issues are dormant. An issue an open PR fixes is shown on that PR's card.

**Routing.** Unanswered contributor items are routed to one editor or helper (helpers take PRs only), weighing the topics each person declares under `knows:` in `dashboard.yml` against recent load. An item's topics are its labels and the spec pages its diff touches, mapped to labels through `topics:`. Cards can also suggest a `topic:` label or a GitHub team to ask, with a button that copies the `gh` command; nothing is applied automatically. `python dashboard.py expertise --state .state --spec-repo .spec-repo` compares the declarations with history, to help edit them.

## Browser-local state

**You are** names who is using this browser; it starts at **Just browsing**, since most readers of the public site are not editors. It is separate from the Editor control, so reading a colleague's queue never touches your own state.

- Opening an item marks its current attention signal as seen.
- **Address until changed** hides an item until somebody else changes it. The fingerprint leaves your own comments, reviews and cleared review requests out, so replying and then addressing doesn't bring it straight back.
- Pins, snoozes and control settings are local too. State can be exported and imported as JSON.
- A tab left open reloads the data in place once a newer build is deployed.

`localStorage` is scoped to the origin, not the path. The key is namespaced by path, but other pages on the same origin could read it, so use a dedicated origin if they aren't equally trusted. The values are workflow metadata, not credentials. "Unseen" is not GitHub's private notification state, which the dashboard never fetches.

## Health view

Four trends over twelve weeks: open pull requests, contributors waiting past the first-response target, and the shares of contributor PRs and new issues answered inside it, each with its good direction and a verdict when it is off target or getting worse. Then how the work divides: merges by week split by editor, reviews, first replies, and a **Who did what** table per editor and helper.

The weekly backlog is reconstructed from the current sample rather than stored. The newest week never counts toward the response rate, because its PRs can't have missed the target yet. Wording describes event order, not causation: "merged after a review", never "because of".

## Running locally

Python 3.12 or newer; the only dependency is PyYAML.

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install --requirement requirements.txt

# Offline demo
python dashboard.py build --fixture fixtures/sample_api_data.json --now 2026-08-03T12:00:00Z --output site
python -m http.server --directory site 8000

# Live data. --state makes later builds incremental (--full forces a full fetch);
# --spec-repo clones whatwg/html (about 850 MB) for routing by spec page;
# --evaluate reports how the rules did.
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site --state .state --spec-repo .spec-repo --evaluate

python dashboard.py validate
python -m unittest discover -s tests -v
```

## Deploying

In **Settings → Pages**, select **GitHub Actions**, then push to `main` or run the workflow. It runs on push to `main`, daily, and on manual dispatch, uses only the job's `GITHUB_TOKEN` (`contents: read` to build; `pages: write` and `id-token: write` only to deploy), pins actions to commit SHAs, runs the tests first, and never runs code from `whatwg/html` branches. GitHub disables schedules in public repositories after 60 days without activity, so watch for that if the source repository is public.

The build is incremental: it restores the previous snapshot from the Actions cache, refetches what changed, and does a full fetch weekly. GitHub answers GraphQL timeouts with HTTP 502 or 504; the client backs off, shrinks the page size, and keeps trying for up to an hour in total, so an ordinary GitHub incident is waited out. A 502 is not a permissions problem, so a PAT won't help.

## Configuration

`dashboard.yml` holds the repository, the editors, retired editors and helpers (with their `share` of routed work and `knows:` topics), the label-to-page `topics:` map, response targets, the activity window, the Ready thresholds, waiting-on labels, sampling sizes, and the Suggested next lead and cycle. The editor list applies retroactively to the whole sample, so changing it changes historical metrics.

## Limitations

- Long discussions are sampled at the beginning and end; metrics that need the middle are reported only when the sample is complete, and coverage is published.
- Inline review-thread replies aren't read for mentions or responses.
- Description checklists are shown as what the author ticked, not as tests, implementer interest or consensus being verified.
- Draft PRs are left out; a PR's age starts when it was last marked ready for review.
- Only `whatwg/html`, and no cross-device state.

## License

MIT.
