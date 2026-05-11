"""Build the runtime wordbank from WordNet.

Runs at Docker image build time (see infra/lambda/Dockerfile, builder stage).
Reads WordNet via nltk, writes two files the server loads at startup:

- data/dictionary.txt  -- one lowercase word per line, sorted.
- data/thesaurus.json  -- {"word": ["syn1", "syn2", ...], ...}

Conversion rules
----------------
- We use WordNet synsets as "synonym clusters": all lemmas within a synset
  are synonyms of each other.
- POS is collapsed: "happy" (adj) and "happy" (noun) share one entry, with
  the union of synonyms from both synsets.
- Multi-word expressions ("ice_cream", "fly_off_the_handle") are kept,
  with underscores replaced by spaces to match how users would search.
- Results are deduplicated, lowercased, and sorted for determinism.

Idempotent. Running twice produces byte-identical output (stable sort).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def _normalize(raw: str) -> str:
    """WordNet lemma -> our canonical form."""
    return raw.replace("_", " ").strip().lower()


def build(out_dir: Path) -> tuple[int, int]:
    """Generate dictionary.txt + thesaurus.json under out_dir.

    Returns (num_words, num_synonym_pairs).
    """
    # Imported lazily because nltk + WordNet download only matters at build
    # time, not when the server loads the produced JSON.
    from nltk.corpus import wordnet as wn

    out_dir.mkdir(parents=True, exist_ok=True)

    # Aggregate: word -> set of synonyms. Using set to dedupe.
    thesaurus: dict[str, set[str]] = defaultdict(set)

    synset_count = 0
    for synset in wn.all_synsets():
        synset_count += 1
        lemmas = [_normalize(l.name()) for l in synset.lemmas()]
        lemmas = [w for w in lemmas if w]  # drop empties

        # Each pair within the synset is a synonym relationship.
        for i, a in enumerate(lemmas):
            for b in lemmas[i + 1:]:
                if a == b:
                    continue
                thesaurus[a].add(b)
                thesaurus[b].add(a)

    # Serialize with sorted keys + values for deterministic output.
    sorted_words = sorted(thesaurus.keys())

    dict_path = out_dir / "dictionary.txt"
    with dict_path.open("w", encoding="utf-8") as f:
        f.write(f"# Generated from WordNet by scripts/build_wordbank.py\n")
        f.write(f"# {len(sorted_words)} words from {synset_count} synsets\n")
        for w in sorted_words:
            f.write(w)
            f.write("\n")

    thes_path = out_dir / "thesaurus.json"
    # Write compact (no indent) to minimize JSON size: WordNet-scale indent
    # can add 10+ MB of whitespace.
    thesaurus_out = {w: sorted(thesaurus[w]) for w in sorted_words}
    with thes_path.open("w", encoding="utf-8") as f:
        json.dump(thesaurus_out, f, ensure_ascii=False, separators=(",", ":"))

    num_pairs = sum(len(syns) for syns in thesaurus.values()) // 2

    return len(sorted_words), num_pairs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("data"),
        help="Output directory. Default: data/",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    print(f"Building wordbank into {args.out}/ ...", file=sys.stderr)
    n_words, n_pairs = build(args.out)
    print(
        f"Wrote {n_words:,} words and {n_pairs:,} synonym pairs to {args.out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
