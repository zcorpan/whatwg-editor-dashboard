const STATE_VERSION = 1;
const STATE_NAMESPACE = location.pathname.replace(/\/+$/, "") || "/";
const STATE_KEY = `whatwg-editor-dashboard:v${STATE_VERSION}:${STATE_NAMESPACE}`;
const LANE_ORDER = ["reply_window", "active", "direct", "rereview", "overdue", "oldest_wait", "ready_bounded", "stale_direct", "all"];
const SUGGESTED_LIMIT_AFTER_ACTIVE = 12;
const SORT_ORDERS = new Set(["queue", "checklist", "unchecked", "wait", "updated", "created"]);

const DATA_URL = "data.json";
// The scheduled build runs once every 24 hours, so data younger than that cannot have a successor yet.
const BUILD_INTERVAL_MS = 24 * 60 * 60 * 1000;
// Once the data is old enough for a new build to exist, poll on this cadence until one appears.
const STALE_POLL_INTERVAL_MS = 15 * 60 * 1000;
// Floor between any two network checks, so returning to the tab repeatedly cannot hammer the origin.
const MIN_CHECK_INTERVAL_MS = 60 * 1000;
const CLOCK_TICK_MS = 60 * 1000;

// Collapsible sections, and how each one starts before this browser has an opinion.
const SECTION_DEFAULTS = {"queue-controls": false, "suggested-section": true, "lanes-section": true};
const DEFAULT_SETTINGS = () => ({showAddressed: false, showSnoozed: false, sortOrder: "queue", perspective: null, identity: null, openSections: {}});

let dashboard = null;
let itemsByKey = new Map();
let localState = loadLocalState();
let activeLane = "active";
let searchQuery = "";
let storageWarningShown = false;
let lastCheckAt = 0;
let checkInFlight = false;

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

