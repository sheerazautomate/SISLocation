// Pure dashboard logic: scope filtering, live dropdown options, search, sorting, CSV.
// No DOM and no Leaflet, so it runs under `node --test` (see dashboard/test/).
//
// Row layout comes from data.columns (see sis/dashboard_data.py). A "selection" is
// { district, wing, tehsil, markaz }, each an index into the matching data table, or
// null for "All". Wing indexes data.wings, district data.districts, and so on.

// Each dropdown only lists options inside the scope of the dropdowns above it.
const PARENTS = {
  district: [],
  wing: ["district"],
  tehsil: ["district", "wing"],
  markaz: ["district", "wing", "tehsil"],
};
const CHILDREN = {
  district: ["wing", "tehsil", "markaz"],
  wing: ["tehsil", "markaz"],
  tehsil: ["markaz"],
  markaz: [],
};

export const EMPTY_SELECTION = Object.freeze({ district: null, wing: null, tehsil: null, markaz: null });

// A point shared by this many schools or more is listed under Data issues. Smaller shared
// points (usually two or more schools on one site) are not listed, but their pins still
// spread apart when zoomed in.
export const SHARED_MIN = 5;

const collator = new Intl.Collator("en", { numeric: true, sensitivity: "base" });

export function createModel(data) {
  const C = Object.fromEntries(data.columns.map((name, i) => [name, i]));
  const rows = data.rows;
  const emisIndex = new Map();
  rows.forEach((row, i) => emisIndex.set(String(row[C.emis]), i));
  return {
    data,
    C,
    rows,
    emisIndex,
    districtById: new Map(data.districts.map((d, i) => [d.id, i])),
    tehsilById: new Map(data.tehsils.map((t, i) => [t.id, i])),
    markazById: new Map(data.markaz.map((m, i) => [m.id, i])),
  };
}

export function statusOf(model, i) {
  return model.data.statuses[model.rows[i][model.C.status]];
}

export function matches(model, row, sel) {
  const C = model.C;
  if (sel.district !== null && row[C.district] !== sel.district) return false;
  if (sel.wing !== null && row[C.wing] !== sel.wing) return false;
  if (sel.tehsil !== null && row[C.tehsil] !== sel.tehsil) return false;
  if (sel.markaz !== null && row[C.markaz] !== sel.markaz) return false;
  return true;
}

// Row indexes (into model.rows) inside the selection, in base-file order.
export function scopeRows(model, sel) {
  const out = [];
  for (let i = 0; i < model.rows.length; i++) {
    if (matches(model, model.rows[i], sel)) out.push(i);
  }
  return out;
}

function optionLabel(model, level, value) {
  const d = model.data;
  switch (level) {
    case "district": return d.districts[value].name;
    case "wing": return d.wings[value].label;
    case "tehsil": return d.tehsils[value].name;
    case "markaz": return d.markaz[value].label;
    default: return String(value);
  }
}

// Options for one dropdown, with the number of schools each option covers in the
// current scope. Wings keep their natural order; the rest are sorted by name.
export function levelOptions(model, sel, level) {
  const C = model.C;
  const parents = PARENTS[level];
  const counts = new Map();
  for (const row of model.rows) {
    let ok = true;
    for (const p of parents) {
      if (sel[p] !== null && row[C[p]] !== sel[p]) { ok = false; break; }
    }
    if (!ok) continue;
    const value = row[C[level]];
    if (value === null || value === undefined) continue;
    counts.set(value, (counts.get(value) || 0) + 1);
  }
  const options = [...counts].map(([value, count]) => ({
    value,
    count,
    label: optionLabel(model, level, value),
  }));
  if (level === "wing") {
    options.sort((a, b) => a.value - b.value);
  } else {
    options.sort((a, b) => collator.compare(a.label, b.label));
  }
  return options;
}

// Choose one level and reset every level below it (their options changed).
export function setLevel(sel, level, value) {
  const next = { ...sel, [level]: value };
  for (const child of CHILDREN[level]) next[child] = null;
  return next;
}

// The selection that puts one school's scope on screen.
export function selectionForRow(model, i) {
  const row = model.rows[i];
  const C = model.C;
  return {
    district: row[C.district],
    wing: row[C.wing],
    tehsil: row[C.tehsil],
    markaz: row[C.markaz] ?? null,
  };
}

export function selectionMatchesRow(model, sel, i) {
  return matches(model, model.rows[i], sel);
}

