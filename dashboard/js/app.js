// DOM, map, and events for the school dashboard. The rules (scope, search, sort, CSV)
// live in scope.js so they can be tested without a browser.
import * as S from "./scope.js";

const PAGE_SIZE = 50;
const PIN_BATCH = 2500;  // pins added per step when a scope loads
const PUNJAB = { center: [30.9, 72.7], zoom: 6 };
const FIT_MAX_ZOOM = 14;
const PIN_BASE = { radius: 6, color: "#1e40af", weight: 1, fillColor: "#3b82f6", fillOpacity: 0.85 };
const PIN_FOCUS = { radius: 10, color: "#7c2d12", weight: 2, fillColor: "#f97316", fillOpacity: 1 };
const LEVELS = [
  { level: "district", id: "sel-district", all: "All districts" },
  { level: "wing", id: "sel-wing", all: "All wings" },
  { level: "tehsil", id: "sel-tehsil", all: "All tehsils" },
  { level: "markaz", id: "sel-markaz", all: "All markaz" },
];
const BADGE = {
  mapped: "ok",
  outside: "bad",
  coord_error: "bad",
  lookup_error: "bad",
  no_coords: "warn",
  not_in_sis: "muted",
  pending: "muted",
};

const $ = (id) => document.getElementById(id);
const esc = S.escapeHtml;
const fmt = S.formatCount;

let model = null;
let map = null;
let cluster = null;
let scopeRows = [];      // row indexes in the current scope (all statuses)
let scopeReady = false;
let pinsKey = null;      // scope key of the pins currently in the cluster
let pinLoad = 0;         // id of the newest pin load; older loads stop
let focusPin = null;     // pin currently highlighted
let tableView = [];      // row indexes listed in the table (filtered and sorted)
const pins = new Map();  // row index -> CircleMarker, created on first use

const view = {
  sel: { ...S.EMPTY_SELECTION },
  emis: null,            // selected school (row index)
  query: "",
  onlyIssues: false,
  sortKey: null,
  sortDir: "asc",
  page: 0,
};

const selKey = (sel) => [sel.district, sel.wing, sel.tehsil, sel.markaz].map((v) => v ?? "").join("|");

