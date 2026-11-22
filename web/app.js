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
const SCOPE = `<svg viewBox="0 0 120 120" aria-hidden="true"><defs><radialGradient id="sw" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(60 60) scale(56)"><stop offset="0" stop-color="#7B9CFF" stop-opacity=".55"/><stop offset="1" stop-color="#7B9CFF" stop-opacity="0"/></radialGradient></defs>
  <circle cx="60" cy="60" r="56" fill="none" stroke="#26262B"/><circle cx="60" cy="60" r="38" fill="none" stroke="#1E1E23"/><circle cx="60" cy="60" r="20" fill="none" stroke="#1E1E23"/>
  <path d="M60 4v112M4 60h112" stroke="#18181C"/><g class="sweep"><path d="M60 60 L60 4 A56 56 0 0 1 108.5 32 Z" fill="url(#sw)"/><path d="M60 60 L108.5 32" stroke="#7B9CFF" stroke-width="1.6"/></g>
  <circle cx="83" cy="40" r="2.6" fill="#7B9CFF"/><circle cx="41" cy="78" r="2" fill="#7B9CFF" opacity=".6"/><circle cx="60" cy="60" r="3" fill="#EDEDEF"/></svg>`;

const ITEMS_PAGE_SIZE = 50;
let S = null, AG = null, route = { view: "home", id: null };
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
const keep = () => localStorage.setItem("lookout-ui", JSON.stringify({ ...ui, q: "" }));
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
    if (feedState.scope !== scope) {
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
let popEl = null, subEl = null;
function closePops() { popEl?.remove(); subEl?.remove(); popEl = subEl = null; }
function placePop(el, anchor, side = "above") {
  document.body.appendChild(el);
  const a = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
  let x, y;
  if (side === "right") { x = a.right + 6; y = Math.min(a.top - 6, innerHeight - h - 10); if (x + w > innerWidth - 10) x = a.left - w - 6; }
  else if (side === "below") { x = Math.min(a.right - w, innerWidth - w - 10); y = a.bottom + 6; if (y + h > innerHeight - 10) y = a.top - h - 6; }
  else { x = Math.min(a.left, innerWidth - w - 10); y = a.top - h - 8; if (y < 10) y = a.bottom + 8; }
  el.style.left = Math.max(10, x) + "px"; el.style.top = Math.max(10, y) + "px";
}
async function openModelPicker(anchor) {
  closePops();
  const st = S.settings;
  if (!st.agent) { location.hash = "#/settings"; return toast("Choose an agent first"); }
  const list = await modelsFor(st.agent), fams = groupModels(list), cur = st.model;
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Model for ${esc(agentName(st.agent))}</div><button class="o ${!cur ? "on" : ""}" data-model="">Default model</button>` +
    fams.map((f, k) => {
      const on = f.variants.find((v) => v.id === cur);
      if (f.variants.length === 1) return `<button class="o ${on ? "on" : ""}" data-model="${esc(f.variants[0].id)}">${esc(f.label)}</button>`;
      return `<button class="o fam ${on ? "on" : ""}" data-fam="${k}">${esc(f.label)}<span class="r">${on ? effortLabel(on.effort) : ""} ${I.right}</span></button>`;
    }).join("") +
    `<hr><div class="custom"><input type="text" id="customModel" placeholder="Other model id" value="${esc(cur && !list.some(([k]) => k === cur) ? cur : "")}"><button class="btn sm" data-model-custom>Use</button></div>`;
  placePop(pop, anchor);
  const openFam = (btn) => {
    subEl?.remove(); pop.querySelectorAll(".fam").forEach((b) => b.classList.toggle("hl", b === btn));
    const f = fams[Number(btn.dataset.fam)];
    const sub = document.createElement("div"); sub.className = "pop pop-sub"; subEl = sub;
    sub.innerHTML = `<div class="t">${esc(f.label)}</div>` + f.variants.map((v) => `<button class="o ${v.id === cur ? "on" : ""}" data-model="${esc(v.id)}">${effortLabel(v.effort) || "Standard"}</button>`).join("");
    placePop(sub, btn, "right");
  };
  pop.querySelectorAll(".fam").forEach((b) => { b.addEventListener("mouseenter", () => openFam(b)); b.addEventListener("click", (e) => { e.stopPropagation(); openFam(b); }); });
  pop.querySelectorAll(".o:not(.fam)").forEach((b) => b.addEventListener("mouseenter", () => { subEl?.remove(); subEl = null; pop.querySelectorAll(".fam").forEach((x) => x.classList.remove("hl")); }));
}
async function openAgentPicker(anchor) {
  closePops(); await ensureAgents();
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Agents on this computer</div>` + AG.agents.filter((a) => a.id !== "custom" || a.configured).map((a) =>
    `<button class="o ${S.settings.agent === a.id ? "on" : ""}" data-agent="${a.id}" ${a.installed ? "" : "disabled"}>${esc(a.name)}<span class="r">${a.installed ? "" : "not installed"}</span></button>`).join("") +
    `<hr><button class="o" data-goto="#/settings">Agent settings</button>`;
  placePop(pop, anchor);
}
async function setModel(model) { await api("/api/settings", { model }); S.settings.model = model; closePops(); toast("Model changed"); render.force = true; render(); }
document.addEventListener("mousedown", (e) => { if (popEl && !popEl.contains(e.target) && !subEl?.contains(e.target) && !e.target.closest("[data-pick]")) closePops(); });

// ---------- routing ----------
function parseRoute() {
  const [view, id] = location.hash.replace(/^#\/?/, "").split("/");
  route = { view: view || "home", id: id && /^\d+$/.test(id) ? Number(id) : null };
}
window.addEventListener("hashchange", () => {
  closePops();
  editingSourceId = editingFilterId = editingWatchId = null;
  parseRoute();
  render();
  refresh(false);
});

// ---------- sidebar ----------
let lastWatchesHtml = null, lastHistHtml = null;
function histRight(e, isScan) {
  if (e.status === "running") return '<i class="live"></i>';
  if (e.status === "failed") return '<span class="bad">failed</span>';
  if (e.status === "interrupted") return '<span class="bad">interrupted</span>';
  if (e.status === "cancelled") return '<span class="warn">cancelled</span>';
  if (!isScan) return `<span>${e.found || 0}</span>`;
  return e.new ? `<span class="new">+${e.new}</span>` : `<span>${e.found}</span>`;
}
function renderSide() {
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === route.view));
  const c = S.counts || {};
  const feedOpen = c.feed_open ?? 0, feedNew = c.feed_new ?? 0;
  $("#navFeed").textContent = feedOpen || ""; $("#navFeed").classList.toggle("hot", feedNew > 0);
  $("#navAgenda").textContent = (c.agenda_saved ?? agendaBase().filter((i) => i.action === "saved").length) || "";
  $("#navSources").textContent = (c.sources_active ?? S.sources.filter((s) => s.active).length) || "";
  $("#navFilters").textContent = (c.filters_global_active ?? S.filters.filter((f) => !f.watch_id && f.active).length) || "";
  const p = S.progress, btn = $("#scanBtn");
  btn.disabled = p.running; btn.classList.toggle("spin", p.running);
  $("#scanLabel").textContent = p.running ? "Scanning" : "New scan";
  $("#scanSub").textContent = p.running ? `${p.done} of ${p.total}` : "";

  const wList = S.watches || [], wCounts = c.watches || {};
  const wHtml = wList.map((w) => {
    const wc = wCounts[String(w.id)] || {};
    const cnt = wc.new || 0, total = wc.open || 0;
    const badge = cnt ? `<em class="hot">${cnt}</em>` : total ? `<em>${total}</em>` : "";
    return `<a href="#/watch/${w.id}" class="${route.view === "watch" && route.id === w.id ? "on" : ""}" title="${esc(w.request || w.title)}">${I.watch}<b>${esc(w.title)}</b>${badge}</a>`;
  }).join("") || `<div class="empty-w">No watches yet</div>`;
  if (wHtml !== lastWatchesHtml) { $("#watches").innerHTML = wHtml; lastWatchesHtml = wHtml; }

  const hist = [
    ...S.scans.map((s) => ({ kind: "scan", ...s })),
    ...(S.discoveries || []).map((d) => ({ kind: "search", ...d })),
  ].sort((a, b) => (b.started || "").localeCompare(a.started || "") || b.id - a.id);
  let html = "", last = "";
  for (const e of hist) {
    const day = e.started.slice(0, 10), t0 = today0();
    const dayLabel = day === iso(t0) ? "Today" : day === iso(new Date(t0 - 864e5)) ? "Yesterday" : short(day);
    if (dayLabel !== last) { html += `<div class="day">${dayLabel}</div>`; last = dayLabel; }
    if (e.kind === "search") {
      const title = e.title || e.request || "Web search";
      html += `<a href="#/search/${e.id}" class="h-search ${route.view === "search" && route.id === e.id ? "on" : ""}" title="${esc(title)}">${I.search}<b>${esc(title)}</b>${histRight(e, false)}</a>`;
    } else {
      const label = e.label || (e.trigger === "scheduled" ? "Daily scan" : "Scan"), hm = e.started.slice(11, 16);
      html += `<a href="#/scan/${e.id}" class="${route.view === "scan" && route.id === e.id ? "on" : ""}" title="${esc(`${label} · ${hm} (${e.found} matching, ${e.new} new)`)}"><span class="hl"><b>${esc(label)}</b><time>${hm}</time></span>${histRight(e, true)}</a>`;
    }
  }
  const finalHist = html || `<div class="day">Your history will show up here.</div>`;
  if (finalHist !== lastHistHtml) { $("#history").innerHTML = finalHist; lastHistHtml = finalHist; }
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
  if (!t) return `<div class="when"><b class="s">—</b><span>no date</span></div>`;
  const d = daysTo(t), cls = !i.deadline ? "" : d <= 3 ? "red" : d <= 7 ? "orange" : d <= 14 ? "amber" : "";
  if (d < 0) return `<div class="when"><b class="s">past</b><time>${short(t)}</time></div>`;
  const big = d === 0 ? `<b class="s">today</b><span>${i.deadline ? "last day" : "starts"}</span>` : d === 1 ? `<b class="s">tomorrow</b><span>${i.deadline ? "last day" : "starts"}</span>` : `<b>${d}</b><span>${i.deadline ? "days left" : "days to go"}</span>`;
  return `<div class="when ${cls}">${big}<time>${short(t)}</time></div>`;
}
function itemCard(i, opts = {}) {
  const a = i.action, sn = isSnoozed(i), w = opts.showWatch && i.watch_id ? watchById(i.watch_id) : null;
  const who = [i.org, i.location].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("");
  const b = (act, tip, on, cls = "") => `<button class="ib ${on ? "on" : ""} ${cls}" data-item="${esc(i.id)}" data-set="${act}" data-tip="${tip}" aria-label="${tip}">${act === "hidden" && on ? I.undo : I[{ saved: "save", applied: "done", hidden: "hide" }[act]]}</button>`;
  const upd = isUpdated(i);
  const updLabel = i.last_change_kind === "deadline_moved" ? "Deadline moved" : i.last_change_kind === "reopened" ? "Reopened" : "Updated";
  const updTag = upd ? `<span class="tag updated">${updLabel}</span>` : "";
  return `<article class="item">${whenBox(i)}<div class="ibody">
      <div class="meta">${(opts.isNew ?? isNew(i)) ? '<span class="tag new">New</span>' : ""}${updTag}${i.category ? `<span class="tag accent">${esc(i.category)}</span>` : ""}<span class="tag">${esc(i.source_name)}</span>${w ? `<a class="tag accent no-dec" href="#/watch/${w.id}">${esc(w.title)}</a>` : ""}${sn ? `<span class="tag warn">snoozed until ${short(i.snoozed_until)}</span>` : ""}${i.status === "gone" ? '<span class="tag bad">no longer listed</span>' : ""}</div>
      <h3><a href="${esc(safeUrl(i.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(i.title)}</a></h3>
      ${who ? `<div class="who">${who}</div>` : ""}${upd && i.change_summary ? `<div class="change-note">${esc(i.change_summary)}</div>` : ""}${i.summary ? `<p class="sum">${esc(i.summary)}</p>` : ""}${i.note ? `<div class="note">${esc(i.note)}</div>` : ""}</div>
    <div class="acts">${b("saved", a === "saved" ? "Remove from agenda" : "Add to agenda", a === "saved")}${b("applied", a === "applied" ? "Mark as not done" : "Mark as done", a === "applied", "green")}<button class="ib ${sn ? "on" : ""}" data-pick="snooze" data-sid="${esc(i.id)}" data-tip="${sn ? "Snoozed until " + short(i.snoozed_until) : "Snooze"}" aria-label="Snooze">${I.clock}</button>${b("hidden", a === "hidden" ? "Unhide" : "Hide", a === "hidden")}</div></article>`;
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
  placePop(pop, anchor, "below");
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
function renderHome() {
  const st = S.settings, d = S.discovery, c = S.counts || {};
  const fresh = c.feed_new ?? 0, week = c.closing_this_week ?? 0, savedN = c.agenda_saved ?? 0;
  const hour = new Date().getHours(), greet = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const cands = !d.running ? (d.results || []).map((cand, k) => candCard(cand, k, null, d.watch_id)).join("") : "";
  const globalFCount = c.filters_global_active ?? S.filters.filter((f) => !f.watch_id && f.active).length;
  const srcActiveCount = c.sources_active ?? S.sources.filter((s) => s.active).length;
  return `<div class="home">${S.progress.running ? `<div class="home-prog">${progressBox()}</div>` : ""}
    ${(S.notify_errors || []).length ? `<div class="home-prog">${renderNotifyErrorsBanner()}</div>` : ""}
    <div class="scope">${SCOPE}</div>
    <h1>${greet}. What should Lookout watch?</h1>
    <p class="lede">Describe it in your own words. Lookout creates a watch, finds relevant sources, sets its filters and scans them.</p>
    <form class="composer" id="discover">
      <div class="ctx"><button type="button" class="chip outlined" data-pick="agent">${I.bot} ${st.agent ? esc(agentName(st.agent)) : "Choose an agent"} ${I.down}</button>
        <a class="chip" href="#/sources">${I.globe} ${srcActiveCount} sources</a>
        <a class="chip" href="#/filters">${I.funnel} ${globalFCount} global filters</a></div>
      <div class="field"><textarea name="request" id="ask" rows="2" placeholder="Machine-learning internships in Berlin, or new hackathons with cash prizes" ${d.running ? "disabled" : ""}>${d.running ? esc(d.request) : ""}</textarea>
        <div class="bar"><a class="chip" href="#/sources" title="Add a website yourself" aria-label="Add a website yourself">${I.plus}</a>
          ${st.agent ? `<button type="button" class="chip model" data-pick="model">${modelChipText(st.model, st.agent)} ${I.down}</button>` : ""}<span class="grow"></span>
          <button type="button" class="chip" data-act="scan" ${S.progress.running ? "disabled" : ""}>${I.play} Scan now</button>
          <button class="send" aria-label="Find sources" ${d.running || !st.agent ? "disabled" : ""}>${d.running ? '<svg class="spin" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4"/></svg>' : I.up}</button></div></div>
    </form>
    ${d.running && d.id ? `<div class="disc-bar"><a href="#/search/${d.id}"><i class="live"></i><span>Searching for <b>${esc(d.title || d.request)}</b></span><span class="grow"></span><span class="muted">View search →</span></a><button type="button" class="btn sm ghost" data-act="cancel-disc">${I.stop} Stop</button></div>` : ""}
    ${!d.running && d.error ? `<p class="err-note">${esc(cleanErr(d.error))}</p>` : ""}
    ${cands ? `<div class="found"><div class="h2">Suggested sources <span>${d.results.length}</span><span class="grow"></span><button class="btn sm ghost" data-act="clear-disc">Clear</button></div><div class="list">${cands}</div></div>` : ""}
    <div class="glance">${S.scans.length ? `<a href="#/feed"><b class="${fresh ? "hot" : ""}">${fresh}</b> new in the last 3 days</a><a href="#/feed"><b class="${week ? "warn" : ""}">${week}</b> closing this week</a><a href="#/agenda"><b>${savedN}</b> in your agenda</a>` : !st.agent ? `<a href="#/settings">Start by choosing your agent</a>` : `<a href="#/sources">Add a website to watch</a>`}</div></div>`;
}

// ---------- shared feed / watch list ----------
function renderItemList(opts = {}) {
  const scope = itemsScopeOnly();
  const key = itemsScopeKey();
  if (key && feedState.key !== key && !feedState.loading) {
    Promise.resolve().then(() => loadItems(false));
  }
  const list = dedupeItems(feedState.scope === scope ? feedState.items : []);
  const facets = feedState.facets || {};
  const cats = facets.categories || {};
  const srcs = facets.sources || [];
  const updCount = facets.updated_count || 0;
  const snCount = facets.snoozed_count || 0;
  const hasFilters = Boolean(ui.cats.length || (opts.showSourceSelect && ui.src) || ui.onlyUpdated || ui.q);

  let body = "";
  if (feedState.loading && !feedState.items.length) {
    body = `<div class="empty"><b>Loading items…</b></div>`;
  } else if (!list.length) {
    body = hasFilters
      ? `<div class="empty"><b>Nothing matches</b>Try a different search or category.</div>`
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
    + (snCount || ui.showSnoozed ? `<button class="pill ${ui.showSnoozed ? "on" : ""}" data-act="toggle-snoozed">Snoozed <em>${snCount}</em></button>` : "");
  const srcSelect = opts.showSourceSelect ? `<select id="srcSel" aria-label="Source"><option value="">All sources</option>${srcs.map((s) => `<option ${s === ui.src ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>` : "";
  const loadMore = feedState.hasMore ? `<div class="load-more"><span class="muted">Loaded ${feedState.items.length} of ${feedState.total} matching items</span><button type="button" class="btn sm" data-act="load-more" ${feedState.loading ? "disabled" : ""}>${feedState.loading ? "Loading…" : "Load more"}</button></div>` : "";
  return `<div class="toolbar"><div class="search">${I.search}<input type="search" id="q" placeholder="Search titles, organisations, places" value="${esc(ui.q)}"></div>
      ${srcSelect}
      <select id="sortSel" aria-label="Sort"><option value="deadline" ${ui.sort === "deadline" ? "selected" : ""}>By date</option><option value="new" ${ui.sort === "new" ? "selected" : ""}>Newest first</option></select></div>
    ${pills ? `<div class="pills">${pills}</div>` : ""}${body}${loadMore}`;
}

function renderFeed() {
  const openN = S.counts?.feed_open ?? feedState.total;
  const lastScan = S.scans.find((s) => s.status !== "running");
  const emptyHtml = S.sources.length ? `<div class="empty"><b>Your feed is empty</b>Run a scan to fill it.<div class="row"><button class="btn sm primary" data-act="scan" ${S.progress.running ? "disabled" : ""}>${I.play} New scan</button></div></div>`
    : `<div class="empty"><b>Your feed is empty</b>Describe what you want to monitor on Home or add a website directly.<div class="row"><a class="btn sm primary" href="#/home">Describe on Home</a><a class="btn sm" href="#/sources">Add source</a></div></div>`;
  return `<div class="page">${renderNotifyErrorsBanner()}${progressBox()}
    <div class="head"><div><h1>Feed</h1><p>${openN} open item${openN === 1 ? "" : "s"}${lastScan ? `, last scan ${ago(lastScan.started)}` : ""}</p></div>
      <button type="button" class="btn sm ghost" data-dl="/api/export.csv" data-fn="lookout.csv">${I.dl} Export CSV</button></div>
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
function sourceEditForm(s, compact = false) {
  const watchOpts = `<option value="">Global (no watch)</option>` + (S.watches || []).map((w) => `<option value="${w.id}" ${s.watch_id === w.id ? "selected" : ""}>${esc(w.title)}</option>`).join("");
  return `<form id="editSrcForm" data-sid="${s.id}" class="inline-edit ${compact ? "side-edit" : ""}">
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
  if (!w) return `<div class="page"><div class="empty"><b>This watch no longer exists</b><div class="row"><a class="btn sm" href="#/home">Go to Home</a></div></div></div>`;
  const wSources = S.sources.filter((s) => s.watch_id === w.id);
  const wFilters = S.filters.filter((f) => f.watch_id === w.id);
  const globalFCount = S.filters.filter((f) => !f.watch_id && f.active).length;
  const dRunning = S.discovery.running && S.discovery.watch_id === w.id;
  const discProg = dRunning ? `<div class="progress"><div class="row"><b>Finding sources and filters…</b><span class="grow"></span><a class="btn sm ghost" href="#/search/${S.discovery.id}">Details</a><button class="btn sm ghost" data-act="cancel-disc">${I.stop} Stop</button></div>
    <div class="muted sub-note">Your agent is looking for pages and turning your request into filters. Found ${wSources.length} source${wSources.length === 1 ? "" : "s"} so far.</div><div class="pbar"><i class="indet"></i></div></div>` : "";
  const emptyHtml = !wSources.length ? `<div class="empty"><b>No sources in this watch yet</b>Add a source on the right to start monitoring.<div class="row"><a class="btn sm" href="#/home">Search with AI</a></div></div>`
    : `<div class="empty"><b>No matching items yet</b>Scan this watch to check its sources.<div class="row"><button class="btn sm primary" data-watch-scan="${w.id}" ${S.progress.running ? "disabled" : ""}>${I.play} Scan watch</button></div></div>`;
  const srows = wSources.map((s) => {
    if (editingSourceId === s.id) return sourceEditForm(s, true);
    const l = s.last;
    const stText = s.paused ? "paused" : !l ? "not scanned yet" : l.ok ? `${l.count} items` : l.error === "cancelled" ? "cancelled" : "failed";
    const fcBadge = s.filters_changed && s.has_extractor ? `<span class="tag warn">filters changed</span>` : "";
    const resumeBtn = s.paused ? `<button class="btn sm" data-src-resume="${s.id}">Resume</button>` : "";
    const relearnBtn = s.filters_changed && s.has_extractor ? `<button class="btn sm ghost" data-src-relearn="${s.id}" title="Relearn reader for updated filters">Relearn</button>` : "";
    return `<div class="wrow ${s.active && !s.paused ? "" : "off"}"><label class="switch" title="Watch this source"><input type="checkbox" data-src-active="${s.id}" ${s.active ? "checked" : ""}><i></i></label>
      <div class="wt"><b title="${esc(s.url)}">${esc(s.name)}</b><small>${esc(stText)}</small>${fcBadge}</div>
      <div class="row row-tight">${resumeBtn}${relearnBtn}<button class="ib" data-src-edit="${s.id}" data-tip="Edit source" aria-label="Edit source">${I.edit}</button><button class="ib" data-src-test="${s.id}" data-tip="Test source" aria-label="Test source">${I.search}</button><button class="ib" data-src-scan="${s.id}" data-tip="Scan source" aria-label="Scan source" ${S.progress.running ? "disabled" : ""}>${I.play}</button><button class="ib" data-src-del="${s.id}" data-tip="Delete source" aria-label="Delete source">${I.trash}</button></div></div>`;
  }).join("") || `<div class="wcard-note">No sources yet.</div>`;
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
  return `<div class="page wide">${discProg}${progressBox()}
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
      <aside class="watch-side">
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
          <div class="wcard-note">Also applies ${globalFCount} global filter${globalFCount === 1 ? "" : "s"} · <a href="#/filters">Edit global</a></div>
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
  return `<div class="ag-row ${open ? "open" : ""}">
    <div class="ag-date ${dt ? "" : "none"}">${dt ? `<b>${dt.getDate()}</b><span>${WD[dt.getDay()]} ${MON[dt.getMonth()]}</span>` : "<b>—</b><span>no date</span>"}</div>
    <button class="ag-main" data-ag-open="${esc(i.id)}" aria-expanded="${open}"><span class="t">${esc(i.title)}</span>
      <span class="s">${i.org ? `<span>${esc(i.org)}</span>` : ""}<span>${esc(i.source_name)}</span>${i.deadline ? `<span>deadline ${short(i.deadline)}</span>` : ""}${i.remind_on ? `<span class="rem">reminder ${short(i.remind_on)}</span>` : ""}${i.note && !open ? `<span>has notes</span>` : ""}</span></button>
    <div class="ag-left ${left.c}">${done ? "done" : left.t}</div>
    <div class="ag-acts"><button class="ib" data-pick="remind" data-rid="${esc(i.id)}" data-tip="Set a reminder" aria-label="Set a reminder"><svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10.5 20a1.5 1.5 0 0 0 3 0"/></svg></button>
      <button class="ib ${done ? "on green" : ""}" data-item="${esc(i.id)}" data-set="applied" data-tip="${done ? "Mark as not done" : "Mark as done"}" aria-label="${done ? "Mark as not done" : "Mark as done"}">${I.done}</button>
      <button class="ib" data-item="${esc(i.id)}" data-set="${done ? "applied" : "saved"}" data-unsave="1" data-tip="Remove from agenda" aria-label="Remove from agenda">${I.trash}</button></div>
    ${open ? `<div class="ag-detail"><textarea data-note="${esc(i.id)}" placeholder="Notes: what to prepare, who to contact, links…">${esc(i.note || "")}</textarea>
      <div class="links"><a class="btn sm" href="${esc(safeUrl(i.url))}" data-ext target="_blank" rel="noopener noreferrer">Open page</a>${i.summary ? `<span class="muted ag-sum">${esc(i.summary)}</span>` : ""}</div></div>` : ""}
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
  if (!saved.length) html = `<div class="empty mt-22"><b>Nothing in your agenda</b>Use the bookmark on any item in your feed to keep track of it here.<div class="row"><a class="btn sm" href="#/feed">Browse feed</a></div></div>`;
  if (done.length) html += `<details class="ag-done" data-key="ag-done"><summary>Done (${done.length})</summary><div class="ag-rows mt-8">${done.map(agRow).join("")}</div></details>`;
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
  const entry = (e, showDate) => `<div class="entry"><a href="${esc(safeUrl(e.i.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(e.i.title)}</a><div class="k ${e.kind}">${showDate ? short(e.date) + ", " : ""}${e.kind === "dl" ? "deadline" : "your reminder"}${e.i.org ? " · " + esc(e.i.org) : ""}</div></div>`;
  return `<div class="month"><div><div class="box"><div class="cal-head"><h2>${MON[m.getMonth()]} ${m.getFullYear()}</h2>
      <button class="btn sm ghost" data-cal="-1" aria-label="Previous month">‹</button><button class="btn sm" data-cal="0">Today</button><button class="btn sm ghost" data-cal="1" aria-label="Next month">›</button></div>
      <div class="cal">${cells}</div></div><div class="legend"><span><i class="dl"></i>Deadline</span><span><i></i>Your reminder</span></div></div>
    <div class="box dayp"><h3>${WD[sd.getDay()]}, ${sd.getDate()} ${MON[sd.getMonth()]}</h3><div class="sub">${dayEvs.length ? `${dayEvs.length} thing${dayEvs.length > 1 ? "s" : ""} on this day` : "Nothing on this day"}</div>
      ${dayEvs.map((e) => entry(e, false)).join("")}
      ${upcoming.length ? `<div class="sub mt-18">Coming up after this</div>${upcoming.map((e) => entry(e, true)).join("")}` : ""}</div></div>`;
}
function renderAgenda() {
  const n = agendaBase().filter((i) => i.action === "saved").length;
  return `<div class="page"><div class="head"><div><h1>Agenda</h1><p>${n ? `${n} item${n > 1 ? "s" : ""} you are tracking.` : "Items you save from the feed."} Deadlines come from the site; reminders are dates you pick.</p></div>
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
  placePop(pop, anchor, "below");
}
async function setReminder(id, date) { syncItem(id, { remind_on: date || null }); closePops(); render(); await api(`/api/items/${encodeURIComponent(id)}`, { remind_on: date || "" }); toast(date ? `Reminder set for ${short(date)}` : "Reminder removed"); }

// ---------- sources ----------
async function fetchSourceCode(sid) {
  if (sourceCodeCache[sid] !== undefined || sourceCodeLoading[sid]) return;
  sourceCodeLoading[sid] = true;
  try {
    const res = await api(`/api/sources/${sid}/code`);
    sourceCodeCache[sid] = res.extractor || res.last_good_extractor || "";
    if (route.view === "sources") render();
  } catch (err) {
    toast(err.message || "Could not load reader code");
  } finally {
    delete sourceCodeLoading[sid];
  }
}

function sourceRow(s) {
  const l = s.last, agent = s.method !== "plugin", w = s.watch_id ? watchById(s.watch_id) : null;
  const status = !l ? '<span class="tag">not scanned yet</span>' : l.ok ? `<span class="tag ok">${l.count} items</span>` : l.error === "cancelled" ? '<span class="tag muted">cancelled</span>' : '<span class="tag bad">failed</span>';
  const learn = !agent ? '<span class="tag">plugin</span>' : s.extractor_ok ? '<span class="tag accent" title="Read with code your agent wrote: seconds, no agent quota">learned</span>' : s.has_extractor ? '<span class="tag bad" title="The site changed; your agent will fix the reader on the next scan">needs relearning</span>' : '<span class="tag" title="On the first scan your agent browses the site and writes a reader for it">not learned yet</span>';
  const wBadge = w ? `<a class="tag accent no-dec" href="#/watch/${w.id}">${esc(w.title)}</a>` : "";
  const pausedBadge = s.paused ? `<span class="tag bad">paused</span>` : "";
  const blockedBadge = !s.paused && s.last_error_kind === "blocked" ? `<span class="tag bad">blocked</span>` : "";
  const learnCooldown = !s.paused && s.learn_retry_after ? `<span class="tag warn">relearn cooldown until ${esc(s.learn_retry_after.slice(11, 16))}</span>` : "";
  const backoff = !s.paused && s.last_error_kind === "transient" && s.retry_after ? `<span class="tag warn">retry after ${esc(s.retry_after.slice(11, 16))}</span>` : "";
  const filterChangedBadge = s.filters_changed && s.has_extractor ? `<span class="tag warn">filters changed</span>` : "";
  const cachedCode = sourceCodeCache[s.id];
  const codeBlock = s.has_extractor ? `<details data-key="src-${s.id}" data-code-id="${s.id}"><summary>View the reader your agent wrote${s.extractor_updated ? ", " + short(s.extractor_updated.slice(0, 10)) : ""}${s.extractor_runs ? `, used ${s.extractor_runs} time${s.extractor_runs > 1 ? "s" : ""}` : ""}</summary>${cachedCode !== undefined ? `<div class="code">${esc(cachedCode)}</div>` : `<div class="muted fs-125 mt-8">Loading reader code…</div>`}</details>` : "";
  const editBlock = editingSourceId === s.id ? sourceEditForm(s, false) : "";
  return `<div class="srow"><label class="switch" title="Watch this source"><input type="checkbox" data-src-active="${s.id}" ${s.active ? "checked" : ""}><i></i></label>
    <div class="minw-0"><h4>${esc(s.name)} ${wBadge} ${learn} ${status} ${pausedBadge} ${blockedBadge} ${learnCooldown} ${backoff} ${filterChangedBadge}</h4><div class="u">${esc(s.url)}</div>
      ${s.notes && agent ? `<div class="n">${esc(s.notes)}</div>` : ""}${l && !l.ok && l.error !== "cancelled" ? `<div class="err">${esc(cleanErr(l.error))}</div>` : ""}${s.extractor_error && agent && (!s.extractor_ok || s.last_error_kind === "transient" || s.last_error_kind === "blocked" || s.paused) ? `<div class="err">${esc(cleanErr(s.extractor_error))}</div>` : ""}
      ${codeBlock}${editBlock}</div>
    <div class="row">${s.paused ? `<button type="button" class="btn sm primary" data-src-resume="${s.id}">Resume</button>` : ""}<button type="button" class="btn sm ghost" data-src-test="${s.id}">Test</button>${agent ? `<button type="button" class="btn sm ghost" data-src-edit="${s.id}">Edit</button>${s.has_extractor ? `<button type="button" class="btn sm ghost" data-src-relearn="${s.id}">Relearn</button>` : ""}` : ""}
      <button class="btn sm" data-src-scan="${s.id}" ${S.progress.running ? "disabled" : ""}>${I.play} Scan</button><button class="btn sm ghost danger" data-src-del="${s.id}" aria-label="Delete source">${I.trash}</button></div></div>`;
}
function renderSources() {
  const globalS = S.sources.filter((s) => !s.watch_id), watchS = S.sources.filter((s) => s.watch_id);
  return `<div class="page"><div class="head"><div><h1>Sources</h1><p>Websites Lookout watches. On the first scan your agent browses each one and writes a small reader for it. After that, a scan takes seconds.</p></div>
      <a class="btn sm ghost" href="#/home">Find sources with AI</a></div>
    <div class="box pad"><form id="addSrc" class="grid2">
      <label class="f">Name<input type="text" name="name" required placeholder="Acme careers"></label>
      <label class="f">Address of the page that lists the items<input type="url" name="url" required placeholder="https://"></label>
      <label class="f span-all">Notes for your agent <small>optional: which tab or filter to use, where the dates are</small><textarea name="notes" class="ta-sm" placeholder="Use the Internships filter. Deadlines are on each detail page."></textarea></label>
      <div class="row span-all"><button type="button" class="btn ghost" data-act="test-new-source">Test this source</button><span class="grow"></span><button class="btn primary">Add global source</button></div></form></div>
    ${renderSourcePreviewBox()}
    <div class="h2">Global sources <span>${globalS.length}</span></div>
    ${globalS.length ? `<div class="box">${globalS.map(sourceRow).join("")}</div>` : `<div class="empty"><b>No global sources yet</b>Add a page above, or describe what you want on <a href="#/home">Home</a> to create a watch.</div>`}
    ${watchS.length ? `<div class="h2">Watch sources <span>${watchS.length}</span></div><div class="box">${watchS.map(sourceRow).join("")}</div>` : ""}</div>`;
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
    return `<div class="frow"><span class="tag accent">Watch</span><span></span><a href="#/watch/${w.id}" class="link-bold">${esc(w.title)}</a><span class="muted">${n} active</span></div>`;
  }).join("");
  return `<div class="page"><div class="head"><div><h1>Filters</h1><p>Global filters apply to every watch and global source. Editing filters re-evaluates deterministic exclusions/deadlines immediately and queues semantic filters for the next scan.</p></div></div>
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
function renderSettings() {
  if (!AG) { ensureAgents().then(render); return `<div class="page"><div class="head"><div><h1>Settings</h1><p>Looking for AI agents on this computer…</p></div></div></div>`; }
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
  return `<div class="page">${renderNotifyErrorsBanner()}<div class="head"><div><h1>Settings</h1><p>Stored only on this computer.</p></div></div>
    ${AG.browser.installed ? "" : `<div class="banner">${I.warn}<div><b>agent-browser is missing</b><div class="muted fs-13 mt-8">Agents use it to drive a real browser. Install it once in a terminal:</div><div class="cmd mt-8">${esc(AG.browser.install)}</div></div></div>`}
    <div class="h2">AI agent</div><div class="box">${agents}</div>
    <div class="box pad mt-10"><div class="kv">
      <div class="f">Model<div>${cur ? `<button type="button" class="chip model" data-pick="model">${modelChipText(st.model, cur)} ${I.down}</button>` : '<span class="muted">Choose an agent first</span>'}</div></div>
      <label class="f">Minutes your agent may spend on one site<input type="number" id="timeoutIn" min="3" max="60" value="${esc(st.agent_timeout_min)}"></label>
      <label class="check"><input type="checkbox" id="headedIn" ${st.headed === "1" ? "checked" : ""}> Show the browser while your agent works</label>
      <label class="f">Sites your agent works on at once<input type="number" id="parallelIn" min="1" max="8" value="${esc(st.parallel)}"></label>
      <label class="check span-all"><input type="checkbox" id="verifyIn" ${st.verify_weekly === "1" ? "checked" : ""}> Once a week, check learned sites and relearn only if results look anomalous</label>
    </div><p class="muted fs-125 mt-12">Agent tools don't report how much quota is left. Lookout counts the calls it makes, remembers the last quota error and skips an agent until its quota resets.</p>
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
    <div class="h2">Backup and restore</div><div class="box pad"><div class="setrow"><div><b>Watches, sources and filters</b><small>Export or restore your setup as JSON (passwords are never included).</small></div>
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
  const srows = d.sources.map((s) => `<tr><td><span class="dot ${s.ok ? "" : "bad"}"></span>${esc(s.source_name)}</td><td>${s.ok ? `${s.count} items${s.new ? `, <b>${s.new} new</b>` : ""}` : s.error === "cancelled" ? '<span class="muted">cancelled</span>' : `<span class="err">${esc(cleanErr(s.error))}</span>`}</td><td class="muted num-col">${(s.ms / 1000).toFixed(1)} s</td></tr>`).join("");
  return `<div class="page">${S.progress.running && S.progress.scan_id === d.id ? progressBox() : ""}
    <div class="head"><div><h1>${esc(d.label || (d.trigger === "scheduled" ? "Daily scan" : "Scan"))}, ${short(d.started.slice(0, 10))} at ${d.started.slice(11, 16)}</h1>
      <p>${d.agent ? `${esc(agentName(d.agent))}${d.model ? ", " + esc(d.model) : ""}` : "Plugins only"}, ${statusStr}${d.note ? ". " + esc(d.note) : ""}</p></div>
      <div class="row">${w ? `<a class="btn sm ghost" href="#/watch/${w.id}">${I.watch} Open watch</a>` : ""}<button class="btn sm ghost danger" data-scan-del="${d.id}" ${running ? "disabled" : ""}>${I.trash} Delete</button></div></div>
    <div class="stats"><div class="stat"><b>${d.found}</b><span>matching items</span></div><div class="stat"><b>${d.new}</b><span>new</span></div><div class="stat"><b>${ok} of ${d.sources.length}</b><span>sources read</span></div><div class="stat"><b>${hidden}</b><span>filtered out</span></div></div>
    ${d.filters.length ? `<div class="h2">Filters used</div><div class="preview">${esc(filterText(d.filters))}</div>` : ""}
    <div class="h2">Sources</div><div class="box"><table class="t"><tbody>${srows || '<tr><td class="muted">No sources were scanned.</td></tr>'}</tbody></table></div>
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
    : (running || banner) ? "" : `<div class="empty"><b>No sources suggested</b>Try describing what you want in a different way on Home.<div class="row"><a class="btn sm" href="#/home">Back to Home</a></div></div>`;
  return `<div class="page">${prog}
    <div class="head"><div><h1>${esc(d.title || "Search")}</h1>
      <p>“${esc(d.request)}”${meta ? ` · ${esc(meta)}` : ""}</p></div>
      <div class="row">${w ? `<a class="btn sm" href="#/watch/${w.id}">${I.watch} Open watch</a>` : ""}<button class="btn sm ghost danger" data-disc-del="${d.id}" ${running ? "disabled" : ""}>${I.trash} Delete</button></div></div>
    ${banner}${list}</div>`;
}

// ---------- render & polling ----------
let lastHtml = null, lastTag = null;
function render() {
  if (!S) return;
  renderSide();
  const v = route.view;
  const html = v === "feed" ? renderFeed() : v === "agenda" ? renderAgenda() : v === "sources" ? renderSources() : v === "filters" ? renderFilters() : v === "settings" ? renderSettings() : v === "watch" ? renderWatch() : v === "scan" ? renderScan() : v === "search" ? renderSearch() : renderHome();
  const tag = v === "settings" && AG ? "settings:ag" : v === "scan" ? `scan:${route.id}` : v === "search" ? `search:${route.id}` : v === "watch" ? `watch:${route.id}` : v, main = $("#main");
  const force = render.force; render.force = false;
  const same = main.dataset.view === tag;
  // Settings is a form: background refreshes must not wipe what is being typed.
  if (tag === "settings:ag" && same && !force) return;
  if (same && tag === lastTag && html === lastHtml) return;
  const act = document.activeElement, keepId = act && act.id, pos = act && act.selectionStart;
  const scrollTop = same ? main.scrollTop : 0;
  const open = same ? [...main.querySelectorAll("details[open][data-key]")].map((d) => d.dataset.key) : [];
  main.innerHTML = html; main.dataset.view = tag; lastHtml = html; lastTag = tag;
  for (const k of open) { const d = main.querySelector(`details[data-key="${CSS.escape(k)}"]`); if (d) d.open = true; }
  if (keepId && $("#" + keepId)) { const el = $("#" + keepId); el.focus({ preventScroll: true }); try { el.setSelectionRange(pos, pos); } catch (_) {} }
  main.scrollTop = scrollTop;
}

const isBusy = () => Boolean(S && (S.progress?.running || S.discovery?.running || S.test_job?.running));
let timer = null, wasBusy = false, lastScanDone = 0;

async function pollProgress() {
  clearTimeout(timer);
  try {
    const p = await api("/api/progress");
    if (!S) return refresh(true);
    const prevDone = lastScanDone;
    S.progress = p.progress;
    S.discovery = p.discovery;
    S.test_job = p.test_job;
    S.counts = p.counts;
    lastScanDone = p.progress?.done || 0;
    const nowBusy = isBusy();
    if (route.view === "scan" && (nowBusy || scanCache[route.id]?.status === "running")) loadScan(route.id);
    if (route.view === "search" && (nowBusy || discCache[route.id]?.status === "running")) loadDiscovery(route.id);
    if ((wasBusy && !nowBusy) || (nowBusy && lastScanDone > prevDone)) {
      wasBusy = nowBusy;
      return refresh(true);
    }
    const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && document.activeElement.id !== "q";
    if (!popEl && !typing) render(); else renderSide();
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
    S = await api("/api/state");
    sessionStorage.removeItem("lookout-reauth-ts");
    lastScanDone = S.progress?.done || 0;
    if (S.settings.agent && !modelCache[S.settings.agent]) modelsFor(S.settings.agent).then(() => { render.force = true; render(); }).catch((err) => { if (err?.status === 401 || err?.status === 403) handleAuthFailure(); });
    if (!AG) ensureAgents().then(() => render()).catch((err) => { if (err?.status === 401 || err?.status === 403) handleAuthFailure(); });
    const busy = isBusy();
    if (route.view === "scan" && (wasBusy || busy || scanCache[route.id]?.status === "running")) loadScan(route.id);
    if (route.view === "search" && (wasBusy || busy || discCache[route.id]?.status === "running")) loadDiscovery(route.id);
    if (reloadItemsToo || ((route.view === "feed" || route.view === "watch") && !feedState.loading && (!feedState.initialized || feedState.key !== itemsScopeKey()))) {
      loadItems(false);
    }
    const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && document.activeElement.id !== "q";
    if (!popEl && !typing) render(); else renderSide();
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
    if (d.srcDel) { if (!confirm("Delete this source and its collected items?")) return; await api(`/api/sources/${d.srcDel}/delete`, {}); return refresh(true); }
    if (d.srcEdit) { const id = Number(d.srcEdit); editingSourceId = editingSourceId === id ? null : id; return render(); }
    if (d.cand !== undefined) { const c = S.discovery.results[Number(d.cand)]; await api("/api/sources", { name: c.name, url: c.url, notes: c.notes || "", watch_id: S.discovery.watch_id || null }); toast(`Added ${c.name}`); return refresh(false); }
    if (d.discCand !== undefined) { const dd = discCache[Number(d.discId)]; const c = dd?.results?.[Number(d.discCand)]; if (!c) return; await api("/api/sources", { name: c.name, url: c.url, notes: c.notes || "", watch_id: dd.watch_id || null }); toast(`Added ${c.name}`); return refresh(false); }
    if (d.act === "clear-disc") { await api("/api/discover/clear", {}); return refresh(false); }
    if (d.fEdit) { const id = Number(d.fEdit); editingFilterId = editingFilterId === id ? null : id; return render(); }
    if (d.fDel) { await api(`/api/filters/${d.fDel}/delete`, {}); return refresh(true); }
    if (d.scanDel) { if (!confirm("Delete this scan from history? Items it found stay in your feed.")) return; await api(`/api/scans/${d.scanDel}/delete`, {}); delete scanCache[Number(d.scanDel)]; location.hash = "#/home"; return refresh(false); }
    if (d.discDel) { if (!confirm("Delete this search from history?")) return; await api(`/api/discoveries/${d.discDel}/delete`, {}); delete discCache[Number(d.discDel)]; location.hash = "#/home"; return refresh(false); }
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
    if (d.watchDel) { if (!confirm("Delete this watch along with its sources, filters, scans and items?")) return; await api(`/api/watches/${d.watchDel}/delete`, {}); location.hash = "#/feed"; return refresh(true); }
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
  if (e.target.id === "q") {
    ui.q = e.target.value.trim();
    clearTimeout(qTimer);
    qTimer = setTimeout(() => reloadActiveItems(), 200);
  }
  if (e.target.id === "ask") { e.target.style.height = "auto"; e.target.style.height = Math.min(220, e.target.scrollHeight) + "px"; }
});

document.addEventListener("submit", async (e) => {
  e.preventDefault(); const f = e.target, data = Object.fromEntries(new FormData(f));
  try {
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
      if (r.started && r.id) { toast("Watch created, searching for sources"); location.hash = `#/search/${r.id}`; return; }
      toast("Already searching"); return pollProgress();
    }
  } catch (err) {
    if (err.status === 401 || err.status === 403) { if (!handleAuthFailure()) showSessionExpired(); return; }
    toast(err.message);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closePops(); if (editingSourceId || editingFilterId || editingWatchId) { editingSourceId = editingFilterId = editingWatchId = null; render(); } }
  if (e.key === "Enter" && !e.shiftKey && e.target.id === "ask") { e.preventDefault(); e.target.form.requestSubmit(); }
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { const q = $("#q") || $("#ask"); if (q) { e.preventDefault(); q.focus(); } }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });
parseRoute(); refresh(true);
