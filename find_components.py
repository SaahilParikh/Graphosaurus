"""CLI: find connected components of the thesaurus graph.

Example:
    python find_components.py \\
        --dict sample_data/dictionary.txt \\
        --thesaurus sample_data/thesaurus.json \\
        --out components.json

Output:
    - stderr: human-readable summary (component count, size histogram,
      the actual small components for eyeballing)
    - --out path: full JSON with every component and its members
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List

from components import find_components, summarize
from graph_builder import normalize_inputs
from main import _load_dictionary, _load_thesaurus  # reuse loaders


def _render_summary(components: List[List[str]], show_small_up_to: int) -> str:
    """Build a human-readable summary string for stderr."""
    s = summarize(components)
    lines = [
        f"Components: {s['num_components']:,}",
        f"Words:      {s['num_words']:,}",
        f"Largest:    {s['largest_size']:,} "
        f"({s['giant_component_ratio']*100:.1f}% of all words)",
        f"Singletons: {s['num_singletons']:,}",
        "Size histogram:",
    ]
    for bucket, count in s["size_histogram"].items():
        lines.append(f"  {bucket:<16} {count:,}")

    # Show the non-giant components in full, up to `show_small_up_to` each.
    # These are the interesting ones -- the "truly separate regions".
    small = [
        c for c in components
        if 2 <= len(c) <= show_small_up_to
    ]
    if small:
        lines.append("")
        lines.append(
            f"Non-singleton components of size <= {show_small_up_to} "
            f"({len(small)} total):"
        )
        for i, comp in enumerate(small):
            lines.append(f"  [{len(comp):>3}] {', '.join(comp)}")

    # Big components are just summarized by size (listing them would flood
    # the terminal for real data).
    big = [c for c in components if len(c) > show_small_up_to]
    if big:
        lines.append("")
        lines.append(f"Components of size > {show_small_up_to}:")
        for i, comp in enumerate(big):
            preview = ", ".join(comp[:8])
            more = f" ... (+{len(comp)-8} more)" if len(comp) > 8 else ""
            lines.append(f"  [{len(comp):>5}] {preview}{more}")

    return "\n".join(lines)


def run(
    dict_path: Path,
    thesaurus_path: Path,
    out_path: Path | None,
    show_small_up_to: int,
) -> int:
    raw_words = _load_dictionary(dict_path)
    raw_thes = _load_thesaurus(thesaurus_path)
    thesaurus, dictionary = normalize_inputs(raw_thes, raw_words)

    t0 = time.perf_counter()
    components = find_components(thesaurus, dictionary)
    elapsed = time.perf_counter() - t0

    print(_render_summary(components, show_small_up_to), file=sys.stderr)
    print(f"\nComputed in {elapsed:.3f}s", file=sys.stderr)

    if out_path is not None:
        payload = {
            "summary": summarize(components),
            "components": [
                {"id": i, "size": len(c), "members": c}
                for i, c in enumerate(components)
            ],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Wrote full component data to {out_path}", file=sys.stderr)

    return len(components)


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Find connected components of the thesaurus graph."
    )
    p.add_argument("--dict", required=True, type=Path)
    p.add_argument("--thesaurus", required=True, type=Path)
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional JSON output with full component membership.",
    )
    p.add_argument(
        "--show-small-up-to",
        type=int,
        default=20,
        help=(
            "In the stderr summary, print full membership of components "
            "whose size is <= this value. Default: 20."
        ),
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        run(args.dict, args.thesaurus, args.out, args.show_small_up_to)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
