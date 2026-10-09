// Unit tests for dashboard/js/scope.js. Run: npm --prefix dashboard test
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as S from "../js/scope.js";

const HERE = dirname(fileURLToPath(import.meta.url));

// Synthetic dataset: 6 schools, 2 districts, 3 tehsils, 4 markaz, all 3 wings.
const STATUSES = [
  { key: "mapped", label: "Mapped", mapped: true },
  { key: "outside", label: "Outside Punjab", mapped: false },
  { key: "no_coords", label: "No coordinates", mapped: false },
  { key: "coord_error", label: "Coordinate error", mapped: false },
  { key: "not_in_sis", label: "Not in SIS", mapped: false },
  { key: "lookup_error", label: "Lookup error", mapped: false },
  { key: "pending", label: "Pending lookup", mapped: false },
];
const FIXTURE = {
  columns: ["emis", "name", "wing", "district", "tehsil", "markaz", "school_id", "lat", "lon", "status", "n6", "n7", "n8"],
  statuses: STATUSES,
  wings: [
    { code: "SE", label: "Secondary Education (SE)" },
    { code: "W-EE", label: "Women's Elementary Education (W-EE)" },
    { code: "M-EE", label: "Male Elementary Education (M-EE)" },
  ],
  districts: [{ id: "1", name: "ALPHA" }, { id: "2", name: "BETA" }],
  tehsils: [
    { id: "11", name: "T-ONE", district: 0 },
    { id: "21", name: "T-TWO", district: 1 },
    { id: "12", name: "T-THREE", district: 0 },
  ],
  markaz: [
    { id: "111", label: "SATLUJ (111)", tehsil: 0 },
    { id: "112", label: "SECONDARY-WING (112)", tehsil: 0 },
    { id: "121", label: "NORTH (121)", tehsil: 2 },
    { id: "211", label: "CHAK 5 (211)", tehsil: 1 },
  ],
  built_at: "2026-10-09T00:00:00Z",
  counts: { schools: 6, mapped: 3 },
  rows: [
    ["30000001", "GBHS A", 1, 0, 0, 0, "1001", 30.1, 71.1, 0, 10, 12, 2],
    ["30000002", "GGHS B", 0, 0, 0, 1, "1002", 30.1, 71.1, 0, 10, 12, 2], // shares a point with row 0
    ["30000003", "GHS C", 2, 0, 2, 2, "1003", null, null, 2, 1, 1, 1],
    ["30000004", "GBHS D", 2, 1, 1, 3, "1004", 36.07, 129.38, 1, 1, 1, 1],
    ["30000005", "GGHS E", 1, 1, 1, null, null, null, null, 4, 1, 1, 1],
    ["30000006", "GHS F", 0, 0, 0, 1, "1006", 30.2, 71.2, 0, 1, 1, 1],
  ],
};
const ALL = [0, 1, 2, 3, 4, 5];

function model() {
  return S.createModel(structuredClone(FIXTURE));
}

describe("model and lookups", () => {
  test("createModel indexes columns and EMIS codes", () => {
    const m = model();
    assert.equal(m.C.emis, 0);
    assert.equal(m.C.status, 9);
    assert.equal(S.findEmis(m, "30000004"), 3);
    assert.equal(S.findEmis(m, "  30000002 "), 1, "surrounding spaces are ignored");
    assert.equal(S.findEmis(m, "99999999"), -1);
    assert.equal(S.findEmis(m, ""), -1);
    assert.equal(S.findEmis(m, null), -1);
  });

  test("status lookup and mapped filter", () => {
    const m = model();
    assert.equal(S.statusOf(m, 3).key, "outside");
    assert.deepEqual(S.mappedIn(m, ALL), [0, 1, 5]);
  });
});

