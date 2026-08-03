const STATE_VERSION = 1;
const STATE_NAMESPACE = location.pathname.replace(/\/+$/, "") || "/";
const STATE_KEY = `whatwg-editor-dashboard:v${STATE_VERSION}:${STATE_NAMESPACE}`;
const LANE_ORDER = ["direct", "rereview", "new", "oldest_wait", "ready_bounded", "all"];
const SUGGESTED_LIMIT_AFTER_DIRECT = 12;
const SORT_ORDERS = new Set(["queue", "checklist", "unchecked", "wait", "updated", "created"]);

let dashboard = null;
let itemsByKey = new Map();
let localState = loadLocalState();
let activeLane = "direct";
let searchQuery = "";
let storageWarningShown = false;

function element(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(options)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "className") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name === "dataset") Object.assign(node.dataset, value);
    else if (name === "attrs") {
      for (const [attribute, attributeValue] of Object.entries(value)) {
        node.setAttribute(attribute, String(attributeValue));
      }
    } else if (name.startsWith("on") && typeof value === "function") {
      node.addEventListener(name.slice(2).toLowerCase(), value);
    } else if (name in node) {
      node[name] = value;
    } else {
      node.setAttribute(name, String(value));
    }
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function loadLocalState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STATE_KEY) || "null");
    if (!parsed || parsed.version !== STATE_VERSION || typeof parsed.items !== "object") {
      throw new Error("No compatible local state");
    }
    return {
      version: STATE_VERSION,
      items: parsed.items || {},
      settings: {
        showAddressed: Boolean(parsed.settings?.showAddressed),
        showSnoozed: Boolean(parsed.settings?.showSnoozed),
        sortOrder: SORT_ORDERS.has(parsed.settings?.sortOrder) ? parsed.settings.sortOrder : "queue",
      },
    };
  } catch {
    return {
      version: STATE_VERSION,
      items: {},
      settings: {showAddressed: false, showSnoozed: false, sortOrder: "queue"},
    };
  }
}

function saveLocalState() {
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(localState));
    return true;
  } catch {
    if (!storageWarningShown && document.readyState !== "loading") {
      storageWarningShown = true;
      queueMicrotask(() => toast("Browser storage is unavailable; changes will last only until this page closes."));
    }
    return false;
  }
}

function clearStoredState() {
  try {
    localStorage.removeItem(STATE_KEY);
  } catch {
    // In-memory state can still be reset when browser storage is unavailable.
  }
}

function stateFor(key) {
  if (!localState.items[key] || typeof localState.items[key] !== "object") {
    localState.items[key] = {};
  }
  return localState.items[key];
}

function isAddressed(item) {
  return stateFor(item.key).addressedFingerprint === item.fingerprint;
}

function isSnoozed(item) {
  const value = stateFor(item.key).snoozedUntil;
  if (!value) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date > new Date();
}

function isUnseen(item) {
  if (!item.has_attention_signal) return false;
  return stateFor(item.key).seenAttentionFingerprint !== item.attention_fingerprint;
}

function isPinned(item) {
  return Boolean(stateFor(item.key).pinned);
}

function markSeen(item) {
  const state = stateFor(item.key);
  state.seenAttentionFingerprint = item.attention_fingerprint;
  state.openedAt = new Date().toISOString();
  saveLocalState();
}

function setAddressed(item, addressed) {
  const state = stateFor(item.key);
  state.addressedFingerprint = addressed ? item.fingerprint : null;
  state.seenAttentionFingerprint = item.attention_fingerprint;
  if (addressed) state.snoozedUntil = null;
  saveLocalState();
}

function setPinned(item, pinned) {
  stateFor(item.key).pinned = pinned;
  saveLocalState();
}

function setSnooze(item, days) {
  const state = stateFor(item.key);
  if (!days) {
    state.snoozedUntil = null;
  } else {
    const date = new Date();
    date.setDate(date.getDate() + Number(days));
    state.snoozedUntil = date.toISOString();
    state.seenAttentionFingerprint = item.attention_fingerprint;
  }
  saveLocalState();
}

function localDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
}

