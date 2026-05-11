"""Build the runtime wordbank from WordNet.

Runs at Docker image build time (see infra/lambda/Dockerfile, builder stage).
Reads WordNet via nltk, writes three files the server loads at startup:

- data/dictionary.txt   -- one lowercase word per line, sorted.
- data/thesaurus.json   -- {"word": ["syn1", "syn2", ...], ...}
- data/definitions.json -- {"word": [{"pos": "adjective", "def": "..."}, ...]}

Conversion rules
----------------
- We use WordNet synsets as "synonym clusters": all lemmas within a synset
  are synonyms of each other.
- POS is collapsed for the thesaurus (same word/different POS share an
  entry) but preserved in the definitions (each sense gets its own entry).
- Multi-word expressions ("ice_cream", "fly_off_the_handle") are kept,
  with underscores replaced by spaces to match how users would search.
- Results are deduplicated, lowercased, and sorted for determinism.

Idempotent. Running twice produces byte-identical output.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


# WordNet single-char POS -> human label.
_POS = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}


def _normalize(raw: str) -> str:
    """WordNet lemma -> our canonical form."""
    return raw.replace("_", " ").strip().lower()


def build(out_dir: Path) -> tuple[int, int, int]:
    """Generate all data files under out_dir.

    Returns (num_words, num_synonym_pairs, num_definitions).
    """
    # Imported lazily because nltk + WordNet download only matters at build
    # time, not when the server loads the produced JSON.
    from nltk.corpus import wordnet as wn

    out_dir.mkdir(parents=True, exist_ok=True)

    # Aggregate: word -> set of synonyms.
    thesaurus: dict[str, set[str]] = defaultdict(set)
    # Aggregate: word -> list of {pos, def} (order preserved, most common first).
    # We use a list of tuples to dedupe on (pos, definition) text.
    definitions: dict[str, list[tuple[str, str]]] = defaultdict(list)

    synset_count = 0
    for synset in wn.all_synsets():
        synset_count += 1
        lemmas = [_normalize(l.name()) for l in synset.lemmas()]
        lemmas = [w for w in lemmas if w]
        if not lemmas:
            continue

        pos = _POS.get(synset.pos(), synset.pos())
        definition = synset.definition().strip()

        # Synonym edges
        for i, a in enumerate(lemmas):
            for b in lemmas[i + 1:]:
                if a == b:
                    continue
                thesaurus[a].add(b)
                thesaurus[b].add(a)

        # Definitions: each lemma in this synset gets this sense.
        for w in lemmas:
            entry = (pos, definition)
            if entry not in definitions[w]:
                definitions[w].append(entry)

    # Ensure every lemma (even ones with no synonyms) has a definition entry.
    # Both dicts end up keyed by the same word set.
    all_words = set(thesaurus.keys()) | set(definitions.keys())
    sorted_words = sorted(all_words)

    # dictionary.txt
    dict_path = out_dir / "dictionary.txt"
    with dict_path.open("w", encoding="utf-8") as f:
        f.write("# Generated from WordNet by scripts/build_wordbank.py\n")
        f.write(f"# {len(sorted_words)} words from {synset_count} synsets\n")
        for w in sorted_words:
            f.write(w)
            f.write("\n")

    # thesaurus.json (compact)
    thes_path = out_dir / "thesaurus.json"
    thesaurus_out = {w: sorted(thesaurus[w]) for w in sorted_words if thesaurus[w]}
    with thes_path.open("w", encoding="utf-8") as f:
        json.dump(thesaurus_out, f, ensure_ascii=False, separators=(",", ":"))

    # definitions.json (compact, list of {pos, def})
    defs_path = out_dir / "definitions.json"
    defs_out = {
        w: [{"pos": p, "def": d} for (p, d) in definitions[w]]
        for w in sorted_words
        if definitions[w]
    }
    with defs_path.open("w", encoding="utf-8") as f:
        json.dump(defs_out, f, ensure_ascii=False, separators=(",", ":"))

    num_pairs = sum(len(syns) for syns in thesaurus.values()) // 2
    num_defs = sum(len(d) for d in defs_out.values())

    return len(sorted_words), num_pairs, num_defs


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
    n_words, n_pairs, n_defs = build(args.out)
    print(
        f"Wrote {n_words:,} words, {n_pairs:,} synonym pairs, "
        f"{n_defs:,} definitions to {args.out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
