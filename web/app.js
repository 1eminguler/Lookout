"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cleanErr = (s) => String(s ?? "").replace(/^[A-Za-z_]\w*(?:Error|Exception):\s*/, "");
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const I = {
  save: '<svg viewBox="0 0 24 24"><path d="M6 4h12v17l-6-4-6 4z"/></svg>',
  done: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  hide: '<svg viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 6.2A9.8 9.8 0 0 1 12 6c5 0 8.5 4.5 9.5 6-.4.7-1.3 1.9-2.6 3.1M6.5 7.6C4.4 9 3 10.9 2.5 12c1 1.5 4.5 6 9.5 6 1.3 0 2.5-.3 3.6-.8"/></svg>',
  undo: '<svg viewBox="0 0 24 24"><path d="M4 9h10a6 6 0 0 1 0 12H9"/><path d="m8 5-4 4 4 4"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 3 2 20h20zM12 10v4M12 17h.01"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
  right: '<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  watch: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2"/></svg>',
  bot: '<svg viewBox="0 0 24 24"><rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M9 13h.01M15 13h.01"/></svg>',
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 2.5 14.4 0 17M12 3.5c-2.5 2.6-2.5 14.4 0 17"/></svg>',
  funnel: '<svg viewBox="0 0 24 24"><path d="M4 5h16l-6 8v5l-4 2v-7z"/></svg>',
  dl: '<svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
};
const ITEMS_PAGE_SIZE = 50;
let S = null, AG = null, route = { view: "feed", id: null };
let schedPreview = null;
let editingSourceId = null, editingFilterId = null, editingWatchId = null;
const sourceCodeCache = {}, sourceCodeLoading = {};
let feedState = {
  scope: "",
  key: "",
  items: [],
  total: 0,
  hasMore: false,
  facets: { categories: {}, sources: [], updated_count: 0, snoozed_count: 0 },
  loading: false,
  initialized: false,
};

const ui = JSON.parse(localStorage.getItem("lookout-ui") || "{}");
ui.sort ??= "deadline"; ui.cats ??= []; ui.src ??= ""; ui.q = ""; ui.calMonth ??= null; ui.showSnoozed ??= false; ui.onlyUpdated ??= false;
ui.hideIneligible ??= false;
const keep = () => localStorage.setItem("lookout-ui", JSON.stringify({ ...ui, q: "" }));
const themes = ["system", "light", "dark"];
if (!themes.includes(ui.theme)) ui.theme = "system";
function applyTheme() {
  const html = document.documentElement;
  if (ui.theme === "system") html.removeAttribute("data-theme");
  else html.dataset.theme = ui.theme;
}
applyTheme();
document.body.classList.add("browser");
window.addEventListener("pywebviewready", () => document.body.classList.remove("browser"));

const inflightGets = new Map();
const api = (path, body) => {
  if (body === undefined && inflightGets.has(path)) return inflightGets.get(path);
  const opts = body === undefined
    ? { cache: "no-store", credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const req = fetch(path, opts).then(async (r) => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || r.statusText);
      err.status = r.status;
      throw err;
    }
    return j;
  }).finally(() => {
    if (body === undefined) inflightGets.delete(path);
  });
  if (body === undefined) inflightGets.set(path, req);
  return req;
};
async function downloadApi(path, filename) {
  const r = await fetch(path, { cache: "no-store", credentials: "same-origin" });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    const err = new Error(j.error || r.statusText);
    err.status = r.status;
    throw err;
  }
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a"); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 3200); }

function handleAuthFailure() {
  const prev = Number(sessionStorage.getItem("lookout-reauth-ts") || "0");
  if (!prev || Date.now() - prev > 5000) {
    sessionStorage.setItem("lookout-reauth-ts", String(Date.now()));
    location.reload();
    return true;
  }
  return false;
}

function showSessionExpired() {
  lastHtml = lastTag = null;
  $("#main").innerHTML = `<div class="page"><div class="empty"><b>Session expired</b>Open Lookout again from the app or terminal to sign back in.<div class="row"><button class="btn sm primary" data-act="reload-page">Reload</button></div></div></div>`;
}

// ---------- dates ----------
const today0 = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseIso = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const daysTo = (s) => (s ? Math.round((parseIso(s) - today0()) / 864e5) : null);
const short = (s) => { const d = parseIso(s); return `${d.getDate()} ${MON[d.getMonth()]}` + (d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : ""); };
const target = (i) => i.deadline || i.start_date || null;
function deadlineTooltip(i) {
  return i.deadline_raw ? ` title="${esc(i.deadline_raw)}"` : "";
}
function deadlineLabel(i, label) {
  const tip = deadlineTooltip(i);
  if (!i.deadline) return tip ? `<span${tip}>${label}</span>` : "";
  let extra = i.extra;
  if (typeof extra === "string") {
    try { extra = JSON.parse(extra); } catch { extra = null; }
  }
  const guessed = extra?.deadline_inferred === true ? ' <small class="year-guessed">year guessed</small>' : "";
  return `<time datetime="${esc(i.deadline)}"${tip}>${label}</time>${guessed}`;
}
const isNew = (i) => Date.now() - new Date(i.first_seen.replace(" ", "T")).getTime() < 72 * 36e5;
const isUpdated = (i) => Boolean(i.updated_at && i.last_change_kind && i.last_change_kind !== "closed" && (Date.now() - new Date(i.updated_at.replace(" ", "T")).getTime() < 72 * 36e5));
const isSnoozed = (i) => Boolean(i.snoozed_until && daysTo(i.snoozed_until) > 0);
const agendaBase = () => S?.agenda_items || [];
const findItem = (id) => feedState.items.find((x) => x.id === id) || agendaBase().find((x) => x.id === id) || null;
function syncItem(id, patch) {
  for (const list of [feedState.items, S?.agenda_items]) {
    const it = list?.find((x) => x.id === id);
    if (it) Object.assign(it, patch);
  }
  if ((patch.action === "saved" || patch.action === "applied") && S?.agenda_items && !S.agenda_items.some((x) => x.id === id)) {
    const src = findItem(id);
    if (src) S.agenda_items.unshift({ ...src });
  }
}
function ago(ts) {
  const m = Math.round((Date.now() - new Date(ts.replace(" ", "T")).getTime()) / 6e4);
  if (m < 2) return "just now"; if (m < 60) return `${m} minutes ago`;
  const h = Math.round(m / 60); if (h < 24) return `${h} hour${h > 1 ? "s" : ""} ago`;
  const d = Math.round(h / 24); return d === 1 ? "yesterday" : `${d} days ago`;
}
function duration(a, b) { if (!b) return ""; const s = Math.round((new Date(b.replace(" ", "T")) - new Date(a.replace(" ", "T"))) / 1000); return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`; }
const watchById = (id) => (S?.watches || []).find((w) => w.id === id) || null;

function dedupeItems(list) {
  const seen = new Set();
  return list.filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

// ---------- server-side items fetching ----------
function itemsScopeOnly() {
  if (route.view !== "feed" && route.view !== "watch") return "";
  return route.view === "watch" ? `watch:${route.id}` : "feed";
}

function itemsScopeKey() {
  if (route.view !== "feed" && route.view !== "watch") return "";
  return JSON.stringify({
    v: route.view,
    id: route.view === "watch" ? route.id : null,
    sort: ui.sort,
    cats: [...ui.cats].sort(),
    src: route.view === "feed" ? ui.src : "",
    q: ui.q,
    sn: ui.showSnoozed,
    upd: ui.onlyUpdated,
    eligibility: hideIneligible(),
    profile: S?.settings?.eligibility_profile,
  });
}

let itemsReqSeq = 0, itemsInFlight = null;
async function loadItems(append = false) {
  if (route.view !== "feed" && route.view !== "watch") return;
  const scope = itemsScopeOnly();
  const key = itemsScopeKey();
  if (!append && feedState.loading && feedState.key === key && itemsInFlight) return itemsInFlight;
  const seq = ++itemsReqSeq;
  feedState.loading = true;
  const sameKeyReload = !append && feedState.key === key && feedState.initialized;
  if (!append) {
    if (feedState.key !== key) {
      feedState.items = [];
      feedState.total = 0;
      feedState.hasMore = false;
      feedState.initialized = false;
      feedState.scope = scope;
    }
    feedState.key = key;
  }
  const pageLimit = sameKeyReload ? Math.min(500, Math.max(ITEMS_PAGE_SIZE, feedState.items.length)) : ITEMS_PAGE_SIZE;
  const p = new URLSearchParams({
    status: "open",
    match: "1",
    exclude_hidden: "1",
    exclude_applied: "1",
    exclude_expired: "1",
    snoozed: ui.showSnoozed ? "include" : "exclude",
    sort: ui.sort === "new" ? "new" : "deadline",
    limit: String(pageLimit),
    offset: String(append ? feedState.items.length : 0),
  });
  if (route.view === "watch" && route.id != null) p.set("watch_id", String(route.id));
  if (route.view === "feed" && ui.src) p.set("source", ui.src);
  if (ui.cats.length) p.set("category", ui.cats.join(","));
  if (ui.onlyUpdated) p.set("updated", "1");
  if (hideIneligible()) p.set("eligibility", "hide_ineligible");
  if (ui.q) p.set("q", ui.q);
  if (!append) p.set("facets", "1");
  const run = (async () => {
    try {
      const res = await api(`/api/items?${p.toString()}`);
      if (seq !== itemsReqSeq || key !== itemsScopeKey()) return;
      if (append) {
        const seen = new Set(feedState.items.map((x) => x.id));
        for (const it of res.items || []) {
          if (!seen.has(it.id)) feedState.items.push(it);
        }
      } else {
        feedState.items = res.items || [];
        if (res.facets) feedState.facets = res.facets;
      }
      feedState.total = Number(res.total || 0);
      feedState.hasMore = Boolean(res.has_more);
      feedState.initialized = true;
      feedState.loading = false;
      render();
    } catch (err) {
      if (seq === itemsReqSeq) {
        feedState.loading = false;
        if (err.status === 401 || err.status === 403) handleAuthFailure();
      }
    } finally {
      if (itemsInFlight === run) itemsInFlight = null;
    }
  })();
  itemsInFlight = run;
  return run;
}

function reloadActiveItems() {
  if (route.view === "feed" || route.view === "watch") loadItems(false);
}

// ---------- models: "x-low / x-medium / x-high" become one family with an effort submenu ----------
const EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"];
function groupModels(list) {
  const fams = new Map();
  for (const [id, name] of list || []) {
    const m = id.match(/^(.*)-(minimal|low|medium|high|xhigh|max)$/i);
    const key = m ? m[1] : id;
    const label = m ? name.replace(/\s*\((minimal|low|medium|high|xhigh|max)\)\s*$/i, "") : name;
    if (!fams.has(key)) fams.set(key, { key, label, variants: [] });
    fams.get(key).variants.push({ id, effort: m ? m[2].toLowerCase() : null });
  }
  for (const f of fams.values()) f.variants.sort((a, b) => EFFORTS.indexOf(a.effort) - EFFORTS.indexOf(b.effort));
  return [...fams.values()];
}
const effortLabel = (e) => (e === "xhigh" ? "Extra high" : e ? e[0].toUpperCase() + e.slice(1) : "");
function modelChipText(id, agent) {
  if (!id) return "Default model";
  for (const f of groupModels(modelCache[agent])) { const v = f.variants.find((x) => x.id === id); if (v) return esc(f.label) + (v.effort ? ` <span class="dim">${effortLabel(v.effort)}</span>` : ""); }
  return esc(id);
}
const modelCache = {}, modelInFlight = {};
async function modelsFor(agent) {
  if (!agent || agent === "custom") return [];
  if (modelCache[agent]) return modelCache[agent];
  if (!modelInFlight[agent]) {
    modelInFlight[agent] = api(`/api/agents/${agent}/models`)
      .then((r) => (modelCache[agent] = r.models || []))
      .finally(() => { delete modelInFlight[agent]; });
  }
  return modelInFlight[agent];
}
let agInFlight = null;
async function ensureAgents() {
  if (AG) return AG;
  if (!agInFlight) {
    agInFlight = api("/api/agents")
      .then((r) => (AG = r))
      .finally(() => { agInFlight = null; });
  }
  return agInFlight;
}
const agentName = (id) => AG?.agents.find((a) => a.id === id)?.name || id;

// ---------- popovers ----------
let popEl = null, subEl = null, popAnchor = null;
function closePops(restoreFocus = false) {
  const anchor = popAnchor;
  anchor?.setAttribute("aria-expanded", "false");
  popEl?.remove();
  popEl = subEl = popAnchor = null;
  if (restoreFocus) anchor?.focus();
}
function placePop(el, anchor) {
  el.querySelectorAll("button").forEach((button) => { button.type = "button"; });
  anchor.after(el);
  anchor.setAttribute("aria-expanded", "true");
  if (el.getBoundingClientRect().bottom > $("#main").getBoundingClientRect().bottom &&
      anchor.getBoundingClientRect().top - el.offsetHeight >= $("#main").getBoundingClientRect().top) {
    el.classList.add("pop-above");
  }
}
async function openModelPicker(anchor) {
  closePops();
  const st = S.settings;
  if (!st.agent) { location.hash = "#/settings"; return toast("Choose an agent first"); }
  popAnchor = anchor;
  const list = await modelsFor(st.agent), fams = groupModels(list), cur = st.model;
  if (popAnchor !== anchor || !anchor.isConnected) return;
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Model for ${esc(agentName(st.agent))}</div><button class="o ${!cur ? "on" : ""}" data-model="">Default model</button>` +
    fams.map((f, k) => {
      const on = f.variants.find((v) => v.id === cur);
      if (f.variants.length === 1) return `<button class="o ${on ? "on" : ""}" data-model="${esc(f.variants[0].id)}">${esc(f.label)}</button>`;
      return `<button type="button" class="o fam ${on ? "on" : ""}" data-fam="${k}" aria-expanded="false">${esc(f.label)}<span class="r">${on ? effortLabel(on.effort) : ""} ${I.down}</span></button>`;
    }).join("") +
    `<hr><div class="custom"><input type="text" id="customModel" placeholder="Other model id" value="${esc(cur && !list.some(([k]) => k === cur) ? cur : "")}"><button class="btn sm" data-model-custom>Use</button></div>`;
  placePop(pop, anchor);
  const openFam = (btn) => {
    subEl?.remove();
    pop.querySelectorAll(".fam").forEach((b) => b.setAttribute("aria-expanded", String(b === btn)));
    const f = fams[Number(btn.dataset.fam)];
    const sub = document.createElement("div"); sub.className = "pop pop-sub"; subEl = sub;
    sub.innerHTML = `<div class="t">${esc(f.label)}</div>` + f.variants.map((v) => `<button type="button" class="o ${v.id === cur ? "on" : ""}" data-model="${esc(v.id)}">${effortLabel(v.effort) || "Standard"}</button>`).join("");
    btn.after(sub);
  };
  pop.querySelectorAll(".fam").forEach((b) => b.addEventListener("click", () => openFam(b)));
}
async function openAgentPicker(anchor) {
  closePops();
  popAnchor = anchor;
  await ensureAgents();
  if (popAnchor !== anchor || !anchor.isConnected) return;
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Agents on this computer</div>` + AG.agents.filter((a) => a.id !== "custom" || a.configured).map((a) =>
    `<button class="o ${S.settings.agent === a.id ? "on" : ""}" data-agent="${a.id}" ${a.installed ? "" : "disabled"}>${esc(a.name)}<span class="r">${a.installed ? "" : "not installed"}</span></button>`).join("") +
    `<hr><button class="o" data-goto="#/settings">Agent settings</button>`;
  placePop(pop, anchor);
}
async function setModel(model) { await api("/api/settings", { model }); S.settings.model = model; closePops(); toast("Model changed"); render.force = true; render(); }
document.addEventListener("mousedown", (e) => { if (popAnchor && !popEl?.contains(e.target) && !e.target.closest("[data-pick]")) closePops(); });

// ---------- routing ----------
function parseRoute() {
  const oldHash = location.hash;
  const path = oldHash.replace(/^#\/?/, "");
  const legacy = { feed: "opportunities", home: "opportunities", sources: "watches/sources", filters: "watches/rules" };
  const canonical = Object.hasOwn(legacy, path) ? legacy[path] : path
    .replace(/^watch\/(\d+)$/, "watches/$1")
    .replace(/^scan\/(\d+)$/, "opportunities/history/$1");
  const [section, part, detail, ...rest] = canonical.split("/");
  const id = /^\d+$/.test(part || "") ? Number(part) : null;
  route = { view: "feed", id: null, section: "opportunities" };
  let target = "opportunities";
  if (!rest.length) {
    if (!detail && !part && ["agenda", "settings", "watches"].includes(section)) {
      route = { view: section, id: null, section };
      target = section;
    } else if (!detail && section === "watches" && (id !== null || ["new", "sources", "rules"].includes(part))) {
      route = { view: id !== null ? "watch" : { new: "new-watch", sources: "sources", rules: "filters" }[part], id, section };
      target = canonical;
    } else if (section === "opportunities" && part === "history" && (!detail || /^\d+$/.test(detail))) {
      route = { view: detail ? "scan" : "history", id: detail ? Number(detail) : null, section };
      target = canonical;
    } else if (!detail && section === "search" && id !== null) {
      route = { view: "search", id, section: "watches" };
      target = canonical;
    }
  }
  const hash = "#/" + target;
  if (oldHash !== hash) history.replaceState(history.state, "", hash);
}
window.addEventListener("hashchange", () => {
  closePops();
  editingSourceId = editingFilterId = editingWatchId = null;
  parseRoute();
  render();
  refresh(false);
});

// ---------- navigation ----------
function renderNav() {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === route.section) a.setAttribute("aria-current", "location");
    else a.removeAttribute("aria-current");
  });
}

function renderWatches() {
  const watches = (S.watches || []).map((w) =>
    `<a class="route-row" href="#/watches/${w.id}">${I.watch}<span><b>${esc(w.title)}</b>${w.request ? `<small>${esc(w.request)}</small>` : ""}</span>${I.right}</a>`
  ).join("");
  return `<div class="page"><div class="head"><div><h1>Watches</h1></div></div>
    ${watches ? `<div class="box">${watches}</div>` : '<div class="empty"><b>No watches yet</b><a class="btn primary" href="#/watches/new">New watch</a></div>'}
    <div class="box mt-24"><a class="route-row" href="#/watches/sources">${I.globe}<b>Independent sources</b>${I.right}</a>
    <a class="route-row" href="#/watches/rules">${I.funnel}<b>Rules for all watches</b>${I.right}</a></div></div>`;
}

function renderHistory() {
  const entries = [
    ...S.scans.map((s) => ({ kind: "scan", ...s })),
    ...(S.discoveries || []).map((d) => ({ kind: "search", ...d })),
  ].sort((a, b) => (b.started || "").localeCompare(a.started || "") || b.id - a.id);
  const rows = entries.map((entry) => {
    const scan = entry.kind === "scan";
    const href = scan ? `#/opportunities/history/${entry.id}` : `#/search/${entry.id}`;
    const label = scan ? entry.label || (entry.trigger === "scheduled" ? "Daily scan" : "Scan") : entry.title || entry.request || "Source search";
    return `<a class="route-row" href="${href}">${scan ? I.clock : I.search}<span><b>${esc(label)}</b><small>${short(entry.started.slice(0, 10))} ? ${esc(entry.status)}</small></span>${I.right}</a>`;
  }).join("");
  return `<div class="page"><div class="head"><div><h1>Scan history</h1></div><a class="btn" href="#/opportunities">Opportunities</a></div>
    ${rows ? `<div class="box">${rows}</div>` : '<div class="empty"><b>No scans or source searches yet</b></div>'}</div>`;
}