function relativeTime(value) {
  if (!value) return "unknown time";
  const date = new Date(value);
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 90) return "just now";
  const hours = seconds / 3600;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = hours / 24;
  if (days < 60) return `${Math.round(days)}d ago`;
  const months = days / 30.4375;
  if (months < 24) return `${Math.round(months)}mo ago`;
  return `${(days / 365.25).toFixed(1)}y ago`;
}

function duration(hours) {
  if (hours === null || hours === undefined) return "—";
  if (hours < 24) return `${Math.round(hours)}h`;
  const days = hours / 24;
  if (days < 14) return `${days.toFixed(days < 3 ? 1 : 0)}d`;
  if (days < 90) return `${Math.round(days / 7)}w`;
  if (days < 730) return `${Math.round(days / 30.4375)}mo`;
  return `${(days / 365.25).toFixed(1)}y`;
}

function numberFormat(value) {
  return new Intl.NumberFormat().format(value ?? 0);
}

function toast(message) {
  const region = document.querySelector("#toast-region");
  const node = element("div", {className: "toast", text: message, attrs: {role: "status"}});
  region.append(node);
  setTimeout(() => node.remove(), 3500);
}

function itemMatchesSearch(item) {
  if (!searchQuery) return true;
  const labels = (item.labels || []).join(" ");
  const haystack = `${item.number} ${item.title} ${item.author || ""} ${labels}`.toLowerCase();
  return haystack.includes(searchQuery);
}

function itemVisible(item) {
  if (!itemMatchesSearch(item)) return false;
  if (!localState.settings.showAddressed && isAddressed(item)) return false;
  if (!localState.settings.showSnoozed && isSnoozed(item)) return false;
  return true;
}

function orderedVisibleItems(keys, sortOrder = "queue") {
  const values = keys
    .map((key, index) => ({item: itemsByKey.get(key), index}))
    .filter(entry => entry.item)
    .filter(entry => itemVisible(entry.item));

  const compareNullableNumber = (a, b, direction = 1) => {
    const aMissing = a === null || a === undefined || !Number.isFinite(a);
    const bMissing = b === null || b === undefined || !Number.isFinite(b);
    if (aMissing !== bMissing) return aMissing ? 1 : -1;
    if (aMissing) return 0;
    return (a - b) * direction;
  };

  values.sort((aEntry, bEntry) => {
    const a = aEntry.item;
    const b = bEntry.item;
    const pinned = Number(isPinned(b)) - Number(isPinned(a));
    if (pinned) return pinned;

    let result = 0;
    if (sortOrder === "checklist") {
      const aHas = Boolean(a.checklist.total);
      const bHas = Boolean(b.checklist.total);
      if (aHas !== bHas) result = aHas ? -1 : 1;
      else if (aHas) {
        result = compareNullableNumber(a.checklist.ratio, b.checklist.ratio, -1)
          || (b.checklist.checked - a.checklist.checked)
          || ((a.checklist.total - a.checklist.checked) - (b.checklist.total - b.checklist.checked));
      }
    } else if (sortOrder === "unchecked") {
      const aHas = Boolean(a.checklist.total);
      const bHas = Boolean(b.checklist.total);
      if (aHas !== bHas) result = aHas ? -1 : 1;
      else if (aHas) {
        result = ((a.checklist.total - a.checklist.checked) - (b.checklist.total - b.checklist.checked))
          || compareNullableNumber(a.checklist.ratio, b.checklist.ratio, -1);
      }
    } else if (sortOrder === "wait") {
      result = compareNullableNumber(a.current_wait_hours, b.current_wait_hours, -1);
    } else if (sortOrder === "updated") {
      result = new Date(b.updated_at) - new Date(a.updated_at);
    } else if (sortOrder === "created") {
      result = new Date(a.created_at) - new Date(b.created_at);
    }

    return result || (aEntry.index - bEntry.index);
  });
  return values.map(entry => entry.item);
}

function chip(reason) {
  const text = reason.detail ? `${reason.label} · ${reason.detail}` : reason.label;
  const node = element("span", {className: `chip ${reason.tone || "neutral"}`, text});
  if (reason.timestamp) node.title = localDate(reason.timestamp);
  return node;
}