// Keep the hierarchy consistent. A tehsil or markaz that contradicts a chosen parent is
// dropped (for example "#d=2&t=11" when tehsil 11 is in district 1). A markaz with no
// parents chosen fills in its tehsil and district, so the dropdowns show the same scope.
export function normalizeSelection(model, sel) {
  const d = model.data;
  const out = { ...sel };
  if (out.tehsil !== null && out.district !== null && d.tehsils[out.tehsil].district !== out.district) {
    out.tehsil = null;
  }
  if (out.markaz !== null) {
    const parent = d.markaz[out.markaz].tehsil;
    const inTehsil = out.tehsil === null || out.tehsil === parent;
    const inDistrict = out.district === null || d.tehsils[parent].district === out.district;
    if (inTehsil && inDistrict) {
      out.tehsil = parent;
    } else {
      out.markaz = null;
    }
  }
  if (out.tehsil !== null && out.district === null) {
    out.district = d.tehsils[out.tehsil].district;
  }
  return out;
}

export function findEmis(model, text) {
  const code = String(text ?? "").trim();
  if (!code) return -1;
  const i = model.emisIndex.get(code);
  return i === undefined ? -1 : i;
}

export function rowDetails(model, i) {
  const r = model.rows[i];
  const C = model.C;
  const d = model.data;
  const status = d.statuses[r[C.status]];
  return {
    index: i,
    emis: String(r[C.emis]),
    name: r[C.name],
    wing: d.wings[r[C.wing]]?.label ?? "",
    district: d.districts[r[C.district]]?.name ?? "",
    tehsil: d.tehsils[r[C.tehsil]]?.name ?? "",
    markaz: r[C.markaz] === null ? "" : d.markaz[r[C.markaz]].label,
    schoolId: r[C.school_id] === null ? "" : String(r[C.school_id]),
    lat: r[C.lat],
    lon: r[C.lon],
    status: status.label,
    statusKey: status.key,
    mapped: status.mapped,
  };
}

export function mappedIn(model, rowIdxs) {
  return rowIdxs.filter((i) => statusOf(model, i).mapped);
}

export function summarize(model, rowIdxs) {
  const byWing = new Array(model.data.wings.length).fill(0);
  let mapped = 0;
  for (const i of rowIdxs) {
    byWing[model.rows[i][model.C.wing]] += 1;
    if (statusOf(model, i).mapped) mapped += 1;
  }
  return { total: rowIdxs.length, mapped, notMapped: rowIdxs.length - mapped, byWing };
}

// Schools that are not on the map, grouped by status, plus locations shared by 2+ schools.
export function dataIssues(model) {
  const groups = {};
  const byPoint = new Map();
  model.rows.forEach((row, i) => {
    const st = statusOf(model, i);
    if (!st.mapped) {
      (groups[st.key] ||= { label: st.label, rows: [] }).rows.push(i);
      return;
    }
    const key = `${row[model.C.lat]},${row[model.C.lon]}`;
    if (!byPoint.has(key)) byPoint.set(key, []);
    byPoint.get(key).push(i);
  });
  const shared = [...byPoint.values()]
    .filter((rows) => rows.length >= SHARED_MIN)
    .map((rows) => ({ lat: model.rows[rows[0]][model.C.lat], lon: model.rows[rows[0]][model.C.lon], rows }))
    .sort((a, b) => b.rows.length - a.rows.length);
  return { groups, shared };
}

export function onlyIssues(model, rowIdxs) {
  return rowIdxs.filter((i) => !statusOf(model, i).mapped);
}

export function textFilter(model, rowIdxs, query) {
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return rowIdxs;
  const C = model.C;
  return rowIdxs.filter((i) => {
    const row = model.rows[i];
    return String(row[C.emis]).includes(q) || String(row[C.name]).toLowerCase().includes(q);
  });
}

const ACCESSORS = {
  emis: (m, i) => m.rows[i][m.C.emis],
  name: (m, i) => m.rows[i][m.C.name],
  lat: (m, i) => m.rows[i][m.C.lat],
  lon: (m, i) => m.rows[i][m.C.lon],
  status: (m, i) => statusOf(m, i).label,
};

