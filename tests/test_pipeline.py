"""Offline tests - no network required. Run: python -m pytest -q  (or python tests/test_pipeline.py)"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sis import fetch_coords, fetch_ids, merge  # noqa: E402
from sis.common import JsonlStore, extract_emis_codes  # noqa: E402


class FakeResp:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class TestInputParsing(unittest.TestCase):
    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_array_of_objects(self):
        p = self._write("b.json", json.dumps(
            [{"emis_code": "32230183", "name": "A"}, {"emis_code": "31110001", "name": "B"}]))
        codes, field = extract_emis_codes(p)
        self.assertEqual(codes, ["32230183", "31110001"])
        self.assertEqual(field, "emis_code")

    def test_wrapped_and_alt_key(self):
        p = self._write("b.json", json.dumps(
            {"data": [{"EMIS Code": "32230183"}, {"EMIS Code": "32230184"}]}))
        codes, field = extract_emis_codes(p)
        self.assertEqual(codes, ["32230183", "32230184"])
        self.assertEqual(field, "EMIS Code")

    def test_scalar_list_dedup_and_float_strings(self):
        p = self._write("b.json", json.dumps(["32230183", "32230183", "31110001.0", "bad"]))
        codes, _ = extract_emis_codes(p)
        self.assertEqual(codes, ["32230183", "31110001"])

    def test_csv_input(self):
        p = self._write("b.csv", "id,emis\n1,32230183\n2,31110001\n")
        codes, field = extract_emis_codes(p)
        self.assertEqual(codes, ["32230183", "31110001"])
        self.assertEqual(field, "emis")


class TestIdParsing(unittest.TestCase):
    def _run(self, resp):
        fetch_ids.fetch = lambda *a, **k: resp  # type: ignore[assignment]
        return fetch_ids.resolve_ids("32230183", timeout=1, retries=0, insecure=False)

    def tearDown(self):
        from sis.common import fetch as real

        fetch_ids.fetch = real  # type: ignore[assignment]

    def test_valid(self):
        body = ('{"s_district_idFk":"18","s_tehsil_idFk":"70","s_markaz_idFk":"2605",'
                '"s_id":"10669","s_emis_code":"32230183"}')
        rec = self._run(FakeResp(body))
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["school_id"], "10669")
        self.assertEqual(rec["district_id"], "18")
        self.assertEqual(rec["tehsil_id"], "70")
        self.assertEqual(rec["markaz_id"], "2605")

    def test_null_body_is_missing(self):
        self.assertEqual(self._run(FakeResp("null"))["status"], "missing")

    def test_html_error_page(self):
        rec = self._run(FakeResp("<html>oops</html>"))
        self.assertEqual(rec["status"], "error")

    def test_http_error(self):
        self.assertEqual(self._run(FakeResp("", 404))["status"], "error")


class TestCoordParsing(unittest.TestCase):
    def test_saddr(self):
        self.assertEqual(
            fetch_coords.parse_coords("https://www.google.com/maps?saddr=30.84132237,71.2246089"),
            (30.84132237, 71.2246089))

    def test_at_style_and_q_param(self):
        self.assertEqual(fetch_coords.parse_coords("https://maps.google.com/maps/@31.5,74.3,17z"),
                         (31.5, 74.3))
        self.assertEqual(fetch_coords.parse_coords("https://www.google.com/maps?q=31.1,74.0"),
                         (31.1, 74.0))

    def test_no_coords(self):
        self.assertIsNone(fetch_coords.parse_coords("https://www.google.com/maps"))

    def _resolve(self, resp):
        fetch_coords.fetch = lambda *a, **k: resp  # type: ignore[assignment]
        return fetch_coords.resolve_coords({"school_id": "10669", "emis_code": "32230183"},
                                           timeout=1, retries=0, insecure=False)

    def tearDown(self):
        from sis.common import fetch as real

        fetch_coords.fetch = real  # type: ignore[assignment]

    def test_302_header(self):
        rec = self._resolve(FakeResp("", 302, {
            "Location": "https://www.google.com/maps?saddr=30.84132237,71.2246089"}))
        self.assertEqual(rec["status"], "ok")
        self.assertAlmostEqual(rec["latitude"], 30.84132237)
        self.assertAlmostEqual(rec["longitude"], 71.2246089)

    def test_meta_refresh_body(self):
        body = ('<meta http-equiv="refresh" '
                'content="0;url=https://www.google.com/maps?saddr=31.0,74.0">')
        rec = self._resolve(FakeResp(body, 200))
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["latitude"], 31.0)

    def test_js_redirect_body(self):
        body = 'window.location = "https://www.google.com/maps?saddr=32.1,73.2";'
        self.assertEqual(self._resolve(FakeResp(body, 200))["status"], "ok")

    def test_zero_coords_marked_missing(self):
        rec = self._resolve(FakeResp("", 302, {"Location": "https://www.google.com/maps?saddr=0,0"}))
        self.assertEqual(rec["status"], "missing")

    def test_out_of_range_flagged(self):
        rec = self._resolve(FakeResp("", 302, {
            "Location": "https://www.google.com/maps?saddr=48.85,2.35"}))
        self.assertTrue(rec["out_of_range"])

    def test_no_redirect(self):
        self.assertEqual(self._resolve(FakeResp("<html>no map</html>", 200))["status"], "missing")


class TestResumeAndMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_store_resume_tolerates_torn_line(self):
        p = os.path.join(self.d, "ids.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write('{"emis_code":"1","status":"ok"}\n{"emis_code":"2","stat')
        done = JsonlStore(p, "emis_code").load_done()
        self.assertEqual(set(done), {"1"})

    def test_merge_statuses_and_outputs(self):
        base = os.path.join(self.d, "base.json")
        with open(base, "w", encoding="utf-8") as fh:
            json.dump([{"emis_code": "32230183"}, {"emis_code": "31110001"},
                       {"emis_code": "99999999"}], fh)
        ids = os.path.join(self.d, "ids.jsonl")
        with open(ids, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"emis_code": "32230183", "school_id": "10669",
                                 "district_id": "18", "tehsil_id": "70",
                                 "markaz_id": "2605", "status": "ok"}) + "\n")
            fh.write(json.dumps({"emis_code": "31110001", "school_id": "1",
                                 "status": "ok"}) + "\n")
            fh.write(json.dumps({"emis_code": "99999999", "status": "missing"}) + "\n")
        coords = os.path.join(self.d, "coords.jsonl")
        with open(coords, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"school_id": "10669", "latitude": 30.84132237,
                                 "longitude": 71.2246089, "status": "ok"}) + "\n")
            fh.write(json.dumps({"school_id": "1", "status": "missing"}) + "\n")

        rows = merge.build_rows(base, ids, coords)
        by = {r["emis_code"]: r for r in rows}
        self.assertEqual(by["32230183"]["status"], "ok")
        self.assertEqual(by["32230183"]["latitude"], 30.84132237)
        self.assertEqual(by["31110001"]["status"], "coords_missing")
        self.assertEqual(by["99999999"]["status"], "id_missing")

        prefix = os.path.join(self.d, "final")
        merge.write_outputs(rows, prefix)
        for ext in ("json", "csv", "geojson"):
            self.assertTrue(os.path.exists(f"{prefix}.{ext}"))
        with open(prefix + ".geojson", encoding="utf-8") as fh:
            gj = json.load(fh)
        self.assertEqual(len(gj["features"]), 1)
        self.assertEqual(gj["features"][0]["geometry"]["coordinates"], [71.2246089, 30.84132237])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSharding(unittest.TestCase):
    def test_shards_are_disjoint_and_complete(self):
        from sis.common import select_shard

        for total in (0, 1, 7, 38000):
            items = list(range(total))
            for shards in (1, 3, 8, 20):
                parts = [select_shard(items, i, shards) for i in range(shards)]
                flat = [x for p in parts for x in p]
                self.assertEqual(sorted(flat), items, f"total={total} shards={shards}")
                self.assertEqual(len(flat), len(set(flat)), "shards overlap")
                sizes = [len(p) for p in parts]
                self.assertLessEqual(max(sizes) - min(sizes), 1, "uneven shards")

    def test_shard_bounds_validated(self):
        from sis.common import select_shard

        with self.assertRaises(SystemExit):
            select_shard([1, 2, 3], 5, 3)

    def test_combine_dedups_and_prefers_success(self):
        from sis.combine import combine

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        with open(os.path.join(d, "ids-0.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"emis_code": "1", "status": "error"}) + "\n")
            fh.write(json.dumps({"emis_code": "2", "status": "ok", "school_id": "22"}) + "\n")
        with open(os.path.join(d, "ids-1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"emis_code": "1", "status": "ok", "school_id": "11"}) + "\n")
            fh.write(json.dumps({"emis_code": "3", "status": "missing"}) + "\n")

        out = os.path.join(d, "all.jsonl")
        _, counts = combine([os.path.join(d, "ids-*.jsonl")], "emis_code", out)
        recs = {json.loads(l)["emis_code"]: json.loads(l)
                for l in open(out, encoding="utf-8") if l.strip()}
        self.assertEqual(len(recs), 3)
        # the successful retry must win over the earlier error
        self.assertEqual(recs["1"]["status"], "ok")
        self.assertEqual(recs["1"]["school_id"], "11")
        self.assertEqual(counts["ok"], 2)
