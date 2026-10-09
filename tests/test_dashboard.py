"""Offline tests for the dashboard data builder. Run: python run.py test"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sis import dashboard_data as dd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _row(district, tehsil, locality, wing, emis, name, n6=10, n7=12, n8=2):
    return [district, tehsil, locality, wing, emis, name, n6, n7, n8]


def _id(emis, status="ok", district="1", tehsil="11", markaz="111", school="1001"):
    rec = {"emis_code": emis, "status": status}
    if status == "ok":
        rec.update(district_id=district, tehsil_id=tehsil, markaz_id=markaz,
                   school_id=school, emis_code_api=emis)
    return rec


def _coord(school, status="ok", lat=30.1, lon=71.1, out_of_range=False, emis="x"):
    rec = {"school_id": school, "emis_code": emis, "status": status}
    if status == "ok":
        rec.update(latitude=lat, longitude=lon)
        if out_of_range:
            rec["out_of_range"] = True
    return rec


class CleanLocalityTests(unittest.TestCase):
    def test_gender_tags_are_removed(self):
        cases = {
            "SATLUJ - MALE": "SATLUJ",
            "SHAHDARA - FEMALE": "SHAHDARA",
            "SHAHKOT CITY 2-FEMALE": "SHAHKOT CITY 2",
            "DOMALA-MALE": "DOMALA",
            "JURA KALAN (WEST) (MALE)": "JURA KALAN (WEST)",
            "CHAK NO 50/MB (FEMALE)": "CHAK NO 50/MB",
            "JATLI MALE -MALE": "JATLI",
            "   MALE": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dd.clean_locality(raw), expected)

    def test_secondary_wing_spellings_unify(self):
        for raw in ("SECONDARY-WING", "SECONDARY WING", "SECONDARY- WING", " secondary  wing "):
            with self.subTest(raw=raw):
                self.assertEqual(dd.clean_locality(raw), "SECONDARY-WING")

    def test_names_that_merely_contain_a_tag_word_are_kept(self):
        self.assertEqual(dd.clean_locality("FEMALE-21"), "FEMALE-21")
        self.assertEqual(dd.clean_locality("BURJWALA-B"), "BURJWALA-B")
        self.assertEqual(dd.clean_locality("KHAMBA MALE MOHALLA"), "KHAMBA MALE MOHALLA")


class BuildDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = [
            _row("ALPHA", "T1", "SATLUJ - MALE", "M-EE", "30000001", "GBHS A"),
            _row("ALPHA", "T1", "SATLUJ - FEMALE", "W-EE", "30000002", "GGHS B"),
            _row("ALPHA", "T1", "SECONDARY WING", "SE", "30000003", "GHS C"),
            _row("BETA", "T2", "CHAK 5 - MALE", "M-EE", "30000004", "GBHS D"),
            _row("BETA", "T2", "X", "W-EE", "30000005", "GGHS E"),
            _row("BETA", "T2", "Y", "SE", "30000006", "GHS F"),
            _row("BETA", "T2", "Z", "SE", "30000007", "GHS G"),
            _row("ALPHA", "T1", "SATLUJ - MALE", "M-EE", "30000001", "DUPLICATE"),
        ]
        ids = [
            _id("30000001", district="1", tehsil="11", markaz="111", school="1001"),
            _id("30000002", district="1", tehsil="11", markaz="111", school="1002"),
            _id("30000003", district="1", tehsil="11", markaz="112", school="1003"),
            _id("30000004", district="2", tehsil="21", markaz="221", school="1004"),
            _id("30000005", status="missing"),
            _id("30000006", status="error"),
            _id("30000007", district="2", tehsil="21", markaz="222", school="1007"),
        ]
        coords = [
            _coord("1001", lat=30.1, lon=71.1),
            _coord("1002", lat=30.1, lon=71.1),  # shares a point with 1001
            _coord("1003", status="missing"),
            _coord("1004", lat=36.07, lon=129.38, out_of_range=True),
            # 1007 has no coordinate record yet (phase 2 not run)
        ]
        self.base_p = os.path.join(self.tmp.name, "Base Schools.json")
        self.ids_p = os.path.join(self.tmp.name, "school_ids.jsonl")
        self.coords_p = os.path.join(self.tmp.name, "school_coords.jsonl")
        with open(self.base_p, "w", encoding="utf-8") as fh:
            json.dump(base, fh)
        for path, recs in ((self.ids_p, ids), (self.coords_p, coords)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in recs)
        self.payload = dd.build_dataset(self.base_p, self.ids_p, self.coords_p)
        self.cols = {name: i for i, name in enumerate(self.payload["columns"])}
        self.by_emis = {r[self.cols["emis"]]: r for r in self.payload["rows"]}

    def status_of(self, emis):
        return self.payload["statuses"][self.by_emis[emis][self.cols["status"]]]["key"]

    def test_counts_and_duplicates(self):
        c = self.payload["counts"]
        self.assertEqual(c["base_rows"], 8)
        self.assertEqual(c["schools"], 7)
        self.assertEqual(c["duplicate_rows"], 1)
        self.assertEqual(c["mapped"], 2)
        self.assertEqual(c["by_status"]["outside"], 1)
        self.assertEqual(c["by_status"]["no_coords"], 1)
        self.assertEqual(c["by_status"]["not_in_sis"], 1)
        self.assertEqual(c["by_status"]["lookup_error"], 1)
        self.assertEqual(c["by_status"]["pending"], 1)
        self.assertEqual(c["by_wing"], {"SE": 3, "W-EE": 2, "M-EE": 2})

    def test_status_mapping(self):
        self.assertEqual(self.status_of("30000001"), "mapped")
        self.assertEqual(self.status_of("30000002"), "mapped")
        self.assertEqual(self.status_of("30000003"), "no_coords")
        self.assertEqual(self.status_of("30000004"), "outside")
        self.assertEqual(self.status_of("30000005"), "not_in_sis")
        self.assertEqual(self.status_of("30000006"), "lookup_error")
        self.assertEqual(self.status_of("30000007"), "pending")

    def test_coordinates_only_for_located_schools(self):
        self.assertEqual(self.by_emis["30000001"][self.cols["lat"]], 30.1)
        self.assertEqual(self.by_emis["30000001"][self.cols["lon"]], 71.1)
        self.assertIsNone(self.by_emis["30000003"][self.cols["lat"]])
        self.assertIsNone(self.by_emis["30000005"][self.cols["lon"]])
        # Outside-Punjab points keep their coordinates for the table and issue list.
        self.assertEqual(self.by_emis["30000004"][self.cols["lat"]], 36.07)

    def test_unresolved_school_is_placed_by_base_file_names(self):
        row = self.by_emis["30000005"]  # BETA / T2, not in SIS
        beta = next(i for i, d in enumerate(self.payload["districts"]) if d["name"] == "BETA")
        self.assertEqual(row[self.cols["district"]], beta)
        tehsil = self.payload["tehsils"][row[self.cols["tehsil"]]]
        self.assertEqual(tehsil["id"], "21")  # matched to the SIS tehsil with the same name
        self.assertIsNone(row[self.cols["markaz"]])
        self.assertIsNone(row[self.cols["school_id"]])

    def test_markaz_labels_use_most_common_locality_then_id(self):
        labels = {m["id"]: m["label"] for m in self.payload["markaz"]}
        self.assertEqual(labels["111"], "SATLUJ (111)")  # two schools: MALE and FEMALE tags stripped
        self.assertEqual(labels["112"], "SECONDARY-WING (112)")  # secondary-only markaz
        self.assertEqual(labels["221"], "CHAK 5 (221)")
        self.assertEqual(labels["222"], "Z (222)")

    def test_hierarchy_tables_are_consistent(self):
        p = self.payload
        self.assertEqual(len(p["districts"]), 2)
        self.assertEqual(len(p["tehsils"]), 2)
        for t in p["tehsils"]:
            self.assertTrue(0 <= t["district"] < len(p["districts"]))
        for m in p["markaz"]:
            self.assertTrue(0 <= m["tehsil"] < len(p["tehsils"]))
        for row in p["rows"]:
            self.assertEqual(len(row), len(p["columns"]))

    def test_wing_labels(self):
        labels = {w["code"]: w["label"] for w in self.payload["wings"]}
        self.assertEqual(labels["SE"], "Secondary Education (SE)")
        self.assertEqual(labels["W-EE"], "Women's Elementary Education (W-EE)")
        self.assertEqual(labels["M-EE"], "Male Elementary Education (M-EE)")

    def test_unlabeled_numbers_are_kept(self):
        row = self.by_emis["30000001"]
        self.assertEqual([row[self.cols["n6"]], row[self.cols["n7"]], row[self.cols["n8"]]],
                         [10, 12, 2])

    def test_missing_input_is_reported_clearly(self):
        with self.assertRaises(SystemExit) as ctx:
            dd.build_dataset(self.base_p, os.path.join(self.tmp.name, "nope.jsonl"), self.coords_p)
        self.assertIn("Missing", str(ctx.exception))

    def test_short_rows_are_rejected(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump([["ALPHA", "T1", "x"]], fh)
        with self.assertRaises(SystemExit):
            dd.build_dataset(bad, self.ids_p, self.coords_p)

    def test_cli_writes_site_and_copies_static_files(self):
        site = os.path.join(self.tmp.name, "site")
        rc = dd.main(["-b", self.base_p, "--ids", self.ids_p, "--coords", self.coords_p,
                      "--site", site])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(site, "data", "schools.json")))
        self.assertTrue(os.path.exists(os.path.join(site, "index.html")))
        self.assertTrue(os.path.isdir(os.path.join(site, "js")))


@unittest.skipUnless(
    os.path.exists(os.path.join(ROOT, "data", "Base Schools.json"))
    and os.path.exists(os.path.join(ROOT, "data", "school_ids.jsonl"))
    and os.path.exists(os.path.join(ROOT, "data", "school_coords.jsonl")),
    "committed pipeline data not present",
)
class RealDataInvariantTests(unittest.TestCase):
    """Checks properties that hold for any refresh of the real data (not exact counts)."""

    @classmethod
    def setUpClass(cls):
        cls.p = dd.build_dataset(
            os.path.join(ROOT, "data", "Base Schools.json"),
            os.path.join(ROOT, "data", "school_ids.jsonl"),
            os.path.join(ROOT, "data", "school_coords.jsonl"),
        )

    def test_every_school_appears_once_and_indices_are_valid(self):
        p = self.p
        emis = [r[0] for r in p["rows"]]
        self.assertEqual(len(emis), len(set(emis)))
        self.assertEqual(len(emis), p["counts"]["schools"])
        for r in p["rows"]:
            self.assertTrue(0 <= r[2] < len(p["wings"]))
            self.assertTrue(0 <= r[3] < len(p["districts"]))
            self.assertTrue(0 <= r[4] < len(p["tehsils"]))
            self.assertTrue(r[5] is None or 0 <= r[5] < len(p["markaz"]))
            self.assertTrue(0 <= r[9] < len(p["statuses"]))

    def test_mapped_rows_have_coordinates_and_others_match_status(self):
        p = self.p
        for r in p["rows"]:
            mapped = p["statuses"][r[9]]["mapped"]
            if mapped:
                self.assertIsNotNone(r[7])
                self.assertIsNotNone(r[8])
        self.assertEqual(sum(p["counts"]["by_status"].values()), p["counts"]["schools"])
        self.assertEqual(sum(p["counts"]["by_wing"].values()), p["counts"]["schools"])

    def test_every_markaz_has_a_label_with_its_id(self):
        for m in self.p["markaz"]:
            self.assertTrue(m["label"].endswith(f"({m['id']})"), m)


if __name__ == "__main__":
    unittest.main()
