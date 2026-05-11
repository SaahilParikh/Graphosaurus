"""Quick benchmark: generate synthetic dict/thesaurus, time `main.run` at
various scales + depths. Not a test -- a measurement.

Synthetic thesaurus: each word gets ~avg_syn random synonyms drawn from the
same vocabulary. This mimics real-thesaurus density reasonably well (Moby
averages ~30-50 synonyms/word; we're deliberately a bit leaner).
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import time
from pathlib import Path

from main import run


def _gen(n_words: int, avg_syn: int, seed: int = 0) -> tuple[Path, Path, Path]:
    """Write synthetic dict+thesaurus to a temp dir. Returns (dict, thes, out)."""
    rng = random.Random(seed)
    tmp = Path(tempfile.mkdtemp(prefix="pg_bench_"))
    words = [f"w{i:06d}" for i in range(n_words)]

    dict_path = tmp / "dictionary.txt"
    dict_path.write_text("\n".join(words) + "\n")

    thes = {}
    for w in words:
        # avg_syn synonyms drawn uniformly; some words will have repeats,
        # build_graph dedups and self-filters so that's fine.
        k = max(0, int(rng.gauss(avg_syn, avg_syn / 3)))
        thes[w] = rng.sample(words, min(k, n_words))
    thes_path = tmp / "thesaurus.json"
    thes_path.write_text(json.dumps(thes))

    out = tmp / "out"
    return dict_path, thes_path, out


def bench(n_words: int, avg_syn: int, depth: int, workers: int) -> float:
    dict_p, thes_p, out = _gen(n_words, avg_syn, seed=n_words)
    try:
        t0 = time.perf_counter()
        run(dict_p, thes_p, out, depth=depth, workers=workers)
        return time.perf_counter() - t0
    finally:
        shutil.rmtree(dict_p.parent, ignore_errors=True)


if __name__ == "__main__":
    cpu = os.cpu_count() or 1
    print(f"# CPU count: {cpu}")
    print(f"{'n_words':>10} {'avg_syn':>8} {'depth':>6} {'workers':>8} {'seconds':>10} {'us/word':>10}")
    for n_words, avg_syn, depth, workers in [
        (1_000,   20, 1, cpu),
        (1_000,   20, 2, cpu),
        (10_000,  20, 1, cpu),
        (10_000,  20, 2, cpu),
        (10_000,  20, 3, cpu),
        (50_000,  20, 1, cpu),
        (50_000,  20, 2, cpu),
        (100_000, 20, 1, cpu),
        (100_000, 20, 2, cpu),
        # Single-worker baseline for comparison.
        (10_000,  20, 2, 1),
    ]:
        secs = bench(n_words, avg_syn, depth, workers)
        print(
            f"{n_words:>10} {avg_syn:>8} {depth:>6} {workers:>8} "
            f"{secs:>10.2f} {secs/n_words*1e6:>10.1f}"
        )