function debounce(fn, ms) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function mapsUrl(lat, lon) {
  return `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
}

function badge(statusKey, label) {
  return `<span class="badge badge-${BADGE[statusKey] ?? "muted"}">${esc(label)}</span>`;
}

// ---- map -----------------------------------------------------------------

function setupMap() {
  map = L.map("map", { preferCanvas: true }).setView(PUNJAB.center, PUNJAB.zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  // markercluster's zoomToShowLayer() can call map.hasLayer(undefined) when the pin was
  // removed during the zoom. Leaflet 1.9 throws on that; treat undefined as "not on the map".
  const hasLayer = map.hasLayer.bind(map);
  map.hasLayer = (layer) => layer != null && hasLayer(layer);
  newCluster();
}

// Each scope gets a fresh cluster group, so no state carries over from the previous scope.
// The old group is cleared and removed first.
function newCluster() {
  if (cluster) {
    cluster.clearLayers();
    map.removeLayer(cluster);
  }
  cluster = L.markerClusterGroup({
    chunkedLoading: false,  // loadPins() adds pins in batches itself
    maxClusterRadius: 45,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
  });
  cluster.addTo(map);
  return cluster;
}

function popupHtml(i) {
  const d = S.rowDetails(model, i);
  const place = [d.district, d.tehsil, d.markaz].filter(Boolean).join(" › ");
  return `<div class="popup">
    <div class="popup-name">${esc(d.name)}</div>
    <div class="popup-emis">EMIS ${esc(d.emis)}${d.schoolId ? ` · SIS ${esc(d.schoolId)}` : ""}</div>
    <div>${esc(d.wing)}</div>
    <div>${esc(place)}</div>
    <div class="popup-coords">${S.formatCoord(d.lat)}, ${S.formatCoord(d.lon)}</div>
    <a href="${mapsUrl(d.lat, d.lon)}" target="_blank" rel="noopener">Open in Google Maps</a>
  </div>`;
}

function pinFor(i) {
  let pin = pins.get(i);
  if (!pin) {
    const r = model.rows[i];
    pin = L.circleMarker([r[model.C.lat], r[model.C.lon]], PIN_BASE);
    pin.bindPopup(() => popupHtml(i), { maxWidth: 280 });
    pin.on("click", () => selectSchool(i, { reveal: true }));
    pins.set(i, pin);
  }
  return pin;
}

function fitMap(rowIdxs) {
  if (!rowIdxs.length) {
    map.setView(PUNJAB.center, PUNJAB.zoom);
    return;
  }
  const { lat, lon } = model.C;
  const bounds = L.latLngBounds();
  for (const i of rowIdxs) bounds.extend([model.rows[i][lat], model.rows[i][lon]]);
  map.fitBounds(bounds, { padding: [28, 28], maxZoom: FIT_MAX_ZOOM });
}

function mapNote(sum) {
  if (!sum.total) return "No schools in this scope.";
  if (!sum.mapped) return "None of the schools in this scope has a map position. See the table and Data issues.";
  const extra = sum.notMapped
    ? ` ${S.plural(sum.notMapped, "school")} in this scope ${sum.notMapped === 1 ? "has" : "have"} no pin (see the table).`
    : "";
  return `${S.plural(sum.mapped, "pin")}. Pins group together when zoomed out.${extra}`;
}

// Replace the pins with those for rowIdxs, in batches, so the page stays responsive. A newer
// call stops an older one. (markercluster's own chunked loading is off: its chunks keep
// running after clearLayers(), so a fast scope change could leave the old scope's pins.)
function loadPins(rowIdxs) {
  const token = ++pinLoad;
  const group = newCluster();
  let next = 0;
  const step = () => {
    if (token !== pinLoad) return;
    const end = Math.min(next + PIN_BATCH, rowIdxs.length);
    group.addLayers(rowIdxs.slice(next, end).map(pinFor));
    next = end;
    if (next < rowIdxs.length) setTimeout(step, 0);
  };
  step();
}

// Redraw pins only when the scope changes. Highlights are separate, so they are cheap.
function renderMap(sum) {
  const key = selKey(view.sel);
  if (key !== pinsKey) {
    pinsKey = key;
    focusPin = null;
    const mapped = S.mappedIn(model, scopeRows);
    // The selected school's pin goes first, so a jump can zoom to it before the rest load.
    if (view.emis !== null) {
      const at = mapped.indexOf(view.emis);
      if (at > 0) {
        mapped.splice(at, 1);
        mapped.unshift(view.emis);
      }
    }
    loadPins(mapped);
    fitMap(mapped);
  }
  $("map-note").textContent = mapNote(sum);
}

function highlightPin() {
  if (focusPin) {
    focusPin.setStyle(PIN_BASE);
    focusPin.setRadius(PIN_BASE.radius);
    focusPin = null;
  }
  if (view.emis !== null && pins.has(view.emis)) {
    focusPin = pins.get(view.emis);
    focusPin.setStyle(PIN_FOCUS);
    focusPin.setRadius(PIN_FOCUS.radius);
  }
}

// Zoom the cluster until this pin is visible, then open its popup. Pins are added in
// batches, so the pin may not be in the group yet. Retry for a few seconds.
function focusOnMap(i) {
  if (!S.statusOf(model, i).mapped) return;
  const pin = pinFor(i);
  let tries = 0;
  const attempt = () => {
    if (!cluster.hasLayer(pin)) {
      if (tries++ < 60) setTimeout(attempt, 100);
      return;
    }
    // The callback can run after the user has chosen another school, so check first.
    cluster.zoomToShowLayer(pin, () => {
      if (view.emis === i) pin.openPopup();
    });
  };
  attempt();
}

// ---- scope, selects, summary --------------------------------------------

function renderSelects() {
  for (const { level, id, all } of LEVELS) {
    const el = $(id);
    const opts = S.levelOptions(model, view.sel, level);
    const parts = [`<option value="">${all} (${fmt(opts.length)})</option>`];
    for (const o of opts) {
      parts.push(`<option value="${o.value}">${esc(o.label)} · ${fmt(o.count)}</option>`);
    }
    el.innerHTML = parts.join("");
    el.value = view.sel[level] === null ? "" : String(view.sel[level]);
  }
}

function renderSummary(sum) {
  const kpi = (value, label, title = "") =>
    `<li${title ? ` title="${esc(title)}"` : ""}><b>${fmt(value)}</b> ${esc(label)}</li>`;
  $("scope-line").textContent = S.scopeLabel(model, view.sel);
  $("kpis").innerHTML = [
    kpi(sum.total, "schools"),
    kpi(sum.mapped, "on the map"),
    kpi(sum.notMapped, "not on the map"),
    ...model.data.wings.map((w, k) => kpi(sum.byWing[k], w.code, w.label)),
  ].join("");
}

// ---- table ---------------------------------------------------------------

function refreshView() {
  let rows = scopeRows;
  if (view.onlyIssues) rows = S.onlyIssues(model, rows);
  rows = S.textFilter(model, rows, view.query);
  tableView = S.sortRows(model, rows, view.sortKey, view.sortDir);
}

function rowHtml(i) {
  const d = S.rowDetails(model, i);
  const cls = [i === view.emis ? "is-selected" : "", d.mapped ? "" : "is-unmapped"]
    .filter(Boolean)
    .join(" ");
  return `<tr data-row="${i}" tabindex="0" class="${cls}">
    <td class="mono">${esc(d.emis)}</td>
    <td>${esc(d.name)}</td>
    <td class="num">${S.formatCoord(d.lat)}</td>
    <td class="num">${S.formatCoord(d.lon)}</td>
    <td>${badge(d.statusKey, d.status)}</td>
  </tr>`;
}

function renderPager(pg) {
  $("pager").innerHTML = `
    <button class="btn" type="button" data-page="-1" ${pg.page === 0 ? "disabled" : ""}>‹ Previous</button>
    <span class="muted small" aria-live="polite">${
      pg.total
        ? `Rows ${fmt(pg.from)}–${fmt(pg.to)} of ${fmt(pg.total)} · page ${fmt(pg.page + 1)} of ${fmt(pg.pages)}`
        : "No rows"
    }</span>
    <button class="btn" type="button" data-page="1" ${pg.page >= pg.pages - 1 ? "disabled" : ""}>Next ›</button>`;
}

function renderSortHeaders() {
  for (const btn of document.querySelectorAll("button.sort")) {
    const active = btn.dataset.sort === view.sortKey;
    btn.closest("th").setAttribute(
      "aria-sort",
      active ? (view.sortDir === "asc" ? "ascending" : "descending") : "none",
    );
    btn.dataset.dir = active ? view.sortDir : "";
  }
}

function renderTable() {
  const pg = S.pageOf(tableView, view.page, PAGE_SIZE);
  view.page = pg.page;
  const body = $("rows");
  const focusedRow = document.activeElement?.closest?.("#rows tr")?.dataset.row ?? null;
  body.innerHTML = pg.items.length
    ? pg.items.map(rowHtml).join("")
    : `<tr><td class="empty" colspan="5">No schools match these filters.</td></tr>`;
  if (focusedRow !== null) {
    body.querySelector(`tr[data-row="${focusedRow}"]`)?.focus({ preventScroll: true });
  }
  $("table-count").textContent = `· ${fmt(tableView.length)} listed`;
  renderPager(pg);
  renderSortHeaders();
}

function toggleSort(key) {
  if (view.sortKey === key) {
    view.sortDir = view.sortDir === "asc" ? "desc" : "asc";
  } else {
    view.sortKey = key;
    view.sortDir = "asc";
  }
  view.page = 0;
  refreshView();
  renderTable();
}

// ---- selected school -----------------------------------------------------

function renderCard() {
  const card = $("school-card");
  if (view.emis === null) {
    card.hidden = true;
    card.innerHTML = "";
    return;
  }
  const d = S.rowDetails(model, view.emis);
  const hasCoords = d.lat !== null && d.lon !== null;
  card.hidden = false;
  card.innerHTML = `
    <div class="school-card-head">
      <div>
        <h3>${esc(d.name)}</h3>
        <p class="muted small mono">EMIS ${esc(d.emis)}${d.schoolId ? ` · SIS ID ${esc(d.schoolId)}` : ""}</p>
      </div>
      <button class="btn ghost" type="button" id="card-close" aria-label="Close school details">Close</button>
    </div>
    <dl class="facts">
      <div><dt>Wing</dt><dd>${esc(d.wing)}</dd></div>
      <div><dt>District</dt><dd>${esc(d.district)}</dd></div>
      <div><dt>Tehsil</dt><dd>${esc(d.tehsil)}</dd></div>
      <div><dt>Markaz</dt><dd>${esc(d.markaz || "—")}</dd></div>
      <div><dt>Coordinates</dt><dd class="mono">${hasCoords ? `${S.formatCoord(d.lat)}, ${S.formatCoord(d.lon)}` : "—"}</dd></div>
      <div><dt>Status</dt><dd>${badge(d.statusKey, d.status)}</dd></div>
    </dl>
    <div class="school-card-actions">
      ${hasCoords ? `<a class="btn" href="${mapsUrl(d.lat, d.lon)}" target="_blank" rel="noopener">Open in Google Maps</a>` : ""}
      <span class="muted small">${d.mapped ? "Highlighted on the map." : "Not shown on the map. See Data issues."}</span>
    </div>`;
  $("card-close").addEventListener("click", clearSelection);
}

function writeHash() {
  const emis = view.emis === null ? null : String(model.rows[view.emis][model.C.emis]);
  const hash = S.selectionToHash(model, view.sel, emis);
  history.replaceState(null, "", hash ? `#${hash}` : `${location.pathname}${location.search}`);
}