// ---------- banners ----------
function renderNotifyErrorsBanner() {
  const errs = S?.notify_errors || [];
  if (!errs.length) return "";
  const list = errs.map((e) => `<li><b>${esc(e.channel)}</b>: ${esc(e.message || "Delivery failed")} <span class="muted">(${esc((e.created || "").slice(0, 16))})</span></li>`).join("");
  return `<div class="banner">${I.warn}<div class="banner-body"><b>Notification delivery failed</b><ul class="banner-list">${list}</ul><div class="row mt-8"><a class="btn sm ghost" href="#/settings">Check settings</a><button type="button" class="btn sm ghost" data-act="dismiss-notify-errors">Dismiss</button></div></div></div>`;
}

// ---------- item card ----------
function whenBox(i) {
  const t = target(i);
  const tip = i.deadline ? "" : deadlineTooltip(i);
  if (!t) return `<div class="when"${tip}><b class="s">—</b><span>no date</span></div>`;
  const dateLabel = i.deadline ? deadlineLabel(i, short(t)) : `<time>${short(t)}</time>`;
  const d = daysTo(t), cls = !i.deadline ? "" : d <= 3 ? "red" : d <= 7 ? "orange" : d <= 14 ? "amber" : "";
  if (d < 0) return `<div class="when"${tip}><b class="s">past</b>${dateLabel}</div>`;
  const big = d === 0 ? `<b class="s">today</b><span>${i.deadline ? "last day" : "starts"}</span>` : d === 1 ? `<b class="s">tomorrow</b><span>${i.deadline ? "last day" : "starts"}</span>` : `<b>${d}</b><span>${i.deadline ? "days left" : "days to go"}</span>`;
  return `<div class="when ${cls}"${tip}>${big}${dateLabel}</div>`;
}
const hasEligibilityProfile = () => Object.keys(S?.settings?.eligibility_profile || {}).length > 0;
const hideIneligible = () => ui.hideIneligible && hasEligibilityProfile();

function eligibilityBadge(i) {
  if (!hasEligibilityProfile()) return "";
  switch (i.eligibility?.verdict) {
    case "eligible": return '<span class="tag ok">Eligible</span>';
    case "ineligible": return '<span class="tag bad">Not eligible</span>';
    default: return '<span class="tag warn">Check</span>';
  }
}

function eligibilityDetails(i) {
  const checks = i.eligibility?.checks;
  if (!eligibilityBadge(i) || !checks?.length) return "";
  const labels = { ok: "Meets requirement", unclear: "Check", fail: "Does not meet requirement" };
  return `<details class="eligibility-details" data-key="eligibility-${esc(i.id)}"><summary>Eligibility checks</summary><ul>${checks.map((check) =>
    `<li><b>${esc(check.criterion)}</b> <span class="muted">${labels[check.result]}</span>${check.quote ? `<blockquote>${esc(check.quote)}</blockquote>` : ""}</li>`).join("")}</ul></details>`;
}