function statusLabel(item) {
  if (item.is_draft) return "Draft";
  if (item.status_state === "SUCCESS") return "Checks pass";
  if (item.status_state === "FAILURE" || item.status_state === "ERROR") return "Checks fail";
  if (item.status_state === "PENDING") return "Checks pending";
  return "Checks not reported";
}

function createDetails(item) {
  const details = element("details", {className: "card-details"});
  details.append(element("summary", {text: "Evidence and limitations"}));
  const content = element("div", {className: "card-details-content"});

  if (item.reasons.length) {
    content.append(element("h4", {text: "Why it appears"}));
    const list = element("ul");
    for (const reason of item.reasons) {
      const detail = reason.detail ? ` — ${reason.detail}` : "";
      list.append(element("li", {text: `${reason.label}${detail}`}));
    }
    content.append(list);
  }

  if (item.blockers.length) {
    content.append(element("h4", {text: "Detected blockers or uncertainty"}));
    const list = element("ul");
    for (const blocker of item.blockers) {
      const detail = blocker.detail ? ` — ${blocker.detail}` : "";
      list.append(element("li", {text: `${blocker.label}${detail}`}));
    }
    content.append(list);
  }

  if (item.checklist.total) {
    content.append(element("h4", {text: "Description checklist"}));
    const list = element("ul");
    for (const checklistItem of item.checklist.items) {
      const prefix = checklistItem.checked ? "Checked: " : "Unchecked: ";
      list.append(element("li", {text: `${prefix}${checklistItem.label || "Untitled task"}`}));
    }
    content.append(list);
  }

  if (!item.timeline_sample_complete) {
    content.append(element("p", {text: "The middle of a long comment/review timeline was not sampled; some discussion signals may be absent."}));
  }
  if (!item.review_threads_sample_complete) {
    content.append(element("p", {text: "Not every review thread was sampled, so unresolved-thread status is incomplete."}));
  }
  details.append(content);
  return details;
}

