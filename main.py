"""Parallel driver: fan out `build_graph` across a process pool.

Why processes, not threads?
---------------------------
`build_graph` is pure-Python CPU work (set operations, BFS). The GIL would
serialize threads, killing speedup. Processes sidestep the GIL and scale
with core count -- but only if the per-task work outweighs IPC overhead
(see "Why chunks?" below).

Why chunks, not one task per word?
----------------------------------
A single `build_graph` call at depth 1-2 on realistic data takes <1ms.
`ProcessPoolExecutor`'s per-task overhead (pickle the args, enqueue, pickle
the result back) is ~50-200us. At one-word-per-task, IPC *dominates* and
parallelism can even hurt (measured: 14-worker run was slower than 1-worker).

Solution: each task processes a *chunk* of words. The worker builds the
graphs AND writes the JSON files itself, so only a small summary comes back
over IPC. Default chunk size targets ~4 chunks per worker, which bounds
load imbalance to ~25% of total time.

Worker initialization
---------------------
The thesaurus + dictionary + output directory are read-only for the run. We
use `initializer=` to stash them in each worker process once, then workers
read from module globals. This is the standard pattern for "broadcast a big
read-only blob to workers".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Set, Tuple

from graph_builder import build_graph, normalize_inputs


# Per-worker globals populated by `_init_worker`. Only touched inside workers.
_WORKER_THESAURUS: Dict[str, List[str]] = {}
_WORKER_DICTIONARY: Set[str] = set()
_WORKER_OUT_DIR: Path = Path()


def _init_worker(
    thesaurus: Dict[str, List[str]],
    dictionary: Set[str],
    out_dir: str,
) -> None:
    """Run once per worker process; primes the read-only globals."""
    global _WORKER_THESAURUS, _WORKER_DICTIONARY, _WORKER_OUT_DIR
    _WORKER_THESAURUS = thesaurus
    _WORKER_DICTIONARY = dictionary
    _WORKER_OUT_DIR = Path(out_dir)


def _safe_filename(word: str) -> str:
    """Make a word safe to use as a filename on any OS."""
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in word)


def _worker_build_chunk(
    words: List[str], depth: int
) -> Tuple[int, List[Tuple[str, str]]]:
    """Build + write graphs for a whole chunk inside one worker.

    Returning just (count, errors) keeps IPC tiny -- the graph dicts never
    leave the worker. Writes are independent files, safe to do in parallel.
    """
    written = 0
    errors: List[Tuple[str, str]] = []
    for word in words:
        try:
            graph = build_graph(word, _WORKER_THESAURUS, _WORKER_DICTIONARY, depth)
            out_path = _WORKER_OUT_DIR / f"{_safe_filename(graph['word'])}.json"
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(graph, f, indent=2, sort_keys=True)
            written += 1
        except Exception as e:  # noqa: BLE001 -- per-word isolation
            errors.append((word, f"{type(e).__name__}: {e}"))
    return written, errors


def _load_dictionary(path: Path) -> List[str]:
    """One word per line. Blank lines and '#' comments ignored."""
    words: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            words.append(line)
    return words


def _load_thesaurus(path: Path) -> Dict[str, List[str]]:
    """JSON object: word -> list of synonyms."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: thesaurus must be a JSON object")
    for k, v in data.items():
        if not isinstance(v, list):
            raise ValueError(
                f"{path}: key {k!r} must map to a list, got {type(v).__name__}"
            )
    return data


def _chunked(seq: List[str], size: int) -> List[List[str]]:
    """Split `seq` into lists of length `size` (last chunk may be shorter)."""
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _auto_chunk_size(n_words: int, workers: int) -> int:
    """Aim for ~4 chunks per worker. Bounds load imbalance to ~25%."""
    if n_words <= workers:
        return 1
    return max(1, n_words // (workers * 4))


def run(
    dict_path: Path,
    thesaurus_path: Path,
    out_dir: Path,
    depth: int,
    workers: int,
    chunk_size: int | None = None,
) -> int:
    """Build + write a graph for every word in the dictionary. Returns count."""
    raw_words = _load_dictionary(dict_path)
    raw_thes = _load_thesaurus(thesaurus_path)

    # Normalize once, up front, so workers get clean data.
    thesaurus, dictionary = normalize_inputs(raw_thes, raw_words)

    # Dedupe while preserving first-seen order.
    seen: Set[str] = set()
    words: List[str] = []
    for w in raw_words:
        nw = w.strip().lower()
        if nw and nw not in seen:
            seen.add(nw)
            words.append(nw)

    out_dir.mkdir(parents=True, exist_ok=True)

    if chunk_size is None:
        chunk_size = _auto_chunk_size(len(words), workers)
    chunks = _chunked(words, chunk_size)

    print(
        f"Building {len(words)} graph(s) with depth={depth} "
        f"using {workers} worker(s), chunk_size={chunk_size} "
        f"({len(chunks)} chunk(s))...",
        file=sys.stderr,
    )
    t0 = time.perf_counter()

    written = 0
    # `initializer` ships the big blobs + out_dir once per worker.
    # `out_dir` is passed as a string (always picklable across platforms).
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(thesaurus, dictionary, str(out_dir)),
    ) as pool:
        futures = [pool.submit(_worker_build_chunk, chunk, depth) for chunk in chunks]
        for fut in as_completed(futures):
            try:
                count, errors = fut.result()
            except Exception as e:  # noqa: BLE001 -- worker-level crash
                # An entire chunk died (not a per-word error -- those are
                # caught inside the worker). Very rare: unpickling, OOM, etc.
                print(f"  ! chunk failed: {e}", file=sys.stderr)
                continue
            written += count
            for word, msg in errors:
                print(f"  ! {word}: {msg}", file=sys.stderr)

    elapsed = time.perf_counter() - t0
    print(
        f"Wrote {written}/{len(words)} graph(s) to {out_dir} in {elapsed:.2f}s",
        file=sys.stderr,
    )
    return written


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build per-word thesaurus graphs in parallel."
    )
    p.add_argument("--dict", required=True, type=Path, help="Path to dictionary.txt")
    p.add_argument(
        "--thesaurus", required=True, type=Path, help="Path to thesaurus.json"
    )
    p.add_argument(
        "--out", required=True, type=Path, help="Output directory for graph JSON files"
    )
    p.add_argument(
        "--depth",
        type=int,
        default=1,
        help="BFS depth from each word (default: 1)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of parallel worker processes (default: CPU count)",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help=(
            "Words per task. Default: auto-computed for ~4 chunks per worker. "
            "Smaller = better load balancing but more IPC overhead. Larger = "
            "less IPC but worse load balancing."
        ),
    )
    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        run(
            args.dict,
            args.thesaurus,
            args.out,
            args.depth,
            args.workers,
            args.chunk_size,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
