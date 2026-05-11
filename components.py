"""Connected-component analysis of the thesaurus graph.

Answers: "Are there truly separate regions in this vocabulary, or is the
thesaurus one big connected blob?"

Treats thesaurus edges as **undirected**: if `a -> b` appears anywhere, a and b
are in the same component. This matches how you'd think about synonyms
("happy" and "joyful" belong together regardless of which direction the
dictionary was written in). Getting strictly-symmetric edges out of real-world
thesauri is a losing battle; better to be explicit that we're treating them
as undirected and move on.

Algorithm: union-find with path compression + union by rank. O((|V|+|E|) * α(n)),
effectively linear. On one core this handles WordNet-scale (~150K / ~500K)
in well under a second.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple


def find_components(
    thesaurus: Dict[str, List[str]],
    dictionary: Set[str],
) -> List[List[str]]:
    """Return connected components of the undirected thesaurus graph.

    Only words in `dictionary` are considered. Thesaurus entries whose key or
    synonyms fall outside the dictionary are filtered out. Words in the
    dictionary that never appear in any thesaurus edge become singletons.

    Each component is returned as a sorted list of words. Components
    themselves are returned largest-first, ties broken alphabetically by
    first word. Deterministic output.
    """
    # --- Union-find data structures ---
    parent: Dict[str, str] = {w: w for w in dictionary}
    rank: Dict[str, int] = {w: 0 for w in dictionary}

    def find(x: str) -> str:
        # Iterative two-pass: find root, then compress path.
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            nxt = parent[x]
            parent[x] = root
            x = nxt
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Union by rank keeps the tree shallow.
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    # --- Apply edges (as undirected) ---
    for word, syns in thesaurus.items():
        if word not in dictionary:
            continue
        for syn in syns:
            if syn in dictionary and syn != word:
                union(word, syn)

    # --- Group by root ---
    groups: Dict[str, List[str]] = defaultdict(list)
    for w in dictionary:
        groups[find(w)].append(w)

    components = [sorted(members) for members in groups.values()]
    # Largest first; alphabetical tiebreak for determinism.
    components.sort(key=lambda c: (-len(c), c[0]))
    return components


def summarize(components: List[List[str]]) -> Dict[str, object]:
    """Human-useful statistics for a component list."""
    sizes = [len(c) for c in components]
    non_singleton = [s for s in sizes if s > 1]

    return {
        "num_components": len(components),
        "num_words": sum(sizes),
        "largest_size": sizes[0] if sizes else 0,
        "num_singletons": sum(1 for s in sizes if s == 1),
        "num_small_components": sum(1 for s in sizes if 2 <= s <= 5),
        "num_medium_components": sum(1 for s in sizes if 6 <= s <= 50),
        "num_large_components": sum(1 for s in sizes if s > 50),
        "size_histogram": _histogram(sizes),
        # The "giant component ratio" is a classic graph-connectivity
        # diagnostic: if this is ~1.0 the graph is effectively one blob with
        # a few stragglers; if it's spread out, you have real separate regions.
        "giant_component_ratio": (
            sizes[0] / sum(sizes) if sizes and sum(sizes) > 0 else 0.0
        ),
    }


def _histogram(sizes: List[int]) -> Dict[str, int]:
    buckets: Dict[str, int] = defaultdict(int)
    for s in sizes:
        if s == 1:
            buckets["1 (singleton)"] += 1
        elif s <= 5:
            buckets["2-5"] += 1
        elif s <= 20:
            buckets["6-20"] += 1
        elif s <= 100:
            buckets["21-100"] += 1
        elif s <= 1000:
            buckets["101-1000"] += 1
        else:
            buckets["1000+"] += 1
    # Sort bucket keys for stable output; preserve natural size order.
    order = [
        "1 (singleton)",
        "2-5",
        "6-20",
        "21-100",
        "101-1000",
        "1000+",
    ]
    return {k: buckets[k] for k in order if k in buckets}