function createPRCard(item) {
  const addressed = isAddressed(item);
  const unseen = isUnseen(item);
  const pinned = isPinned(item);
  const cardClasses = ["pr-card"];
  if (addressed) cardClasses.push("addressed");
  if (unseen) cardClasses.push("unseen");
  if (pinned) cardClasses.push("pinned");
  const card = element("article", {className: cardClasses.join(" "), dataset: {key: item.key}});

  const heading = element("div", {className: "pr-heading"});
  const headingRow = element("div", {className: "pr-heading-row"});
  if (unseen) headingRow.append(element("span", {className: "unseen-dot", attrs: {title: "Locally unseen public attention signal", "aria-label": "Unseen"}}));
  const title = element("h3", {className: "pr-title"});
  const markOpened = () => {
    markSeen(item);
    queueMicrotask(renderQueues);
  };
  const markMiddleOpened = event => {
    if (event.button === 1) markOpened();
  };
  const titleLink = element("a", {
    href: item.url,
    target: "_blank",
    rel: "noopener",
    onClick: markOpened,
    onAuxClick: markMiddleOpened,
  }, [
    element("span", {className: "pr-number", text: `#${item.number} `}),
    item.title,
  ]);
  title.append(titleLink);
  headingRow.append(title);
  heading.append(headingRow);

  const meta = element("p", {className: "pr-meta"});
  meta.append(element("span", {text: `by @${item.author || "ghost"}`}));
  const opened = element("span", {text: `opened ${relativeTime(item.created_at)}`});
  opened.title = localDate(item.created_at);
  meta.append(opened);
  if (item.current_wait_hours !== null) {
    meta.append(element("span", {}, ["editor wait ", element("strong", {text: duration(item.current_wait_hours)})]));
  }
  if (item.first_time_contributor) meta.append(element("span", {text: "first-time contributor"}));
  if (item.is_draft) meta.append(element("span", {text: "draft"}));
  heading.append(meta);

  const reasonList = element("div", {className: "reason-list"});
  const visibleReasons = item.reasons.slice(0, 6);
  for (const reason of visibleReasons) reasonList.append(chip(reason));
  if (item.reasons.length > visibleReasons.length) {
    reasonList.append(element("span", {className: "chip muted", text: `+${item.reasons.length - visibleReasons.length} more`}));
  }
  for (const blocker of item.blockers.slice(0, 2)) reasonList.append(chip(blocker));
  heading.append(reasonList);

  if (item.labels.length) {
    const labels = element("div", {className: "label-list"});
    for (const label of item.labels.slice(0, 5)) labels.append(element("span", {className: "chip label-chip", text: label}));
    if (item.labels.length > 5) labels.append(element("span", {className: "chip label-chip", text: `+${item.labels.length - 5}`}));
    heading.append(labels);
  }
  card.append(heading);

  const side = element("div", {className: "pr-side"});
  if (item.checklist.total) {
    side.append(element("div", {className: "pr-summary-stat"}, [
      element("span", {text: "Description checklist"}),
      element("strong", {text: `${item.checklist.checked}/${item.checklist.total}`}),
    ]));
    const track = element("div", {className: "progress-track", attrs: {role: "progressbar", "aria-valuemin": 0, "aria-valuemax": item.checklist.total, "aria-valuenow": item.checklist.checked}});
    const progress = element("div", {className: "progress-value"});
    progress.style.width = `${Math.round((item.checklist.ratio || 0) * 100)}%`;
    track.append(progress);
    side.append(track);
  } else {
    side.append(element("div", {className: "pr-summary-stat"}, [
      element("span", {text: "Description checklist"}),
      element("strong", {text: "None"}),
    ]));
  }
  side.append(element("div", {className: "pr-summary-stat"}, [
    element("span", {text: "Diff"}),
    element("strong", {text: `${numberFormat(item.changed_lines)} lines · ${numberFormat(item.changed_files)} files`} ),
  ]));
  side.append(element("div", {className: "pr-summary-stat"}, [
    element("span", {text: "Status"}),
    element("strong", {text: statusLabel(item)}),
  ]));
  side.append(element("div", {className: "pr-summary-stat"}, [
    element("span", {text: "Updated"}),
    element("strong", {text: relativeTime(item.updated_at), title: localDate(item.updated_at)}),
  ]));
  card.append(side);

  const actions = element("div", {className: "pr-actions"});
  actions.append(element("a", {
    className: "action-link",
    href: item.url,
    target: "_blank",
    rel: "noopener",
    text: "Open on GitHub ↗",
    onClick: markOpened,
    onAuxClick: markMiddleOpened,
  }));

  actions.append(element("button", {
    type: "button",
    className: `action-button${addressed ? " active" : ""}`,
    text: addressed ? "Addressed until changed ✓" : "Address until changed",
    onClick: () => {
      setAddressed(item, !isAddressed(item));
      renderQueues();
      toast(isAddressed(item) ? "Hidden until the PR's public fingerprint changes." : "PR returned to active queues.");
    },
  }));

  actions.append(element("button", {
    type: "button",
    className: `action-button${pinned ? " active" : ""}`,
    text: pinned ? "Pinned ★" : "Pin ☆",
    onClick: () => {
      setPinned(item, !isPinned(item));
      renderQueues();
    },
  }));

  const snooze = element("select", {className: "snooze-select", attrs: {"aria-label": `Snooze PR #${item.number}`}});
  const snoozed = isSnoozed(item);
  const options = [
    ["", snoozed ? `Snoozed until ${new Date(stateFor(item.key).snoozedUntil).toLocaleDateString()}` : "Snooze…"],
    ["1", "Snooze 1 day"],
    ["7", "Snooze 7 days"],
    ["30", "Snooze 30 days"],
  ];
  if (snoozed) options.splice(1, 0, ["clear", "Clear snooze"]);
  for (const [value, label] of options) snooze.append(element("option", {value, text: label}));
  snooze.addEventListener("change", () => {
    if (snooze.value === "clear") setSnooze(item, null);
    else if (snooze.value) setSnooze(item, Number(snooze.value));
    renderQueues();
  });
  actions.append(snooze);
  actions.append(createDetails(item));
  card.append(actions);
  return card;
}

function emptyState(message) {
  return element("div", {className: "empty-state", text: message});
}

function renderList(container, items, emptyMessage) {
  const children = items.length ? items.map(createPRCard) : [emptyState(emptyMessage)];
  container.replaceChildren(...children);
}