function itemCard(i, opts = {}) {
  const a = i.action, sn = isSnoozed(i), w = opts.showWatch && i.watch_id ? watchById(i.watch_id) : null;
  const who = [i.org, i.location].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("");
  const b = (act, tip, on, cls = "") => `<button class="${act === "saved" ? "btn ghost save-action" : "ib"} ${on ? "on" : ""} ${cls}" data-item="${esc(i.id)}" data-set="${act}" data-tip="${tip}" aria-label="${tip}">${act === "hidden" && on ? I.undo : I[{ saved: "save", applied: "done", hidden: "hide" }[act]]}${act === "saved" ? tip : ""}</button>`;
  const upd = isUpdated(i);
  const updLabel = i.last_change_kind === "deadline_moved" ? "Deadline moved" : i.last_change_kind === "reopened" ? "Reopened" : "Updated";
  const updTag = upd ? `<span class="tag updated">${updLabel}</span>` : "";
  return `<article class="item">${whenBox(i)}<div class="ibody">
      <div class="meta">${(opts.isNew ?? isNew(i)) ? '<span class="tag new">New</span>' : ""}${updTag}${eligibilityBadge(i)}${i.category ? `<span class="tag accent">${esc(i.category)}</span>` : ""}<span class="tag">${esc(i.source_name)}</span>${w ? `<a class="tag accent no-dec" href="#/watches/${w.id}">${esc(w.title)}</a>` : ""}${sn ? `<span class="tag warn">snoozed until ${short(i.snoozed_until)}</span>` : ""}${i.status === "gone" ? '<span class="tag bad">no longer listed</span>' : ""}</div>
      <h3><a href="${esc(safeUrl(i.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(i.title)}</a></h3>
      ${who ? `<div class="who">${who}</div>` : ""}${upd && i.change_summary ? `<div class="change-note">${esc(i.change_summary)}</div>` : ""}${i.summary ? `<p class="sum">${esc(i.summary)}</p>` : ""}${eligibilityDetails(i)}${i.note ? `<div class="note">${esc(i.note)}</div>` : ""}</div>
    <div class="acts">${b("saved", a === "saved" ? "Remove from agenda" : "Add to agenda", a === "saved")}${b("applied", a === "applied" ? "Mark as not done" : "Mark as done", a === "applied", "green")}<span class="pop-anchor pop-end"><button class="ib ${sn ? "on" : ""}" aria-expanded="false" data-pick="snooze" data-sid="${esc(i.id)}" data-tip="${sn ? "Snoozed until " + short(i.snoozed_until) : "Snooze"}" aria-label="Snooze">${I.clock}</button></span>${b("hidden", a === "hidden" ? "Unhide" : "Hide", a === "hidden")}</div></article>`;
}
function progressBox() {
  const p = S.progress; if (!p.running) return "";
  const learnActive = Math.min(p.learning_total || p.learning?.length || 0, (p.learning_done || 0) + (p.learning?.length || 0));
  const what = p.learning && p.learning.length
    ? `Learning ${learnActive} of ${p.learning_total || learnActive}: ${esc(p.learning.join(", "))}. This happens once per site.`
    : p.current.length ? `Reading ${esc(p.current.join(", "))}` : "";
  const pct = p.total ? Math.max(3, Math.round((p.done / p.total) * 100)) : 3;
  return `<div class="progress"><div class="row"><b>Scanning ${p.done} of ${p.total} sources</b><span class="grow"></span><button class="btn sm ghost" data-act="cancel">${I.stop} Stop</button></div>
    <div class="muted sub-note">${what}</div><progress class="pbar-prog" max="100" value="${pct}"></progress></div>`;
}
function openSnoozePicker(anchor) {
  closePops();
  const it = findItem(anchor.dataset.sid); if (!it) return;
  const add = (n) => { const d = today0(); d.setDate(d.getDate() + n); return iso(d); };
  const opts = [["Tomorrow", add(1)], ["In 3 days", add(3)], ["In a week", add(7)]];
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Snooze until</div>` + opts.map(([l, d]) => `<button class="o ${it.snoozed_until === d ? "on" : ""}" data-snooze-set="${d}" data-sid="${esc(it.id)}">${l}<span class="r">${short(d)}</span></button>`).join("") +
    `<hr><div class="custom"><input type="date" id="snoozePick" value="${esc(it.snoozed_until || "")}"><button class="btn sm" data-snooze-pick data-sid="${esc(it.id)}">Set</button></div>` +
    (it.snoozed_until ? `<button class="o" data-snooze-set="" data-sid="${esc(it.id)}">Unsnooze</button>` : "");
  popAnchor = anchor;
  placePop(pop, anchor);
}
async function setSnooze(id, date) {
  syncItem(id, { snoozed_until: date || null });
  closePops(); render();
  await api(`/api/items/${encodeURIComponent(id)}`, { snoozed_until: date || "" });
  toast(date ? `Snoozed until ${short(date)}` : "Unsnoozed");
  refresh(true);
}

// ---------- home ----------
function candCard(c, k, discId = null, watchId = null) {
  const have = S.sources.some((s) => s.url === c.url && (!watchId || s.watch_id === watchId));
  const attrs = discId !== null ? `data-disc-cand="${k}" data-disc-id="${discId}"` : `data-cand="${k}"`;
  return `<div class="cand"><div><h4>${esc(c.name)}</h4><div class="u"><a href="${esc(safeUrl(c.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(c.url)}</a></div><p>${esc(c.why || "")}</p>${c.sample ? `<p class="muted fs-12">Example: ${esc(c.sample)}</p>` : ""}</div>
      <button class="btn sm" ${attrs} ${have ? "disabled" : ""}>${have ? "Added" : "Add source"}</button></div>`;
}
let watchDraft = "";
function renderNewWatch() {
  const st = S.settings, d = S.discovery;
  const cands = !d.running ? (d.results || []).map((cand, k) => candCard(cand, k, null, d.watch_id)).join("") : "";
  return `<div class="page"><div class="head"><div><h1>New watch</h1><p>Lookout finds sources that match what you want to follow.</p></div></div>
    <form class="composer" id="discover">
      <label class="f" for="ask">What should Lookout watch?</label>
      <textarea name="request" id="ask" rows="4" placeholder="Machine learning internships in Berlin" ${d.running ? "disabled" : ""}>${esc(d.running ? d.request : watchDraft)}</textarea>
      <div class="row">
        <span class="pop-anchor"><button type="button" class="chip outlined" data-pick="agent" aria-expanded="false">${I.bot} ${st.agent ? esc(agentName(st.agent)) : "Choose an agent"} ${I.down}</button></span>
        ${st.agent ? `<span class="pop-anchor"><button type="button" class="chip model" data-pick="model" aria-expanded="false">${modelChipText(st.model, st.agent)} ${I.down}</button></span>` : ""}
        <span class="grow"></span><button type="submit" class="btn primary" ${d.running || !st.agent ? "disabled" : ""}>Start watch</button>
      </div>
      <a href="#/watches/sources">I have a source URL</a>
    </form>
    ${d.running && d.id ? `<div class="disc-bar"><a href="#/search/${d.id}"><i class="live"></i><span>Searching for <b>${esc(d.title || d.request)}</b></span><span class="grow"></span><span class="muted">View search →</span></a><button type="button" class="btn sm ghost" data-act="cancel-disc">${I.stop} Stop</button></div>` : ""}
    ${!d.running && d.error ? `<p class="err-note">${esc(cleanErr(d.error))}</p>` : ""}
    ${cands ? `<div class="found"><div class="h2">Suggested sources <span>${d.results.length}</span><span class="grow"></span><button class="btn sm ghost" data-act="clear-disc">Clear</button></div><div class="list">${cands}</div></div>` : ""}
  </div>`;
}

// ---------- shared feed / watch list ----------
function renderItemList(opts = {}) {
  const scope = itemsScopeOnly();
  const key = itemsScopeKey();
  if (key && feedState.key !== key) {
    Promise.resolve().then(() => loadItems(false));
  }
  const list = dedupeItems(feedState.scope === scope ? feedState.items : []);
  const facets = feedState.facets || {};
  const cats = facets.categories || {};
  const srcs = facets.sources || [];
  const updCount = facets.updated_count || 0;
  const snCount = facets.snoozed_count || 0;
  const hasFilters = Boolean(ui.cats.length || (opts.showSourceSelect && ui.src) || ui.onlyUpdated || ui.q || hideIneligible());

  let body = "";
  if (feedState.loading && !feedState.items.length) {
    body = `<div class="empty"><b>Loading items…</b></div>`;
  } else if (!list.length) {
    body = hasFilters
      ? `<div class="empty"><b>Nothing matches</b>Try changing your filters.</div>`
      : (opts.emptyHtml || `<div class="empty"><b>No open items yet</b>Run a scan to collect matching items.</div>`);
  } else if (ui.sort === "deadline") {
    let rest = list;
    for (const [name, test] of [["This week", (d) => d !== null && d <= 7], ["This month", (d) => d !== null && d <= 31], ["Later", (d) => d !== null], ["No date", () => true]]) {
      const part = rest.filter((i) => test(daysTo(target(i)))); rest = rest.filter((i) => !test(daysTo(target(i))));
      if (part.length) body += `<div class="h2">${name} <span>${part.length}</span></div><div class="list">${part.map((i) => itemCard(i, { showWatch: opts.showWatch })).join("")}</div>`;
    }
  } else body = `<div class="h2">Newest first <span>${list.length}</span></div><div class="list">${list.map((i) => itemCard(i, { showWatch: opts.showWatch })).join("")}</div>`;

  const pills = Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([c, n]) => `<button class="pill ${ui.cats.includes(c) ? "on" : ""}" data-cat="${esc(c)}">${esc(c)} <em>${n}</em></button>`).join("")
    + (updCount || ui.onlyUpdated ? `<button class="pill ${ui.onlyUpdated ? "on" : ""}" data-act="toggle-updated">Updated <em>${updCount}</em></button>` : "")
    + (snCount || ui.showSnoozed ? `<button class="pill ${ui.showSnoozed ? "on" : ""}" data-act="toggle-snoozed">Snoozed <em>${snCount}</em></button>` : "")
    + (hasEligibilityProfile() ? `<button type="button" class="pill ${hideIneligible() ? "on" : ""}" aria-pressed="${hideIneligible()}" data-act="toggle-ineligible">Hide not eligible</button>` : "");
  const srcSelect = opts.showSourceSelect ? `<select id="srcSel" aria-label="Source"><option value="">All sources</option>${srcs.map((s) => `<option ${s === ui.src ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>` : "";
  const loadMore = feedState.hasMore ? `<div class="load-more"><span class="muted">Loaded ${feedState.items.length} of ${feedState.total} matching items</span><button type="button" class="btn sm" data-act="load-more" ${feedState.loading ? "disabled" : ""}>${feedState.loading ? "Loading…" : "Load more"}</button></div>` : "";
  return `<div class="toolbar"><div class="search">${I.search}<input type="search" id="q" placeholder="Search titles, organisations, places" value="${esc(ui.q)}"></div>
      <select id="sortSel" aria-label="Sort"><option value="deadline" ${ui.sort === "deadline" ? "selected" : ""}>By date</option><option value="new" ${ui.sort === "new" ? "selected" : ""}>Newest first</option></select></div>
    ${pills || srcSelect ? `<details class="list-filters" data-key="list-filters"><summary>Filter list</summary>${srcSelect}${pills ? `<div class="pills">${pills}</div>` : ""}</details>` : ""}${body}${loadMore}`;
}

function renderOpportunities() {
  const openN = hideIneligible() ? (feedState.key === itemsScopeKey() && !feedState.loading ? feedState.total : null) : (S.counts?.feed_open ?? feedState.total);
  const lastScan = S.scans.find((scan) => scan.status !== "running");
  const emptyHtml = S.sources.length ? `<div class="empty"><b>No opportunities yet</b>Update your sources to collect matching items.</div>`
    : `<div class="empty"><b>No opportunities yet</b><div class="row"><a class="btn primary" href="#/watches/new">New watch</a><a class="btn" href="#/watches/sources">Add source</a></div></div>`;
  return `<div class="page">${renderNotifyErrorsBanner()}${progressBox()}
    <div class="head"><div><h1>Opportunities</h1><p>${openN === null ? "Loading items?" : `${openN} open item${openN === 1 ? "" : "s"}`} ?
      <a href="#/opportunities/history">${lastScan ? `Last updated ${ago(lastScan.started)}` : "Scan history"}</a></p></div>
      <div class="row"><button type="button" class="btn ghost" data-dl="/api/export.csv" data-fn="lookout.csv">${I.dl} Export CSV</button>
      <button type="button" class="btn primary" data-act="scan" ${S.progress.running || !S.sources.some((source) => source.active && !source.paused) ? "disabled" : ""}>${I.play} Update all</button></div></div>
    ${S.discovery.running && S.discovery.id ? `<p><a href="#/search/${S.discovery.id}">View current source search</a></p>` : ""}
    ${renderItemList({ showSourceSelect: true, showWatch: true, emptyHtml })}</div>`;
}

function renderSourcePreviewBox() {
  const tj = S?.test_job;
  if (!tj || (!tj.running && !tj.result)) return "";
  if (tj.running) {
    return `<div class="box pad test-preview"><div class="row"><b>Testing ${esc(tj.title || "source")}…</b><span class="grow"></span><button type="button" class="btn sm ghost" data-act="cancel-test">${I.stop} Stop</button></div>
      <p class="muted fs-13 mt-8">Running reader or quick browser preview…</p>
      <div class="pbar"><i class="indet"></i></div></div>`;
  }
  const r = tj.result || {};
  const cancelled = Boolean(r.cancelled || r.status === "cancelled" || tj.status === "cancelled");
  if (cancelled) {
    return `<div class="box pad test-preview"><div class="row"><b>Test stopped: ${esc(tj.title || "source")}</b>${r.method ? `<span class="tag accent">${esc(r.method)}</span>` : ""}<span class="tag warn">cancelled</span><span class="grow"></span><button type="button" class="btn sm ghost" data-act="close-preview">Close</button></div>
      <p class="muted fs-13 mt-8">Test stopped.</p></div>`;
  }
  const rows = (r.items || []).slice(0, 15).map((it) => `<div class="tp-row"><a href="${esc(safeUrl(it.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(it.title)}</a><div class="muted fs-12">${esc([it.org, it.location, it.deadline ? "deadline " + it.deadline : "", it.passes_filters === false ? "filtered out" : ""].filter(Boolean).join(" · "))}</div></div>`).join("");
  return `<div class="box pad test-preview"><div class="row"><b>Preview: ${esc(tj.title || "source")}</b>${r.method ? `<span class="tag accent">${esc(r.method)}</span>` : ""}${r.ok ? `<span class="tag ok">${r.count} items (${r.matching} matching)</span>` : `<span class="tag bad">failed</span>`}<span class="grow"></span><button type="button" class="btn sm ghost" data-act="close-preview">Close</button></div>
    ${r.error ? `<p class="err mt-8">${esc(cleanErr(r.error))}</p>` : ""}
    ${r.ok ? (rows ? `<div class="tp-list">${rows}</div>` : `<p class="muted fs-13 mt-8">Reader ran cleanly and found 0 items.</p>`) : ""}</div>`;
}

// ---------- watch detail ----------
function sourceEditForm(s) {
  const watchOpts = `<option value="">Independent source</option>` + (S.watches || []).map((w) => `<option value="${w.id}" ${s.watch_id === w.id ? "selected" : ""}>${esc(w.title)}</option>`).join("");
  return `<form id="editSrcForm" data-sid="${s.id}" class="inline-edit">
    <input type="text" name="name" required value="${esc(s.name)}" placeholder="Source name" aria-label="Source name">
    <input type="url" name="url" required value="${esc(s.url)}" placeholder="https://example.com/listings" aria-label="Source URL">
    <input type="text" name="notes" value="${esc(s.notes || "")}" placeholder="Notes for your agent (optional)" aria-label="Source notes">
    <select name="watch_id" aria-label="Watch">${watchOpts}</select>
    <div class="row"><span class="grow"></span><button type="button" class="btn sm ghost" data-act="cancel-edit">Cancel</button><button class="btn sm primary">Save</button></div>
  </form>`;
}
function filterEditForm(f, compact = false) {
  return `<form id="editFilterForm" data-fid="${f.id}" class="inline-edit ${compact ? "side-edit" : "span-all"}">
    <div class="row nowrap">
      <select name="kind" aria-label="Filter kind">${Object.entries(FKIND).map(([k, v]) => `<option value="${k}" ${f.kind === k ? "selected" : ""}>${v[0]}</option>`).join("")}</select>
      <input type="text" name="value" required value="${esc(f.value)}" aria-label="Filter value">
    </div>
    <div class="row"><span class="grow"></span><button type="button" class="btn sm ghost" data-act="cancel-edit">Cancel</button><button class="btn sm primary">Save</button></div>
  </form>`;
}
function renderWatch() {
  const w = watchById(route.id);
  if (!w) return `<div class="page"><div class="empty"><b>This watch no longer exists</b><div class="row"><a class="btn sm" href="#/watches">Watches</a></div></div></div>`;
  const wSources = S.sources.filter((s) => s.watch_id === w.id);
  const wFilters = S.filters.filter((f) => f.watch_id === w.id);
  const globalFCount = S.filters.filter((f) => !f.watch_id && f.active).length;
  const dRunning = S.discovery.running && S.discovery.watch_id === w.id;
  const discProg = dRunning ? `<div class="progress"><div class="row"><b>Finding sources and filters…</b><span class="grow"></span><a class="btn sm ghost" href="#/search/${S.discovery.id}">Details</a><button class="btn sm ghost" data-act="cancel-disc">${I.stop} Stop</button></div>
    <div class="muted sub-note">Your agent is looking for pages and turning your request into filters. Found ${wSources.length} source${wSources.length === 1 ? "" : "s"} so far.</div><div class="pbar"><i class="indet"></i></div></div>` : "";
  const emptyHtml = !wSources.length ? `<div class="empty"><b>No sources in this watch yet</b>Add a source on the right to start monitoring.<div class="row"><a class="btn sm" href="#/watches/new">Search with AI</a></div></div>`
    : `<div class="empty"><b>No matching items yet</b>Scan this watch to check its sources.<div class="row"><button class="btn sm primary" data-watch-scan="${w.id}" ${S.progress.running ? "disabled" : ""}>${I.play} Scan watch</button></div></div>`;
  const srows = wSources.map(sourceRow).join("") || `<div class="wcard-note">No sources yet.</div>`;
  const frows = wFilters.map((f) => {
    if (editingFilterId === f.id) return filterEditForm(f, true);
    return `<div class="wrow ${f.active ? "" : "off"}"><label class="switch" title="Use this filter"><input type="checkbox" data-f-active="${f.id}" ${f.active ? "checked" : ""}><i></i></label>
      <div class="wt"><b>${esc(f.value)}${f.kind === "deadline_within" ? " days" : ""}</b><small>${esc(FKIND[f.kind]?.[0] || f.kind)}</small></div>
      <div class="row row-tight"><button class="ib" data-f-edit="${f.id}" data-tip="Edit filter" aria-label="Edit filter">${I.edit}</button><button class="ib" data-f-del="${f.id}" data-tip="Delete filter" aria-label="Delete filter">${I.trash}</button></div></div>`;
  }).join("") || `<div class="wcard-note">No watch filters yet.</div>`;
  const rssUrl = `${location.origin}/api/watches/${w.id}/feed.xml`;
  const editWatchBlock = editingWatchId === w.id ? `<form id="editWatchForm" data-wid="${w.id}" class="inline-edit mt-12">
    <label class="f">Watch title<input type="text" name="title" required value="${esc(w.title)}"></label>
    <label class="f">Search prompt<textarea name="request" class="ta-sm">${esc(w.request || "")}</textarea></label>
    <div class="row"><button type="button" class="btn sm" data-watch-rediscover="${w.id}" ${dRunning || !S.settings.agent ? "disabled" : ""}>${I.search} Save &amp; search again</button><span class="grow"></span><button type="button" class="btn sm ghost" data-act="cancel-edit">Cancel</button><button class="btn sm primary">Save</button></div>
  </form>` : "";
  return `<div class="page">${discProg}${progressBox()}
    <div class="head"><div class="minw-0"><h1>${esc(w.title)}</h1>${w.request ? `<p>“${esc(w.request)}”</p>` : ""}
        ${editWatchBlock}
        <div class="rss-bar"><span>RSS:</span><button type="button" class="btn sm ghost" data-dl="/api/watches/${w.id}/feed.xml" data-fn="watch-${w.id}.xml">Download RSS</button><button type="button" class="btn sm ghost" data-dl="/api/watches/${w.id}/atom.xml" data-fn="watch-${w.id}-atom.xml">Atom</button><button type="button" class="btn sm ghost" data-copy="${esc(rssUrl)}">Copy URL</button><span>${S.settings.rss_public === "1" ? "Localhost without cookie (127.0.0.1 only)" : "Requires session cookie (enable unauthenticated localhost RSS in Settings)"}</span></div></div>
      <div class="row"><button class="btn sm ghost" data-watch-edit="${w.id}">${I.edit} Edit</button>
        ${w.request ? `<button class="btn sm ghost" data-watch-rediscover="${w.id}" ${dRunning || !S.settings.agent ? "disabled" : ""}>${I.search} Search again</button>` : ""}
        <button class="btn sm" data-watch-scan="${w.id}" ${S.progress.running ? "disabled" : ""}>${I.play} Scan watch</button>
        <button class="btn sm ghost danger" data-watch-del="${w.id}">${I.trash} Delete</button></div></div>
    ${renderSourcePreviewBox()}
    <div class="watch-layout">
      <div>${renderItemList({ showSourceSelect: false, showWatch: false, emptyHtml })}</div>
      <aside class="watch-side" aria-labelledby="watch-settings"><h2 id="watch-settings">Watch settings</h2>
        <div class="wcard"><div class="wcard-h">Sources <span>${wSources.length}</span></div>${srows}
          <form id="addWatchSrc" class="wform" data-watch-id="${w.id}">
            <input type="text" name="name" required placeholder="Source name">
            <input type="url" name="url" required placeholder="https://example.com/listings">
            <div class="row"><input type="text" name="notes" class="grow" placeholder="Notes (optional)"><button type="button" class="btn sm ghost" data-act="test-new-source">Test</button><button class="btn sm primary">Add</button></div>
          </form></div>
        <div class="wcard"><div class="wcard-h">Filters <span>${wFilters.length}</span></div>${frows}
          <form id="addWatchF" class="wform" data-watch-id="${w.id}">
            <select name="kind" id="wfKind">${Object.entries(FKIND).map(([k, v]) => `<option value="${k}">${v[0]}</option>`).join("")}</select>
            <div class="row"><input type="text" name="value" id="wfVal" class="grow" required placeholder="${FKIND.include[2]}"><button class="btn sm primary">Add</button></div>
          </form>
          <div class="wcard-note">Also applies ${globalFCount} global filter${globalFCount === 1 ? "" : "s"} · <a href="#/watches/rules">Rules for all watches</a></div>
        </div>
      </aside>
    </div></div>`;
}

// ---------- agenda ----------
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
ui.agView ??= "upcoming";
let agOpen = null, agDay = null;
function agendaEntries() {
  const out = [];
  for (const i of agendaBase().filter((x) => x.action === "saved" || x.action === "applied")) {
    if (i.deadline) out.push({ i, date: i.deadline, kind: "dl" });
    if (i.remind_on) out.push({ i, date: i.remind_on, kind: "rm" });
  }
  return out;
}
function leftText(i) {
  const d = daysTo(i.deadline); if (d === null) return { t: "", c: "" };
  if (d < 0) return { t: `closed ${-d} day${d === -1 ? "" : "s"} ago`, c: "" };
  if (d === 0) return { t: "closes today", c: "red" };
  return { t: `${d} day${d === 1 ? "" : "s"} left`, c: d <= 3 ? "red" : d <= 7 ? "orange" : "" };
}
function agRow(i) {
  const when = i.remind_on && (!i.deadline || i.remind_on < i.deadline) && daysTo(i.remind_on) >= 0 ? i.remind_on : i.deadline;
  const dt = when ? parseIso(when) : null, left = leftText(i), done = i.action === "applied", open = agOpen === i.id;
  const deadline = deadlineLabel(i, i.deadline ? `deadline ${short(i.deadline)}` : "deadline text");
  return `<div class="ag-row ${open ? "open" : ""}">
    <div class="ag-date ${dt ? "" : "none"}">${dt ? `<b>${dt.getDate()}</b><span>${WD[dt.getDay()]} ${MON[dt.getMonth()]}</span>` : "<b>—</b><span>no date</span>"}</div>
    <button class="ag-main" data-ag-open="${esc(i.id)}" aria-expanded="${open}"><span class="t">${esc(i.title)}</span>
      <span class="s">${i.org ? `<span>${esc(i.org)}</span>` : ""}<span>${esc(i.source_name)}</span>${eligibilityBadge(i)}${deadline ? `<span>${deadline}</span>` : ""}${i.remind_on ? `<span class="rem">reminder ${short(i.remind_on)}</span>` : ""}${i.note && !open ? `<span>has notes</span>` : ""}</span></button>
    <div class="ag-left ${left.c}">${done ? "done" : left.t}</div>
    <div class="ag-acts"><span class="pop-anchor pop-end"><button class="ib" aria-expanded="false" data-pick="remind" data-rid="${esc(i.id)}" data-tip="Set a reminder" aria-label="Set a reminder"><svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10.5 20a1.5 1.5 0 0 0 3 0"/></svg></button></span>
      <button class="ib ${done ? "on green" : ""}" data-item="${esc(i.id)}" data-set="applied" data-tip="${done ? "Mark as not done" : "Mark as done"}" aria-label="${done ? "Mark as not done" : "Mark as done"}">${I.done}</button>
      <button class="ib" data-item="${esc(i.id)}" data-set="${done ? "applied" : "saved"}" data-unsave="1" data-tip="Remove from agenda" aria-label="Remove from agenda">${I.trash}</button></div>
    ${open ? `<div class="ag-detail">${i.deadline_raw ? `<div class="deadline-text">Deadline text: ${esc(i.deadline_raw)}</div>` : ""}<textarea data-note="${esc(i.id)}" placeholder="Notes: what to prepare, who to contact, links…">${esc(i.note || "")}</textarea>
      <div class="links"><a class="btn sm" href="${esc(safeUrl(i.url))}" data-ext target="_blank" rel="noopener noreferrer">Open page</a>${i.summary ? `<span class="muted ag-sum">${esc(i.summary)}</span>` : ""}</div>${eligibilityDetails(i)}</div>` : ""}
  </div>`;
}
function renderUpcoming() {
  const saved = agendaBase().filter((i) => i.action === "saved"), done = agendaBase().filter((i) => i.action === "applied");
  const key = (i) => { const r = i.remind_on && daysTo(i.remind_on) >= 0 ? i.remind_on : null; const c = [r, i.deadline].filter(Boolean).sort()[0]; return c || null; };
  const groups = [["Overdue", (d) => d !== null && d < 0, "overdue"], ["Today", (d) => d === 0], ["Tomorrow", (d) => d === 1], ["This week", (d) => d !== null && d <= 7], ["Next week", (d) => d !== null && d <= 14], ["Later", (d) => d !== null], ["No date", () => true]];
  let rest = [...saved].sort((a, b) => (key(a) || "9999").localeCompare(key(b) || "9999")), html = "";
  for (const [name, test, cls] of groups) {
    const part = rest.filter((i) => test(daysTo(key(i)))); rest = rest.filter((i) => !test(daysTo(key(i))));
    if (part.length) html += `<section class="ag-group ${cls || ""}"><h3>${name} <span>${part.length}</span></h3><div class="ag-rows">${part.map(agRow).join("")}</div></section>`;
  }
  if (!saved.length) html = `<div class="empty mt-22"><b>Nothing in your agenda</b>Use the bookmark on any item in Opportunities to keep track of it here.<div class="row"><a class="btn sm" href="#/opportunities">Browse opportunities</a></div></div>`;
  if (done.length) html += `<details class="ag-done" data-key="ag-done"><summary>Applied (${done.length})</summary><div class="ag-rows mt-8">${done.map(agRow).join("")}</div></details>`;
  return html;
}
function renderMonth() {
  const m = ui.calMonth ? parseIso(ui.calMonth + "-01") : new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  const first = new Date(m.getFullYear(), m.getMonth(), 1), start = new Date(first); start.setDate(1 - ((first.getDay() + 6) % 7));
  const byDay = {}; for (const e of agendaEntries()) (byDay[e.date] ||= []).push(e);
  const sel = agDay || iso(today0());
  let cells = DOW.map((d) => `<div class="dow">${d}</div>`).join("");
  for (let k = 0; k < 42; k++) {
    const d = new Date(start); d.setDate(start.getDate() + k); const key = iso(d), evs = byDay[key] || [];
    const chips = evs.slice(0, 3).map((e) => `<span class="ev ${e.kind} ${e.i.action === "applied" ? "done" : ""}" title="${esc((e.kind === "dl" ? "Deadline: " : "Reminder: ") + e.i.title)}"><span>${esc(e.i.title)}</span></span>`).join("") + (evs.length > 3 ? `<span class="ev more">+${evs.length - 3} more</span>` : "");
    cells += `<button class="cell ${d.getMonth() !== m.getMonth() ? "out" : ""} ${key === iso(today0()) ? "today" : ""} ${key === sel ? "sel" : ""} ${k % 7 > 4 ? "we" : ""}" data-day="${key}"><span class="d">${d.getDate()}</span>${chips}</button>`;
  }
  const dayEvs = byDay[sel] || [], sd = parseIso(sel);
  const upcoming = agendaEntries().filter((e) => e.date > sel && e.i.action === "saved").sort((a, b) => a.date.localeCompare(b.date)).slice(0, 4);
  const entry = (e, showDate) => {
    const dateLabel = showDate ? short(e.date) + ", " : "";
    const label = e.kind === "dl" ? deadlineLabel(e.i, dateLabel + "deadline") : dateLabel + "your reminder";
    return `<div class="entry"><a href="${esc(safeUrl(e.i.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(e.i.title)}</a><div class="k ${e.kind}">${label}${e.i.org ? " · " + esc(e.i.org) : ""}</div></div>`;
  };
  return `<div class="month"><div><div class="box"><div class="cal-head"><h2>${MON[m.getMonth()]} ${m.getFullYear()}</h2>
      <button class="btn sm ghost" data-cal="-1" aria-label="Previous month">‹</button><button class="btn sm" data-cal="0">Today</button><button class="btn sm ghost" data-cal="1" aria-label="Next month">›</button></div>
      <div class="cal">${cells}</div></div><div class="legend"><span><i class="dl"></i>Deadline</span><span><i></i>Your reminder</span></div></div>
    <div class="box dayp"><h3>${WD[sd.getDay()]}, ${sd.getDate()} ${MON[sd.getMonth()]}</h3><div class="sub">${dayEvs.length ? `${dayEvs.length} thing${dayEvs.length > 1 ? "s" : ""} on this day` : "Nothing on this day"}</div>
      ${dayEvs.map((e) => entry(e, false)).join("")}
      ${upcoming.length ? `<div class="sub mt-18">Coming up after this</div>${upcoming.map((e) => entry(e, true)).join("")}` : ""}</div></div>`;
}
function renderAgenda() {
  const n = agendaBase().filter((i) => i.action === "saved").length;
  return `<div class="page"><div class="head"><div><h1>Agenda</h1><p>${n ? `${n} item${n > 1 ? "s" : ""} you are tracking.` : "Items you save from Opportunities."} Deadlines come from the site; reminders are dates you pick.</p></div>
      <div class="row"><div class="seg"><button data-agview="upcoming" class="${ui.agView === "upcoming" ? "on" : ""}">Upcoming</button><button data-agview="month" class="${ui.agView === "month" ? "on" : ""}">Month</button></div>
      <button type="button" class="btn sm ghost" data-dl="/api/agenda.ics" data-fn="lookout-agenda.ics" title="Import into Google Calendar, Outlook or Apple Calendar">${I.dl} Add to calendar</button></div></div>
    ${ui.agView === "month" ? renderMonth() : renderUpcoming()}</div>`;
}
function openRemindPicker(anchor) {
  closePops();
  const it = findItem(anchor.dataset.rid); if (!it) return;
  const add = (n) => { const d = today0(); d.setDate(d.getDate() + n); return iso(d); };
  const opts = [["Tomorrow", add(1)], ["In 3 days", add(3)], ["In a week", add(7)]];
  if (it.deadline && daysTo(it.deadline) > 2) opts.push(["2 days before the deadline", iso(new Date(parseIso(it.deadline) - 2 * 864e5))]);
  if (it.deadline && daysTo(it.deadline) > 7) opts.push(["A week before the deadline", iso(new Date(parseIso(it.deadline) - 7 * 864e5))]);
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Remind me</div>` + opts.map(([l, d]) => `<button class="o ${it.remind_on === d ? "on" : ""}" data-remind-set="${d}" data-rid="${esc(it.id)}">${l}<span class="r">${short(d)}</span></button>`).join("") +
    `<hr><div class="custom"><input type="date" id="remindPick" value="${esc(it.remind_on || "")}"><button class="btn sm" data-remind-pick data-rid="${esc(it.id)}">Set</button></div>` +
    (it.remind_on ? `<button class="o" data-remind-set="" data-rid="${esc(it.id)}">Remove reminder</button>` : "");
  popAnchor = anchor;
  placePop(pop, anchor);
}
async function setReminder(id, date) { syncItem(id, { remind_on: date || null }); closePops(); render(); await api(`/api/items/${encodeURIComponent(id)}`, { remind_on: date || "" }); toast(date ? `Reminder set for ${short(date)}` : "Reminder removed"); }

// ---------- sources ----------
async function fetchSourceCode(sid) {
  if (sourceCodeCache[sid] !== undefined || sourceCodeLoading[sid]) return;
  sourceCodeLoading[sid] = true;
  try {
    const res = await api(`/api/sources/${sid}/code`);
    sourceCodeCache[sid] = res.extractor || res.last_good_extractor || "";
    if (route.view === "sources" || route.view === "watch") render();
  } catch (err) {
    toast(err.message || "Could not load reader code");
  } finally {
    delete sourceCodeLoading[sid];
  }
}

function sourceRow(s) {
  const l = s.last, agent = s.method !== "plugin";
  const status = !l ? '<span class="tag">not scanned yet</span>' : l.ok ? `<span class="tag ok">${l.count} items</span>` : l.error === "cancelled" ? '<span class="tag muted">cancelled</span>' : '<span class="tag bad">failed</span>';
  const learn = !agent ? '<span class="tag">plugin</span>' : s.extractor_ok ? '<span class="tag accent" title="Read with code your agent wrote: seconds, no agent quota">learned</span>' : s.has_extractor ? '<span class="tag bad" title="The site changed; your agent will fix the reader on the next scan">needs relearning</span>' : '<span class="tag" title="On the first scan your agent browses the site and writes a reader for it">not learned yet</span>';
  const pausedBadge = s.paused ? `<span class="tag bad">paused</span>` : "";
  const blockedBadge = !s.paused && s.last_error_kind === "blocked" ? `<span class="tag bad">blocked</span>` : "";
  const learnCooldown = !s.paused && s.learn_retry_after ? `<span class="tag warn">relearn cooldown until ${esc(s.learn_retry_after.slice(11, 16))}</span>` : "";
  const backoff = !s.paused && s.last_error_kind === "transient" && s.retry_after ? `<span class="tag warn">retry after ${esc(s.retry_after.slice(11, 16))}</span>` : "";
  const filterChangedBadge = s.filters_changed && s.has_extractor ? `<span class="tag warn">filters changed</span>` : "";
  const cachedCode = sourceCodeCache[s.id];
  const codeBlock = s.has_extractor ? `<details data-key="src-${s.id}" data-code-id="${s.id}"><summary>View the reader your agent wrote${s.extractor_updated ? ", " + short(s.extractor_updated.slice(0, 10)) : ""}${s.extractor_runs ? `, used ${s.extractor_runs} time${s.extractor_runs > 1 ? "s" : ""}` : ""}</summary>${cachedCode !== undefined ? `<div class="code">${esc(cachedCode)}</div>` : `<div class="muted fs-125 mt-8">Loading reader code…</div>`}</details>` : "";
  const editBlock = editingSourceId === s.id ? sourceEditForm(s) : "";
  return `<div class="srow"><label class="switch" title="Watch this source"><input type="checkbox" data-src-active="${s.id}" ${s.active ? "checked" : ""}><i></i></label>
    <div class="minw-0"><h4>${esc(s.name)} ${learn} ${status} ${pausedBadge} ${blockedBadge} ${learnCooldown} ${backoff} ${filterChangedBadge}</h4><div class="u">${esc(s.url)}</div>
      ${s.notes && agent ? `<div class="n">${esc(s.notes)}</div>` : ""}${l && !l.ok && l.error !== "cancelled" ? `<div class="err">${esc(cleanErr(l.error))}</div>` : ""}${s.extractor_error && agent && (!s.extractor_ok || s.last_error_kind === "transient" || s.last_error_kind === "blocked" || s.paused) ? `<div class="err">${esc(cleanErr(s.extractor_error))}</div>` : ""}
      ${codeBlock}${editBlock}</div>
    <div class="row">${s.paused ? `<button type="button" class="btn sm primary" data-src-resume="${s.id}">Resume</button>` : ""}<button type="button" class="btn sm ghost" data-src-test="${s.id}">Test</button>${agent ? `<button type="button" class="btn sm ghost" data-src-edit="${s.id}">Edit</button>${s.has_extractor ? `<button type="button" class="btn sm ghost" data-src-relearn="${s.id}">Relearn</button>` : ""}` : ""}
      <button class="btn sm" data-src-scan="${s.id}" ${S.progress.running ? "disabled" : ""}>${I.play} Scan</button><button class="btn sm ghost danger" data-src-del="${s.id}" aria-label="Delete source">${I.trash}</button></div></div>`;
}
function renderSources() {
  const globalS = S.sources.filter((s) => !s.watch_id);
  return `<div class="page"><div class="head"><div><h1>Independent sources</h1><p>Websites Lookout watches. On the first scan your agent browses each one and writes a small reader for it. After that, a scan takes seconds.</p></div>
      <a class="btn sm ghost" href="#/watches/new">Find sources with AI</a></div>
    <div class="box pad"><form id="addSrc" class="grid2">
      <label class="f">Name<input type="text" name="name" required placeholder="Acme careers"></label>
      <label class="f">Address of the page that lists the items<input type="url" name="url" required placeholder="https://"></label>
      <label class="f span-all">Notes for your agent <small>optional: which tab or filter to use, where the dates are</small><textarea name="notes" class="ta-sm" placeholder="Use the Internships filter. Deadlines are on each detail page."></textarea></label>
      <div class="row span-all"><button type="button" class="btn ghost" data-act="test-new-source">Test this source</button><span class="grow"></span><button class="btn primary">Add source</button></div></form></div>
    ${renderSourcePreviewBox()}
    <div class="h2">Sources <span>${globalS.length}</span></div>
    ${globalS.length ? `<div class="box">${globalS.map(sourceRow).join("")}</div>` : `<div class="empty"><b>No independent sources yet</b>Add a source using the form above.</div>`}</div>`;
}

// ---------- filters ----------
const FKIND = {
  include: ["Must match", "Topics or keywords the item must be about", "machine learning, backend, internship"],
  exclude: ["Exclude", "Drop items that mention any of these, separated by commas", "senior, sales, unpaid"],
  type: ["Kinds of items", "What counts as a result", "internships, hackathons, graduate programmes"],
  location: ["Location", "Where it must be", "Istanbul or remote"],
  deadline_within: ["Deadline within", "Only items closing in the next N days", "30"],
  instruction: ["About you", "Anything else your agent should know", "I am a third-year computer engineering student"],
};
const FLABEL = { include: "Must be about / mention", exclude: "Exclude anything about", type: "Only these kinds", location: "Location must be", deadline_within: "Deadline or date within the next N days", instruction: "Extra instruction" };
const filterText = (fs) => (fs.length ? fs.map((f) => `- ${FLABEL[f.kind]}: ${f.value}`).join("\n") : "- No filters: collect every listed item.");
function renderFilters() {
  const globalF = S.filters.filter((f) => !f.watch_id);
  const rows = globalF.map((f) => {
    if (editingFilterId === f.id) return `<div class="frow">${filterEditForm(f, false)}</div>`;
    return `<div class="frow ${f.active ? "" : "off"}"><label class="switch" title="Use this filter"><input type="checkbox" data-f-active="${f.id}" ${f.active ? "checked" : ""}><i></i></label>
      <span class="tag ${f.kind === "exclude" ? "bad" : "accent"}">${FKIND[f.kind][0]}</span><span class="v">${esc(f.value)}${f.kind === "deadline_within" ? " days" : ""}</span>
      <div class="row row-tight"><button class="btn sm ghost" data-f-edit="${f.id}" aria-label="Edit filter">${I.edit}</button><button class="btn sm ghost danger" data-f-del="${f.id}" aria-label="Delete filter">${I.trash}</button></div></div>`;
  }).join("");
  const watchSummary = (S.watches || []).filter((w) => S.filters.some((f) => f.watch_id === w.id)).map((w) => {
    const n = S.filters.filter((f) => f.watch_id === w.id && f.active).length;
    return `<div class="frow"><span class="tag accent">Watch</span><span></span><a href="#/watches/${w.id}" class="link-bold">${esc(w.title)}</a><span class="muted">${n} active</span></div>`;
  }).join("");
  return `<div class="page"><div class="head"><div><h1>Rules for all watches</h1><p>Global filters apply to every watch and global source. Editing filters re-evaluates deterministic exclusions/deadlines immediately and queues semantic filters for the next scan.</p></div></div>
    <div class="box pad"><form id="addF" class="row align-end">
      <label class="f w-190">Type<select name="kind" id="fKind">${Object.entries(FKIND).map(([k, v]) => `<option value="${k}">${v[0]}</option>`).join("")}</select></label>
      <label class="f grow"><span id="fHint">${FKIND.include[1]}</span><input type="text" name="value" id="fVal" required placeholder="${FKIND.include[2]}"></label>
      <button class="btn primary">Add filter</button></form></div>
    <div class="h2">Global filters <span>${globalF.length}</span></div>
    ${globalF.length ? `<div class="box">${rows}</div>` : `<div class="empty"><b>No global filters</b>Global filters apply everywhere; each watch also keeps its own filters.</div>`}
    <div class="h2">What your agent is told (global)</div><div class="preview">${esc(filterText(globalF.filter((f) => f.active)))}</div>
    ${watchSummary ? `<div class="h2">Watch-specific filters</div><div class="box">${watchSummary}</div>` : ""}</div>`;
}

// ---------- settings ----------
function eligibilityProfileForm(profile) {
  const val = (name) => esc(profile[name] || "");
  const select = (label, name, options) => `<label class="f">${label}<select name="${name}">
    <option value="">Not specified</option>${options.map(([value, text]) =>
    `<option value="${value}" ${profile[name] === value ? "selected" : ""}>${text}</option>`).join("")}</select></label>`;
  const yesNo = [["yes", "Yes"], ["no", "No"]];
  const years = ["1", "2", "3", "4", "5", "6"].map((n) => [n, `Year ${n}`]);
  const levels = ["A1", "A2", "B1", "B2", "C1", "C2"].map((n) => [n, n]);
  return `<div class="h2">Eligibility profile</div><div class="box pad">
    <p class="muted fs-13">All fields are optional. Lookout checks listings during scans and shares this profile with your selected agent for those checks.</p>
    <form id="eligibilityForm" class="grid2 mt-12">
      ${select("Class year", "class_year", [...years, ["final", "Final year"], ["graduate", "Graduated"]])}
      <label class="f">Graduation date (actual or expected)<input type="date" name="graduation_date" value="${val("graduation_date")}"></label>
      ${select("Mandatory internship (zorunlu staj)", "mandatory_internship", yesNo)}
      ${select("School covers insurance (SGK)", "school_insurance", yesNo)}
      <label class="f">Available duration (weeks)<input type="number" min="0" max="9999" name="duration_weeks" value="${val("duration_weeks")}"></label>
      <label class="f">Available period<input name="period" maxlength="1000" value="${val("period")}" placeholder="e.g. June to August"></label>
      <label class="f">Cities you can work in<input name="cities" maxlength="1000" value="${val("cities")}" placeholder="e.g. Istanbul, Ankara"></label>
      ${select("Can work remotely", "remote_ok", yesNo)}
      ${select("English level (CEFR)", "english_level", [["none", "None"], ...levels])}
      <label class="f">Field or department<input name="department" maxlength="1000" value="${val("department")}"></label>
      <label class="f span-all">Citizenship or work permit<input name="work_permit" maxlength="1000" value="${val("work_permit")}"></label>
      <label class="f span-all">Other<textarea name="other" class="ta-sm" maxlength="1000">${val("other")}</textarea></label>
      <div class="row span-all"><span class="grow"></span><button type="submit" class="btn primary">Save profile</button></div>
    </form></div>`;
}

function renderSettings() {
  const profileForm = eligibilityProfileForm(S.settings.eligibility_profile || {});
  if (!AG) { ensureAgents().then(render); return `<div class="page"><div class="head"><div><h1>Settings</h1><p>Looking for AI agents on this computer…</p></div></div>${profileForm}</div>`; }
  const st = S.settings, cur = st.agent, sch = S.schedule;
  const usage = (u) => !u.length ? "Not used by Lookout yet" : u.slice(0, 3).map((m) => `${esc(m.model || "default model")}: ${m.today} today, ${m.calls} this week` + (m.blocked_until ? ` · <span class="bad">quota used up until ${m.blocked_until.slice(11, 16)}</span>` : "")).join("<br>");
  const agents = AG.agents.map((a) => `<label class="agent ${a.installed || a.configured ? "" : "off"} ${cur === a.id ? "on" : ""}">
      <input type="radio" name="agent" value="${a.id}" ${cur === a.id ? "checked" : ""} ${a.installed || a.configured ? "" : "disabled"}>
      <div><b>${esc(a.name)}</b><span class="v">${a.id === "custom" ? (a.configured ? (a.installed ? "configured" : "executable not found") : "local config only") : a.installed ? (a.version && a.version !== "?" ? "v" + esc(a.version) : "installed") : "not installed"}</span>${a.verified || a.id === "custom" || !a.installed ? "" : ' <span class="tag" title="Command-line flags not yet tested on a real install">experimental</span>'}
        ${a.id === "custom" ? `<small>Configured locally for security (not editable via web UI): place your template in <code>${esc(a.config_path || "data/agent_command.txt")}</code> or start Lookout with <code>--custom-command "..."</code>.</small>` : ""}
        ${a.installed && a.id !== "custom" ? `<div class="usage">${usage(a.usage)}</div>` : ""}</div></label>`).join("");
  const snipHtml = schedPreview ? `<div class="sched-snippets">
    <div><small class="muted">Cron (Linux / macOS)</small><pre>${esc(schedPreview.cron || schedPreview.crontab || "")}</pre></div>
    <div><small class="muted">systemd --user (~/.config/systemd/user/lookout-scan.service)</small><pre>${esc(schedPreview.systemd_service || "")}</pre></div>
    <div><small class="muted">systemd --user (~/.config/systemd/user/lookout-scan.timer)</small><pre>${esc(schedPreview.systemd_timer || "")}</pre></div>
    <div><small class="muted">launchd (~/Library/LaunchAgents/com.lookout.dailyscan.plist)</small><pre>${esc(schedPreview.launchd_plist || "")}</pre></div>
    ${schedPreview.test_command ? `<div><small class="muted">Self-test command</small><pre>${esc(schedPreview.test_command)}</pre></div>` : ""}
  </div>` : "";
  return `<div class="page">${renderNotifyErrorsBanner()}<div class="head"><div><h1>Settings</h1><p>Settings are saved on this computer.</p></div></div>
    <div class="h2">Appearance</div><div class="box pad">
      <label class="f">Theme<select id="themeSel">${themes.map((theme) => `<option value="${theme}" ${theme === ui.theme ? "selected" : ""}>${theme[0].toUpperCase() + theme.slice(1)}</option>`).join("")}</select></label>
    </div>
    ${profileForm}
    ${AG.browser.installed ? "" : `<div class="banner">${I.warn}<div><b>agent-browser is missing</b><div class="muted fs-13 mt-8">Agents use it to drive a real browser. Install it once in a terminal:</div><div class="cmd mt-8">${esc(AG.browser.install)}</div></div></div>`}
    <div class="h2">AI agent</div><div class="box">${agents}</div>
    <div class="h2">Advanced</div><div class="box pad"><div class="kv">
      <div class="f">Model<div class="pop-anchor">${cur ? `<button type="button" class="chip model" data-pick="model" aria-expanded="false">${modelChipText(st.model, cur)} ${I.down}</button>` : '<span class="muted">Choose an agent first</span>'}</div></div>
      <label class="f">Minutes your agent may spend on one site<input type="number" id="timeoutIn" min="3" max="60" value="${esc(st.agent_timeout_min)}"></label>
      <label class="check"><input type="checkbox" id="headedIn" ${st.headed === "1" ? "checked" : ""}> Show the browser while your agent works</label>
      <label class="f">Sites your agent works on at once<input type="number" id="parallelIn" min="1" max="8" value="${esc(st.parallel)}"></label>
      <label class="check span-all"><input type="checkbox" id="verifyIn" ${st.verify_weekly === "1" ? "checked" : ""}> Once a week, check learned sites and relearn only if results look anomalous</label>
    </div><p class="muted fs-125 mt-12">Isolation: Codex runs with its workspace-write sandbox; Gemini uses its CLI sandbox only when <code>--help</code> advertises it. Claude restricts tools per task but has no OS sandbox. Antigravity, opencode, Cursor Agent, Qwen, Copilot and custom commands are not sandboxed by Lookout. Agent tasks run from a temporary folder with secret-like environment variables removed, except known login variables for the selected CLI.</p>
    <p class="muted fs-125 mt-12">Agent tools don't report how much quota is left. Lookout counts the calls it makes, remembers the last quota error and skips an agent until its quota resets.</p>
    <div class="row mt-12"><span class="grow"></span><button class="btn primary" data-act="save-agent">Save</button></div></div>
    <div class="h2">Schedule and notifications</div><div class="box pad">
      <div class="setrow"><div><b>Daily scan</b><small>${sch.supported ? `Runs in the background even when Lookout is closed${sch.backend ? ` (${esc(sch.backend)})` : ""}.` : esc(sch.hint || "Manual cron/systemd/launchd setup available below.")}</small></div>
        <div class="row"><input type="time" id="schTime" class="w-110" value="${esc(st.schedule_time)}" aria-label="Time">${sch.supported ? `<label class="switch"><input type="checkbox" id="schOn" ${sch.enabled ? "checked" : ""}><i></i></label>` : ""}${sch.supported && sch.enabled ? `<button type="button" class="btn sm ghost" data-act="sched-test">Run now</button>` : ""}<button type="button" class="btn sm ghost" data-act="sched-preview">${schedPreview ? "Hide configs" : "Cron / systemd / launchd"}</button></div></div>
      ${snipHtml}
      <div class="setrow"><div><b>Desktop notification</b><small>When a scan finds new or updated items.</small></div><label class="switch"><input type="checkbox" id="notifyIn" ${st.notify === "1" ? "checked" : ""}><i></i></label></div>
      <div class="setrow"><div><b>Unauthenticated localhost RSS/Atom feeds</b><small>Allow local feed readers on this computer (127.0.0.1) to fetch /api/watches/&lt;id&gt;/feed.xml without a session cookie while Lookout is running (default off).</small></div><label class="switch"><input type="checkbox" id="rssPublicIn" ${st.rss_public === "1" ? "checked" : ""}><i></i></label></div></div>
    <div class="h2">Webhook notification <span>optional (JSON / Slack / Discord / Telegram / ntfy)</span></div><div class="box pad"><div class="kv">
      <label class="f span-all">Webhook or ntfy topic URL<input type="url" id="webhookUrl" value="${esc(st.webhook_url || "")}" placeholder="https://ntfy.sh/my-lookout-alerts"></label>
      <label class="f">Payload format<select id="webhookFormat">
        <option value="auto" ${(st.webhook_format || "auto") === "auto" ? "selected" : ""}>Auto-detect from URL</option>
        <option value="json" ${st.webhook_format === "json" ? "selected" : ""}>Generic JSON</option>
        <option value="slack" ${st.webhook_format === "slack" ? "selected" : ""}>Slack</option>
        <option value="discord" ${st.webhook_format === "discord" ? "selected" : ""}>Discord</option>
        <option value="telegram" ${st.webhook_format === "telegram" ? "selected" : ""}>Telegram Bot API</option>
        <option value="ntfy" ${st.webhook_format === "ntfy" ? "selected" : ""}>ntfy</option>
      </select></label>
      <label class="f">Telegram chat ID <small>only needed for Telegram webhooks</small><input type="text" id="telegramChatId" value="${esc(st.telegram_chat_id || "")}" placeholder="123456789 or @channel"></label>
      <label class="check span-all"><input type="checkbox" id="webhookLocalIn" ${st.webhook_allow_local === "1" ? "checked" : ""}> Allow local / private network webhook targets</label>
    </div><div class="row mt-12"><span class="grow"></span><button class="btn" data-act="webhook-test">Send test webhook</button><button class="btn primary" data-act="save-webhook">Save</button></div></div>
    <div class="h2">E-mail digest <span>optional</span></div><div class="box pad"><div class="kv">
      <label class="f">Send to <small>comma-separated for multiple</small><input type="text" id="mailTo" value="${esc(st.mail_to)}" placeholder="you@example.com, team@example.com"></label>
      <label class="f">From address <small>optional (defaults to SMTP user)</small><input type="email" id="mailFrom" value="${esc(st.mail_from || "")}" placeholder="alerts@example.com"></label>
      <label class="f">SMTP user<input type="email" id="smtpUser" value="${esc(st.smtp_user)}" placeholder="sender@gmail.com"></label>
      <label class="f">SMTP password <small>for Gmail, an app password</small><input type="password" id="smtpPass" placeholder="${st.smtp_password_set ? "saved, leave empty to keep" : ""}"></label>
      <label class="f">SMTP server<div class="row nowrap"><input type="text" id="smtpHost" value="${esc(st.smtp_host)}"><input type="number" id="smtpPort" class="w-90" value="${esc(st.smtp_port)}" aria-label="Port"></div></label>
      <label class="f">STARTTLS<select id="smtpStarttls">
        <option value="auto" ${(st.smtp_starttls || "auto") === "auto" ? "selected" : ""}>Auto (when offered)</option>
        <option value="1" ${st.smtp_starttls === "1" ? "selected" : ""}>Always</option>
        <option value="0" ${st.smtp_starttls === "0" ? "selected" : ""}>Never (plain / SSL on 465)</option>
      </select></label>
    </div><div class="row mt-12"><span class="grow"></span><button class="btn" data-act="mail-test">Send test e-mail</button><button class="btn primary" data-act="save-mail">Save</button></div></div>
    <div class="h2">Backup and restore</div><div class="box pad"><div class="setrow"><div><b>Setup and tracked items</b><small>Export or restore watches, sources, filters, saved/applied items and item history (passwords are never included).</small></div>
      <div class="row"><button type="button" class="btn sm" data-dl="/api/backup.json" data-fn="lookout-backup.json">${I.dl} Export JSON</button>
        <button type="button" class="btn sm" data-act="import-btn">Import JSON</button>
        <input type="file" id="importFile" accept="application/json" class="hidden"></div></div></div></div>`;
}

// ---------- scan detail ----------
const scanCache = {}, scanInFlight = {};
async function loadScan(id) {
  if (scanInFlight[id]) return;
  scanInFlight[id] = true;
  try {
    const data = await api(`/api/scans/${id}`).catch(() => (scanCache[id] ? null : { missing: true }));
    if (!data) return;
    const changed = !scanCache[id] || JSON.stringify(scanCache[id]) !== JSON.stringify(data);
    scanCache[id] = data;
    if (changed && route.view === "scan" && route.id === id) render();
  } finally {
    delete scanInFlight[id];
  }
}
function renderScan() {
  const d = scanCache[route.id];
  if (!d) { loadScan(route.id); return `<div class="page"><div class="head"><div><h1>Scan</h1><p>Loading…</p></div></div></div>`; }
  if (d.missing) return `<div class="page"><div class="empty"><b>This scan no longer exists</b></div></div>`;
  const running = d.status === "running", w = d.watch_id ? watchById(d.watch_id) : null;
  const ok = d.sources.filter((s) => s.ok).length, shown = d.items.filter((i) => i.match !== 0), hidden = d.items.length - shown.length;
  const statusStr = running ? "running" : d.status === "interrupted" ? "interrupted" : d.status === "cancelled" ? "cancelled" : d.finished ? `took ${duration(d.started, d.finished)}` : d.status;
  const srows = d.sources.map((s) => `<tr><td><span class="dot ${s.ok ? "" : "bad"}"></span>${s.source_id ? `<a href="#/watches/sources">${esc(s.source_name)}</a>` : esc(s.source_name)}</td><td>${s.ok ? `${s.count} items${s.new ? `, <b>${s.new} new</b>` : ""}` : s.skipped ? '<span class="muted">skipped</span>' : `<span class="err">${esc(cleanErr(s.error))}</span> <span class="muted">${esc(s.error_kind || "")}</span><div class="muted">Last successful read: ${esc(s.last_successful_read || "Never")}</div>`}</td><td class="muted num-col">${(s.ms / 1000).toFixed(1)} s</td></tr>`).join("");
  return `<div class="page">${S.progress.running && S.progress.scan_id === d.id ? progressBox() : ""}
    <div class="head"><div><h1>${esc(d.label || (d.trigger === "scheduled" ? "Daily scan" : "Scan"))}, ${short(d.started.slice(0, 10))} at ${d.started.slice(11, 16)}</h1>
      <p>${d.agent ? `${esc(agentName(d.agent))}${d.model ? ", " + esc(d.model) : ""}` : "Plugins only"}, ${statusStr}${d.note ? ". " + esc(d.note) : ""}</p></div>
      <div class="row">${w ? `<a class="btn sm ghost" href="#/watches/${w.id}">${I.watch} Open watch</a>` : ""}<button class="btn sm ghost danger" data-scan-del="${d.id}" ${running ? "disabled" : ""}>${I.trash} Delete</button></div></div>
    <div class="stats"><div class="stat"><b>${d.found}</b><span>matching items</span></div><div class="stat"><b>${d.new}</b><span>new</span></div><div class="stat"><b>Read ${ok} of ${d.sources.length} sources</b><span>${d.sources_failed || 0} failed · ${d.sources_skipped || 0} skipped</span></div><div class="stat"><b>${hidden}</b><span>filtered out</span></div></div>
    ${d.filters.length ? `<div class="h2">Filters used</div><div class="preview">${esc(filterText(d.filters))}</div>` : ""}
    <details class="scan-details" data-key="scan-details"><summary>Technical details</summary><div class="h2">Sources</div><div class="box"><table class="t"><tbody>${srows || '<tr><td class="muted">No sources were scanned.</td></tr>'}</tbody></table></div></details>
    <div class="h2">Matching items <span>${shown.length}</span></div>
    ${shown.length ? `<div class="list">${shown.map((i) => itemCard(i, { isNew: !!i.is_new })).join("")}</div>` : '<div class="empty"><b>No matching items in this scan</b></div>'}</div>`;
}

// ---------- search (discovery) detail ----------
const discCache = {}, discInFlight = {};
async function loadDiscovery(id) {
  if (discInFlight[id]) return;
  discInFlight[id] = true;
  try {
    const data = await api(`/api/discoveries/${id}`).catch(() => (discCache[id] ? null : { missing: true }));
    if (!data) return;
    const changed = !discCache[id] || JSON.stringify(discCache[id]) !== JSON.stringify(data);
    discCache[id] = data;
    if (changed && route.view === "search" && route.id === id) render();
  } finally {
    delete discInFlight[id];
  }
}
function renderSearch() {
  const d = discCache[route.id];
  if (!d) { loadDiscovery(route.id); return `<div class="page"><div class="head"><div><h1>Search</h1><p>Loading…</p></div></div></div>`; }
  if (d.missing) return `<div class="page"><div class="empty"><b>This search no longer exists</b></div></div>`;
  const running = d.status === "running", results = d.results || [], w = d.watch_id ? watchById(d.watch_id) : null;
  const stLabel = running ? "searching…" : d.status === "interrupted" ? "interrupted" : d.status === "cancelled" ? "cancelled" : d.finished ? `took ${duration(d.started, d.finished)}` : "";
  const meta = [d.agent ? `${agentName(d.agent)}${d.model ? ", " + d.model : ""}` : "", stLabel].filter(Boolean).join(" · ");
  const prog = running ? `<div class="progress"><div class="row"><b>Searching the web…</b><span class="grow"></span><button class="btn sm ghost" data-act="cancel-disc">${I.stop} Stop</button></div>
    <div class="muted sub-note">Your agent is looking for pages that list relevant items and turning your request into watch filters.</div>
    <div class="pbar"><i class="indet"></i></div></div>` : "";
  const banner = d.error ? `<div class="banner">${I.warn}<div><b>Search failed</b><div class="muted sub-note">${esc(cleanErr(d.error))}</div></div></div>`
    : d.status === "interrupted" ? `<div class="banner">${I.warn}<div><b>Search interrupted</b><div class="muted sub-note">Lookout closed while this search was running.</div></div></div>`
    : d.status === "cancelled" ? `<div class="banner">${I.warn}<div><b>Search cancelled</b><div class="muted sub-note">Stopped before finishing.</div></div></div>` : "";
  const list = results.length ? `<div class="h2">Suggested sources <span>${results.length}</span></div><div class="list">${results.map((c, k) => candCard(c, k, d.id, d.watch_id)).join("")}</div>`
    : (running || banner) ? "" : `<div class="empty"><b>No sources suggested</b>Try describing what you want in a different way on the New watch page.<div class="row"><a class="btn sm" href="#/watches/new">New watch</a></div></div>`;
  return `<div class="page">${prog}
    <div class="head"><div><h1>${esc(d.title || "Search")}</h1>
      <p>“${esc(d.request)}”${meta ? ` · ${esc(meta)}` : ""}</p></div>
      <div class="row">${w ? `<a class="btn sm" href="#/watches/${w.id}">${I.watch} Open watch</a>` : ""}<button class="btn sm ghost danger" data-disc-del="${d.id}" ${running ? "disabled" : ""}>${I.trash} Delete</button></div></div>
    ${banner}${list}</div>`;
}

// ---------- render & polling ----------
let lastHtml = null, lastTag = null;
function render() {
  if (!S) return;
  renderNav();
  const v = route.view;
  const html = v === "feed" ? renderOpportunities() : v === "agenda" ? renderAgenda() : v === "watches" ? renderWatches() : v === "history" ? renderHistory() : v === "sources" ? renderSources() : v === "filters" ? renderFilters() : v === "settings" ? renderSettings() : v === "watch" ? renderWatch() : v === "scan" ? renderScan() : v === "search" ? renderSearch() : renderNewWatch();
  const tag = v === "settings" && AG ? "settings:ag" : v === "scan" ? `scan:${route.id}` : v === "search" ? `search:${route.id}` : v === "watch" ? `watch:${route.id}` : v, main = $("#main");
  const force = render.force; render.force = false;
  const same = main.dataset.view === tag;
  // Settings is a form: background refreshes must not wipe what is being typed.
  if (tag === "settings:ag" && same && !force) return;
  if (same && tag === lastTag && html === lastHtml) return;
  closePops();
  const act = document.activeElement, keepId = act && act.id, pos = act && act.selectionStart;
  const scrollTop = same ? main.scrollTop : 0;
  const open = same ? [...main.querySelectorAll("details[open][data-key]")].map((d) => d.dataset.key) : [];
  main.innerHTML = html; main.dataset.view = tag; lastHtml = html; lastTag = tag;
  for (const k of open) { const d = main.querySelector(`details[data-key="${CSS.escape(k)}"]`); if (d) d.open = true; }
  if (keepId && $("#" + keepId)) { const el = $("#" + keepId); el.focus({ preventScroll: true }); try { el.setSelectionRange(pos, pos); } catch (_) {} }
  main.scrollTop = scrollTop;
}

const isBusy = () => Boolean(S && (S.progress?.running || S.discovery?.running || S.test_job?.running));
const eligibilityQuery = () => ui.hideIneligible ? "?eligibility=hide_ineligible" : "";
let timer = null, wasBusy = false, lastScanDone = 0;

async function pollProgress() {
  clearTimeout(timer);
  try {
    const p = await api("/api/progress" + eligibilityQuery());
    if (!S) return refresh(true);
    const prevDone = lastScanDone;
    S.progress = p.progress;
    S.discovery = p.discovery;
    S.test_job = p.test_job;
    S.counts = p.counts;
    S.eligibility_counts = p.eligibility_counts;
    lastScanDone = p.progress?.done || 0;
    const nowBusy = isBusy();
    if (route.view === "scan" && (nowBusy || scanCache[route.id]?.status === "running")) loadScan(route.id);
    if (route.view === "search" && (nowBusy || discCache[route.id]?.status === "running")) loadDiscovery(route.id);
    if ((wasBusy && !nowBusy) || (nowBusy && lastScanDone > prevDone)) {
      wasBusy = nowBusy;
      return refresh(true);
    }
    const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && document.activeElement.id !== "q";
    if (!popEl && !typing) render(); else renderNav();
    wasBusy = nowBusy;
    timer = setTimeout(nowBusy ? pollProgress : () => refresh(false), nowBusy ? 1500 : 30000);
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      if (handleAuthFailure()) return;
      lastHtml = lastTag = null;
      $("#main").innerHTML = `<div class="page"><div class="empty"><b>Session expired</b>Open Lookout again from the app or terminal to sign back in.<div class="row"><button class="btn sm primary" data-act="reload-page">Reload</button></div></div></div>`;
      return;
    }
    lastHtml = lastTag = null;
    $("#main").innerHTML = `<div class="page"><div class="empty"><b>Lookout's engine stopped</b>Close this window and open Lookout again.</div></div>`;
    timer = setTimeout(() => refresh(false), 30000);
  }
}

async function refresh(reloadItemsToo = false) {
  clearTimeout(timer);
  try {
    S = await api("/api/state" + eligibilityQuery());
    sessionStorage.removeItem("lookout-reauth-ts");
    lastScanDone = S.progress?.done || 0;
    if (S.settings.agent && !modelCache[S.settings.agent]) modelsFor(S.settings.agent).then(() => { render.force = true; render(); }).catch((err) => { if (err?.status === 401 || err?.status === 403) handleAuthFailure(); });
    if (!AG) ensureAgents().then(() => render()).catch((err) => { if (err?.status === 401 || err?.status === 403) handleAuthFailure(); });
    const busy = isBusy();
    if (route.view === "scan" && (wasBusy || busy || scanCache[route.id]?.status === "running")) loadScan(route.id);
    if (route.view === "search" && (wasBusy || busy || discCache[route.id]?.status === "running")) loadDiscovery(route.id);
    if (reloadItemsToo || ((route.view === "feed" || route.view === "watch") && feedState.key !== itemsScopeKey())) {
      loadItems(false);
    }
    const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && document.activeElement.id !== "q";
    if (!popEl && !typing) render(); else renderNav();
    wasBusy = busy;
    timer = setTimeout(busy ? pollProgress : () => refresh(false), busy ? 1500 : 30000);
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      if (handleAuthFailure()) return;
      lastHtml = lastTag = null;
      $("#main").innerHTML = `<div class="page"><div class="empty"><b>Session expired</b>Open Lookout again from the app or terminal to sign back in.<div class="row"><button class="btn sm primary" data-act="reload-page">Reload</button></div></div></div>`;
      return;
    }
    lastHtml = lastTag = null;
    $("#main").innerHTML = `<div class="page"><div class="empty"><b>Lookout's engine stopped</b>Close this window and open Lookout again.</div></div>`;
    timer = setTimeout(() => refresh(false), 30000);
  }
}

