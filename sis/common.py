"""Shared helpers: HTTP session, JSONL checkpointing, concurrent runner."""

from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Iterator

import requests
from requests.adapters import HTTPAdapter

BASE_URL = os.environ.get("SIS_BASE_URL", "https://sis.pesrp.edu.pk").rstrip("/")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_thread_local = threading.local()


def get_session(insecure: bool = False, pool: int = 32) -> requests.Session:
    """One pooled Session per worker thread (Sessions are not thread-safe)."""
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        if insecure:
            sess.verify = False
        _thread_local.session = sess
    return sess


def fetch(
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 4,
    backoff: float = 1.5,
    insecure: bool = False,
    allow_redirects: bool = True,
) -> requests.Response:
    """GET with exponential backoff + jitter. Raises the last error on failure."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = get_session(insecure).get(
                url, timeout=timeout, allow_redirects=allow_redirects
            )
            # Retry only on transient server-side / rate-limit conditions.
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            return resp
        except Exception as exc:  # noqa: BLE001 - deliberately broad, we retry
            last_exc = exc
            if attempt == retries:
                break
            sleep = backoff ** attempt + random.uniform(0, 0.4)
            time.sleep(sleep)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# JSONL checkpoint store
# --------------------------------------------------------------------------- #

class JsonlStore:
    """Append-only JSONL results file that makes runs resumable and crash-safe."""

    def __init__(self, path: str, key: str):
        self.path = path
        self.key = key
        self._lock = threading.Lock()
        self._fh = None

    def load_done(self) -> dict[str, dict[str, Any]]:
        """Return already-processed records keyed by `self.key`."""
        done: dict[str, dict[str, Any]] = {}
        if not os.path.exists(self.path):
            return done
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn final line from a hard kill
                k = rec.get(self.key)
                if k is not None:
                    done[str(k)] = rec
        return done

    def __enter__(self) -> "JsonlStore":
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def __exit__(self, *exc) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# Progress + concurrency
# --------------------------------------------------------------------------- #

class Progress:
    def __init__(self, total: int, label: str, already: int = 0):
        self.total = total
        self.label = label
        self.done = 0
        self.ok = 0
        self.miss = 0
        self.err = 0
        self.already = already
        self.start = time.time()
        self._lock = threading.Lock()

    def update(self, status: str) -> None:
        with self._lock:
            self.done += 1
            if status == "ok":
                self.ok += 1
            elif status == "missing":
                self.miss += 1
            else:
                self.err += 1
            if self.done % 25 == 0 or self.done == self.total:
                self._render()

    def _render(self) -> None:
        elapsed = time.time() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        remaining = (self.total - self.done) / rate if rate > 0 else 0.0
        sys.stderr.write(
            f"\r{self.label}: {self.done}/{self.total} "
            f"ok={self.ok} missing={self.miss} err={self.err} "
            f"| {rate:.1f}/s | eta {_fmt(remaining)}   "
        )
        sys.stderr.flush()

    def finish(self) -> None:
        self._render()
        sys.stderr.write("\n")
        sys.stderr.flush()


def _fmt(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def run_pool(
    items: list[Any],
    worker: Callable[[Any], dict[str, Any]],
    *,
    store: JsonlStore,
    progress: Progress,
    workers: int,
    delay: float = 0.0,
) -> None:
    """Map `worker` over `items` with a thread pool, streaming results to `store`."""
    def wrapped(item: Any) -> dict[str, Any]:
        if delay:
            time.sleep(random.uniform(0, delay))
        return worker(item)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(wrapped, it): it for it in items}
        try:
            for fut in as_completed(futures):
                try:
                    rec = fut.result()
                except Exception as exc:  # noqa: BLE001
                    rec = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                store.write(rec)
                progress.update(rec.get("status", "error"))
        except KeyboardInterrupt:
            sys.stderr.write("\nInterrupted - progress saved, re-run to resume.\n")
            pool.shutdown(wait=False, cancel_futures=True)
            raise


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #

EMIS_RE = re.compile(r"^\d{6,10}$")

_EMIS_KEY_HINTS = (
    "emis",
    "emis_code",
    "s_emis_code",
    "emiscode",
    "school_emis",
    "code",
)


def iter_records(path: str) -> Iterator[Any]:
    """Yield records from .json (array or object-of-arrays), .jsonl, .csv or .txt."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".jsonl":
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
    elif ext == ".json":
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        yield from _flatten_json(data)
    elif ext == ".csv":
        import csv

        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            yield from csv.DictReader(fh)
    else:  # plain text: one code per line
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line


