#!/usr/bin/env python3
"""Single entry point for the SIS location pipeline.

    python run.py ids     -i "data/Base Schools.json" -o data/school_ids.jsonl
    python run.py coords  -i data/school_ids.jsonl    -o data/school_coords.jsonl
    python run.py merge   -b "data/Base Schools.json" -o data/schools_final
    python run.py all     -b "data/Base Schools.json"
    python run.py dashboard [--serve]     # build dashboard/data/schools.json
    python run.py test
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

USAGE = __doc__


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "ids":
        from sis.fetch_ids import main as run

        return run(rest)
    if cmd == "coords":
        from sis.fetch_coords import main as run

        return run(rest)
    if cmd == "merge":
        from sis.merge import main as run

        return run(rest)
    if cmd == "dashboard":
        from sis.dashboard_data import main as run

        return run(rest)
    if cmd == "test":
        import unittest

        loader = unittest.TestLoader()
        suite = loader.discover("tests")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    if cmd == "all":
        import argparse

        ap = argparse.ArgumentParser(prog="run.py all")
        ap.add_argument("-b", "--base", default="data/Base Schools.json")
        ap.add_argument("-w", "--workers", type=int, default=8)
        ap.add_argument("--delay", type=float, default=0.0)
        ap.add_argument("--emis-field", default=None)
        ap.add_argument("--insecure", action="store_true")
        ap.add_argument("--out-dir", default="data")
        args = ap.parse_args(rest)

        ids_path = os.path.join(args.out_dir, "school_ids.jsonl")
        coords_path = os.path.join(args.out_dir, "school_coords.jsonl")
        prefix = os.path.join(args.out_dir, "schools_final")
        common = ["-w", str(args.workers), "--delay", str(args.delay)]
        if args.insecure:
            common.append("--insecure")

        from sis.fetch_coords import main as coords_main
        from sis.fetch_ids import main as ids_main
        from sis.merge import main as merge_main

        a = ["-i", args.base, "-o", ids_path] + common
        if args.emis_field:
            a += ["--emis-field", args.emis_field]
        rc = ids_main(a)
        if rc:
            return rc
        rc = coords_main(["-i", ids_path, "-o", coords_path] + common)
        if rc:
            return rc
        m = ["-b", args.base, "--ids", ids_path, "--coords", coords_path, "-o", prefix]
        if args.emis_field:
            m += ["--emis-field", args.emis_field]
        return merge_main(m)

    print(f"Unknown command: {cmd}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