function readOpenSections(value) {
  const result = {};
  for (const id of Object.keys(SECTION_DEFAULTS)) {
    if (typeof value?.[id] === "boolean") result[id] = value[id];
  }
  return result;
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
        // Validated against the payload's option list at read time, not here: the
        // configured editors can change between builds, and a stored login that is
        // no longer one of them has to fall back rather than empty the queue.
        perspective: typeof parsed.settings?.perspective === "string" ? parsed.settings.perspective : null,
        identity: typeof parsed.settings?.identity === "string" ? parsed.settings.identity : null,
        openSections: readOpenSections(parsed.settings?.openSections),
      },
    };
  } catch {
    return {version: STATE_VERSION, items: {}, settings: DEFAULT_SETTINGS()};
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

// Which editor's fingerprints the browser measures its own state against. Null
// until the user picks one: on a public site most readers are not editors, and
// "addressed until somebody else changes it" has no meaning without a "somebody".
function identityKey() {
  const stored = localState.settings.identity;
  return editorOptions().some(option => option.key === stored) ? stored : null;
}

// The identity's view of one item, which is what seen and addressed are read from,
// whichever perspective's lanes are on screen.
function identityView(item) {
  const identity = identityKey();
  return identity ? item.perspectives?.[identity] || null : null;
}

function isAddressed(item) {
  const fingerprint = identityView(item)?.fingerprint;
  return Boolean(fingerprint) && stateFor(item.key).addressedFingerprint === fingerprint;
}

function isSnoozed(item) {
  const value = stateFor(item.key).snoozedUntil;
  if (!value) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date > new Date();
}

function isUnseen(item) {
  const view = identityView(item);
  if (!view?.has_attention_signal) return false;
  return stateFor(item.key).seenAttentionFingerprint !== view.attention_fingerprint;
}

function isPinned(item) {
  return Boolean(stateFor(item.key).pinned);
}

function markSeen(item) {
  const view = identityView(item);
  const state = stateFor(item.key);
  if (view) state.seenAttentionFingerprint = view.attention_fingerprint;
  state.openedAt = new Date().toISOString();
  saveLocalState();
}

function setAddressed(item, addressed) {
  const view = identityView(item);
  if (!view) return;
  const state = stateFor(item.key);
  state.addressedFingerprint = addressed ? view.fingerprint : null;
  state.seenAttentionFingerprint = view.attention_fingerprint;
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
    const view = identityView(item);
    if (view) state.seenAttentionFingerprint = view.attention_fingerprint;
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

function perspectiveOptions() {
  return dashboard?.perspectives?.options || [];
}

// The perspectives that name one editor, which are the ones that can be an identity.
function editorOptions() {
  return perspectiveOptions().filter(option => option.editor);
}

// Which editor's queue is on screen. An unknown or dropped login falls back to the
// payload's default rather than resolving to an empty queue.
function perspectiveKey() {
  const stored = localState.settings.perspective;
  if (perspectiveOptions().some(option => option.key === stored)) return stored;
  return dashboard?.perspectives?.default || perspectiveOptions()[0]?.key || null;
}

function perspectiveLabel(key = perspectiveKey()) {
  const option = perspectiveOptions().find(value => value.key === key);
  if (!option) return "";
  return option.key === identityKey() ? `${option.label} (you)` : option.label;
}

// Your own queue and the team default are the two unremarkable states; only reading
// somebody else's lanes is worth calling out away from the controls panel.
function browsingSomebodyElse() {
  const current = perspectiveKey();
  return current !== dashboard.perspectives?.default && current !== identityKey();
}

// `active`, `direct`, `stale_direct` and `rereview` depend on which editor is
// asking and come from `perspective_lanes`; the rest are properties of the pull
// request and are published once.
function laneKeys(lane) {
  const lanes = dashboard.perspective_lanes?.[perspectiveKey()] || {};
  return lanes[lane] || dashboard.lanes[lane] || [];
}

// The perspective's own evidence first, then the evidence every perspective shares,
// which is the order the Python side used to emit as one list.
function itemReasons(item) {
  const own = item.perspectives?.[perspectiveKey()]?.reasons;
  return own ? [...own, ...item.reasons] : item.reasons;
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

  const reasons = itemReasons(item);
  if (reasons.length) {
    content.append(element("h4", {text: "Why it appears"}));
    const list = element("ul");
    for (const reason of reasons) {
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
  const reasons = itemReasons(item);
  const visibleReasons = reasons.slice(0, 6);
  for (const reason of visibleReasons) reasonList.append(chip(reason));
  if (reasons.length > visibleReasons.length) {
    reasonList.append(element("span", {className: "chip muted", text: `+${reasons.length - visibleReasons.length} more`}));
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

  if (identityKey()) {
    actions.append(element("button", {
      type: "button",
      className: `action-button${addressed ? " active" : ""}`,
      text: addressed ? "Addressed until changed ✓" : "Address until changed",
      onClick: () => {
        setAddressed(item, !isAddressed(item));
        renderQueues();
        toast(isAddressed(item) ? "Hidden until somebody else changes the PR." : "PR returned to active queues.");
      },
    }));
  }

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
  card.append(actions);
  // The disclosure sits in its own card row: inside the action row, opening it
  // re-centred and re-wrapped the buttons and moved the summary itself.
  card.append(createDetails(item));
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

  // Ahead of even the active lane: these are the only PRs where a first reply can
  // still land inside the response target. Past it the outcome is already fixed, so
  // those stay in the cycle. Bounded so incoming work cannot bury live reviews, and
  // the limit is applied after filtering so addressing one promotes the next.
  const leadLimit = dashboard.suggested_next.first_response_lead || 0;
  for (const item of orderedVisibleItems(laneKeys("reply_window")).slice(0, leadLimit)) add(item);

  for (const item of orderedVisibleItems(laneKeys("active"))) add(item);

  const cycle = dashboard.suggested_next.cycle || [];
  const laneItems = new Map(cycle.map(lane => [lane, orderedVisibleItems(laneKeys(lane))]));
  const indexes = new Map(cycle.map(lane => [lane, 0]));
  let addedAfterActive = 0;
  let madeProgress = true;
  while (madeProgress && addedAfterActive < SUGGESTED_LIMIT_AFTER_ACTIVE) {
    madeProgress = false;
    for (const lane of cycle) {
      const values = laneItems.get(lane) || [];
      let index = indexes.get(lane) || 0;
      while (index < values.length && seen.has(values[index].key)) index += 1;
      indexes.set(lane, index + 1);
      if (index < values.length && add(values[index])) {
        madeProgress = true;
        addedAfterActive += 1;
        if (addedAfterActive >= SUGGESTED_LIMIT_AFTER_ACTIVE) break;
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
    const visibleCount = orderedVisibleItems(laneKeys(lane)).length;
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

// The option lists only change when a build changes the configured editors or when
// the identity moves the "(you)" marker, so they are filled then rather than on
// every re-render, which would replace the children of a select being operated.
function renderPerspectiveOptions() {
  document.querySelector("#editor-perspective").replaceChildren(
    ...perspectiveOptions().map(option => element("option", {value: option.key, text: perspectiveLabel(option.key)})),
  );
  document.querySelector("#editor-identity").replaceChildren(
    element("option", {value: "", text: "Just browsing"}),
    ...editorOptions().map(option => element("option", {value: option.key, text: option.label})),
  );
}

function syncPerspectiveControl() {
  const current = perspectiveKey();
  document.querySelector("#editor-perspective").value = current || "";
  document.querySelector("#editor-identity").value = identityKey() || "";

  // Repeated next to the heading, because the controls panel starts collapsed and a
  // queue showing somebody else's attention lanes must not look like your own.
  const badge = document.querySelector("#suggested-perspective");
  const elsewhere = browsingSomebodyElse();
  badge.textContent = elsewhere ? perspectiveLabel(current) : "";
  badge.hidden = !elsewhere;
}

function renderLocalSummary() {
  const allItems = [...itemsByKey.values()];
  const snoozed = allItems.filter(isSnoozed).length;
  const pinned = allItems.filter(isPinned).length;
  const summary = document.querySelector("#local-state-summary");
  if (!identityKey()) {
    // Seen and addressed both compare against a fingerprint that leaves one
    // editor's own footprint out, so neither can be tracked for nobody.
    summary.textContent = `${snoozed} snoozed · ${pinned} pinned in this browser. Say who you are to track seen and addressed too.`;
    return;
  }
  const addressed = allItems.filter(isAddressed).length;
  const unseen = allItems.filter(isUnseen).length;
  summary.textContent = `${unseen} unseen · ${addressed} addressed · ${snoozed} snoozed · ${pinned} pinned in this browser`;
}

function renderQueues() {
  if (!dashboard) return;
  syncPerspectiveControl();
  renderLocalSummary();
  renderLaneTabs();

  const suggested = suggestedItems();
  document.querySelector("#suggested-count").textContent = numberFormat(suggested.length);
  renderList(
    document.querySelector("#suggested-list"),
    suggested,
    searchQuery ? "No suggested items match this search and the current local filters." : "No active suggested items. Addressed or snoozed items remain local to this browser."
  );

  const descriptor = dashboard.lane_descriptions[activeLane];
  document.querySelector("#lane-description").textContent = descriptor?.description || "";
  const laneItems = orderedVisibleItems(laneKeys(activeLane), localState.settings.sortOrder);
  const forEditor = browsingSomebodyElse() ? ` for ${perspectiveLabel()}` : "";
  renderList(
    document.querySelector("#lane-list"),
    laneItems,
    searchQuery
      ? "No items in this queue match your search and local filters."
      : `No active items in this queue${forEditor}.`
  );
}

const SVG_NS = "http://www.w3.org/2000/svg";
// Charts are drawn at their display size so the label text is not scaled by the
// viewBox: a card chart stays small, the wide one gets more room per column.
const CARD_CHART = {width: 320, height: 132};
const WIDE_CHART = {width: 660, height: 200};
const CHART_MARGIN = {top: 12, right: 14, bottom: 22, left: 40};
const NICE_STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];

function svgElement(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(name, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function niceStep(span) {
  for (const step of NICE_STEPS) {
    if (step >= span) return step;
  }
  return NICE_STEPS[NICE_STEPS.length - 1];
}

// Three round gridline values covering the data. Counts get a focused range rather than
// a zero baseline: a backlog moving between 257 and 283 is a flat line against zero,
// which is exactly the reading the trend is supposed to correct. The tick labels always
// state where the axis starts, and the columns chart, where length encodes the value,
// keeps its zero baseline.
function niceScale(values, {zeroBaseline = false} = {}) {
  const numbers = values.filter(value => value !== null);
  const high = Math.max(1, ...numbers);
  const low = zeroBaseline ? 0 : Math.min(...numbers, high);
  let step = niceStep(Math.max((high - low) / 2, high / 40, 0.5));
  let min = zeroBaseline ? 0 : Math.max(0, Math.floor(low / step) * step);
  while (min + step * 2 < high) {
    step = niceStep(step + 0.5);
    min = zeroBaseline ? 0 : Math.max(0, Math.floor(low / step) * step);
  }
  return {min, max: min + step * 2, step};
}

function weekLabel(value) {
  return new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"}).format(new Date(value));
}

function chartFrame(scale, formatTick, size) {
  const plot = {
    left: CHART_MARGIN.left,
    right: size.width - CHART_MARGIN.right,
    top: CHART_MARGIN.top,
    bottom: size.height - CHART_MARGIN.bottom,
  };
  const nodes = [];
  for (const fraction of [0, 0.5, 1]) {
    const value = scale.min + (scale.max - scale.min) * fraction;
    const y = plot.bottom - (plot.bottom - plot.top) * fraction;
    nodes.push(svgElement("line", {
      class: fraction === 0 ? "chart-axis" : "chart-grid",
      x1: plot.left, x2: plot.right, y1: y, y2: y,
    }));
    nodes.push(svgElement("text", {class: "chart-tick", x: plot.left - 6, y: y + 4, "text-anchor": "end"}, formatTick(value)));
  }
  const position = value => plot.bottom - (plot.bottom - plot.top) * ((value - scale.min) / (scale.max - scale.min));
  return {plot, nodes, position};
}

function positions(plot, count) {
  if (count <= 1) return [(plot.left + plot.right) / 2];
  const step = (plot.right - plot.left) / (count - 1);
  return Array.from({length: count}, (unused, index) => plot.left + step * index);
}

// A single-series line chart: no legend needed, and the trend is the whole message.
function lineChart(values, {labels, scale, formatValue, formatTick, description}) {
  const size = CARD_CHART;
  const {plot, nodes, position: y} = chartFrame(scale ?? niceScale(values), formatTick, size);
  const x = positions(plot, values.length);

  let segment = [];
  const segments = [];
  values.forEach((value, index) => {
    if (value === null) {
      if (segment.length) segments.push(segment);
      segment = [];
      return;
    }
    segment.push(`${x[index]},${y(value)}`);
  });
  if (segment.length) segments.push(segment);

  for (const points of segments) {
    if (points.length === 1) {
      const [pointX, pointY] = points[0].split(",");
      nodes.push(svgElement("circle", {class: "chart-dot", cx: pointX, cy: pointY, r: 3}));
    } else {
      nodes.push(svgElement("polyline", {class: "chart-line", points: points.join(" ")}));
    }
  }

  const lastIndex = values.reduce((found, value, index) => (value === null ? found : index), -1);
  if (lastIndex >= 0) {
    nodes.push(svgElement("circle", {class: "chart-end-dot", cx: x[lastIndex], cy: y(values[lastIndex]), r: 4}));
  }

  values.forEach((value, index) => {
    if (value === null) return;
    nodes.push(svgElement("circle", {class: "chart-hit", cx: x[index], cy: y(value), r: 9}, [
      svgElement("title", {}, `${labels[index]}: ${formatValue(value)}`),
    ]));
  });

  nodes.push(svgElement("text", {class: "chart-label", x: plot.left, y: size.height - 6}, labels[0]));
  nodes.push(svgElement("text", {class: "chart-label", x: plot.right, y: size.height - 6, "text-anchor": "end"}, labels[labels.length - 1]));

  return svgElement("svg", {
    class: "chart",
    viewBox: `0 0 ${size.width} ${size.height}`,
    role: "img",
    "aria-label": description,
  }, nodes);
}

function roundedTopBar(x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height);
  return `M${x} ${y + height} L${x} ${y + r} Q${x} ${y} ${x + r} ${y} L${x + width - r} ${y} Q${x + width} ${y} ${x + width} ${y + r} L${x + width} ${y + height} Z`;
}

// Stacked columns. Segments are drawn bottom-up in the order given, so the series
// order has to be stable across renders: it is what makes the colours comparable.
function stackedColumnChart(rows, {formatTick, description}) {
  const size = WIDE_CHART;
  const totals = rows.map(row => row.total);
  const {plot, nodes, position} = chartFrame(niceScale(totals, {zeroBaseline: true}), formatTick, size);
  const slot = (plot.right - plot.left) / rows.length;
  const width = Math.min(24, slot - 6);
  const scale = value => plot.bottom - position(value);

  rows.forEach((row, index) => {
    const x = plot.left + slot * index + (slot - width) / 2;
    const drawn = row.segments.filter(segment => segment.value > 0);
    let bottom = plot.bottom;
    drawn.forEach((segment, depth) => {
      const full = scale(segment.value);
      const covered = depth < drawn.length - 1;
      // A 2px surface gap, not a stroke, separates one segment from the next.
      const height = Math.max(0, full - (covered ? 2 : 0));
      const top = bottom - height;
      nodes.push(svgElement("path", {
        class: segment.className,
        d: covered
          ? `M${x} ${bottom} h${width} v${-height} h${-width} Z`
          : roundedTopBar(x, top, width, height, 4),
      }, [svgElement("title", {}, `${row.label}: ${numberFormat(segment.value)} of ${numberFormat(row.total)} ${segment.title}`)]));
      bottom -= full;
    });
    if (row.total === 0) {
      nodes.push(svgElement("rect", {class: "chart-hit", x, y: plot.bottom - 8, width, height: 8}, [
        svgElement("title", {}, `${row.label}: ${row.emptyTitle}`),
      ]));
    }
  });

  const last = rows[rows.length - 1];
  if (last && last.total > 0) {
    nodes.push(svgElement("text", {
      class: "chart-value",
      x: plot.left + slot * (rows.length - 1) + slot / 2,
      y: plot.bottom - scale(last.total) - 6,
      "text-anchor": "middle",
    }, numberFormat(last.total)));
  }

  nodes.push(svgElement("text", {class: "chart-label", x: plot.left, y: size.height - 6}, rows[0]?.label || ""));
  nodes.push(svgElement("text", {class: "chart-label", x: plot.right, y: size.height - 6, "text-anchor": "end"}, last?.label || ""));

  return svgElement("svg", {
    class: "chart chart-wide",
    viewBox: `0 0 ${size.width} ${size.height}`,
    role: "img",
    "aria-label": description,
  }, nodes);
}

const ATTRIBUTION_BAR = {width: 240, height: 8, gap: 2};

// One stat card's number split by editor, in the colours the columns above use.
function attributionBar(parts, {description}) {
  const drawn = parts.filter(part => part.value > 0);
  const total = drawn.reduce((sum, part) => sum + part.value, 0);
  if (!total) return null;
  const span = ATTRIBUTION_BAR.width - ATTRIBUTION_BAR.gap * (drawn.length - 1);
  let x = 0;
  const nodes = drawn.map(part => {
    const width = span * part.value / total;
    const node = svgElement("rect", {
      class: part.className,
      x,
      y: 0,
      width,
      height: ATTRIBUTION_BAR.height,
      rx: 3,
    }, [svgElement("title", {}, `${part.label}: ${numberFormat(part.value)}`)]);
    x += width + ATTRIBUTION_BAR.gap;
    return node;
  });
  return svgElement("svg", {
    class: "attribution-bar",
    viewBox: `0 0 ${ATTRIBUTION_BAR.width} ${ATTRIBUTION_BAR.height}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-label": `${description}: ${drawn.map(part => `${part.label} ${numberFormat(part.value)}`).join(", ")}`,
  }, nodes);
}

function legend(entries) {
  return element("ul", {className: "chart-legend"}, entries.map(entry => element("li", {}, [
    element("span", {className: `legend-swatch ${entry.className}`, attrs: {"aria-hidden": "true"}}),
    entry.label,
  ])));
}

function changePhrase(change, {unit = "", period}) {
  if (change === 0) return `unchanged ${period}`;
  const direction = change < 0 ? "down" : "up";
  return `${direction} ${numberFormat(Math.round(Math.abs(change)))}${unit} ${period}`;
}

function deltaChip(change, {lowerIsBetter, unit = "", period}) {
  if (change === null || change === undefined) {
    return element("span", {className: "trend-delta", dataset: {tone: "flat"}, text: "no comparison yet"});
  }
  const improving = lowerIsBetter ? change < 0 : change > 0;
  const tone = change === 0 ? "flat" : improving ? "good" : "bad";
  return element("span", {className: "trend-delta", dataset: {tone}}, [
    element("span", {className: "trend-arrow", text: change === 0 ? "→" : change < 0 ? "▼" : "▲", attrs: {"aria-hidden": "true"}}),
    changePhrase(change, {unit, period}),
  ]);
}

function trendCard({title, value, chart, delta, hint, status}) {
  return element("article", {className: "trend-card", dataset: {status}}, [
    element("h2", {className: "trend-title", text: title}),
    element("div", {className: "trend-headline"}, [
      element("strong", {className: "trend-value", text: value}),
      delta,
    ]),
    chart,
    element("p", {className: "trend-hint", text: hint}),
  ]);
}

function percentText(value) {
  return value === null || value === undefined ? "—" : `${Math.round(value)}%`;
}

function statCard(value, label, note, bar) {
  return element("article", {className: "stat-card"}, [
    element("strong", {className: "stat-value", text: value}),
    element("span", {className: "stat-label", text: label}),
    bar,
    element("span", {className: "stat-note", text: note}),
  ]);
}

function tableRow(cells) {
  return element("tr", {}, cells.map((value, index) => element(index === 0 ? "th" : "td", {
    text: value,
    attrs: index === 0 ? {scope: "row"} : {},
  })));
}

function renderHealth() {
  const metrics = dashboard.metrics;
  const health = metrics.health;
  const trends = metrics.trends;
  const points = trends.points;
  const buckets = trends.buckets;
  const weeks = Math.round(trends.window_days / 7);
  const period = `in ${weeks} weeks`;
  const targetDays = dashboard.response_targets.initial_editor_response_days;
  document.querySelector("#health-date").textContent = `Generated ${localDate(metrics.generated_at)}`;

  const pointLabels = points.map(point => weekLabel(point.at));
  const backlog = health.indicators.backlog;
  const overdue = health.indicators.overdue_first_response;
  const rate = health.indicators.first_response_rate;

  const matureBuckets = buckets.filter(bucket => bucket.first_response_mature);
  const rateValues = matureBuckets.map(bucket => (
    bucket.first_response_eligible
      ? (bucket.first_response_within_target / bucket.first_response_eligible) * 100
      : null
  ));
  const rateLabels = matureBuckets.map(bucket => weekLabel(bucket.start));

  document.querySelector("#trend-cards").replaceChildren(
    trendCard({
      title: backlog.label,
      status: backlog.status,
      value: numberFormat(backlog.value),
      delta: deltaChip(backlog.change, {lowerIsBetter: true, period}),
      hint: "Open pull requests at the end of each week. Fewer is better.",
      chart: lineChart(points.map(point => point.open_prs), {
        labels: pointLabels,
        formatValue: numberFormat,
        formatTick: value => numberFormat(Math.round(value)),
        description: `Open pull requests each week for ${weeks} weeks, ending at ${numberFormat(backlog.value)}.`,
      }),
    }),
    trendCard({
      title: overdue.label,
      status: overdue.status,
      value: numberFormat(overdue.value),
      delta: deltaChip(overdue.change, {lowerIsBetter: true, period}),
      hint: `Open contributor PRs with no editor reply after ${targetDays} days. Fewer is better.`,
      chart: lineChart(points.map(point => point.overdue_first_response), {
        labels: pointLabels,
        formatValue: numberFormat,
        formatTick: value => numberFormat(Math.round(value)),
        description: `Contributor pull requests waiting more than ${targetDays} days for a first editor reply, each week for ${weeks} weeks, ending at ${numberFormat(overdue.value)}.`,
      }),
    }),
    trendCard({
      title: rate.label,
      status: rate.status,
      value: percentText(rate.percent),
      delta: deltaChip(rate.change, {lowerIsBetter: false, unit: " points", period: "vs the earlier weeks"}),
      hint: `Share of contributor PRs answered inside ${targetDays} days. Target ${rate.target_percent}%. Higher is better.`,
      chart: lineChart(rateValues, {
        labels: rateLabels,
        scale: {min: 0, max: 100, step: 50},
        formatValue: percentText,
        formatTick: value => `${Math.round(value)}%`,
        description: `Share of contributor pull requests answered within ${targetDays} days, by week opened; ${percentText(rate.percent)} across the window.`,
      }),
    }),
  );

  const problems = [
    backlog.status === "on_track" ? null : `the backlog is ${changePhrase(backlog.change, {period})}`,
    overdue.status === "on_track" ? null : `contributors waiting over ${targetDays} days are ${changePhrase(overdue.change, {period})}`,
    rate.status === "on_track" || rate.percent === null
      ? null
      : `${percentText(rate.percent)} of contributor PRs get a first reply within ${targetDays} days, below the ${rate.target_percent}% target`,
  ].filter(Boolean);
  const verdict = document.querySelector("#health-verdict");
  verdict.dataset.status = health.overall_status;
  verdict.textContent = problems.length
    ? `Needs attention: ${problems.join("; ")}.`
    : `On target: the backlog is ${changePhrase(backlog.change, {period})}, and ${percentText(rate.percent)} of contributor PRs get a first reply within ${targetDays} days.`;

  document.querySelector("#trend-table tbody").replaceChildren(...buckets.map((bucket, index) => {
    const point = points[index + 1];
    return tableRow([
      weekLabel(bucket.start),
      numberFormat(point.open_prs),
      numberFormat(point.awaiting_first_response),
      numberFormat(point.overdue_first_response),
      numberFormat(bucket.opened),
      numberFormat(bucket.merged),
      bucket.first_response_eligible
        ? `${bucket.first_response_within_target}/${bucket.first_response_eligible}${bucket.first_response_mature ? "" : " (partial week)"}`
        : "—",
    ]);
  }));

  renderImpact(weeks);

  const coverage = metrics.coverage;
  document.querySelector("#health-coverage").textContent = (
    `Sampling: ${coverage.open_timeline_complete} of ${coverage.open_timeline_total} open pull requests have a complete sampled comment timeline, and ` +
    `${coverage.first_response_unknown_due_to_sampling} are left out of the first-response figures because theirs is not. ` +
    `Closed pull requests cover the last ${coverage.history_days} days.`
  );
}

// Editors get a colour by their position in the payload's fixed alphabetical list,
// so a colour means the same person in every chart on the page. The palette in
// style.css is validated for exactly these five slots on every pair of colours;
// a sixth editor folds into one "other editors" series rather than taking an
// unvalidated hue, and every editor still has their own row in the tables.
const SERIES_SLOTS = 5;

function editorSeries(members) {
  const series = members.slice(0, SERIES_SLOTS).map((member, index) => ({
    logins: [member.login],
    label: `@${member.login}`,
    className: `series-${index + 1}`,
  }));
  const folded = members.slice(SERIES_SLOTS);
  if (folded.length) {
    series.push({
      logins: folded.map(member => member.login),
      label: `${numberFormat(folded.length)} other editors`,
      className: "series-other",
    });
  }
  return series;
}

function renderImpact(weeks) {
  const metrics = dashboard.metrics;
  const impact = metrics.editors;
  const overall = impact.team.window;
  const recent = impact.team.recent;
  const buckets = metrics.trends.buckets;
  const recentWeeks = Math.round(recent.days / 7);
  const series = editorSeries(impact.members);
  const byLogin = new Map(impact.members.map(member => [member.login, member.window]));
  const sumOver = (logins, field) => logins.reduce((total, login) => total + (byLogin.get(login)?.[field] || 0), 0);

  document.querySelector("#impact-window").textContent = (
    `Public activity by the ${numberFormat(impact.members.length)} configured editors over the last ${weeks} weeks.`
  );
  document.querySelector("#impact-hero-value").textContent = percentText(overall.merged_with_review_percent);
  document.querySelector("#impact-hero-caption").textContent = (
    `of the ${numberFormat(overall.merged)} pull requests merged in the last ${weeks} weeks had a review from an editor ` +
    `other than the author before they merged. Last ${recentWeeks} weeks: ${percentText(recent.merged_with_review_percent)} ` +
    `(${numberFormat(recent.merged_with_review)} of ${numberFormat(recent.merged)}).`
  );

  const rows = buckets.map(bucket => {
    const credited = bucket.merged_by_first_reviewer || {};
    const segments = series.map(entry => ({
      className: entry.className,
      value: entry.logins.reduce((total, login) => total + (credited[login] || 0), 0),
      title: `merged after a review by ${entry.label}`,
    }));
    segments.push({
      className: "series-rest",
      value: bucket.merged - bucket.merged_with_editor_review,
      title: "merged without an editor review",
    });
    return {
      label: weekLabel(bucket.start),
      total: bucket.merged,
      segments,
      emptyTitle: "nothing merged",
    };
  });

  document.querySelector("#merge-chart").replaceChildren(
    stackedColumnChart(rows, {
      formatTick: value => numberFormat(Math.round(value)),
      description: (
        `Pull requests merged each week for ${weeks} weeks, split by the editor credited with reviewing them first. ` +
        `${numberFormat(overall.merged_with_review)} of ${numberFormat(overall.merged)} had an editor review; ` +
        "the week-by-week figures are in the table below the chart."
      ),
    }),
    legend([
      ...series.map(entry => ({className: entry.className, label: entry.label})),
      {className: "series-rest", label: "No editor review"},
    ]),
  );

  document.querySelector("#merge-table thead tr").replaceChildren(
    element("th", {text: "Week of", attrs: {scope: "col"}}),
    element("th", {text: "Merged", attrs: {scope: "col"}}),
    ...series.map(entry => element("th", {text: entry.label, attrs: {scope: "col"}})),
    element("th", {text: "No editor review", attrs: {scope: "col"}}),
  );
  document.querySelector("#merge-table tbody").replaceChildren(...rows.map(row => tableRow([
    row.label,
    numberFormat(row.total),
    ...row.segments.map(segment => numberFormat(segment.value)),
  ])));

  const bar = (field, description) => attributionBar(
    series.map(entry => ({
      className: entry.className,
      label: entry.label,
      value: sumOver(entry.logins, field),
    })),
    {description},
  );

  document.querySelector("#impact-stats").replaceChildren(
    statCard(
      numberFormat(overall.first_responses_total),
      "First replies by editors",
      `Pull requests whose first editor reply landed in the last ${weeks} weeks`,
      bar("first_responses", "First replies by editor"),
    ),
    statCard(
      numberFormat(overall.reviews_submitted),
      "Reviews submitted",
      `${numberFormat(recent.reviews_submitted)} in the last ${recentWeeks} weeks`,
      bar("reviews_submitted", "Reviews submitted by editor"),
    ),
    statCard(
      numberFormat(overall.contributors_engaged),
      "Contributors replied to",
      "Distinct pull-request authors, excluding bots and editors. An author two editors replied to counts for each of them.",
      bar("contributors_engaged", "Contributors replied to by editor"),
    ),
    statCard(
      numberFormat(overall.authored_prs_merged),
      "Editors' own PRs merged",
      "Merged pull requests an editor authored",
      bar("authored_prs_merged", "Merged pull requests by author"),
    ),
  );

  document.querySelector("#editor-table tbody").replaceChildren(...impact.members.map(member => tableRow([
    `@${member.login}`,
    numberFormat(member.window.first_responses),
    numberFormat(member.window.reviews_submitted),
    numberFormat(member.window.contributors_engaged),
    numberFormat(member.window.authored_prs_merged),
  ])));

  document.querySelector("#impact-note").textContent = (
    "These figures describe the order of public events. “Merged after a review” does not assert that the review caused the merge. " +
    "A merge several editors reviewed is credited to whoever reviewed it first, so the weekly columns add up to the merges; every " +
    "review still counts in its own editor's total."
  );
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

function updateBuildIndicator() {
  const indicator = document.querySelector("#build-indicator");
  indicator.textContent = `Updated ${relativeTime(dashboard.generated_at)}`;
  indicator.title = `Generated ${localDate(dashboard.generated_at)} · source: ${dashboard.build.source}`;
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
      perspective: typeof parsed.settings?.perspective === "string" ? parsed.settings.perspective : null,
      identity: typeof parsed.settings?.identity === "string" ? parsed.settings.identity : null,
      openSections: readOpenSections(parsed.settings?.openSections),
    },
  };
  saveLocalState();
  syncControlState();
  renderQueues();
  toast("Local state imported.");
}

function syncControlState() {
  // Import and reset can move the identity, which moves the "(you)" marker.
  if (dashboard) renderPerspectiveOptions();
  for (const [id, fallback] of Object.entries(SECTION_DEFAULTS)) {
    const stored = localState.settings.openSections[id];
    document.querySelector(`#${id}`).open = typeof stored === "boolean" ? stored : fallback;
  }
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
  document.querySelector("#editor-perspective").addEventListener("change", event => {
    localState.settings.perspective = event.target.value;
    saveLocalState();
    renderQueues();
  });
  document.querySelector("#editor-identity").addEventListener("change", event => {
    localState.settings.identity = event.target.value || null;
    // Saying who you are is almost always followed by wanting your own queue, but
    // an explicit perspective choice is left alone.
    if (localState.settings.identity && localState.settings.perspective === null) {
      localState.settings.perspective = localState.settings.identity;
    }
    saveLocalState();
    renderPerspectiveOptions();
    renderQueues();
  });
  document.querySelector("#sort-order").addEventListener("change", event => {
    localState.settings.sortOrder = SORT_ORDERS.has(event.target.value) ? event.target.value : "queue";
    saveLocalState();
    renderQueues();
  });
  for (const id of Object.keys(SECTION_DEFAULTS)) {
    document.querySelector(`#${id}`).addEventListener("toggle", event => {
      localState.settings.openSections[id] = event.target.open;
      saveLocalState();
    });
  }
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
    localState = {version: STATE_VERSION, items: {}, settings: DEFAULT_SETTINGS()};
    syncControlState();
    renderQueues();
    toast("Local state reset.");
  });
}

async function fetchDashboard() {
  const response = await fetch(DATA_URL, {cache: "no-store"});
  if (!response.ok) throw new Error(`Dashboard data returned HTTP ${response.status}.`);
  return response.json();
}

function applyDashboard(payload, {preserveScroll = false} = {}) {
  const scrollY = window.scrollY;
  dashboard = payload;
  itemsByKey = new Map(dashboard.items.map(item => [item.key, item]));
  updateBuildIndicator();
  renderPerspectiveOptions();
  renderQueues();
  renderHealth();
  renderMethodology();
  if (preserveScroll && window.scrollY !== scrollY) window.scrollTo(window.scrollX, scrollY);
}

function dataOldEnoughForANewBuild() {
  const generated = Date.parse(dashboard?.generated_at);
  if (!Number.isFinite(generated)) return true;
  return Date.now() - generated >= BUILD_INTERVAL_MS;
}

// Reload data.json in place when the deployed build is newer than the one this tab is showing, so a
// tab left open across a scheduled build does not keep serving yesterday's queue. Also retries the
// initial load when that failed.
async function checkForFreshData({userReturned = false} = {}) {
  if (checkInFlight || document.hidden) return;
  if (dashboard && !dataOldEnoughForANewBuild()) return;
  const sinceLastCheck = Date.now() - lastCheckAt;
  if (sinceLastCheck < (userReturned ? MIN_CHECK_INTERVAL_MS : STALE_POLL_INTERVAL_MS)) return;

  checkInFlight = true;
  lastCheckAt = Date.now();
  const hadData = Boolean(dashboard);
  try {
    const payload = await fetchDashboard();
    if (!payload?.generated_at || payload.generated_at === dashboard?.generated_at) return;
    applyDashboard(payload, {preserveScroll: hadData});
    toast(hadData
      ? `Loaded a fresh build generated ${relativeTime(dashboard.generated_at)}.`
      : "Dashboard data loaded.");
  } catch {
    // Keep whatever is already on screen; the next check tries again.
  } finally {
    checkInFlight = false;
  }
}

function installRefreshHandlers() {
  const onReturn = () => {
    if (document.hidden) return;
    if (dashboard) updateBuildIndicator();
    checkForFreshData({userReturned: true});
  };
  window.addEventListener("pageshow", onReturn);
  window.addEventListener("focus", onReturn);
  document.addEventListener("visibilitychange", onReturn);
  setInterval(() => {
    if (dashboard) updateBuildIndicator();
    checkForFreshData();
  }, CLOCK_TICK_MS);
}

async function start() {
  installEventHandlers();
  installRefreshHandlers();
  syncControlState();
  showView();
  lastCheckAt = Date.now();
  try {
    applyDashboard(await fetchDashboard());
  } catch (error) {
    document.querySelector("#build-indicator").textContent = "Data unavailable";
    document.querySelector("#suggested-list").replaceChildren(emptyState(error.message || "Could not load dashboard data."));
    document.querySelector("#lane-list").replaceChildren(emptyState("The dashboard data could not be loaded."));
    toast(error.message || "Could not load dashboard data.");
  }
}

start();