function suggestedItems() {
  const result = [];
  const seen = new Set();
  const add = item => {
    if (!item || seen.has(item.key) || !itemVisible(item)) return false;
    seen.add(item.key);
    result.push(item);
    return true;
  };

  for (const item of orderedVisibleItems(dashboard.lanes.direct || [])) add(item);

  const cycle = dashboard.suggested_next.cycle || [];
  const laneItems = new Map(cycle.map(lane => [lane, orderedVisibleItems(dashboard.lanes[lane] || [])]));
  const indexes = new Map(cycle.map(lane => [lane, 0]));
  let addedAfterDirect = 0;
  let madeProgress = true;
  while (madeProgress && addedAfterDirect < SUGGESTED_LIMIT_AFTER_DIRECT) {
    madeProgress = false;
    for (const lane of cycle) {
      const values = laneItems.get(lane) || [];
      let index = indexes.get(lane) || 0;
      while (index < values.length && seen.has(values[index].key)) index += 1;
      indexes.set(lane, index + 1);
      if (index < values.length && add(values[index])) {
        madeProgress = true;
        addedAfterDirect += 1;
        if (addedAfterDirect >= SUGGESTED_LIMIT_AFTER_DIRECT) break;
      }
    }
  }
  return result;
}

function renderLaneTabs() {
  const container = document.querySelector("#lane-tabs");
  const tabs = [];
  for (const lane of LANE_ORDER) {
    const descriptor = dashboard.lane_descriptions[lane];
    if (!descriptor) continue;
    const visibleCount = orderedVisibleItems(dashboard.lanes[lane] || []).length;
    const button = element("button", {
      type: "button",
      className: "lane-tab",
      attrs: {
        role: "tab",
        "aria-selected": lane === activeLane,
        "aria-controls": "lane-list",
      },
      onClick: () => {
        activeLane = lane;
        renderQueues();
      },
    }, [
      descriptor.title,
      element("span", {className: "lane-tab-count", text: visibleCount}),
    ]);
    tabs.push(button);
  }
  container.replaceChildren(...tabs);
}

function renderLocalSummary() {
  const allItems = [...itemsByKey.values()];
  const addressed = allItems.filter(isAddressed).length;
  const snoozed = allItems.filter(isSnoozed).length;
  const pinned = allItems.filter(isPinned).length;
  const unseen = allItems.filter(isUnseen).length;
  document.querySelector("#local-state-summary").textContent = `${unseen} unseen · ${addressed} addressed · ${snoozed} snoozed · ${pinned} pinned in this browser`;
}

function renderQueues() {
  if (!dashboard) return;
  renderLocalSummary();
  renderLaneTabs();

  const direct = orderedVisibleItems(dashboard.lanes.direct || []);
  const summary = document.querySelector("#attention-summary");
  summary.querySelector(".hero-stat-value").textContent = numberFormat(direct.length);
  summary.querySelector(".hero-stat-label").textContent = direct.length === 1 ? "active direct request" : "active direct requests";

  const suggested = suggestedItems();
  document.querySelector("#suggested-count").textContent = numberFormat(suggested.length);
  renderList(
    document.querySelector("#suggested-list"),
    suggested,
    searchQuery ? "No suggested items match this search and the current local filters." : "No active suggested items. Addressed or snoozed items remain local to this browser."
  );

  const descriptor = dashboard.lane_descriptions[activeLane];
  document.querySelector("#lane-description").textContent = descriptor?.description || "";
  const laneItems = orderedVisibleItems(dashboard.lanes[activeLane] || [], localState.settings.sortOrder);
  renderList(
    document.querySelector("#lane-list"),
    laneItems,
    searchQuery ? "No items in this queue match your search and local filters." : "No active items in this queue."
  );
}

function metricCard(label, value, note) {
  return element("article", {className: "metric-card"}, [
    element("span", {className: "metric-label", text: label}),
    element("strong", {className: "metric-value", text: numberFormat(value)}),
    element("span", {className: "metric-note", text: note}),
  ]);
}

