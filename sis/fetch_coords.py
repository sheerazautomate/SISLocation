"""Phase 2 - extract coordinates from the Google Maps redirect for each school ID.

Endpoint:
    GET /transfer/show_google_map_school/<school_id>
    -> 302 Location: https://www.google.com/maps?saddr=30.84132237,71.2246089

Some responses redirect via meta-refresh or `window.location` instead of a 302,
and schools with no stored location yield a blank/zero coordinate - all handled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from .common import (
    BASE_URL,
    JsonlStore,
    Progress,
    add_shard_args,
    fetch,
    iter_records,
    run_pool,
    select_shard,
)

ENDPOINT = BASE_URL + "/transfer/show_google_map_school/{sid}"

# -73.9, 40.7 style pair inside a maps URL parameter
_COORD = r"(-?\d{1,3}\.\d+|-?\d{1,3})\s*,\s*(-?\d{1,3}\.\d+|-?\d{1,3})"
COORD_PARAM_RE = re.compile(r"[?&](?:saddr|daddr|q|ll|center|destination)=" + _COORD, re.I)
COORD_AT_RE = re.compile(r"/@" + _COORD)
META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*url=([^"'>\s]+)""",
    re.I,
)
JS_LOCATION_RE = re.compile(
    r"""(?:window\.)?(?:location(?:\.href)?|location\.replace\s*\()\s*=?\s*["']([^"']+)["']""",
    re.I,
)
ANY_MAPS_URL_RE = re.compile(
    r"""https?://(?:www\.)?google\.[^\s"'<>]*maps[^\s"'<>]*""", re.I
)

# Punjab bounding box - generous, just to flag obviously bogus rows.
LAT_RANGE = (27.0, 34.5)
LON_RANGE = (68.5, 76.0)


def parse_coords(text: str) -> tuple[float, float] | None:
    for pattern in (COORD_PARAM_RE, COORD_AT_RE):
        m = pattern.search(text)
        if m:
            try:
                return float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
    return None


def _redirect_target(resp) -> str | None:
    """Find where the page wants to send us: header, meta refresh, or JS."""
    loc = resp.headers.get("Location")
    if loc:
        return loc
    body = resp.text or ""
    for pattern in (META_REFRESH_RE, JS_LOCATION_RE, ANY_MAPS_URL_RE):
        m = pattern.search(body)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return None


def resolve_coords(
    row: dict[str, Any], *, timeout: float, retries: int, insecure: bool
) -> dict[str, Any]:
    sid = str(row["school_id"])
    rec: dict[str, Any] = {
        "school_id": sid,
        "emis_code": row.get("emis_code"),
        "district_id": row.get("district_id"),
        "tehsil_id": row.get("tehsil_id"),
        "markaz_id": row.get("markaz_id"),
    }
    url = ENDPOINT.format(sid=sid)
    try:
        # Don't follow into Google - the 302 Location already carries the data.
        resp = fetch(
            url,
            timeout=timeout,
            retries=retries,
            insecure=insecure,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec

    if resp.status_code >= 500:
        rec["status"] = "error"
        rec["error"] = f"HTTP {resp.status_code}"
        return rec

    target = _redirect_target(resp)
    source = target or (resp.text or "")
    coords = parse_coords(source)

    if coords is None:
        rec["status"] = "missing"
        rec["map_url"] = target
        if not target:
            rec["error"] = f"no redirect (HTTP {resp.status_code})"
        return rec

    lat, lon = coords
    rec["latitude"] = lat
    rec["longitude"] = lon
    rec["map_url"] = target

    if lat == 0 and lon == 0:
        rec["status"] = "missing"
        rec["error"] = "zero coordinates"
    elif not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        rec["status"] = "ok"
        rec["out_of_range"] = True
    else:
        rec["status"] = "ok"
    return rec


def load_id_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in iter_records(path):
        if not isinstance(rec, dict):
            continue
        sid = rec.get("school_id") or rec.get("s_id")
        if not sid or str(sid) in seen:
            continue
        if rec.get("status") not in (None, "ok"):
            continue
        seen.add(str(sid))
        rows.append(
            {
                "school_id": str(sid),
                "emis_code": rec.get("emis_code") or rec.get("s_emis_code"),
                "district_id": rec.get("district_id") or rec.get("s_district_idFk"),
                "tehsil_id": rec.get("tehsil_id") or rec.get("s_tehsil_idFk"),
                "markaz_id": rec.get("markaz_id") or rec.get("s_markaz_idFk"),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_coords",
        description="Extract lat/lon from the SIS Google Maps redirect per school ID.",
    )
    ap.add_argument("-i", "--input", default="data/school_ids.jsonl",
                    help="Phase-1 output (or any file with school_id fields)")
    ap.add_argument("-o", "--output", default="data/school_coords.jsonl")
    ap.add_argument("-w", "--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--retry-missing", action="store_true",
                    help="Also re-attempt rows with no coordinates")
    ap.add_argument("--insecure", action="store_true")
    add_shard_args(ap)
    args = ap.parse_args(argv)

    if args.insecure:
        import urllib3

        urllib3.disable_warnings()

    if not os.path.exists(args.input):
        sys.stderr.write(
            f"Input not found: {args.input}\nRun phase 1 (fetch_ids) first.\n"
        )
        return 2

    rows = load_id_rows(args.input)
    sys.stderr.write(f"Loaded {len(rows)} school IDs\n")

    if args.shards > 1:
        rows = select_shard(rows, args.shard, args.shards)
        sys.stderr.write(
            f"Shard {args.shard + 1}/{args.shards}: {len(rows)} schools in this slice\n"
        )

    store = JsonlStore(args.output, key="school_id")
    done = store.load_done()
    skip = {"error"} if args.retry_errors else set()
    if args.retry_missing:
        skip.add("missing")
    if skip:
        done = {k: v for k, v in done.items() if v.get("status") not in skip}
    pending = [r for r in rows if r["school_id"] not in done]
    if args.limit:
        pending = pending[: args.limit]

    sys.stderr.write(f"Already done: {len(done)} | pending this run: {len(pending)}\n")
    if not pending:
        sys.stderr.write("Nothing to do.\n")
        return 0

    progress = Progress(len(pending), "Coords", already=len(done))
    with store:
        run_pool(
            pending,
            lambda r: resolve_coords(
                r, timeout=args.timeout, retries=args.retries, insecure=args.insecure
            ),
            store=store,
            progress=progress,
            workers=args.workers,
            delay=args.delay,
        )
    progress.finish()
    sys.stderr.write(f"Wrote {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
