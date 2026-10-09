"""Build the school dashboard dataset, and optionally serve the dashboard.

Joins the committed pipeline outputs into one compact JSON file that the static
dashboard (``dashboard/``) loads in the browser:

    data/Base Schools.json     one row per school, 9 columns, no header
    data/school_ids.jsonl      phase 1: SIS district / tehsil / markaz / school IDs
    data/school_coords.jsonl   phase 2: latitude / longitude per SIS school ID

Base-file columns (0-based):

    0 district   1 tehsil   2 locality text (markaz name)   3 wing code
    4 EMIS code  5 school name   6-8 unlabeled numbers

The three unlabeled numbers are kept in the JSON but not shown in the UI.
Column 8 equals column 7 minus column 6 in the current data.

    python run.py dashboard                  # writes dashboard/data/schools.json
    python run.py dashboard --serve          # ...and serves dashboard/ on 0.0.0.0:8000
    python run.py dashboard --site _site     # complete static site (CI / GitHub Pages)
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from typing import Any

from .common import iter_records

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(PACKAGE_ROOT, "dashboard")
STATIC_ENTRIES = ("index.html", "css", "js", "vendor")

# Row layout of "rows" in schools.json. The JS reads it by name via "columns".
COLUMNS = [
    "emis", "name", "wing", "district", "tehsil", "markaz",
    "school_id", "lat", "lon", "status", "n6", "n7", "n8",
]

WING_LABELS = {
    "SE": "Secondary Education (SE)",
    "W-EE": "Women's Elementary Education (W-EE)",
    "M-EE": "Male Elementary Education (M-EE)",
}
WING_ORDER = list(WING_LABELS)

STATUSES = [
    {"key": "mapped", "label": "Mapped", "mapped": True},
    {"key": "outside", "label": "Outside Punjab", "mapped": False},
    {"key": "no_coords", "label": "No coordinates", "mapped": False},
    {"key": "coord_error", "label": "Coordinate error", "mapped": False},
    {"key": "not_in_sis", "label": "Not in SIS", "mapped": False},
    {"key": "lookup_error", "label": "Lookup error", "mapped": False},
    {"key": "pending", "label": "Pending lookup", "mapped": False},
]
STATUS_INDEX = {s["key"]: i for i, s in enumerate(STATUSES)}

SECONDARY_NAME = "SECONDARY-WING"
_SECONDARY_RE = re.compile(r"SECONDARY[\s-]*WING")
# A trailing gender tag, with any separator before it: " - MALE", "-FEMALE", " (MALE)".
_GENDER_TAG_RE = re.compile(r"[\s\-(]*\b(?:MALE|FEMALE)\)?\s*$")


def clean_locality(text: Any) -> str:
    """Turn the locality column into a markaz name.

    'SATLUJ - MALE' -> 'SATLUJ'
    'SHAHKOT CITY 2-FEMALE' -> 'SHAHKOT CITY 2'
    'JURA KALAN (WEST) (MALE)' -> 'JURA KALAN (WEST)'
    'SECONDARY WING' / 'SECONDARY- WING' -> 'SECONDARY-WING'
    '   MALE' -> ''   (a gender tag with no name)
    """
    s = _squash(text).upper()
    if _SECONDARY_RE.fullmatch(s):
        return SECONDARY_NAME
    while True:  # some values carry two tags, e.g. 'JATLI MALE -MALE'
        stripped = _GENDER_TAG_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    return _squash(s).strip(" -")


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _most_common(counter: Counter) -> str:
    """Most frequent key; ties go to the alphabetically first key, so output is stable."""
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _load_base(path: str) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for rec in iter_records(path):
        if not isinstance(rec, (list, tuple)) or len(rec) < 9:
            raise SystemExit(
                f"{path}: every row must be the 9-column base layout (district, tehsil, "
                "locality, wing, EMIS, school name, 3 numbers)."
            )
        rows.append(list(rec))
    if not rows:
        raise SystemExit(f"{path}: no rows found.")
    return rows


def _index(path: str, key: str) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        raise SystemExit(f"Missing {path}. Run the pipeline first (python run.py ids / coords).")
    out: dict[str, dict[str, Any]] = {}
    for rec in iter_records(path):
        if isinstance(rec, dict) and rec.get(key) is not None:
            out[str(rec[key])] = rec  # later lines win, as in merge.py
    return out


def _resolved(rec: dict[str, Any] | None) -> bool:
    return bool(
        rec
        and rec.get("status") == "ok"
        and rec.get("school_id") not in (None, "")
        and rec.get("district_id") not in (None, "")
        and rec.get("tehsil_id") not in (None, "")
    )


def _status_key(id_rec: dict[str, Any] | None, coord_rec: dict[str, Any] | None) -> str:
    if id_rec is None:
        return "pending"
    st = id_rec.get("status")
    if st == "missing":
        return "not_in_sis"
    if st == "error":
        return "lookup_error"
    if st != "ok" or not _resolved(id_rec):
        return "pending"
    if coord_rec is None:
        return "pending"
    cst = coord_rec.get("status")
    if cst == "ok":
        if not (_num(coord_rec.get("latitude")) and _num(coord_rec.get("longitude"))):
            return "coord_error"
        return "outside" if coord_rec.get("out_of_range") else "mapped"
    if cst == "missing":
        return "no_coords"
    if cst == "error":
        return "coord_error"
    return "pending"


def _markaz_labels(schools: list[dict[str, Any]]) -> dict[str, str]:
    """One label per SIS markaz ID: its most common locality name, plus the ID.

    Secondary-only markaz have no locality name, so they read SECONDARY-WING. The ID
    always follows the name, so the labels stay distinct in a dropdown.
    """
    names: dict[str, Counter] = defaultdict(Counter)
    secondary: set[str] = set()
    markaz_ids: set[str] = set()
    for s in schools:
        mk = s["markaz_key"]
        if not mk:
            continue
        markaz_ids.add(mk)
        name = clean_locality(s["locality"])
        if name == SECONDARY_NAME:
            secondary.add(mk)
        elif name:
            names[mk][name] += 1

    labels: dict[str, str] = {}
    for mk in markaz_ids:
        if names.get(mk):
            name: str | None = _most_common(names[mk])
        elif mk in secondary:
            name = SECONDARY_NAME
        else:
            name = None
        labels[mk] = f"{name} ({mk})" if name else f"Markaz {mk}"
    return labels


def build_dataset(base_path: str, ids_path: str, coords_path: str) -> dict[str, Any]:
    """Return the dashboard payload (see module docstring for the layout)."""
    base = _load_base(base_path)
    ids = _index(ids_path, "emis_code")
    coords = _index(coords_path, "school_id")

    # Pass 1: one entry per unique EMIS code, in base-file order (first occurrence wins).
    schools: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for rec in base:
        emis = str(rec[4]).strip().split(".")[0]
        if not emis:
            continue
        if emis in seen:
            duplicates += 1
            continue
        seen.add(emis)
        schools.append({
            "emis": emis,
            "name": _squash(rec[5]),
            "wing": _squash(rec[3]).upper(),
            "district_name": _squash(rec[0]).upper(),
            "tehsil_name": _squash(rec[1]).upper(),
            "locality": rec[2],
            "numbers": [_int_or_none(rec[6]), _int_or_none(rec[7]), _int_or_none(rec[8])],
            "id_rec": ids.get(emis),
        })

    # Pass 2: resolve hierarchy keys. SIS IDs are authoritative; schools missing from
    # SIS fall back to the base-file names, matched against the resolved schools.
    dist_by_name: dict[str, str] = {}
    tehsil_by_name: dict[tuple[str, str], str] = {}
    for s in schools:
        if _resolved(s["id_rec"]):
            dist_by_name.setdefault(s["district_name"], str(s["id_rec"]["district_id"]))
            tehsil_by_name.setdefault(
                (s["district_name"], s["tehsil_name"]), str(s["id_rec"]["tehsil_id"])
            )
    for s in schools:
        rec = s["id_rec"]
        if _resolved(rec):
            s["district_key"] = str(rec["district_id"])
            s["tehsil_key"] = str(rec["tehsil_id"])
            markaz = rec.get("markaz_id")
            s["markaz_key"] = str(markaz) if markaz not in (None, "") else None
            s["school_id"] = str(rec["school_id"])
        else:
            s["district_key"] = dist_by_name.get(s["district_name"], "name:" + s["district_name"])
            s["tehsil_key"] = tehsil_by_name.get(
                (s["district_name"], s["tehsil_name"]),
                f"name:{s['district_name']}/{s['tehsil_name']}",
            )
            s["markaz_key"] = None
            s["school_id"] = None

        coord = coords.get(s["school_id"]) if s["school_id"] else None
        s["status"] = _status_key(rec, coord)
        if s["status"] in ("mapped", "outside"):
            s["lat"] = round(float(coord["latitude"]), 6)
            s["lon"] = round(float(coord["longitude"]), 6)
        else:
            s["lat"] = s["lon"] = None

    # Labels: most common spelling per key. Parents are the most common parent per child.
    dist_names: dict[str, Counter] = defaultdict(Counter)
    teh_names: dict[str, Counter] = defaultdict(Counter)
    teh_parent: dict[str, Counter] = defaultdict(Counter)
    mk_parent: dict[str, Counter] = defaultdict(Counter)
    for s in schools:
        dist_names[s["district_key"]][s["district_name"]] += 1
        teh_names[s["tehsil_key"]][s["tehsil_name"]] += 1
        teh_parent[s["tehsil_key"]][s["district_key"]] += 1
        if s["markaz_key"]:
            mk_parent[s["markaz_key"]][s["tehsil_key"]] += 1

    district_keys = sorted(dist_names, key=lambda k: _most_common(dist_names[k]))
    district_index = {k: i for i, k in enumerate(district_keys)}
    districts = [{"id": k, "name": _most_common(dist_names[k])} for k in district_keys]

    tehsil_keys = sorted(
        teh_names,
        key=lambda k: (
            _most_common(dist_names[_most_common(teh_parent[k])]),
            _most_common(teh_names[k]),
        ),
    )
    tehsil_index = {k: i for i, k in enumerate(tehsil_keys)}
    tehsils = [
        {
            "id": k,
            "name": _most_common(teh_names[k]),
            "district": district_index[_most_common(teh_parent[k])],
        }
        for k in tehsil_keys
    ]

    labels = _markaz_labels(schools)
    markaz_keys = sorted(labels, key=lambda k: labels[k].lower())
    markaz_index = {k: i for i, k in enumerate(markaz_keys)}
    markaz = [
        {"id": k, "label": labels[k], "tehsil": tehsil_index[_most_common(mk_parent[k])]}
        for k in markaz_keys
    ]

    wing_codes = sorted(
        {s["wing"] for s in schools},
        key=lambda c: (WING_ORDER.index(c) if c in WING_ORDER else len(WING_ORDER), c),
    )
    wing_index = {c: i for i, c in enumerate(wing_codes)}
    wings = [{"code": c, "label": WING_LABELS.get(c, c)} for c in wing_codes]

    rows: list[list[Any]] = []
    for s in schools:
        rows.append([
            s["emis"],
            s["name"],
            wing_index[s["wing"]],
            district_index[s["district_key"]],
            tehsil_index[s["tehsil_key"]],
            markaz_index.get(s["markaz_key"]) if s["markaz_key"] else None,
            s["school_id"],
            s["lat"],
            s["lon"],
            STATUS_INDEX[s["status"]],
            *s["numbers"],
        ])

    by_status = Counter(s["status"] for s in schools)
    by_wing = Counter(s["wing"] for s in schools)
    return {
        "version": 1,
        "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        "sources": {
            "base": os.path.basename(base_path),
            "ids": os.path.basename(ids_path),
            "coords": os.path.basename(coords_path),
        },
        "counts": {
            "base_rows": len(base),
            "schools": len(schools),
            "duplicate_rows": duplicates,
            "mapped": by_status.get("mapped", 0),
            "by_status": {s["key"]: by_status.get(s["key"], 0) for s in STATUSES},
            "by_wing": dict(by_wing),
            "districts": len(districts),
            "tehsils": len(tehsils),
            "markaz": len(markaz),
        },
        "columns": COLUMNS,
        "statuses": STATUSES,
        "wings": wings,
        "districts": districts,
        "tehsils": tehsils,
        "markaz": markaz,
        "rows": rows,
    }


def serve(directory: str, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    sys.stderr.write(f"Serving {directory} on http://0.0.0.0:{port}  (Ctrl+C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def write_site(site: str, payload: dict[str, Any]) -> str:
    """Write data/schools.json under ``site``, copying static files in when needed."""
    if os.path.abspath(site) != SOURCE_DIR:
        os.makedirs(site, exist_ok=True)
        for entry in STATIC_ENTRIES:
            src = os.path.join(SOURCE_DIR, entry)
            dst = os.path.join(site, entry)
            if not os.path.exists(src):
                raise SystemExit(f"Dashboard file missing: {src} (is the dashboard/ folder complete?)")
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    out = os.path.join(site, "data", "schools.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dashboard",
        description="Build the school dashboard data and optionally serve the dashboard.",
    )
    ap.add_argument("-b", "--base", default="data/Base Schools.json")
    ap.add_argument("--ids", default="data/school_ids.jsonl")
    ap.add_argument("--coords", default="data/school_coords.jsonl")
    ap.add_argument(
        "--site", default=SOURCE_DIR,
        help="Folder to write the site into (default: dashboard/). Static files are "
             "copied there when it differs from dashboard/.",
    )
    ap.add_argument("--serve", action="store_true",
                    help="Serve the site on 0.0.0.0 after building")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    payload = build_dataset(args.base, args.ids, args.coords)
    site = os.path.abspath(args.site)
    out = write_site(site, payload)

    c = payload["counts"]
    not_mapped = c["schools"] - c["mapped"]
    sys.stderr.write(
        f"Dashboard data: {c['schools']} schools ({c['mapped']} mapped, {not_mapped} not mapped) "
        f"-> {out}\n"
    )
    if args.serve:
        serve(site, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