function renderBarChart(selector, entries) {
  const container = document.querySelector(selector);
  const max = Math.max(1, ...entries.map(entry => entry.count));
  const rows = entries.map(entry => {
    const track = element("div", {className: "bar-track"});
    const value = element("div", {className: "bar-value"});
    value.style.width = `${Math.round((entry.count / max) * 100)}%`;
    track.append(value);
    return element("div", {className: "bar-row"}, [
      element("span", {className: "bar-label", text: entry.label}),
      track,
      element("span", {className: "bar-count", text: numberFormat(entry.count)}),
    ]);
  });
  container.replaceChildren(...rows);
}

function createTableCell(tag, value, className) {
  return element(tag, {text: value, className: className || ""});
}

function renderHealth() {
  const metrics = dashboard.metrics;
  const current = metrics.repository.current;
  document.querySelector("#health-date").textContent = `Generated ${localDate(metrics.generated_at)}`;

  document.querySelector("#motivation-list").replaceChildren(
    ...metrics.motivation.map(value => element("li", {text: value}))
  );

  document.querySelector("#current-metrics").replaceChildren(
    metricCard("Open pull requests", current.open_prs, `${current.ready_for_review_prs} ready for review · ${current.draft_prs} drafts`),
    metricCard("Waiting on editor", current.waiting_on_editor, `${current.over_response_target} over the 7-day response target`),
    metricCard("Direct or re-review attention", current.direct_requests + current.rereview_owed, `${current.direct_requests} direct · ${current.rereview_owed} changed since review`),
    metricCard("Ready and bounded", current.ready_and_bounded, "Deterministic quick-win candidates, not merge recommendations")
  );

  renderBarChart("#wait-chart", metrics.repository.wait_distribution);
  renderBarChart("#reason-chart", metrics.repository.waiting_reasons);

  const flowBody = document.querySelector("#flow-table tbody");
  const flowRows = [7, 28, 90].map(days => {
    const windowMetrics = metrics.repository.windows[String(days)];
    const response = windowMetrics.first_editor_response;
    const netClass = windowMetrics.net_backlog_change < 0 ? "positive-number" : windowMetrics.net_backlog_change > 0 ? "negative-number" : "";
    const netText = windowMetrics.net_backlog_change > 0 ? `+${windowMetrics.net_backlog_change}` : String(windowMetrics.net_backlog_change);
    return element("tr", {}, [
      createTableCell("th", `${days} days`),
      createTableCell("td", numberFormat(windowMetrics.opened)),
      createTableCell("td", numberFormat(windowMetrics.closed)),
      createTableCell("td", numberFormat(windowMetrics.merged)),
      createTableCell("td", netText, netClass),
      createTableCell("td", duration(response.median_hours)),
      createTableCell("td", duration(response.p90_hours)),
      createTableCell("td", response.responded ? `${response.within_target}/${response.responded}` : "—"),
    ]);
  });
  flowBody.replaceChildren(...flowRows);

  const viewerWindows = metrics.viewer.windows;
  const impactRows = [
    ["Reviews submitted", "reviews_submitted", numberFormat],
    ["PRs merged after review", "prs_merged_after_review", numberFormat],
    ["First editor responses", "first_editor_responses", numberFormat],
    ["Unique PR authors engaged with", "unique_pr_authors_engaged_with", numberFormat],
    ["PRs with author activity after review", "prs_with_author_activity_after_review", numberFormat],
    ["Long sampled waits addressed", "long_waits_addressed", numberFormat],
    ["Sampled contributor-wait days ended", "sampled_contributor_waiting_days_ended", numberFormat],
    ["Median sampled response", "median_sampled_response_hours", duration],
    ["Authored PRs merged", "authored_prs_merged", numberFormat],
  ].map(([label, key, formatter]) => element("tr", {}, [
    createTableCell("th", label),
    createTableCell("td", formatter(viewerWindows["7"][key])),
    createTableCell("td", formatter(viewerWindows["28"][key])),
    createTableCell("td", formatter(viewerWindows["90"][key])),
  ]));
  document.querySelector("#impact-table tbody").replaceChildren(...impactRows);

  const checklist = metrics.repository.checklist;
  document.querySelector("#checklist-metrics").replaceChildren(
    compactStat(checklist.complete, "complete"),
    compactStat(checklist.partial, "partly checked"),
    compactStat(checklist.none_checked, "none checked"),
    compactStat(checklist.without_checklist, "no task list"),
    compactStat(checklist.average_percent === null ? "—" : `${checklist.average_percent}%`, "average completion"),
  );

  const coverage = metrics.coverage;
  document.querySelector("#coverage-metrics").replaceChildren(
    compactStat(`${coverage.open_timeline_complete}/${coverage.open_timeline_total}`, "complete open timelines"),
    compactStat(`${coverage.closed_timeline_complete}/${coverage.closed_timeline_total}`, "complete closed timelines"),
    compactStat(`${coverage.open_review_threads_complete}/${coverage.open_review_threads_total}`, "complete review-thread samples"),
    compactStat(coverage.viewer_review_connections_truncated, `truncated @${dashboard.viewer.login} review histories`),
    compactStat(`${coverage.history_days}d`, "historical window"),
  );
}