// ---------- events ----------
document.addEventListener("toggle", (e) => {
  const sid = e.target?.dataset?.codeId;
  if (sid && e.target.open) fetchSourceCode(Number(sid));
}, true);

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a"); if (!t) return;
  const d = t.dataset;
  try {
    if (d.win) { e.preventDefault(); return window.pywebview?.api?.[d.win](); }
    if (d.ext !== undefined && window.pywebview?.api) { e.preventDefault(); return window.pywebview.api.open_external(t.href); }
    if (d.dl) { e.preventDefault(); return await downloadApi(d.dl, d.fn || "download"); }
    if (d.copy) { e.preventDefault(); try { await navigator.clipboard.writeText(d.copy); toast("Copied URL"); } catch { toast(d.copy); } return; }
    if (d.pick === "model") { e.preventDefault(); return popEl ? closePops() : openModelPicker(t); }
    if (d.pick === "agent") { e.preventDefault(); return popEl ? closePops() : openAgentPicker(t); }
    if (d.pick === "remind") { e.preventDefault(); return popEl ? closePops() : openRemindPicker(t); }
    if (d.pick === "snooze") { e.preventDefault(); return popEl ? closePops() : openSnoozePicker(t); }
    if (d.remindSet !== undefined) return setReminder(d.rid, d.remindSet);
    if (d.remindPick !== undefined) { const v = $("#remindPick").value; if (!v) return toast("Pick a date"); return setReminder(d.rid, v); }
    if (d.snoozeSet !== undefined) return setSnooze(d.sid, d.snoozeSet);
    if (d.snoozePick !== undefined) { const v = $("#snoozePick").value; if (!v) return toast("Pick a date"); return setSnooze(d.sid, v); }
    if (d.agview) { ui.agView = d.agview; keep(); return render(); }
    if (d.day) { agDay = d.day; return render(); }
    if (d.model !== undefined) return setModel(d.model);
    if (d.modelCustom !== undefined) return setModel($("#customModel").value.trim());
    if (d.agent) { await api("/api/settings", { agent: d.agent, model: "" }); closePops(); await modelsFor(d.agent); toast(`Using ${agentName(d.agent)}`); render.force = true; return refresh(false); }
    if (d.goto) { closePops(); location.hash = d.goto; return; }
    if (d.act === "reload-page") { location.reload(); return; }
    if (d.act === "dismiss-notify-errors") { await api("/api/notifications/dismiss", {}); S.notify_errors = []; return render(); }
    if (d.act === "cancel-edit") { editingSourceId = editingFilterId = editingWatchId = null; return render(); }
    if (d.act === "scan") { e.preventDefault(); const r = await api("/api/scan", {}); toast(r.started ? "Scan started" : "A scan is already running"); return pollProgress(); }
    if (d.act === "cancel") { await api("/api/scan/cancel", {}); toast("Stopping scan…"); return pollProgress(); }
    if (d.act === "cancel-disc") { await api("/api/discover/cancel", {}); toast("Stopping search…"); return pollProgress(); }
    if (d.act === "toggle-snoozed") { ui.showSnoozed = !ui.showSnoozed; keep(); reloadActiveItems(); return render(); }
    if (d.act === "toggle-updated") { ui.onlyUpdated = !ui.onlyUpdated; keep(); reloadActiveItems(); return render(); }
    if (d.act === "toggle-ineligible") { ui.hideIneligible = !ui.hideIneligible; keep(); return refresh(true); }
    if (d.act === "load-more") { return loadItems(true); }
    if (d.act === "close-preview") { await api("/api/sources/test/clear", {}); S.test_job = { id: null, running: false, result: null }; return render(); }
    if (d.act === "cancel-test") { await api("/api/sources/test/cancel", {}); toast("Stopping test…"); return pollProgress(); }
    if (d.act === "test-new-source") {
      const form = t.closest("form");
      const data = Object.fromEntries(new FormData(form));
      if (!data.url) return toast("Enter an http(s) URL first");
      const wid = form.dataset.watchId ? Number(form.dataset.watchId) : null;
      S.test_job = await api("/api/sources/test", { ...data, watch_id: wid });
      render();
      return pollProgress();
    }
    if (d.act === "import-btn") { $("#importFile")?.click(); return; }
    if (d.item && d.unsave) { syncItem(d.item, { action: null }); render(); await api(`/api/items/${encodeURIComponent(d.item)}`, { action: null }); toast("Removed from agenda"); return refresh(false); }
    if (d.item) { const it = findItem(d.item); if (!it) return; const nv = it.action === d.set ? null : d.set; syncItem(d.item, { action: nv }); render(); await api(`/api/items/${encodeURIComponent(d.item)}`, { action: nv }); if (nv === "saved") toast("Added to agenda"); return refresh(true); }
    if (d.cat) { ui.cats = ui.cats.includes(d.cat) ? ui.cats.filter((c) => c !== d.cat) : [...ui.cats, d.cat]; keep(); reloadActiveItems(); return render(); }
    if (d.cal) { const base = ui.calMonth ? parseIso(ui.calMonth + "-01") : new Date(); const m = d.cal === "0" ? new Date() : new Date(base.getFullYear(), base.getMonth() + Number(d.cal), 1); ui.calMonth = iso(m).slice(0, 7); keep(); return render(); }
    if (d.agOpen) { agOpen = agOpen === d.agOpen ? null : d.agOpen; return render(); }
    if (d.srcTest) {
      S.test_job = await api(`/api/sources/${d.srcTest}/test`, {});
      render();
      return pollProgress();
    }
    if (d.srcResume) { await api(`/api/sources/${d.srcResume}/resume`, {}); toast("Source resumed"); return refresh(false); }
    if (d.srcScan) { const r = await api("/api/scan", { only: [Number(d.srcScan)] }); toast(r.started ? "Scanning this source" : "A scan is already running"); return pollProgress(); }
    if (d.srcRelearn) { await api(`/api/sources/${d.srcRelearn}`, { relearn: true }); toast("Your agent will relearn this site on the next scan"); return refresh(false); }
    if (d.srcDel) { const { count } = await api(`/api/sources/${d.srcDel}/tracked-count`); const detail = count ? ` ${count} tracked item${count === 1 ? "" : "s"} with actions, notes, or reminders will be kept in Agenda/Applied.` : " No tracked items will be kept."; if (!confirm(`Delete this source?${detail} Other collected items will be deleted.`)) return; await api(`/api/sources/${d.srcDel}/delete`, {}); return refresh(true); }
    if (d.srcEdit) { const id = Number(d.srcEdit); editingSourceId = editingSourceId === id ? null : id; return render(); }
    if (d.cand !== undefined) { const c = S.discovery.results[Number(d.cand)]; await api("/api/sources", { name: c.name, url: c.url, notes: c.notes || "", watch_id: S.discovery.watch_id || null }); toast(`Added ${c.name}`); return refresh(false); }
    if (d.discCand !== undefined) { const dd = discCache[Number(d.discId)]; const c = dd?.results?.[Number(d.discCand)]; if (!c) return; await api("/api/sources", { name: c.name, url: c.url, notes: c.notes || "", watch_id: dd.watch_id || null }); toast(`Added ${c.name}`); return refresh(false); }
    if (d.act === "clear-disc") { await api("/api/discover/clear", {}); return refresh(false); }
    if (d.fEdit) { const id = Number(d.fEdit); editingFilterId = editingFilterId === id ? null : id; return render(); }
    if (d.fDel) { await api(`/api/filters/${d.fDel}/delete`, {}); return refresh(true); }
    if (d.scanDel) { if (!confirm("Delete this scan from history? Items it found stay in Opportunities.")) return; await api(`/api/scans/${d.scanDel}/delete`, {}); delete scanCache[Number(d.scanDel)]; location.hash = "#/opportunities/history"; return refresh(false); }
    if (d.discDel) { if (!confirm("Delete this search from history?")) return; await api(`/api/discoveries/${d.discDel}/delete`, {}); delete discCache[Number(d.discDel)]; location.hash = "#/opportunities/history"; return refresh(false); }
    if (d.watchScan) { const r = await api(`/api/watches/${d.watchScan}/scan`, {}); toast(r.started ? "Scanning watch" : "A scan is already running"); return pollProgress(); }
    if (d.watchEdit) { const id = Number(d.watchEdit); editingWatchId = editingWatchId === id ? null : id; return render(); }
    if (d.watchRediscover) {
      const wid = Number(d.watchRediscover);
      const w = watchById(wid);
      const form = $("#editWatchForm");
      const title = form ? form.querySelector("[name=title]").value.trim() : w?.title;
      const req = (form ? form.querySelector("[name=request]").value : w?.request || "").trim();
      if (!req || req.length < 5) return toast("Enter a search prompt of at least 5 characters");
      if (form && title) await api(`/api/watches/${wid}`, { title, request: req });
      editingWatchId = null;
      const r = await api("/api/discover", { watch_id: wid, request: req });
      if (r.started) { toast("Searching for more sources…"); location.hash = `#/search/${r.id}`; return; }
      toast("A search is already running");
      return pollProgress();
    }
    if (d.watchDel) { const { count } = await api(`/api/watches/${d.watchDel}/tracked-count`); const detail = count ? ` ${count} tracked item${count === 1 ? "" : "s"} with actions, notes, or reminders will be kept in Agenda/Applied.` : " No tracked items will be kept."; if (!confirm(`Delete this watch, its sources, filters and scans?${detail} Other collected items will be deleted.`)) return; await api(`/api/watches/${d.watchDel}/delete`, {}); location.hash = "#/opportunities"; return refresh(true); }
    if (d.act === "save-agent") {
      await api("/api/settings", {
        agent: $("input[name=agent]:checked")?.value || "",
        headed: $("#headedIn").checked ? "1" : "0",
        agent_timeout_min: $("#timeoutIn").value,
        parallel: $("#parallelIn").value,
        verify_weekly: $("#verifyIn").checked ? "1" : "0",
      });
      toast("Saved"); AG = null; render.force = true; return refresh(false);
    }
    if (d.act === "sched-test") {
      const r = await api("/api/schedule/test", {});
      toast(r.message);
      return;
    }
    if (d.act === "sched-preview") {
      if (schedPreview) { schedPreview = null; render.force = true; return render(); }
      const at = $("#schTime")?.value || S.settings.schedule_time || "08:30";
      schedPreview = await api(`/api/schedule/preview?at=${encodeURIComponent(at)}`);
      render.force = true; return render();
    }
    if (d.act === "save-webhook" || d.act === "webhook-test") {
      const wu = $("#webhookUrl").value.trim(), wf = $("#webhookFormat").value, chatId = $("#telegramChatId").value.trim(), allowLocal = $("#webhookLocalIn").checked ? "1" : "0";
      await api("/api/settings", { webhook_url: wu, webhook_format: wf, telegram_chat_id: chatId, webhook_allow_local: allowLocal });
      if (d.act === "webhook-test") {
        const res = await api("/api/webhook/test", { url: wu, format: wf, chat_id: chatId, allow_local: allowLocal === "1" });
        toast(res.message);
        return refresh(false);
      }
      return toast("Saved");
    }
    if (d.act === "save-mail" || d.act === "mail-test") {
      await api("/api/settings", {
        mail_to: $("#mailTo").value,
        mail_from: $("#mailFrom").value,
        smtp_user: $("#smtpUser").value,
        smtp_password: $("#smtpPass").value,
        smtp_host: $("#smtpHost").value,
        smtp_port: $("#smtpPort").value,
        smtp_starttls: $("#smtpStarttls").value,
      });
      if (d.act === "mail-test") {
        const res = await api("/api/mail/test", {});
        toast(res.message);
        return refresh(false);
      }
      return toast("Saved");
    }
  } catch (err) {
    if (err.status === 401 || err.status === 403) { if (!handleAuthFailure()) showSessionExpired(); return; }
    toast(err.message);
  }
});