describe("scope and live dropdown options", () => {
  test("scopeRows follows each level independently", () => {
    const m = model();
    const sel = (o) => ({ ...S.EMPTY_SELECTION, ...o });
    assert.deepEqual(S.scopeRows(m, sel({})), ALL);
    assert.deepEqual(S.scopeRows(m, sel({ district: 0 })), [0, 1, 2, 5]);
    assert.deepEqual(S.scopeRows(m, sel({ district: 0, wing: 0 })), [1, 5]);
    assert.deepEqual(S.scopeRows(m, sel({ tehsil: 0 })), [0, 1, 5]);
    assert.deepEqual(S.scopeRows(m, sel({ markaz: 1 })), [1, 5]);
    assert.deepEqual(S.scopeRows(m, sel({ district: 1, markaz: 0 })), []);
  });

  test("district options list every district with its school count", () => {
    const m = model();
    const opts = S.levelOptions(m, S.EMPTY_SELECTION, "district");
    assert.deepEqual(opts.map((o) => [o.label, o.count]), [["ALPHA", 4], ["BETA", 2]]);
  });

  test("wing options only show wings that exist under the chosen district", () => {
    const m = model();
    const beta = S.levelOptions(m, { ...S.EMPTY_SELECTION, district: 1 }, "wing");
    assert.deepEqual(beta.map((o) => o.label), ["Women's Elementary Education (W-EE)", "Male Elementary Education (M-EE)"]);
    assert.ok(!beta.some((o) => o.value === 0), "no SE schools in BETA, so SE is not offered");
  });

  test("tehsil options respect district and wing", () => {
    const m = model();
    const alphaAll = S.levelOptions(m, { ...S.EMPTY_SELECTION, district: 0 }, "tehsil");
    assert.deepEqual(alphaAll.map((o) => [o.label, o.count]), [["T-ONE", 3], ["T-THREE", 1]]);
    const alphaSe = S.levelOptions(m, { ...S.EMPTY_SELECTION, district: 0, wing: 0 }, "tehsil");
    assert.deepEqual(alphaSe.map((o) => [o.label, o.count]), [["T-ONE", 2]]);
  });

  test("markaz options are limited to the chosen tehsil and show the stored label", () => {
    const m = model();
    const opts = S.levelOptions(m, { ...S.EMPTY_SELECTION, tehsil: 0 }, "markaz");
    assert.deepEqual(opts.map((o) => [o.label, o.count]), [["SATLUJ (111)", 1], ["SECONDARY-WING (112)", 2]]);
  });

  test("changing a level resets every level below it", () => {
    const sel = { district: 0, wing: 0, tehsil: 0, markaz: 1 };
    assert.deepEqual(S.setLevel(sel, "district", 1), { district: 1, wing: null, tehsil: null, markaz: null });
    assert.deepEqual(S.setLevel(sel, "wing", 2), { district: 0, wing: 2, tehsil: null, markaz: null });
    assert.deepEqual(S.setLevel(sel, "tehsil", 2), { district: 0, wing: 0, tehsil: 2, markaz: null });
    assert.deepEqual(S.setLevel(sel, "markaz", null), { district: 0, wing: 0, tehsil: 0, markaz: null });
  });

  test("normalizeSelection drops levels that contradict their parent", () => {
    const m = model();
    assert.deepEqual(
      S.normalizeSelection(m, { district: 1, wing: null, tehsil: 0, markaz: 1 }),
      { district: 1, wing: null, tehsil: null, markaz: null },
    );
    assert.deepEqual(
      S.normalizeSelection(m, { district: 0, wing: null, tehsil: 0, markaz: 2 }),
      { district: 0, wing: null, tehsil: 0, markaz: null },
    );
  });
});

describe("jump to school and details", () => {
  test("selectionForRow puts the school's own scope on screen", () => {
    const m = model();
    assert.deepEqual(S.selectionForRow(m, 1), { district: 0, wing: 0, tehsil: 0, markaz: 1 });
    assert.equal(S.selectionForRow(m, 4).markaz, null, "unresolved school has no markaz");
    assert.ok(S.selectionMatchesRow(m, S.selectionForRow(m, 3), 3));
  });

  test("rowDetails returns readable names and blank IDs for unresolved schools", () => {
    const m = model();
    const d = S.rowDetails(m, 2);
    assert.equal(d.wing, "Male Elementary Education (M-EE)");
    assert.equal(d.tehsil, "T-THREE");
    assert.equal(d.markaz, "NORTH (121)");
    assert.equal(d.lat, null);
    assert.equal(d.status, "No coordinates");
    assert.equal(d.mapped, false);
    const unresolved = S.rowDetails(m, 4);
    assert.equal(unresolved.markaz, "");
    assert.equal(unresolved.schoolId, "");
    assert.equal(unresolved.statusKey, "not_in_sis");
  });
});