def _flatten_json(data: Any) -> Iterator[Any]:
    if isinstance(data, list):
        # Support array-of-arrays (e.g. [["ATTOCK", ..., "37130015", ...], ...])
        # as well as a plain array of objects/scalars - both are yielded row by row.
        yield from data
    elif isinstance(data, dict):
        # Common wrapper shapes: {"data": [...]}, {"schools": [...]}, {...}
        for value in data.values():
            if isinstance(value, list) and value:
                yield from value
                return
        yield data
    else:
        yield data


def detect_emis_field(records: Iterable[Any]) -> str | None:
    """Guess which dict key (or column index for list records) holds the EMIS code."""
    records = list(records)[:200]
    dict_sample = [r for r in records if isinstance(r, dict)]
    list_sample = [r for r in records if isinstance(r, (list, tuple))]

    # --- Dict records ---
    if dict_sample:
        keys = list(dict_sample[0].keys())
        best, best_score = None, -1.0
        for key in keys:
            norm = key.strip().lower().replace(" ", "_")
            hits = sum(1 for r in dict_sample if EMIS_RE.match(str(r.get(key, "")).strip()))
            ratio = hits / len(dict_sample)
            if ratio < 0.5:
                continue
            score = ratio
            if any(h in norm for h in _EMIS_KEY_HINTS):
                score += 1.0
            if norm in ("emis", "emis_code", "s_emis_code"):
                score += 1.0
            if score > best_score:
                best, best_score = key, score
        if best:
            return best

    # --- List / array-of-arrays records ---
    if list_sample:
        # Try to find which column contains valid EMIS codes
        max_cols = max((len(r) for r in list_sample), default=0)
        best_idx, best_score = None, -1.0
        for idx in range(max_cols):
            hits = sum(
                1 for r in list_sample
                if len(r) > idx and EMIS_RE.match(str(r[idx]).strip())
            )
            ratio = hits / len(list_sample) if list_sample else 0
            if ratio >= 0.5 and ratio > best_score:
                best_idx, best_score = idx, ratio
        if best_idx is not None:
            return str(best_idx)  # return index as string so caller can treat it specially

    return None


def add_shard_args(ap) -> None:
    """Register --shard/--shards on an ArgumentParser."""
    ap.add_argument("--shards", type=int, default=1,
                    help="Total number of shards (parallel runners)")
    ap.add_argument("--shard", type=int, default=0,
                    help="0-based index of this shard")


def select_shard(items: list[Any], shard: int, shards: int) -> list[Any]:
    """Deterministic contiguous slice so each runner owns a disjoint block.

    Contiguous (not round-robin) keeps each shard's output file a clean range,
    which makes partial results easier to reason about and merge.
    """
    if shards <= 1:
        return items
    if not (0 <= shard < shards):
        raise SystemExit(f"--shard must be in [0,{shards - 1}], got {shard}")
    total = len(items)
    per, rem = divmod(total, shards)
    start = shard * per + min(shard, rem)
    end = start + per + (1 if shard < rem else 0)
    return items[start:end]


def extract_emis_codes(path: str, field: str | None = None) -> tuple[list[str], str]:
    """Return (unique ordered EMIS codes, field name used)."""
    records = list(iter_records(path))
    if not records:
        raise SystemExit(f"No records found in {path}")

    if isinstance(records[0], (str, int)):
        codes = [str(r).strip() for r in records]
        used = "<scalar>"
    elif isinstance(records[0], (list, tuple)):
        used = field or detect_emis_field(records)
        if not used:
            raise SystemExit(
                "Could not auto-detect the EMIS column in this array-of-arrays file.\n"
                "Pass --emis-field with a 0-based column index."
            )
        try:
            idx = int(used)
        except ValueError:
            raise SystemExit(
                f"--emis-field must be a column index for array-of-arrays input, got {used!r}"
            )
        codes = [str(r[idx]).strip() if len(r) > idx else "" for r in records]
    else:
        used = field or detect_emis_field(records)
        if not used:
            keys = sorted({k for r in records[:50] if isinstance(r, dict) for k in r})
            raise SystemExit(
                "Could not auto-detect the EMIS field. Pass --emis-field.\n"
                f"Available keys: {keys}"
            )
        codes = [str(r.get(used, "")).strip() for r in records if isinstance(r, dict)]

    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        c = c.split(".")[0]  # tolerate 32230183.0 from spreadsheet exports
        if c and EMIS_RE.match(c) and c not in seen:
            seen.add(c)
            out.append(c)
    if not out:
        raise SystemExit(
            f"Found records in {path} but none looked like a 6-10 digit EMIS code "
            f"in field/column {used!r}. Pass --emis-field to point at the right one."
        )
    return out, used