document.addEventListener("change", async (e) => {
  if (e.target.id === "themeSel") {
    ui.theme = e.target.value;
    applyTheme();
    keep();
    return;
  }
  const t = e.target, d = t.dataset;
  try {
    if (t.id === "srcSel") { ui.src = t.value; keep(); reloadActiveItems(); return render(); }
    if (t.id === "sortSel") { ui.sort = t.value; keep(); reloadActiveItems(); return render(); }
    if (t.id === "importFile" && t.files?.[0]) {
      const payload = JSON.parse(await t.files[0].text()); t.value = "";
      const r = await api("/api/import", payload);
      toast(`Imported ${r.added.watches} watches, ${r.added.sources} sources, ${r.added.filters} filters`);
      return refresh(true);
    }
    if (d.srcActive) { await api(`/api/sources/${d.srcActive}`, { active: t.checked }); return refresh(false); }
    if (d.fActive) { await api(`/api/filters/${d.fActive}`, { active: t.checked }); return refresh(true); }
    if (d.remind !== undefined) { await api(`/api/items/${encodeURIComponent(d.remind)}`, { remind_on: t.value }); toast("Reminder set"); return refresh(false); }
    if (d.note !== undefined) { await api(`/api/items/${encodeURIComponent(d.note)}`, { note: t.value }); return toast("Note saved"); }
    if (t.name === "agent") { document.querySelectorAll(".agent").forEach((l) => l.classList.toggle("on", l.contains(t))); return; }
    if (t.id === "notifyIn") { await api("/api/settings", { notify: t.checked ? "1" : "0" }); return toast("Saved"); }
    if (t.id === "rssPublicIn") { await api("/api/settings", { rss_public: t.checked ? "1" : "0" }); return toast("Saved"); }
    if (t.id === "schOn" || t.id === "schTime") {
      if ($("#schOn")) { const r = await api("/api/schedule", { enabled: $("#schOn").checked, at: $("#schTime").value }); toast(r.message); }
      if (schedPreview) { schedPreview = await api(`/api/schedule/preview?at=${encodeURIComponent($("#schTime").value || "08:30")}`); render.force = true; }
      return refresh(false);
    }
    if (t.id === "fKind") { $("#fHint").textContent = FKIND[t.value][1]; $("#fVal").placeholder = FKIND[t.value][2]; $("#fVal").type = t.value === "deadline_within" ? "number" : "text"; }
    if (t.id === "wfKind") { $("#wfVal").placeholder = FKIND[t.value][2]; $("#wfVal").type = t.value === "deadline_within" ? "number" : "text"; }
  } catch (err) {
    if (err.status === 401 || err.status === 403) { if (!handleAuthFailure()) showSessionExpired(); return; }
    toast(err.message);
  }
});