// Move the table to the page that holds this school, or clear the filters that hide it.
function revealInTable(i, { clearFilters = false } = {}) {
  let pos = tableView.indexOf(i);
  if (pos < 0 && clearFilters) {
    view.query = "";
    view.onlyIssues = false;
    $("table-filter").value = "";
    $("only-issues").checked = false;
    refreshView();
    pos = tableView.indexOf(i);
  }
  if (pos >= 0) view.page = Math.floor(pos / PAGE_SIZE);
}

// Select a school without changing the scope (row click, pin click).
function selectSchool(i, { focus = false, reveal = false } = {}) {
  view.emis = i;
  if (reveal) revealInTable(i);
  renderCard();
  highlightPin();
  renderTable();
  writeHash();
  if (focus) focusOnMap(i);
}

function clearSelection() {
  view.emis = null;
  setEmisMessage("");
  renderCard();
  highlightPin();
  renderTable();
  writeHash();
}

// Full refresh after the scope changes. Pins only redraw when the scope key changes.
function render() {
  refreshView();
  const sum = S.summarize(model, scopeRows);
  renderSelects();
  renderSummary(sum);
  renderMap(sum);
  renderTable();
  renderCard();
  highlightPin();
  writeHash();
}

function applyScope(sel, emis = null) {
  const changed = !scopeReady || selKey(sel) !== selKey(view.sel);
  view.sel = sel;
  view.emis = emis;
  if (changed) {
    scopeRows = S.scopeRows(model, sel);
    scopeReady = true;
    view.page = 0;
  }
  if (view.emis !== null && !S.selectionMatchesRow(model, sel, view.emis)) view.emis = null;
  render();
}

