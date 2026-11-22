"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
  warn: '<svg viewBox="0 0 24 24"><path d="M12 3 2 20h20zM12 10v4M12 17h.01"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
  right: '<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  bot: '<svg viewBox="0 0 24 24"><rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M9 13h.01M15 13h.01"/></svg>',
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 2.5 14.4 0 17M12 3.5c-2.5 2.6-2.5 14.4 0 17"/></svg>',
  funnel: '<svg viewBox="0 0 24 24"><path d="M4 5h16l-6 8v5l-4 2v-7z"/></svg>',
  dl: '<svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
};
const SCOPE = `<svg viewBox="0 0 120 120" aria-hidden="true"><defs><radialGradient id="sw" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(60 60) scale(56)"><stop offset="0" stop-color="#7B9CFF" stop-opacity=".55"/><stop offset="1" stop-color="#7B9CFF" stop-opacity="0"/></radialGradient></defs>
  <circle cx="60" cy="60" r="56" fill="none" stroke="#26262B"/><circle cx="60" cy="60" r="38" fill="none" stroke="#1E1E23"/><circle cx="60" cy="60" r="20" fill="none" stroke="#1E1E23"/>
  <path d="M60 4v112M4 60h112" stroke="#18181C"/><g class="sweep"><path d="M60 60 L60 4 A56 56 0 0 1 108.5 32 Z" fill="url(#sw)"/><path d="M60 60 L108.5 32" stroke="#7B9CFF" stroke-width="1.6"/></g>
  <circle cx="83" cy="40" r="2.6" fill="#7B9CFF"/><circle cx="41" cy="78" r="2" fill="#7B9CFF" opacity=".6"/><circle cx="60" cy="60" r="3" fill="#EDEDEF"/></svg>`;

let S = null, AG = null, route = { view: "home" };
const ui = JSON.parse(localStorage.getItem("lookout-ui") || "{}");
ui.sort ??= "deadline"; ui.cats ??= []; ui.src ??= ""; ui.q = ""; ui.calMonth ??= null;
const keep = () => localStorage.setItem("lookout-ui", JSON.stringify({ ...ui, q: "" }));
document.body.classList.add("browser");
window.addEventListener("pywebviewready", () => document.body.classList.remove("browser"));

const api = (path, body) => fetch(path, body === undefined ? { cache: "no-store" } : {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(async (r) => {
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
});
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 3200); }