describe("summaries, issues, and filters", () => {
  test("summarize counts schools, mapped schools, and wings", () => {
    const m = model();
    assert.deepEqual(S.summarize(m, ALL), { total: 6, mapped: 3, notMapped: 3, byWing: [2, 2, 2] });
    assert.deepEqual(S.summarize(m, [1, 5]), { total: 2, mapped: 2, notMapped: 0, byWing: [2, 0, 0] });
  });

  test("dataIssues groups unmapped schools and finds shared locations", () => {
    const m = model();
    const { groups, shared } = S.dataIssues(m);
    assert.deepEqual(groups.outside.rows, [3]);
    assert.deepEqual(groups.no_coords.rows, [2]);
    assert.deepEqual(groups.not_in_sis.rows, [4]);
    assert.deepEqual(shared, [], "a pair is below the threshold, so it is not listed");
  });

  test("a point shared by five or more schools is listed as a shared location", () => {
    const data = structuredClone(FIXTURE);
    for (let k = 0; k < 3; k++) {
      data.rows.push([`3001001${k}`, `EXTRA ${k}`, 0, 0, 0, 1, `200${k}`, 30.1, 71.1, 0, 1, 1, 1]);
    }
    const m = S.createModel(data);
    const { shared } = S.dataIssues(m);
    assert.equal(shared.length, 1);
    assert.deepEqual(shared[0].rows, [0, 1, 6, 7, 8]);
    assert.equal(shared[0].lat, 30.1);
  });

  test("plural forms counts correctly", () => {
    assert.equal(S.plural(1, "school"), "1 school");
    assert.equal(S.plural(2, "school"), "2 schools");
    assert.equal(S.plural(1, "pin"), "1 pin");
    assert.equal(S.plural(1234, "school"), "1,234 schools");
    assert.equal(S.plural(3, "child", "children"), "3 children");
  });

  test("onlyIssues keeps the schools that are not on the map", () => {
    const m = model();
    assert.deepEqual(S.onlyIssues(m, ALL), [2, 3, 4]);
  });

  test("textFilter matches EMIS or name, case-insensitively", () => {
    const m = model();
    assert.deepEqual(S.textFilter(m, ALL, "gbhs"), [0, 3]);
    assert.deepEqual(S.textFilter(m, ALL, "3000000"), ALL);
    assert.deepEqual(S.textFilter(m, ALL, "30000005"), [4]);
    assert.deepEqual(S.textFilter(m, ALL, "nope"), []);
    assert.deepEqual(S.textFilter(m, ALL, "   "), ALL);
  });

  test("sortRows sorts names and EMIS, puts blanks last, and keeps ties stable", () => {
    const m = model();
    assert.deepEqual(S.sortRows(m, ALL, "name", "asc"), [0, 3, 1, 4, 2, 5]);
    assert.deepEqual(S.sortRows(m, ALL, "name", "desc"), [5, 2, 4, 1, 3, 0]);
    assert.deepEqual(S.sortRows(m, ALL, "lat", "asc"), [0, 1, 5, 3, 2, 4]);
    assert.deepEqual(S.sortRows(m, ALL, "lat", "desc"), [3, 5, 0, 1, 2, 4]);
    assert.deepEqual(S.sortRows(m, ALL, "unknown", "asc"), ALL);
  });

  test("pageOf slices pages and clamps out-of-range page numbers", () => {
    const p0 = S.pageOf(ALL, 0, 4);
    assert.deepEqual([p0.items, p0.pages, p0.from, p0.to, p0.total], [[0, 1, 2, 3], 2, 1, 4, 6]);
    const p9 = S.pageOf(ALL, 9, 4);
    assert.deepEqual([p9.page, p9.items, p9.from, p9.to], [1, [4, 5], 5, 6]);
    const empty = S.pageOf([], 3, 50);
    assert.deepEqual([empty.page, empty.pages, empty.from, empty.to, empty.items], [0, 1, 0, 0, []]);
  });
});

describe("CSV export", () => {
  test("csvCell quotes commas and quotes and neutralises spreadsheet formulas", () => {
    assert.equal(S.csvCell("plain"), "plain");
    assert.equal(S.csvCell("a,b"), '"a,b"');
    assert.equal(S.csvCell('say "hi"'), '"say ""hi"""');
    assert.equal(S.csvCell("=HYPERLINK(1)"), "'=HYPERLINK(1)");
    assert.equal(S.csvCell("-7.5"), "-7.5", "negative numbers are left alone");
    assert.equal(S.csvCell(-7.5), "-7.5");
    assert.equal(S.csvCell(0), "0");
    assert.equal(S.csvCell(null), "");
    assert.equal(S.csvCell(undefined), "");
  });

  test("toCsv writes a header, CRLF endings, and blanks for missing coordinates", () => {
    const m = model();
    const text = S.toCsv(m, [2, 4]);
    const lines = text.split("\r\n");
    assert.equal(lines[0], S.CSV_HEADER.join(","));
    assert.equal(lines.length, 4, "header, two rows, trailing newline");
    assert.equal(lines[1], "30000003,GHS C,Male Elementary Education (M-EE),ALPHA,T-THREE,NORTH (121),1003,,,No coordinates");
    assert.equal(lines[2], "30000005,GGHS E,Women's Elementary Education (W-EE),BETA,T-TWO,,,,,Not in SIS");
    assert.ok(text.endsWith("\r\n"));
  });
});