// EMIS search, issue links, and shared links: scope to the school, select it, reveal it.
// A shared link keeps its own filters when they already contain the school.
function jumpTo(i, sel = null) {
  const target = sel !== null && S.selectionMatchesRow(model, sel, i) ? sel : S.selectionForRow(model, i);
  applyScope(target, i);
  revealInTable(i, { clearFilters: true });
  renderTable();
  focusOnMap(i);
}

function openFromHash() {
  const { sel, emis } = S.selectionFromHash(model, location.hash);
  const idx = S.findEmis(model, emis ?? "");
  if (idx >= 0) {
    jumpTo(idx, sel);
  } else {
    applyScope(sel, null);
  }
}

// ---- EMIS search ---------------------------------------------------------

function setEmisMessage(text, tone = "") {
  const el = $("emis-message");
  el.textContent = text;
  el.dataset.tone = tone;
}

function onEmisSubmit(event) {
  event.preventDefault();
  const text = $("emis-input").value.trim();
  if (!text) {
    setEmisMessage("Type an EMIS code, for example 37110028.", "warn");
    return;
  }
  const i = S.findEmis(model, text);
  if (i < 0) {
    setEmisMessage(
      `No school with EMIS ${text} in this snapshot. Check the code. Schools missing from SIS are listed under Data issues.`,
      "warn",
    );
    return;
  }
  const d = S.rowDetails(model, i);
  setEmisMessage(`${d.name} · ${d.district} › ${d.tehsil}`, "ok");
  jumpTo(i);
}

function resetAll() {
  $("emis-input").value = "";
  setEmisMessage("");
  $("table-filter").value = "";
  $("only-issues").checked = false;
  view.query = "";
  view.onlyIssues = false;
  view.sortKey = null;
  view.sortDir = "asc";
  view.page = 0;
  applyScope({ ...S.EMPTY_SELECTION }, null);
}

// ---- data issues panel ---------------------------------------------------

function schoolList(rows) {
  const items = rows.map((i) => {
    const d = S.rowDetails(model, i);
    return `<li><button class="link mono" type="button" data-row="${i}">${esc(d.emis)}</button> ${esc(d.name)}
      <span class="muted">· ${esc(d.district)} › ${esc(d.tehsil)}</span></li>`;
  });
  return `<ul class="issue-list">${items.join("")}</ul>`;
}