// ---------- dates ----------
const today0 = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseIso = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const daysTo = (s) => (s ? Math.round((parseIso(s) - today0()) / 864e5) : null);
const short = (s) => { const d = parseIso(s); return `${d.getDate()} ${MON[d.getMonth()]}` + (d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : ""); };
const target = (i) => i.deadline || i.start_date || null;
const isNew = (i) => Date.now() - new Date(i.first_seen.replace(" ", "T")).getTime() < 72 * 36e5;
function ago(ts) {
  const m = Math.round((Date.now() - new Date(ts.replace(" ", "T")).getTime()) / 6e4);
  if (m < 2) return "just now"; if (m < 60) return `${m} minutes ago`;
  const h = Math.round(m / 60); if (h < 24) return `${h} hour${h > 1 ? "s" : ""} ago`;
  const d = Math.round(h / 24); return d === 1 ? "yesterday" : `${d} days ago`;
}
function duration(a, b) { if (!b) return ""; const s = Math.round((new Date(b.replace(" ", "T")) - new Date(a.replace(" ", "T"))) / 1000); return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`; }
const openItems = () => S.items.filter((i) => i.status === "open" && i.action !== "hidden" && i.action !== "applied" && i.match !== 0 && !(target(i) && daysTo(target(i)) < 0));

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
const modelCache = {};
async function modelsFor(agent) {
  if (!agent || agent === "custom") return [];
  if (!modelCache[agent]) modelCache[agent] = (await api(`/api/agents/${agent}/models`)).models;
  return modelCache[agent];
}
async function ensureAgents() { if (!AG) AG = await api("/api/agents"); return AG; }
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
    const sub = document.createElement("div"); sub.className = "pop"; sub.style.minWidth = "180px"; subEl = sub;
    sub.innerHTML = `<div class="t">${esc(f.label)}</div>` + f.variants.map((v) => `<button class="o ${v.id === cur ? "on" : ""}" data-model="${esc(v.id)}">${effortLabel(v.effort) || "Standard"}</button>`).join("");
    placePop(sub, btn, "right");
  };
  pop.querySelectorAll(".fam").forEach((b) => { b.addEventListener("mouseenter", () => openFam(b)); b.addEventListener("click", (e) => { e.stopPropagation(); openFam(b); }); });
  pop.querySelectorAll(".o:not(.fam)").forEach((b) => b.addEventListener("mouseenter", () => { subEl?.remove(); subEl = null; pop.querySelectorAll(".fam").forEach((x) => x.classList.remove("hl")); }));
}
async function openAgentPicker(anchor) {
  closePops(); await ensureAgents();
  const pop = document.createElement("div"); pop.className = "pop"; popEl = pop;
  pop.innerHTML = `<div class="t">Agents on this computer</div>` + AG.agents.filter((a) => a.id !== "custom" || a.command).map((a) =>
    `<button class="o ${S.settings.agent === a.id ? "on" : ""}" data-agent="${a.id}" ${a.installed ? "" : "disabled"}>${esc(a.name)}<span class="r">${a.installed ? "" : "not installed"}</span></button>`).join("") +
    `<hr><button class="o" data-goto="#/settings">Agent settings</button>`;
  placePop(pop, anchor);
}
async function setModel(model) { await api("/api/settings", { model }); S.settings.model = model; closePops(); toast("Model changed"); render.force = true; render(); }
document.addEventListener("mousedown", (e) => { if (popEl && !popEl.contains(e.target) && !subEl?.contains(e.target) && !e.target.closest("[data-pick]")) closePops(); });

// ---------- routing ----------
function parseRoute() { const [view, id] = location.hash.replace(/^#\/?/, "").split("/"); route = { view: view || "home", id: id ? Number(id) : null }; }
window.addEventListener("hashchange", () => { closePops(); parseRoute(); render(); refresh(); });

// ---------- sidebar ----------
function renderSide() {
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === route.view));
  const open = openItems(), fresh = open.filter(isNew).length;
  $("#navFeed").textContent = open.length || ""; $("#navFeed").classList.toggle("hot", fresh > 0);
  $("#navAgenda").textContent = S.items.filter((i) => i.action === "saved").length || "";
  $("#navSources").textContent = S.sources.filter((s) => s.active).length || "";
  $("#navFilters").textContent = S.filters.filter((f) => f.active).length || "";
  const p = S.progress, btn = $("#scanBtn");
  btn.disabled = p.running; btn.classList.toggle("spin", p.running);
  $("#scanLabel").textContent = p.running ? "Scanning" : "New scan";
  $("#scanSub").textContent = p.running ? `${p.done} of ${p.total}` : "";
  let html = "", last = "";
  for (const s of S.scans) {
    const day = s.started.slice(0, 10), t0 = today0();
    const label = day === iso(t0) ? "Today" : day === iso(new Date(t0 - 864e5)) ? "Yesterday" : short(day);
    if (label !== last) { html += `<div class="day">${label}</div>`; last = label; }
    const right = s.status === "running" ? '<i class="live"></i>' : s.status === "failed" ? '<span class="bad">failed</span>' : s.new ? `<span class="new">+${s.new}</span>` : `<span>${s.found}</span>`;
    html += `<a href="#/scan/${s.id}" class="${route.view === "scan" && route.id === s.id ? "on" : ""}" title="${s.found} matching, ${s.new} new"><b>${s.trigger === "scheduled" ? "Daily scan" : "Scan"} at ${s.started.slice(11, 16)}</b>${right}</a>`;
  }
  $("#history").innerHTML = html || `<div class="day">Your scans will show up here.</div>`;
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
  const a = i.action, who = [i.org, i.location].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("");
  const b = (act, tip, on, cls = "") => `<button class="ib ${on ? "on" : ""} ${cls}" data-item="${esc(i.id)}" data-set="${act}" data-tip="${tip}" aria-label="${tip}">${act === "hidden" && on ? I.undo : I[{ saved: "save", applied: "done", hidden: "hide" }[act]]}</button>`;
  return `<article class="item">${whenBox(i)}<div class="ibody">
      <div class="meta">${(opts.isNew ?? isNew(i)) ? '<span class="tag new">New</span>' : ""}${i.category ? `<span class="tag accent">${esc(i.category)}</span>` : ""}<span class="tag">${esc(i.source_name)}</span>${i.status === "gone" ? '<span class="tag bad">no longer listed</span>' : ""}</div>
      <h3><a href="${esc(safeUrl(i.url))}" data-ext target="_blank" rel="noopener noreferrer">${esc(i.title)}</a></h3>
      ${who ? `<div class="who">${who}</div>` : ""}${i.summary ? `<p class="sum">${esc(i.summary)}</p>` : ""}${i.note ? `<div class="note">${esc(i.note)}</div>` : ""}</div>
    <div class="acts">${b("saved", a === "saved" ? "Remove from agenda" : "Add to agenda", a === "saved")}${b("applied", a === "applied" ? "Mark as not done" : "Mark as done", a === "applied", "green")}${b("hidden", a === "hidden" ? "Unhide" : "Hide", a === "hidden")}</div></article>`;
}
function progressBox() {
  const p = S.progress; if (!p.running) return "";
  const what = p.learning && p.learning.length ? `Your agent is learning ${esc(p.learning.join(", "))}. This happens once per site and takes a few minutes.` : p.current.length ? `Reading ${esc(p.current.join(", "))}` : "";
  return `<div class="progress"><div class="row"><b>Scanning ${p.done} of ${p.total} sources</b><span class="grow"></span><button class="btn sm ghost" data-act="cancel">Stop</button></div>
    <div class="muted" style="font-size:13px;margin-top:2px">${what}</div><div class="pbar"><i style="width:${p.total ? Math.round((p.done / p.total) * 100) : 3}%"></i></div></div>`;
}

// ---------- home ----------
function renderHome() {
  const st = S.settings, d = S.discovery, open = openItems();
  const fresh = open.filter(isNew).length, week = open.filter((i) => { const x = daysTo(i.deadline); return x !== null && x <= 7; }).length;
  const hour = new Date().getHours(), greet = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const cands = d.results.map((c, k) => { const have = S.sources.some((s) => s.url === c.url); return `<div class="cand"><div><h4>${esc(c.name)}</h4><div class="u">${esc(c.url)}</div><p>${esc(c.why || "")}</p></div>
      <button class="btn sm" data-cand="${k}" ${have ? "disabled" : ""}>${have ? "Added" : "Add source"}</button></div>`; }).join("");
  return `<div class="home">${S.progress.running ? `<div style="width:min(760px,100%)">${progressBox()}</div>` : ""}
    <div class="scope">${SCOPE}</div>
    <h1>${greet}. What should Lookout watch?</h1>
    <p class="lede">Describe it in your own words. Your agent searches the web, checks each page and suggests sources to follow.</p>
    <form class="composer" id="discover">
      <div class="ctx"><button type="button" class="chip outlined" data-pick="agent">${I.bot} ${st.agent ? esc(agentName(st.agent)) : "Choose an agent"} ${I.down}</button>
        <a class="chip" href="#/sources">${I.globe} ${S.sources.filter((s) => s.active).length} sources</a>
        <a class="chip" href="#/filters">${I.funnel} ${S.filters.filter((f) => f.active).length} filters</a></div>
      <div class="field"><textarea name="request" id="ask" rows="2" placeholder="Machine-learning internships in Berlin, or new hackathons with cash prizes" ${d.running ? "disabled" : ""}>${esc(d.request)}</textarea>
        <div class="bar"><a class="chip" href="#/sources" title="Add a website yourself" aria-label="Add a website yourself">${I.plus}</a>
          ${st.agent ? `<button type="button" class="chip model" data-pick="model">${modelChipText(st.model, st.agent)} ${I.down}</button>` : ""}<span class="grow"></span>
          ${d.running ? '<span class="muted" style="font-size:13px">Searching the web</span>' : ""}
          <button type="button" class="chip" data-act="scan" ${S.progress.running ? "disabled" : ""}>${I.play} Scan now</button>
          <button class="send" aria-label="Find sources" ${d.running || !st.agent ? "disabled" : ""}>${d.running ? '<svg class="spin" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4"/></svg>' : I.up}</button></div></div>
    </form>
    ${d.error ? `<p style="color:var(--red);font-size:13px;margin:12px 0 0">${esc(d.error)}</p>` : ""}
    ${cands ? `<div class="found"><div class="h2">Suggested sources <span>${d.results.length}</span><span class="grow"></span><button class="btn sm ghost" data-act="clear-disc">Clear</button></div><div class="list">${cands}</div></div>` : ""}
    <div class="glance">${S.scans.length ? `<a href="#/feed"><b class="${fresh ? "hot" : ""}">${fresh}</b> new in the last 3 days</a><a href="#/feed"><b class="${week ? "warn" : ""}">${week}</b> closing this week</a><a href="#/agenda"><b>${S.items.filter((i) => i.action === "saved").length}</b> in your agenda</a>` : !st.agent ? `<a href="#/settings">Start by choosing your agent</a>` : `<a href="#/sources">Add a website to watch</a>`}</div></div>`;
}

// ---------- feed ----------
function renderFeed() {
  const base = openItems(), cats = {};
  base.forEach((i) => { const c = i.category || "Other"; cats[c] = (cats[c] || 0) + 1; });
  const srcs = [...new Set(base.map((i) => i.source_name))].sort();
  let list = base.filter((i) => (!ui.cats.length || ui.cats.includes(i.category || "Other")) && (!ui.src || i.source_name === ui.src));
  if (ui.q) list = list.filter((i) => { const h = `${i.title} ${i.org} ${i.summary} ${i.category} ${i.location} ${i.source_name}`.toLowerCase(); return ui.q.toLowerCase().split(/\s+/).every((w) => h.includes(w)); });
  list.sort(ui.sort === "new" ? (a, b) => b.first_seen.localeCompare(a.first_seen) : (a, b) => (target(a) || "9999").localeCompare(target(b) || "9999") || b.first_seen.localeCompare(a.first_seen));
  const lastScan = S.scans.find((s) => s.status !== "running");
  let body = "";
  if (!list.length) body = `<div class="empty"><b>${base.length ? "Nothing matches" : "Your feed is empty"}</b>${base.length ? "Try a different search or category." : S.sources.length ? "Run a scan to fill it." : 'Add a website to watch in <a href="#/sources">Sources</a>.'}</div>`;
  else if (ui.sort === "deadline") {
    let rest = list;
    for (const [name, test] of [["This week", (d) => d !== null && d <= 7], ["This month", (d) => d !== null && d <= 31], ["Later", (d) => d !== null], ["No date", () => true]]) {
      const part = rest.filter((i) => test(daysTo(target(i)))); rest = rest.filter((i) => !test(daysTo(target(i))));
      if (part.length) body += `<div class="h2">${name} <span>${part.length}</span></div><div class="list">${part.map((i) => itemCard(i)).join("")}</div>`;
    }
  } else body = `<div class="h2">Newest first <span>${list.length}</span></div><div class="list">${list.map((i) => itemCard(i)).join("")}</div>`;
  return `<div class="page">${progressBox()}
    <div class="head"><div><h1>Feed</h1><p>${base.length} open item${base.length === 1 ? "" : "s"}${lastScan ? `, last scan ${ago(lastScan.started)}` : ""}</p></div>
      <a class="btn sm ghost" href="/api/export.csv" download="lookout.csv">${I.dl} Export CSV</a></div>
    <div class="toolbar"><div class="search">${I.search}<input type="search" id="q" placeholder="Search titles, organisations, places" value="${esc(ui.q)}"></div>
      <select id="srcSel" aria-label="Source"><option value="">All sources</option>${srcs.map((s) => `<option ${s === ui.src ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
      <select id="sortSel" aria-label="Sort"><option value="deadline" ${ui.sort === "deadline" ? "selected" : ""}>By date</option><option value="new" ${ui.sort === "new" ? "selected" : ""}>Newest first</option></select></div>
    <div class="pills">${Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([c, n]) => `<button class="pill ${ui.cats.includes(c) ? "on" : ""}" data-cat="${esc(c)}">${esc(c)} <em>${n}</em></button>`).join("")}</div>${body}</div>`;
}

// ---------- agenda ----------
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
ui.agView ??= "upcoming";
let agOpen = null, agDay = null;
function agendaEntries() {
  // One entry per date: the deadline and, separately, the reminder the user picked.
  const out = [];
  for (const i of S.items.filter((x) => x.action === "saved" || x.action === "applied")) {
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
      <div class="links"><a class="btn sm" href="${esc(safeUrl(i.url))}" data-ext target="_blank" rel="noopener noreferrer">Open page</a>${i.summary ? `<span class="muted" style="font-size:12.5px;max-width:260px;text-align:right">${esc(i.summary)}</span>` : ""}</div></div>` : ""}
  </div>`;
}
function renderUpcoming() {
  const saved = S.items.filter((i) => i.action === "saved"), done = S.items.filter((i) => i.action === "applied");
  const key = (i) => { const r = i.remind_on && daysTo(i.remind_on) >= 0 ? i.remind_on : null; const c = [r, i.deadline].filter(Boolean).sort()[0]; return c || null; };
  const groups = [["Overdue", (d) => d !== null && d < 0, "overdue"], ["Today", (d) => d === 0], ["Tomorrow", (d) => d === 1], ["This week", (d) => d !== null && d <= 7], ["Next week", (d) => d !== null && d <= 14], ["Later", (d) => d !== null], ["No date", () => true]];
  let rest = [...saved].sort((a, b) => (key(a) || "9999").localeCompare(key(b) || "9999")), html = "";
  for (const [name, test, cls] of groups) {
    const part = rest.filter((i) => test(daysTo(key(i)))); rest = rest.filter((i) => !test(daysTo(key(i))));
    if (part.length) html += `<section class="ag-group ${cls || ""}"><h3>${name} <span>${part.length}</span></h3><div class="ag-rows">${part.map(agRow).join("")}</div></section>`;
  }
  if (!saved.length) html = `<div class="empty" style="margin-top:22px"><b>Nothing in your agenda</b>Use the bookmark on any item in your feed to keep track of it here.</div>`;
  if (done.length) html += `<details class="ag-done" data-key="ag-done"><summary>Done (${done.length})</summary><div class="ag-rows" style="margin-top:8px">${done.map(agRow).join("")}</div></details>`;
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
      ${upcoming.length ? `<div class="sub" style="margin-top:18px">Coming up after this</div>${upcoming.map((e) => entry(e, true)).join("")}` : ""}</div></div>`;
}
function renderAgenda() {
  const n = S.items.filter((i) => i.action === "saved").length;
  return `<div class="page"><div class="head"><div><h1>Agenda</h1><p>${n ? `${n} item${n > 1 ? "s" : ""} you are tracking.` : "Items you save from the feed."} Deadlines come from the site; reminders are dates you pick.</p></div>
      <div class="row"><div class="seg"><button data-agview="upcoming" class="${ui.agView === "upcoming" ? "on" : ""}">Upcoming</button><button data-agview="month" class="${ui.agView === "month" ? "on" : ""}">Month</button></div>
      <a class="btn sm ghost" href="/api/agenda.ics" download="lookout-agenda.ics" title="Import into Google Calendar, Outlook or Apple Calendar">${I.dl} Add to calendar</a></div></div>
    ${ui.agView === "month" ? renderMonth() : renderUpcoming()}</div>`;
}
function openRemindPicker(anchor) {
  closePops();
  const it = S.items.find((x) => x.id === anchor.dataset.rid); if (!it) return;
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
async function setReminder(id, date) { const it = S.items.find((x) => x.id === id); it.remind_on = date || null; closePops(); render(); await api(`/api/items/${encodeURIComponent(id)}`, { remind_on: date || "" }); toast(date ? `Reminder set for ${short(date)}` : "Reminder removed"); }

// ---------- sources ----------
function renderSources() {
  const rows = S.sources.map((s) => {
    const l = s.last, agent = s.method !== "plugin";
    const status = !l ? '<span class="tag">not scanned yet</span>' : l.ok ? `<span class="tag ok">${l.count} items</span>` : '<span class="tag bad">failed</span>';
    const learn = !agent ? '<span class="tag">plugin</span>' : s.extractor_ok ? '<span class="tag accent" title="Read with code your agent wrote: seconds, no agent quota">learned</span>' : s.extractor ? '<span class="tag bad" title="The site changed; your agent will fix the reader on the next scan">needs relearning</span>' : '<span class="tag" title="On the first scan your agent browses the site and writes a reader for it">not learned yet</span>';
    return `<div class="srow"><label class="switch" title="Watch this source"><input type="checkbox" data-src-active="${s.id}" ${s.active ? "checked" : ""}><i></i></label>
      <div style="min-width:0"><h4>${esc(s.name)} ${learn} ${status}</h4><div class="u">${esc(s.url)}</div>
        ${s.notes && agent ? `<div class="n">${esc(s.notes)}</div>` : ""}${l && !l.ok ? `<div class="err">${esc(l.error)}</div>` : ""}${s.extractor_error && agent && !s.extractor_ok ? `<div class="err">${esc(s.extractor_error)}</div>` : ""}
        ${s.extractor ? `<details data-key="src-${s.id}"><summary>View the reader your agent wrote${s.extractor_updated ? ", " + short(s.extractor_updated.slice(0, 10)) : ""}${s.extractor_runs ? `, used ${s.extractor_runs} time${s.extractor_runs > 1 ? "s" : ""}` : ""}</summary><div class="code">${esc(s.extractor)}</div></details>` : ""}</div>
      <div class="row">${agent ? `<button class="btn sm ghost" data-src-edit="${s.id}">Edit notes</button>${s.extractor ? `<button class="btn sm ghost" data-src-relearn="${s.id}">Relearn</button>` : ""}` : ""}
        <button class="btn sm" data-src-scan="${s.id}" ${S.progress.running ? "disabled" : ""}>${I.play} Scan</button><button class="btn sm ghost danger" data-src-del="${s.id}" aria-label="Delete source">${I.trash}</button></div></div>`;
  }).join("");
  return `<div class="page"><div class="head"><div><h1>Sources</h1><p>Websites Lookout watches. On the first scan your agent browses each one and writes a small reader for it. After that, a scan takes seconds.</p></div>
      <a class="btn sm ghost" href="#/home">Find sources with AI</a></div>
    <div class="box pad"><form id="addSrc" class="grid2">
      <label class="f">Name<input type="text" name="name" required placeholder="Acme careers"></label>
      <label class="f">Address of the page that lists the items<input type="url" name="url" required placeholder="https://"></label>
      <label class="f" style="grid-column:1/-1">Notes for your agent <small>optional: which tab or filter to use, where the dates are</small><textarea name="notes" style="min-height:60px" placeholder="Use the Internships filter. Deadlines are on each detail page."></textarea></label>
      <div class="row" style="grid-column:1/-1"><span class="grow"></span><button class="btn primary">Add source</button></div></form></div>
    <div class="h2">Your sources <span>${S.sources.length}</span></div>
    ${S.sources.length ? `<div class="box">${rows}</div>` : `<div class="empty"><b>No sources yet</b>Add a page above, or describe what you want on Home and let your agent find sites.</div>`}</div>`;
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
  const rows = S.filters.map((f) => `<div class="frow ${f.active ? "" : "off"}"><label class="switch" title="Use this filter"><input type="checkbox" data-f-active="${f.id}" ${f.active ? "checked" : ""}><i></i></label>
      <span class="tag ${f.kind === "exclude" ? "bad" : "accent"}">${FKIND[f.kind][0]}</span><span class="v">${esc(f.value)}${f.kind === "deadline_within" ? " days" : ""}</span>
      <button class="btn sm ghost danger" data-f-del="${f.id}" aria-label="Delete filter">${I.trash}</button></div>`).join("");
  return `<div class="page"><div class="head"><div><h1>Filters</h1><p>Every scan uses these. Your agent checks new items against them in one quick pass, and Lookout enforces exclusions and deadlines itself. When you change a filter, your items are checked again on the next scan.</p></div></div>
    <div class="box pad"><form id="addF" class="row" style="align-items:flex-end;flex-wrap:nowrap">
      <label class="f" style="width:190px;flex:none">Type<select name="kind" id="fKind">${Object.entries(FKIND).map(([k, v]) => `<option value="${k}">${v[0]}</option>`).join("")}</select></label>
      <label class="f grow"><span id="fHint">${FKIND.include[1]}</span><input type="text" name="value" id="fVal" required placeholder="${FKIND.include[2]}"></label>
      <button class="btn primary">Add filter</button></form></div>
    <div class="h2">Your filters <span>${S.filters.length}</span></div>
    ${S.filters.length ? `<div class="box">${rows}</div>` : `<div class="empty"><b>No filters</b>Without filters, every item on your sources shows up.</div>`}
    <div class="h2">What your agent is told</div><div class="preview">${esc(filterText(S.filters.filter((f) => f.active)))}</div></div>`;
}

// ---------- settings ----------
function renderSettings() {
  if (!AG) { ensureAgents().then(render); return `<div class="page"><div class="head"><div><h1>Settings</h1><p>Looking for AI agents on this computer…</p></div></div></div>`; }
  const st = S.settings, cur = st.agent, sch = S.schedule;
  const usage = (u) => !u.length ? "Not used by Lookout yet" : u.slice(0, 3).map((m) => `${esc(m.model || "default model")}: ${m.today} today, ${m.calls} this week` + (m.blocked_until ? ` · <span class="bad">quota used up until ${m.blocked_until.slice(11, 16)}</span>` : "")).join("<br>");
  const agents = AG.agents.map((a) => `<label class="agent ${a.installed || a.id === "custom" ? "" : "off"} ${cur === a.id ? "on" : ""}">
      <input type="radio" name="agent" value="${a.id}" ${cur === a.id ? "checked" : ""} ${a.installed || a.id === "custom" ? "" : "disabled"}>
      <div><b>${esc(a.name)}</b><span class="v">${a.id === "custom" ? "" : a.installed ? (a.version && a.version !== "?" ? "v" + esc(a.version) : "installed") : "not installed"}</span>${a.verified || a.id === "custom" || !a.installed ? "" : ' <span class="tag" title="Command-line flags not yet tested on a real install">experimental</span>'}
        ${a.id === "custom" ? `<small>Any other command-line agent. Lookout fills in {prompt} and {model}.</small><input type="text" id="customCmd" style="margin-top:8px" placeholder="mycli run --yes {prompt}" value="${esc(a.command || "")}">` : ""}
        ${a.installed && a.id !== "custom" ? `<div class="usage">${usage(a.usage)}</div>` : ""}</div></label>`).join("");
  return `<div class="page"><div class="head"><div><h1>Settings</h1><p>Stored only on this computer.</p></div></div>
    ${AG.browser.installed ? "" : `<div class="banner">${I.warn}<div><b>agent-browser is missing</b><div class="muted" style="font-size:13px;margin:2px 0 8px">Agents use it to drive a real browser. Install it once in a terminal:</div><div class="cmd">${esc(AG.browser.install)}</div></div></div>`}
    <div class="h2">AI agent</div><div class="box">${agents}</div>
    <div class="box pad" style="margin-top:10px"><div class="kv">
      <div class="f">Model<div>${cur ? `<button type="button" class="chip model" data-pick="model">${modelChipText(st.model, cur)} ${I.down}</button>` : '<span class="muted">Choose an agent first</span>'}</div></div>
      <label class="f">Minutes your agent may spend on one site<input type="number" id="timeoutIn" min="3" max="60" value="${esc(st.agent_timeout_min)}"></label>
      <label class="check"><input type="checkbox" id="headedIn" ${st.headed === "1" ? "checked" : ""}> Show the browser while your agent works</label>
      <label class="f">Sites your agent works on at once<input type="number" id="parallelIn" min="1" max="6" value="${esc(st.parallel)}"></label>
      <label class="check" style="grid-column:1/-1"><input type="checkbox" id="verifyIn" ${st.verify_weekly === "1" ? "checked" : ""}> Once a week, let your agent re-check each learned site and update the reader if the site changed</label>
    </div><p class="muted" style="font-size:12.5px;margin:12px 0 0">Agent tools don't report how much quota is left. Lookout counts the calls it makes, remembers the last quota error and skips an agent until its quota resets.</p>
    <div class="row" style="margin-top:12px"><span class="grow"></span><button class="btn primary" data-act="save-agent">Save</button></div></div>
    <div class="h2">Schedule and notifications</div><div class="box pad">
      <div class="setrow"><div><b>Daily scan</b><small>${sch.supported ? "Runs in the background even when Lookout is closed. If the computer is off, it runs at the next start-up." : esc(sch.hint)}</small></div>
        ${sch.supported ? `<div class="row"><input type="time" id="schTime" value="${esc(st.schedule_time)}" style="width:110px" aria-label="Time"><label class="switch"><input type="checkbox" id="schOn" ${sch.enabled ? "checked" : ""}><i></i></label></div>` : ""}</div>
      <div class="setrow"><div><b>Desktop notification</b><small>When a scan finds new items.</small></div><label class="switch"><input type="checkbox" id="notifyIn" ${st.notify === "1" ? "checked" : ""}><i></i></label></div></div>
    <div class="h2">E-mail digest <span>optional</span></div><div class="box pad"><div class="kv">
      <label class="f">Send to<input type="email" id="mailTo" value="${esc(st.mail_to)}" placeholder="you@example.com"></label>
      <label class="f">SMTP user<input type="email" id="smtpUser" value="${esc(st.smtp_user)}" placeholder="sender@gmail.com"></label>
      <label class="f">SMTP password <small>for Gmail, an app password</small><input type="password" id="smtpPass" placeholder="${st.smtp_password_set ? "saved, leave empty to keep" : ""}"></label>
      <label class="f">SMTP server<div class="row" style="flex-wrap:nowrap"><input type="text" id="smtpHost" value="${esc(st.smtp_host)}"><input type="number" id="smtpPort" value="${esc(st.smtp_port)}" style="width:90px" aria-label="Port"></div></label>
    </div><div class="row" style="margin-top:12px"><span class="grow"></span><button class="btn" data-act="mail-test">Send test e-mail</button><button class="btn primary" data-act="save-mail">Save</button></div></div></div>`;
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
  const ok = d.sources.filter((s) => s.ok).length, shown = d.items.filter((i) => i.match !== 0), hidden = d.items.length - shown.length;
  const srows = d.sources.map((s) => `<tr><td><span class="dot ${s.ok ? "" : "bad"}"></span>${esc(s.source_name)}</td><td>${s.ok ? `${s.count} items${s.new ? `, <b>${s.new} new</b>` : ""}` : `<span class="err">${esc(s.error)}</span>`}</td><td class="muted" style="text-align:right;white-space:nowrap">${(s.ms / 1000).toFixed(1)} s</td></tr>`).join("");
  return `<div class="page">${S.progress.running && S.progress.scan_id === d.id ? progressBox() : ""}
    <div class="head"><div><h1>${d.trigger === "scheduled" ? "Daily scan" : "Scan"}, ${short(d.started.slice(0, 10))} at ${d.started.slice(11, 16)}</h1>
      <p>${d.agent ? `${esc(agentName(d.agent))}${d.model ? ", " + esc(d.model) : ""}` : "Plugins only"}${d.finished ? `, took ${duration(d.started, d.finished)}` : ", running"}${d.note ? ". " + esc(d.note) : ""}</p></div>
      <button class="btn sm ghost danger" data-scan-del="${d.id}">${I.trash} Delete</button></div>
    <div class="stats"><div class="stat"><b>${d.found}</b><span>matching items</span></div><div class="stat"><b>${d.new}</b><span>new</span></div><div class="stat"><b>${ok} of ${d.sources.length}</b><span>sources read</span></div><div class="stat"><b>${hidden}</b><span>filtered out</span></div></div>
    ${d.filters.length ? `<div class="h2">Filters used</div><div class="preview">${esc(filterText(d.filters))}</div>` : ""}
    <div class="h2">Sources</div><div class="box"><table class="t"><tbody>${srows || '<tr><td class="muted">No sources were scanned.</td></tr>'}</tbody></table></div>
    <div class="h2">Matching items <span>${shown.length}</span></div>
    ${shown.length ? `<div class="list">${shown.map((i) => itemCard(i, { isNew: !!i.is_new })).join("")}</div>` : '<div class="empty"><b>No matching items in this scan</b></div>'}</div>`;
}

// ---------- render ----------
let lastHtml = null, lastTag = null;
function render() {
  if (!S) return;
  renderSide();
  const v = route.view;
  const html = v === "feed" ? renderFeed() : v === "agenda" ? renderAgenda() : v === "sources" ? renderSources() : v === "filters" ? renderFilters() : v === "settings" ? renderSettings() : v === "scan" ? renderScan() : renderHome();
  const tag = v === "settings" && AG ? "settings:ag" : v === "scan" ? `scan:${route.id}` : v, main = $("#main");
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
let timer = null, wasBusy = false;
async function refresh() {
  clearTimeout(timer);
  try {
    S = await api("/api/state");
    if (S.settings.agent && !modelCache[S.settings.agent]) modelsFor(S.settings.agent).then(() => { render.force = true; render(); });
    if (!AG) ensureAgents().then(() => render());
    const busy = S.progress.running || S.discovery.running;
    if ((wasBusy || busy) && route.view === "scan") loadScan(route.id);
    const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && document.activeElement.id !== "q";
    if (!popEl && !typing) render(); else renderSide();
    wasBusy = busy;
  } catch (e) { lastHtml = lastTag = null; $("#main").innerHTML = `<div class="page"><div class="empty"><b>Lookout's engine stopped</b>Close this window and open Lookout again.</div></div>`; }
  timer = setTimeout(refresh, S && (S.progress.running || S.discovery.running) ? 1500 : 30000);
}

// ---------- events ----------
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a"); if (!t) return;
  const d = t.dataset;
  try {
    if (d.win) { e.preventDefault(); return window.pywebview?.api?.[d.win](); }
    if (d.ext !== undefined && window.pywebview?.api) { e.preventDefault(); return window.pywebview.api.open_external(t.href); }
    if (d.pick === "model") { e.preventDefault(); return popEl ? closePops() : openModelPicker(t); }
    if (d.pick === "agent") { e.preventDefault(); return popEl ? closePops() : openAgentPicker(t); }
    if (d.pick === "remind") { e.preventDefault(); return popEl ? closePops() : openRemindPicker(t); }
    if (d.remindSet !== undefined) return setReminder(d.rid, d.remindSet);
    if (d.remindPick !== undefined) { const v = $("#remindPick").value; if (!v) return toast("Pick a date"); return setReminder(d.rid, v); }
    if (d.agview) { ui.agView = d.agview; keep(); return render(); }
    if (d.day) { agDay = d.day; return render(); }
    if (d.model !== undefined) return setModel(d.model);
    if (d.modelCustom !== undefined) return setModel($("#customModel").value.trim());
    if (d.agent) { await api("/api/settings", { agent: d.agent, model: "" }); closePops(); await modelsFor(d.agent); toast(`Using ${agentName(d.agent)}`); render.force = true; return refresh(); }
    if (d.goto) { closePops(); location.hash = d.goto; return; }
    if (d.act === "scan") { e.preventDefault(); const r = await api("/api/scan", {}); toast(r.started ? "Scan started" : "A scan is already running"); return refresh(); }
    if (d.act === "cancel") { await api("/api/scan/cancel", {}); toast("Stopping after the sites in progress"); return; }
    if (d.item && d.unsave) { const it = S.items.find((x) => x.id === d.item); it.action = null; render(); await api(`/api/items/${encodeURIComponent(d.item)}`, { action: null }); return toast("Removed from agenda"); }
    if (d.item) { const it = S.items.find((x) => x.id === d.item); const nv = it.action === d.set ? null : d.set; it.action = nv; render(); await api(`/api/items/${encodeURIComponent(d.item)}`, { action: nv }); if (nv === "saved") toast("Added to agenda"); return; }
    if (d.cat) { ui.cats = ui.cats.includes(d.cat) ? ui.cats.filter((c) => c !== d.cat) : [...ui.cats, d.cat]; keep(); return render(); }
    if (d.cal) { const base = ui.calMonth ? parseIso(ui.calMonth + "-01") : new Date(); const m = d.cal === "0" ? new Date() : new Date(base.getFullYear(), base.getMonth() + Number(d.cal), 1); ui.calMonth = iso(m).slice(0, 7); keep(); return render(); }
    if (d.agOpen) { agOpen = agOpen === d.agOpen ? null : d.agOpen; return render(); }
    if (d.focus) { const el = document.getElementById("m-" + d.focus); if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.style.outline = "2px solid var(--accent)"; setTimeout(() => (el.style.outline = ""), 1500); } return; }
    if (d.srcScan) { const r = await api("/api/scan", { only: [Number(d.srcScan)] }); toast(r.started ? "Scanning this source" : "A scan is already running"); return refresh(); }
    if (d.srcRelearn) { await api(`/api/sources/${d.srcRelearn}`, { relearn: true }); toast("Your agent will relearn this site on the next scan"); return refresh(); }
    if (d.srcDel) { if (!confirm("Delete this source? Items already found stay in your feed.")) return; await api(`/api/sources/${d.srcDel}/delete`, {}); return refresh(); }
    if (d.srcEdit) { const s = S.sources.find((x) => x.id === Number(d.srcEdit)); const notes = prompt(`Notes for your agent about ${s.name}:`, s.notes || ""); if (notes === null) return; await api(`/api/sources/${s.id}`, { notes }); toast("Notes saved"); return refresh(); }
    if (d.cand !== undefined) { const c = S.discovery.results[Number(d.cand)]; await api("/api/sources", { name: c.name, url: c.url, notes: c.notes || "" }); toast(`Added ${c.name}`); return refresh(); }
    if (d.act === "clear-disc") { await api("/api/discover/clear", {}); return refresh(); }
    if (d.fDel) { await api(`/api/filters/${d.fDel}/delete`, {}); return refresh(); }
    if (d.scanDel) { if (!confirm("Delete this scan from history? Items it found stay in your feed.")) return; await api(`/api/scans/${d.scanDel}/delete`, {}); location.hash = "#/home"; return refresh(); }
    if (d.act === "save-agent") {
      await api("/api/settings", { agent: $("input[name=agent]:checked")?.value || "", headed: $("#headedIn").checked ? "1" : "0", agent_timeout_min: $("#timeoutIn").value, parallel: $("#parallelIn").value, verify_weekly: $("#verifyIn").checked ? "1" : "0", custom_command: $("#customCmd")?.value || "" });
      toast("Saved"); AG = null; render.force = true; return refresh();
    }
    if (d.act === "save-mail" || d.act === "mail-test") {
      await api("/api/settings", { mail_to: $("#mailTo").value, smtp_user: $("#smtpUser").value, smtp_password: $("#smtpPass").value, smtp_host: $("#smtpHost").value, smtp_port: $("#smtpPort").value });
      if (d.act === "mail-test") return toast((await api("/api/mail/test", {})).message);
      return toast("Saved");
    }
  } catch (err) { toast(err.message); }
});
document.addEventListener("change", async (e) => {
  const t = e.target, d = t.dataset;
  try {
    if (t.id === "srcSel") { ui.src = t.value; keep(); return render(); }
    if (t.id === "sortSel") { ui.sort = t.value; keep(); return render(); }
    if (d.srcActive) { await api(`/api/sources/${d.srcActive}`, { active: t.checked }); return refresh(); }
    if (d.fActive) { await api(`/api/filters/${d.fActive}`, { active: t.checked }); return refresh(); }
    if (d.remind !== undefined) { await api(`/api/items/${encodeURIComponent(d.remind)}`, { remind_on: t.value }); toast("Reminder set"); return refresh(); }
    if (d.note !== undefined) { await api(`/api/items/${encodeURIComponent(d.note)}`, { note: t.value }); return toast("Note saved"); }
    if (t.name === "agent") { document.querySelectorAll(".agent").forEach((l) => l.classList.toggle("on", l.contains(t))); return; }
    if (t.id === "notifyIn") { await api("/api/settings", { notify: t.checked ? "1" : "0" }); return toast("Saved"); }
    if (t.id === "schOn" || t.id === "schTime") { const r = await api("/api/schedule", { enabled: $("#schOn").checked, at: $("#schTime").value }); toast(r.message); return refresh(); }
    if (t.id === "fKind") { $("#fHint").textContent = FKIND[t.value][1]; $("#fVal").placeholder = FKIND[t.value][2]; $("#fVal").type = t.value === "deadline_within" ? "number" : "text"; }
  } catch (err) { toast(err.message); }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "q") { ui.q = e.target.value.trim(); render(); }
  if (e.target.id === "ask") { e.target.style.height = "auto"; e.target.style.height = Math.min(220, e.target.scrollHeight) + "px"; }
});
document.addEventListener("submit", async (e) => {
  e.preventDefault(); const f = e.target, data = Object.fromEntries(new FormData(f));
  try {
    if (f.id === "addSrc") { await api("/api/sources", data); toast(`Added ${data.name}`); f.reset(); return refresh(); }
    if (f.id === "addF") { await api("/api/filters", data); f.reset(); $("#fHint").textContent = FKIND.include[1]; return refresh(); }
    if (f.id === "discover") { if (!data.request || data.request.trim().length < 5) return toast("Describe what you want to follow"); const r = await api("/api/discover", data); toast(r.started ? "Your agent is searching the web" : "Already searching"); return refresh(); }
  } catch (err) { toast(err.message); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePops();
  if (e.key === "Enter" && !e.shiftKey && e.target.id === "ask") { e.preventDefault(); e.target.form.requestSubmit(); }
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { const q = $("#q") || $("#ask"); if (q) { e.preventDefault(); q.focus(); } }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
parseRoute(); refresh();