function compactStat(value, label) {
  return element("div", {className: "compact-stat"}, [
    element("strong", {text: value}),
    element("span", {text: label}),
  ]);
}

function renderMethodology() {
  const methodology = dashboard.methodology;
  const content = document.querySelector("#methodology-content");

  const principles = element("article", {className: "methodology-card"}, [
    element("h2", {text: "Principles"}),
    element("ul", {}, methodology.principles.map(value => element("li", {text: value}))),
  ]);

  const limitations = element("article", {className: "methodology-card"}, [
    element("h2", {text: "Known limitations"}),
    element("ul", {}, methodology.known_limitations.map(value => element("li", {text: value}))),
  ]);

  const sampling = element("article", {className: "methodology-card wide"}, [
    element("h2", {text: "Sampling and API economy"}),
  ]);
  const samplingList = element("ul");
  for (const value of Object.values(methodology.sampling)) samplingList.append(element("li", {text: value}));
  sampling.append(samplingList);

  const rules = element("article", {className: "methodology-card wide"}, [
    element("h2", {text: "Queue definitions"}),
  ]);
  const ruleGrid = element("div", {className: "rule-grid"});
  for (const lane of LANE_ORDER.filter(value => value !== "all")) {
    const descriptor = dashboard.lane_descriptions[lane];
    ruleGrid.append(element("div", {className: "rule-item"}, [
      element("strong", {text: descriptor.title}),
      element("span", {text: descriptor.description}),
    ]));
  }
  rules.append(ruleGrid);

  const privacy = element("article", {className: "methodology-card wide"}, [
    element("h2", {text: "Public/private boundary"}),
    element("p", {text: "The scheduled build fetches public repository data only. The deployed data.json contains derived public signals and metrics, never GitHub notification state or browser-local workflow choices."}),
    element("p", {text: "The browser stores seen attention fingerprints, addressed content fingerprints, pins, snooze deadlines, and opened timestamps in localStorage. Export and import are manual; no state is sent to a server."}),
  ]);

  content.replaceChildren(principles, limitations, sampling, rules, privacy);
}

function renderBuildMetadata() {
  const generated = new Date(dashboard.generated_at);
  const indicator = document.querySelector("#build-indicator");
  indicator.textContent = `Updated ${relativeTime(dashboard.generated_at)}`;
  indicator.title = `Generated ${localDate(dashboard.generated_at)} · source: ${dashboard.build.source}`;
  document.querySelector("#repository-label").textContent = dashboard.repository.slug;
  document.querySelector("#health-intro").textContent = `Track flow, contributor wait, backlog shape, and public @${dashboard.viewer.login} activity. Metrics describe observed sequence, not causation.`;
  document.querySelector("#impact-eyebrow").textContent = `Public activity by @${dashboard.viewer.login}`;
  document.querySelector("#impact-caption").textContent = `Public @${dashboard.viewer.login} activity in rolling windows`;
  document.querySelector("#source-repository-link").href = dashboard.repository.url;
  document.querySelector("#source-repository-link").textContent = `Open ${dashboard.repository.slug} pull requests`;
  document.querySelector("#footer-copy").textContent = `Built ${generated.toLocaleString()} from public GitHub data · no LLM`;
}