let qTimer = null;
document.addEventListener("input", (e) => {
  if (e.target.id === "ask") watchDraft = e.target.value;
  if (e.target.id === "q") {
    ui.q = e.target.value.trim();
    clearTimeout(qTimer);
    qTimer = setTimeout(() => reloadActiveItems(), 200);
  }
});

document.addEventListener("submit", async (e) => {
  e.preventDefault(); const f = e.target, data = Object.fromEntries(new FormData(f));
  try {
    if (f.id === "eligibilityForm") {
      await api("/api/settings", { eligibility_profile: data });
      toast("Eligibility profile saved");
      return refresh(true);
    }
    if (f.id === "addSrc") { await api("/api/sources", data); toast(`Added ${data.name}`); f.reset(); return refresh(false); }
    if (f.id === "addWatchSrc") { await api("/api/sources", { ...data, watch_id: Number(f.dataset.watchId) }); toast(`Added ${data.name}`); f.reset(); return refresh(false); }
    if (f.id === "editSrcForm") {
      const sid = Number(f.dataset.sid);
      const wid = data.watch_id === "" ? null : Number(data.watch_id);
      await api(`/api/sources/${sid}`, { name: data.name, url: data.url, notes: data.notes, watch_id: wid });
      delete sourceCodeCache[sid];
      editingSourceId = null;
      toast("Source saved");
      return refresh(true);
    }
    if (f.id === "addF") { await api("/api/filters", data); f.reset(); $("#fHint").textContent = FKIND.include[1]; return refresh(true); }
    if (f.id === "addWatchF") { await api("/api/filters", { ...data, watch_id: Number(f.dataset.watchId) }); f.reset(); $("#wfVal").type = "text"; $("#wfVal").placeholder = FKIND.include[2]; return refresh(true); }
    if (f.id === "editFilterForm") {
      const fid = Number(f.dataset.fid);
      await api(`/api/filters/${fid}`, { kind: data.kind, value: data.value });
      editingFilterId = null;
      toast("Filter saved");
      return refresh(true);
    }
    if (f.id === "editWatchForm") {
      const wid = Number(f.dataset.wid);
      await api(`/api/watches/${wid}`, { title: data.title, request: data.request });
      editingWatchId = null;
      toast("Watch updated");
      return refresh(false);
    }
    if (f.id === "discover") {
      if (!data.request || data.request.trim().length < 5) return toast("Describe what you want to follow");
      const r = await api("/api/discover", data);
      if (r.started && r.id) { watchDraft = ""; toast("Watch created, searching for sources"); location.hash = `#/search/${r.id}`; return; }
      toast("Already searching"); return pollProgress();
    }
  } catch (err) {
    if (err.status === 401 || err.status === 403) { if (!handleAuthFailure()) showSessionExpired(); return; }
    toast(err.message);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closePops(true); if (editingSourceId || editingFilterId || editingWatchId) { editingSourceId = editingFilterId = editingWatchId = null; render(); } }
  if (!e.isComposing && e.key === "Enter" && e.target.id === "customModel") {
    e.preventDefault();
    popEl?.querySelector("[data-model-custom]")?.click();
  }
  if (!e.isComposing && e.key === "Enter" && !e.shiftKey && e.target.id === "ask") { e.preventDefault(); e.target.form.requestSubmit(); }
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { const q = $("#q") || $("#ask"); if (q) { e.preventDefault(); q.focus(); } }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });
parseRoute(); refresh(true);
