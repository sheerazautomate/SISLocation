"""Phase-2 regression tests using only temporary files and fake responses."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from sis import coords_ci, fetch_coords, merge
from sis.common import JsonlStore


class Response:
    def __init__(self, text="", status=302, location=None):
        self.text = text
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class TestRedirects(unittest.TestCase):
    def resolve(self, response):
        with patch.object(fetch_coords, "fetch", return_value=response) as get:
            result = fetch_coords.resolve_coords(
                {"school_id": "10669", "emis_code": "32230183"},
                timeout=1, retries=0, insecure=False)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            return result

    def test_encoded_query_and_entities(self):
        response = Response(location="https://www.google.com/maps?foo=x&amp;saddr=31.2%2C%2074.3")
        rec = self.resolve(response)
        self.assertEqual((rec["latitude"], rec["longitude"]), (31.2, 74.3))

    def test_meta_reordered_attributes_and_spaces(self):
        response = Response('<meta content="0; URL=https://www.google.com/maps?saddr=31.2, 74.3" '
                            "http-equiv='Refresh'>", status=200)
        self.assertEqual(self.resolve(response)["status"], "ok")

    def test_js_replace_and_escaped_slashes(self):
        response = Response(r'location.replace("https:\/\/www.google.com\/maps?saddr=31,74")',
                            status=200)
        self.assertEqual(self.resolve(response)["status"], "ok")

    def test_negative_coordinates_and_country_domain(self):
        rec = self.resolve(Response(location="https://maps.google.co.uk/maps?q=-33.8,151.2"))
        self.assertEqual(rec["status"], "ok")
        self.assertTrue(rec["out_of_range"])

    def test_http_errors_remain_retryable(self):
        for code in (401, 403, 404, 429, 500, 502):
            with self.subTest(code=code):
                self.assertEqual(self.resolve(Response(status=code))["status"], "error")

    def test_login_or_internal_redirect_is_error(self):
        for url in ("/login", "https://sis.pesrp.edu.pk/login",
                    "https://www.google.com.evil.example/maps?saddr=31,74",
                    "https://[bad/maps?saddr=31,74"):
            with self.subTest(url=url):
                self.assertEqual(self.resolve(Response(location=url))["status"], "error")

    def test_invalid_coords_not_truncated_or_exported(self):
        rec = self.resolve(Response(location="https://www.google.com/maps?saddr=31,740"))
        self.assertEqual(rec["status"], "error")
        self.assertNotIn("longitude", rec)
        self.assertIsNone(fetch_coords.parse_coords("https://www.google.com/maps?q=31,74oops"))

    def test_maps_without_coords_is_missing(self):
        self.assertEqual(self.resolve(Response(location="https://www.google.com/maps"))["status"],
                         "missing")

    def test_network_exception_keeps_key(self):
        with patch.object(fetch_coords, "fetch", side_effect=TimeoutError("offline")):
            rec = fetch_coords.resolve_coords({"school_id": "1"}, timeout=1,
                                              retries=0, insecure=False)
        self.assertEqual(rec["school_id"], "1")
        self.assertEqual(rec["status"], "error")


class TestCheckpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ids = self.write("ids.jsonl", [
            {"school_id": str(i), "emis_code": str(32230000+i), "status": "ok"}
            for i in range(1, 5)])
        self.base = self.path("base.json")
        with open(self.base, "w") as fh:
            json.dump([{"emis_code": str(32230000+i)} for i in range(1, 5)], fh)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, name, rows):
        path = self.path(name)
        with open(path, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path

    def test_plan_counts_dedupes_and_clamps(self):
        with open(self.ids, "a") as fh:
            fh.write(json.dumps({"school_id": "1", "status": "ok"}) + "\n")
            fh.write(json.dumps({"school_id": "5", "status": "error"}) + "\n")
        p = coords_ci.plan(self.ids, self.base, 10, 4, 0.25, 20)
        self.assertEqual(p["schools"], "4")
        self.assertEqual(json.loads(p["matrix"]), [0, 1, 2, 3])

    def test_id_order_does_not_change_shards_or_cache_digest(self):
        rows = fetch_coords.load_id_rows(self.ids)
        reversed_ids = self.write("reversed.jsonl", list(reversed(rows)))
        self.assertEqual(fetch_coords.load_id_rows(reversed_ids), rows)
        self.assertEqual(coords_ci.plan(self.ids, self.base, 2, 4, 0, 0),
                         coords_ci.plan(reversed_ids, self.base, 2, 4, 0, 0))

    def test_plan_rejects_bad_inputs(self):
        for shards, workers, delay, limit in [(0,4,0,0), (21,4,0,0), (1,0,0,0),
                                              (1,4,-1,0), (1,4,float('nan'),0), (1,4,0,-1)]:
            with self.subTest(values=(shards,workers,delay,limit)), self.assertRaises(ValueError):
                coords_ci.plan(self.ids, self.base, shards, workers, delay, limit)

    def test_seed_best_of_baseline_and_cache_filters_to_shard(self):
        baseline = self.write("baseline.jsonl", [
            {"school_id": "1", "status": "ok", "latitude": 31},
            {"school_id": "2", "status": "ok"}])
        out = self.write("coords-0.jsonl", [{"school_id": "1", "status": "error"},
                                            {"school_id": "3", "status": "missing"}])
        coords_ci.seed(self.ids, baseline, out, 0, 2)
        rows = JsonlStore(out, "school_id").load_done()
        self.assertEqual(set(rows), {"1", "2"})
        self.assertEqual(rows["1"]["status"], "ok")

    def test_torn_checkpoint_repair_and_complete_unterminated_line(self):
        for tail, expected in [(b'{"school_id":"2",', {"1", "3"}),
                               (b'{"school_id":"2","status":"ok"}', {"1", "2", "3"}),
                               (b'{"school_id":"2","error":"\xe2', {"1", "3"})]:
            with self.subTest(tail=tail):
                out = self.path("torn.jsonl")
                with open(out, "wb") as fh:
                    fh.write(b'{"school_id":"1","status":"ok"}\n' + tail)
                self.assertIn("1", JsonlStore(out, "school_id").load_done())
                with JsonlStore(out, "school_id") as store:
                    store.write({"school_id": "3", "status": "ok"})
                self.assertEqual(set(JsonlStore(out, "school_id").load_done()), expected)

    def test_partial_finalize_preserves_baseline_and_reports_pending(self):
        baseline = self.write("baseline.jsonl", [{"school_id": "1", "status": "ok",
                                                   "latitude": 31, "longitude": 74},
                                                  {"school_id": "99", "status": "ok"}])
        part = self.write("coords-0.jsonl", [{"school_id": "1", "status": "error"},
                                            {"school_id": "2", "status": "missing",
                                             "latitude": 0, "longitude": 0}])
        out, prefix = self.path("all.jsonl"), self.path("final")
        report = coords_ci.finalize(self.ids, self.base, baseline, part, out, prefix)
        records = JsonlStore(out, "school_id").load_done()
        self.assertEqual(set(records), {"1", "2", "99"})
        self.assertEqual(records["1"]["status"], "ok")
        self.assertIn("Pending: **2**", report)
        with open(prefix + ".geojson") as fh:
            self.assertEqual(len(json.load(fh)["features"]), 1)

    def test_no_shard_artifacts_preserves_baseline(self):
        baseline = self.write("baseline.jsonl", [{"school_id": "1", "status": "missing"}])
        coords_ci.finalize(self.ids, self.base, baseline, self.path("missing-*.jsonl"),
                           baseline, self.path("final"))
        self.assertEqual(set(JsonlStore(baseline, "school_id").load_done()), {"1"})

    def test_multiple_attempt_artifacts_preserve_earlier_success(self):
        from sis.combine import combine

        parts = self.path("parts")
        for attempt, rows in [(1, [{"school_id": "1", "status": "ok"},
                                  {"school_id": "2", "status": "ok"}]),
                              (2, [{"school_id": "1", "status": "error"},
                                  {"school_id": "3", "status": "ok"}])]:
            directory = os.path.join(parts, f"coords-part-0-attempt-{attempt}")
            os.makedirs(directory)
            with open(os.path.join(directory, "coords-0.jsonl"), "wb") as fh:
                for row in rows:
                    fh.write(json.dumps(row).encode() + b"\n")
                fh.write(b'{"school_id":"4","error":"\xe2')
        out = self.path("combined.jsonl")
        combine([os.path.join(parts, "**", "coords-*.jsonl")], "school_id", out)
        rows = JsonlStore(out, "school_id").load_done()
        self.assertEqual(set(rows), {"1", "2", "3"})
        self.assertEqual(rows["1"]["status"], "ok")

    def test_cli_resume_and_retry_selection(self):
        out = self.write("coords.jsonl", [{"school_id": "1", "status": "ok"},
                                          {"school_id": "2", "status": "error"},
                                          {"school_id": "3", "status": "missing"}])
        seen = []

        def resolve(row, **kwargs):
            seen.append(row["school_id"])
            return dict(row, status="ok", latitude=31, longitude=74)

        args = ["-i", self.ids, "-o", out, "-w", "1"]
        with patch.object(fetch_coords, "resolve_coords", side_effect=resolve):
            self.assertEqual(fetch_coords.main(args), 0)
            self.assertEqual(seen, ["4"])
            seen.clear()
            self.assertEqual(fetch_coords.main(args + ["--retry-errors", "--retry-missing"]), 0)
            self.assertEqual(set(seen), {"2", "3"})

    def test_geojson_filters_invalid_and_missing_coordinates(self):
        rows = [{"status": status, "latitude": lat, "longitude": lon}
                for status, lat, lon in [("ok", 31, 74), ("ok", 48, 2), ("coords_missing", 0, 0),
                                         ("ok", 100, 74), ("ok", 0, 0), ("ok", float("nan"), 74)]]
        prefix = self.path("final")
        merge.write_outputs(rows, prefix)
        with open(prefix + ".geojson") as fh:
            self.assertEqual(len(json.load(fh)["features"]), 2)


if __name__ == "__main__":
    unittest.main()