function showView() {
  const requested = location.hash.replace(/^#/, "") || "queue";
  const view = ["queue", "health", "methodology"].includes(requested) ? requested : "queue";
  for (const section of document.querySelectorAll("[data-view]")) section.hidden = section.dataset.view !== view;
  for (const link of document.querySelectorAll("[data-view-link]")) {
    if (link.dataset.viewLink === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function exportState() {
  const payload = {
    exportedAt: new Date().toISOString(),
    dashboard: dashboard?.repository?.slug || null,
    ...localState,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = element("a", {href: url, download: `html-editor-dashboard-state-${new Date().toISOString().slice(0, 10)}.json`});
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  toast("Local state exported.");
}

async function importState(file) {
  const parsed = JSON.parse(await file.text());
  if (parsed.version !== STATE_VERSION || !parsed.items || typeof parsed.items !== "object") {
    throw new Error("The file is not a compatible dashboard state export.");
  }
  localState = {
    version: STATE_VERSION,
    items: parsed.items,
    settings: {
      showAddressed: Boolean(parsed.settings?.showAddressed),
      showSnoozed: Boolean(parsed.settings?.showSnoozed),
      sortOrder: SORT_ORDERS.has(parsed.settings?.sortOrder) ? parsed.settings.sortOrder : "queue",
    },
  };
  saveLocalState();
  syncControlState();
  renderQueues();
  toast("Local state imported.");
}

function syncControlState() {
  document.querySelector("#show-addressed").checked = localState.settings.showAddressed;
  document.querySelector("#show-snoozed").checked = localState.settings.showSnoozed;
  document.querySelector("#sort-order").value = localState.settings.sortOrder;
}

function installEventHandlers() {
  window.addEventListener("hashchange", showView);
  document.querySelector("#search-input").addEventListener("input", event => {
    searchQuery = event.target.value.trim().toLowerCase();
    renderQueues();
  });
  document.querySelector("#show-addressed").addEventListener("change", event => {
    localState.settings.showAddressed = event.target.checked;
    saveLocalState();
    renderQueues();
  });
  document.querySelector("#show-snoozed").addEventListener("change", event => {
    localState.settings.showSnoozed = event.target.checked;
    saveLocalState();
    renderQueues();
  });
  document.querySelector("#sort-order").addEventListener("change", event => {
    localState.settings.sortOrder = SORT_ORDERS.has(event.target.value) ? event.target.value : "queue";
    saveLocalState();
    renderQueues();
  });
  document.querySelector("#export-state").addEventListener("click", exportState);
  document.querySelector("#import-state").addEventListener("click", () => document.querySelector("#import-state-file").click());
  document.querySelector("#import-state-file").addEventListener("change", async event => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      await importState(file);
    } catch (error) {
      toast(error.message || "Could not import local state.");
    } finally {
      event.target.value = "";
    }
  });
  document.querySelector("#reset-state").addEventListener("click", () => {
    if (!confirm("Reset all seen, addressed, pinned, snoozed, and opened state in this browser?")) return;
    clearStoredState();
    localState = {
      version: STATE_VERSION,
      items: {},
      settings: {showAddressed: false, showSnoozed: false, sortOrder: "queue"},
    };
    syncControlState();
    renderQueues();
    toast("Local state reset.");
  });
}

async function start() {
  installEventHandlers();
  syncControlState();
  showView();
  try {
    const response = await fetch("data.json", {cache: "no-store"});
    if (!response.ok) throw new Error(`Dashboard data returned HTTP ${response.status}.`);
    dashboard = await response.json();
    itemsByKey = new Map(dashboard.items.map(item => [item.key, item]));
    renderBuildMetadata();
    renderQueues();
    renderHealth();
    renderMethodology();
  } catch (error) {
    document.querySelector("#build-indicator").textContent = "Data unavailable";
    document.querySelector("#suggested-list").replaceChildren(emptyState(error.message || "Could not load dashboard data."));
    document.querySelector("#lane-list").replaceChildren(emptyState("The dashboard data could not be loaded."));
    toast(error.message || "Could not load dashboard data.");
  }
}

start();