describe("shareable hash", () => {
  test("round-trips the selection and EMIS", () => {
    const m = model();
    const sel = { district: 0, wing: 1, tehsil: 0, markaz: 0 };
    const hash = S.selectionToHash(m, sel, "30000001");
    assert.equal(hash, "d=1&w=W-EE&t=11&m=111&e=30000001");
    const back = S.selectionFromHash(m, "#" + hash);
    assert.deepEqual(back.sel, sel);
    assert.equal(back.emis, "30000001");
  });

  test("empty and unknown values are ignored", () => {
    const m = model();
    assert.deepEqual(S.selectionFromHash(m, ""), { sel: S.EMPTY_SELECTION, emis: null });
    assert.deepEqual(S.selectionFromHash(m, "#d=999&w=XX&t=nope&m=0"), { sel: S.EMPTY_SELECTION, emis: null });
    assert.equal(S.selectionToHash(m, S.EMPTY_SELECTION), "");
  });

  test("a markaz-only hash fills in its tehsil and district", () => {
    const m = model();
    assert.deepEqual(S.selectionFromHash(m, "#m=121").sel, { district: 0, wing: null, tehsil: 2, markaz: 2 });
  });

  test("a level with no schools under its parents is dropped", () => {
    const m = model();
    // T-THREE (tehsil index 2) has only M-EE schools, so a wing=SE link cannot keep it.
    assert.deepEqual(S.selectionFromHash(m, "#w=SE&t=12").sel, { district: 0, wing: 0, tehsil: null, markaz: null });
    assert.deepEqual(S.selectionFromHash(m, "#w=M-EE&t=12").sel, { district: 0, wing: 2, tehsil: 2, markaz: null });
  });

  test("a hash with a tehsil from another district is corrected", () => {
    const m = model();
    const back = S.selectionFromHash(m, "#d=2&t=11&m=111");
    assert.deepEqual(back.sel, { district: 1, wing: null, tehsil: null, markaz: null });
  });
});

describe("formatting helpers", () => {
  test("escapeHtml, counts, coordinates, and scope labels", () => {
    assert.equal(S.escapeHtml(`<a href="x">&'`), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;");
    assert.equal(S.escapeHtml(null), "");
    assert.equal(S.formatCount(38134), "38,134");
    assert.equal(S.formatCoord(30.1), "30.100000");
    assert.equal(S.formatCoord(null), "—");
    const m = model();
    assert.equal(
      S.scopeLabel(m, { district: 0, wing: 0, tehsil: null, markaz: null }),
      "ALPHA › Secondary Education (SE) › All tehsils › All markaz",
    );
  });
});

// Optional check against the generated dataset (python run.py dashboard). Skipped when absent.
const REAL = join(HERE, "..", "data", "schools.json");
describe("generated dataset", { skip: !existsSync(REAL) && "dashboard/data/schools.json not built" }, () => {
  const data = existsSync(REAL) ? JSON.parse(readFileSync(REAL, "utf8")) : null;

  test("scope totals agree with the build counts", () => {
    const m = S.createModel(data);
    const all = S.scopeRows(m, S.EMPTY_SELECTION);
    assert.equal(all.length, data.counts.schools);
    assert.equal(S.summarize(m, all).mapped, data.counts.mapped);
    const districtTotal = S.levelOptions(m, S.EMPTY_SELECTION, "district").reduce((n, o) => n + o.count, 0);
    assert.equal(districtTotal, data.counts.schools);
  });

  test("every district's wings and tehsils add up to the district", () => {
    const m = S.createModel(data);
    for (let d = 0; d < data.districts.length; d++) {
      const sel = { ...S.EMPTY_SELECTION, district: d };
      const n = S.scopeRows(m, sel).length;
      const byWing = S.levelOptions(m, sel, "wing").reduce((s, o) => s + o.count, 0);
      const byTehsil = S.levelOptions(m, sel, "tehsil").reduce((s, o) => s + o.count, 0);
      assert.equal(byWing, n, data.districts[d].name);
      assert.equal(byTehsil, n, data.districts[d].name);
    }
  });

  test("a sample of schools jumps to a scope that contains them", () => {
    const m = S.createModel(data);
    for (let i = 0; i < data.rows.length; i += 997) {
      const sel = S.selectionForRow(m, i);
      assert.ok(S.selectionMatchesRow(m, sel, i), `row ${i}`);
      const back = S.selectionFromHash(m, "#" + S.selectionToHash(m, sel, data.rows[i][0]));
      assert.deepEqual(back.sel, sel, `row ${i} round trip`);
    }
  });
});
