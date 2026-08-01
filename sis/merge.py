"""Merge base schools + phase-1 IDs + phase-2 coordinates into final JSON/CSV/GeoJSON."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any

from .common import detect_emis_field, iter_records

OUT_FIELDS = [
    "emis_code",
    "school_id",
    "district_id",
    "tehsil_id",
    "markaz_id",
    "latitude",
    "longitude",
    "status",
]


def _index(path: str, key: str) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    if not path or not os.path.exists(path):
        return idx
    for rec in iter_records(path):
        if isinstance(rec, dict) and rec.get(key) is not None:
            idx[str(rec[key])] = rec  # later lines win (retries supersede)
    return idx


def build_rows(base: str | None, ids_path: str, coords_path: str,
               emis_field: str | None = None) -> list[dict[str, Any]]:
    ids = _index(ids_path, "emis_code")
    coords = _index(coords_path, "school_id")

    rows: list[dict[str, Any]] = []
    if base and os.path.exists(base):
        records = list(iter_records(base))
        is_dict_records = bool(records) and isinstance(records[0], dict)
        is_list_records = bool(records) and isinstance(records[0], (list, tuple))
        field = emis_field or (
            detect_emis_field(records) if (is_dict_records or is_list_records) else None
        )
        idx: int | None = None
        if is_list_records and field is not None:
            try:
                idx = int(field)
            except ValueError:
                idx = None  # user passed a dict-style field name for list records
        for rec in records:
            if isinstance(rec, dict):
                emis = str(rec.get(field, "")).strip().split(".")[0] if field else ""
                extra = {k: v for k, v in rec.items() if k != field}
            elif isinstance(rec, (list, tuple)) and idx is not None:
                emis = str(rec[idx]).strip().split(".")[0] if len(rec) > idx else ""
                extra = {f"col_{i}": v for i, v in enumerate(rec) if i != idx}
            else:
                emis, extra = str(rec).strip(), {}
            if not emis:
                continue
            rows.append(_combine(emis, ids.get(emis), coords, extra))
    else:
        for emis, rec in ids.items():
            rows.append(_combine(emis, rec, coords, {}))
    return rows


def _combine(emis: str, id_rec: dict[str, Any] | None,
             coords: dict[str, dict[str, Any]], extra: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "emis_code": emis,
        "school_id": None,
        "district_id": None,
        "tehsil_id": None,
        "markaz_id": None,
        "latitude": None,
        "longitude": None,
        "status": "id_pending",
    }
    if id_rec:
        row.update(
            school_id=id_rec.get("school_id"),
            district_id=id_rec.get("district_id"),
            tehsil_id=id_rec.get("tehsil_id"),
            markaz_id=id_rec.get("markaz_id"),
        )
        st = id_rec.get("status")
        row["status"] = {
            "ok": "coords_pending",
            "missing": "id_missing",
            "error": "id_error",
        }.get(st, "id_pending")

    sid = row.get("school_id")
    if sid is not None:
        crec = coords.get(str(sid))
        if crec:
            row["latitude"] = crec.get("latitude")
            row["longitude"] = crec.get("longitude")
            row["status"] = {
                "ok": "ok",
                "missing": "coords_missing",
                "error": "coords_error",
            }.get(crec.get("status"), row["status"])
            if crec.get("out_of_range"):
                row["out_of_range"] = True

    for k, v in extra.items():
        row.setdefault(k, v)
    return row


def write_outputs(rows: list[dict[str, Any]], out_prefix: str, geojson: bool = True) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_prefix)) or ".", exist_ok=True)

    with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)

    extra_keys = [k for k in dict.fromkeys(
        k for r in rows for k in r if k not in OUT_FIELDS
    )]
    header = OUT_FIELDS + extra_keys
    with open(out_prefix + ".csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    if geojson:
        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["longitude"], r["latitude"]],
                },
                "properties": {k: v for k, v in r.items()
                               if k not in ("latitude", "longitude")},
            }
            for r in rows
            if isinstance(r.get("latitude"), (int, float))
            and isinstance(r.get("longitude"), (int, float))
        ]
        with open(out_prefix + ".geojson", "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": features}, fh,
                      ensure_ascii=False)


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="merge", description="Merge phases into final datasets.")
    ap.add_argument("-b", "--base", default="data/Base Schools.json")
    ap.add_argument("--ids", default="data/school_ids.jsonl")
    ap.add_argument("--coords", default="data/school_coords.jsonl")
    ap.add_argument("-o", "--out-prefix", default="data/schools_final")
    ap.add_argument("--emis-field", default=None)
    ap.add_argument("--no-geojson", action="store_true")
    args = ap.parse_args(argv)

    rows = build_rows(args.base, args.ids, args.coords, args.emis_field)
    if not rows:
        sys.stderr.write("No rows to merge - run the fetch phases first.\n")
        return 2
    write_outputs(rows, args.out_prefix, geojson=not args.no_geojson)

    total = len(rows)
    sys.stderr.write(f"\nMerged {total} schools -> {args.out_prefix}.{{json,csv,geojson}}\n")
    for status, n in summarize(rows).items():
        sys.stderr.write(f"  {status:<16} {n:>7}  ({n / total:.1%})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