// Blanks sort last whichever way the column is sorted. Unknown keys keep base order.
export function sortRows(model, rowIdxs, key, dir = "asc") {
  const get = ACCESSORS[key];
  if (!get) return rowIdxs.slice();
  const sign = dir === "desc" ? -1 : 1;
  return rowIdxs.slice().sort((a, b) => {
    const va = get(model, a);
    const vb = get(model, b);
    const na = va === null || va === undefined || va === "";
    const nb = vb === null || vb === undefined || vb === "";
    if (na || nb) return na === nb ? 0 : na ? 1 : -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * sign;
    return collator.compare(String(va), String(vb)) * sign;
  });
}

export function pageOf(rowIdxs, page, size) {
  const pages = Math.max(1, Math.ceil(rowIdxs.length / size));
  const p = Math.min(Math.max(0, page), pages - 1);
  const start = p * size;
  return {
    page: p,
    pages,
    total: rowIdxs.length,
    items: rowIdxs.slice(start, start + size),
    from: rowIdxs.length ? start + 1 : 0,
    to: Math.min(rowIdxs.length, start + size),
  };
}

export function csvCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return String(value);
  let s = String(value);
  // Stop spreadsheet formula injection from text fields (names are free text).
  if (/^[=+\-@\t\r]/.test(s) && Number.isNaN(Number(s))) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export const CSV_HEADER = [
  "EMIS", "School name", "Wing", "District", "Tehsil", "Markaz",
  "SIS school ID", "Latitude", "Longitude", "Status",
];

export function toCsv(model, rowIdxs) {
  const lines = [CSV_HEADER.map(csvCell).join(",")];
  for (const i of rowIdxs) {
    const d = rowDetails(model, i);
    lines.push([
      d.emis, d.name, d.wing, d.district, d.tehsil, d.markaz,
      d.schoolId, d.lat, d.lon, d.status,
    ].map(csvCell).join(","));
  }
  return lines.join("\r\n") + "\r\n";
}

// Shareable state: #d=<district id>&w=<wing code>&t=<tehsil id>&m=<markaz id>&e=<EMIS>
export function selectionToHash(model, sel, emis = null) {
  const d = model.data;
  const p = new URLSearchParams();
  if (sel.district !== null) p.set("d", d.districts[sel.district].id);
  if (sel.wing !== null) p.set("w", d.wings[sel.wing].code);
  if (sel.tehsil !== null) p.set("t", d.tehsils[sel.tehsil].id);
  if (sel.markaz !== null) p.set("m", d.markaz[sel.markaz].id);
  if (emis) p.set("e", emis);
  return p.toString();
}

// Levels that have no schools under their parents are dropped too, so a shared link
// never shows an empty dropdown with a silently applied filter.
export function validateSelection(model, sel) {
  let out = normalizeSelection(model, sel);
  for (const level of ["district", "wing", "tehsil", "markaz"]) {
    if (out[level] === null) continue;
    const exists = levelOptions(model, out, level).some((o) => o.value === out[level]);
    if (!exists) out = setLevel(out, level, null);
  }
  return out;
}

export function selectionFromHash(model, hash) {
  const p = new URLSearchParams(String(hash ?? "").replace(/^#/, ""));
  const d = model.data;
  const sel = { ...EMPTY_SELECTION };
  if (model.districtById.has(p.get("d"))) sel.district = model.districtById.get(p.get("d"));
  const wing = d.wings.findIndex((w) => w.code === p.get("w"));
  if (wing >= 0) sel.wing = wing;
  if (model.tehsilById.has(p.get("t"))) sel.tehsil = model.tehsilById.get(p.get("t"));
  if (model.markazById.has(p.get("m"))) sel.markaz = model.markazById.get(p.get("m"));
  return { sel: validateSelection(model, sel), emis: p.get("e") || null };
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export function formatCount(n) {
  return Number(n).toLocaleString("en-US");
}

// "1 school", "2 schools", "1,234 pins". Pass the plural form when it is irregular.
export function plural(n, one, many = `${one}s`) {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}

export function formatCoord(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(6);
}

export function scopeLabel(model, sel) {
  const d = model.data;
  const parts = [];
  parts.push(sel.district === null ? "All districts" : d.districts[sel.district].name);
  parts.push(sel.wing === null ? "All wings" : d.wings[sel.wing].label);
  parts.push(sel.tehsil === null ? "All tehsils" : d.tehsils[sel.tehsil].name);
  parts.push(sel.markaz === null ? "All markaz" : d.markaz[sel.markaz].label);
  return parts.join(" › ");
}
