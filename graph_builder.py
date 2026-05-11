"""Core graph-building logic.

Pure functions, no I/O, no globals. Everything here must be safe to call from a
ProcessPoolExecutor worker, so `build_graph` takes the thesaurus and dictionary
as explicit arguments and returns a plain dict.

Graph model
-----------
For a given source word we build its *ego graph*: a BFS traversal of the
thesaurus starting at the word, expanding up to `depth` hops. Only words that
exist in the dictionary are kept as nodes; synonyms missing from the dictionary
are dropped. Edges are undirected and deduplicated.

- depth=0 -> just the source word (if it's in the dictionary)
- depth=1 -> source + direct synonyms
- depth=N -> BFS N hops out
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Set, Tuple


# An edge is a sorted tuple so (a, b) and (b, a) collapse to one entry.
Edge = Tuple[str, str]
Graph = Dict[str, object]  # {"word": str, "depth": int, "nodes": list, "edges": list}


def _normalize(word: str) -> str:
    """Lowercase + strip. Central place so dict/thesaurus/input all agree."""
    return word.strip().lower()


def build_graph(
    word: str,
    thesaurus: Dict[str, List[str]],
    dictionary: Set[str],
    depth: int = 1,
) -> Graph:
    """Build the ego graph for `word`.

    Args:
        word: Source word.
        thesaurus: Mapping of word -> list of synonyms. Assumed already
            normalized by the caller (see `normalize_inputs`). Passing
            un-normalized data still works but will produce fewer matches.
        dictionary: Set of valid words. Synonyms not in this set are dropped.
        depth: BFS depth from the source. Must be >= 0.

    Returns:
        A dict with keys: word, depth, nodes (sorted list), edges (sorted list
        of [a, b] pairs). Returns an empty-graph structure (no nodes, no edges)
        if `word` is not in the dictionary.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")

    source = _normalize(word)

    # Source not in dictionary -> nothing to build. We still return a
    # well-formed object so downstream code doesn't have to special-case None.
    if source not in dictionary:
        return {"word": source, "depth": depth, "nodes": [], "edges": []}

    nodes: Set[str] = {source}
    edges: Set[Edge] = set()

    # BFS. Each queue entry is (current_word, remaining_hops). We stop
    # expanding a node once remaining_hops hits 0, but the node itself is
    # still part of the graph.
    queue: deque[Tuple[str, int]] = deque([(source, depth)])
    visited: Set[str] = {source}

    while queue:
        current, remaining = queue.popleft()
        if remaining == 0:
            continue

        for raw_syn in thesaurus.get(current, ()):
            syn = _normalize(raw_syn)
            if syn == current:
                # Self-loops add noise; skip.
                continue
            if syn not in dictionary:
                # Dictionary acts as the vocabulary filter.
                continue

            # Add the edge regardless of whether we've seen `syn` before --
            # we might reach it via a different path and still need the edge.
            edges.add(_edge(current, syn))
            nodes.add(syn)

            if syn not in visited:
                visited.add(syn)
                queue.append((syn, remaining - 1))

    return {
        "word": source,
        "depth": depth,
        "nodes": sorted(nodes),
        # Sort edges for deterministic output (makes tests + diffs sane).
        "edges": sorted([list(e) for e in edges]),
    }


def _edge(a: str, b: str) -> Edge:
    """Canonical undirected edge: lexicographically smaller end first."""
    return (a, b) if a <= b else (b, a)


def normalize_inputs(
    thesaurus: Dict[str, Iterable[str]],
    dictionary: Iterable[str],
) -> Tuple[Dict[str, List[str]], Set[str]]:
    """Lowercase/strip everything once, up front.

    Doing this in the driver (before fanning out to workers) means each worker
    gets already-clean data and doesn't re-normalize on every call.
    """
    norm_dict: Set[str] = {_normalize(w) for w in dictionary if _normalize(w)}
    norm_thes: Dict[str, List[str]] = {}
    for key, syns in thesaurus.items():
        k = _normalize(key)
        if not k:
            continue
        # Deduplicate while preserving determinism via sort.
        norm_thes[k] = sorted({_normalize(s) for s in syns if _normalize(s)})
    return norm_thes, norm_dict