function renderIssues() {
  const { groups, shared } = S.dataIssues(model);
  const notOnMap = Object.values(groups).reduce((n, g) => n + g.rows.length, 0);
  $("issues-summary").textContent =
    `Data issues · ${S.plural(notOnMap, "school")} not on the map · ${S.plural(shared.length, "shared location")}`;

  const parts = [];
  for (const key of Object.keys(groups)) {
    const g = groups[key];
    parts.push(`<section class="issue-group">
      <h3>${badge(key, g.label)} <span class="muted">${S.plural(g.rows.length, "school")}</span></h3>
      ${schoolList(g.rows)}
    </section>`);
  }
  if (shared.length) {
    const blocks = shared.map((s) => `<div class="shared">
      <p class="mono small">${S.formatCoord(s.lat)}, ${S.formatCoord(s.lon)} · ${S.plural(s.rows.length, "school")}</p>
      ${schoolList(s.rows)}
    </div>`);
    parts.push(`<section class="issue-group">
      <h3>Shared locations</h3>
      <p class="muted small">Five or more schools report the same coordinates. Their pins stack, and they spread apart when you zoom in. Groups of two to four are not listed.</p>
      ${blocks.join("")}
    </section>`);
  }
  $("issues-body").innerHTML = parts.join("") || "<p>No data issues.</p>";
}

// ---- header, download, events -------------------------------------------

function renderHeader() {
  const d = model.data;
  const when = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Karachi",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(d.built_at));
  const built = $("built-at");
  built.dateTime = d.built_at;
  built.textContent = `${when} PKT`;
  $("emis-help").textContent = `Searches this snapshot, built ${when} PKT. It does not query the live SIS.`;
  $("header-stats").innerHTML = `<b>${fmt(d.counts.mapped)}</b> of ${fmt(d.counts.schools)} schools on the map`;
}

function fileStem() {
  const d = model.data;
  const s = view.sel;
  const parts = [];
  if (s.district !== null) parts.push(d.districts[s.district].name);
  if (s.wing !== null) parts.push(d.wings[s.wing].code);
  if (s.tehsil !== null) parts.push(d.tehsils[s.tehsil].name);
  if (s.markaz !== null) parts.push(d.markaz[s.markaz].id);
  const slug = (parts.join("-") || "punjab-all")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `sis-schools-${slug}`;
}

function downloadCsv() {
  const text = "\uFEFF" + S.toCsv(model, tableView);
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `${fileStem()}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function bindControls() {
  $("emis-form").addEventListener("submit", onEmisSubmit);
  $("emis-clear").addEventListener("click", () => {
    $("emis-input").value = "";
    clearSelection();
  });
  $("reset").addEventListener("click", resetAll);

  for (const { level, id } of LEVELS) {
    $(id).addEventListener("change", (event) => {
      const v = event.target.value;
      applyScope(S.setLevel(view.sel, level, v === "" ? null : Number(v)), view.emis);
    });
  }

  const onQuery = debounce(() => {
    view.query = $("table-filter").value;
    view.page = 0;
    refreshView();
    renderTable();
  }, 150);
  $("table-filter").addEventListener("input", onQuery);

  $("only-issues").addEventListener("change", (event) => {
    view.onlyIssues = event.target.checked;
    view.page = 0;
    refreshView();
    renderTable();
  });

  $("download").addEventListener("click", downloadCsv);

  document.querySelector("thead").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-sort]");
    if (btn) toggleSort(btn.dataset.sort);
  });

  const selectRow = (event) => {
    const tr = event.target.closest("tr[data-row]");
    if (tr) selectSchool(Number(tr.dataset.row), { focus: true });
  };
  $("rows").addEventListener("click", selectRow);
  $("rows").addEventListener("keydown", (event) => {
    if (event.key === "Enter") selectRow(event);
  });

  $("pager").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-page]");
    if (!btn) return;
    view.page += Number(btn.dataset.page);
    renderTable();
    $("table-title").scrollIntoView({ block: "start" });
  });

  $("issues-body").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-row]");
    if (btn) jumpTo(Number(btn.dataset.row));
  });

  window.addEventListener("hashchange", openFromHash);
}

async function start() {
  try {
    const res = await fetch("data/schools.json");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    model = S.createModel(await res.json());
  } catch (err) {
    const el = $("fatal");
    el.hidden = false;
    el.textContent = `Could not load the school data (${err.message}). Build it with: python run.py dashboard`;
    return;
  }
  renderHeader();
  setupMap();
  renderIssues();
  bindControls();
  openFromHash();
}

start();
