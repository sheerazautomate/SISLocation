"""Combine sharded JSONL outputs from parallel CI runners into one file.

Later records win on duplicate keys, so a re-run shard supersedes an older one,
and successful records always beat earlier errors for the same key.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any

_RANK = {"ok": 3, "missing": 2, "error": 1}


def combine(patterns: list[str], key: str, out_path: str) -> tuple[int, dict[str, int]]:
    best: dict[str, dict[str, Any]] = {}
    files: list[str] = []
    for pat in patterns:
        files.extend(sorted(glob.glob(pat, recursive=True)))

    for path in files:
        if not os.path.isfile(path) or os.path.abspath(path) == os.path.abspath(out_path):
            continue
        with open(path, "rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                k = rec.get(key)
                if k is None:
                    continue
                k = str(k)
                prev = best.get(k)
                if prev is None or _RANK.get(rec.get("status"), 0) >= _RANK.get(
                    prev.get("status"), 0
                ):
                    best[k] = rec

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for rec in best.values():
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for rec in best.values():
        st = str(rec.get("status"))
        counts[st] = counts.get(st, 0) + 1
    return len(files), counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="combine", description="Merge sharded JSONL parts into one deduped file."
    )
    ap.add_argument("patterns", nargs="+", help="Glob(s), e.g. 'parts/ids-*.jsonl'")
    ap.add_argument("-k", "--key", default="emis_code",
                    help="Dedup key: emis_code (ids) or school_id (coords)")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args(argv)

    n_files, counts = combine(args.patterns, args.key, args.output)
    total = sum(counts.values())
    sys.stderr.write(f"Combined {n_files} file(s) -> {args.output}: {total} unique {args.key}\n")
    for st, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        sys.stderr.write(f"  {st:<10} {n:>7}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
