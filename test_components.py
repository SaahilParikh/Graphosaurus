"""Unit tests for components.find_components.

Covers: single component, multiple components, asymmetric edges (still merge),
singletons, words in thesaurus but not dict, words in dict but not thesaurus,
determinism.
"""

from __future__ import annotations

from components import find_components, summarize


def test_empty_inputs():
    assert find_components({}, set()) == []


def test_single_word_no_edges():
    comps = find_components({}, {"lonely"})
    assert comps == [["lonely"]]


def test_two_disconnected_pairs():
    thes = {"a": ["b"], "c": ["d"]}
    dct = {"a", "b", "c", "d"}
    comps = find_components(thes, dct)
    # Two components of size 2. Order: larger first, alphabetical tiebreak.
    assert comps == [["a", "b"], ["c", "d"]]


def test_asymmetric_edge_is_still_undirected():
    """a -> b is listed but b -> a is not; they should still be one component."""
    thes = {"a": ["b"]}  # no reciprocal
    dct = {"a", "b"}
    comps = find_components(thes, dct)
    assert comps == [["a", "b"]]


def test_singleton_when_only_external_synonyms():
    """'a' has synonyms, but none are in the dictionary -> a is a singleton."""
    thes = {"a": ["notindict"]}
    dct = {"a"}
    comps = find_components(thes, dct)
    assert comps == [["a"]]


def test_word_in_thesaurus_but_not_dict_is_ignored():
    thes = {"a": ["b"], "c": ["d"]}
    dct = {"a", "b"}  # c, d missing
    comps = find_components(thes, dct)
    assert comps == [["a", "b"]]


def test_linear_chain_merges_into_one():
    thes = {"a": ["b"], "b": ["c"], "c": ["d"]}
    dct = {"a", "b", "c", "d"}
    comps = find_components(thes, dct)
    assert comps == [["a", "b", "c", "d"]]


def test_cycle_is_one_component():
    thes = {"a": ["b"], "b": ["c"], "c": ["a"]}
    dct = {"a", "b", "c"}
    comps = find_components(thes, dct)
    assert comps == [["a", "b", "c"]]


def test_mixed_sizes_sort_largest_first():
    thes = {
        # Big component: a-b-c-d
        "a": ["b"], "b": ["c"], "c": ["d"],
        # Small component: e-f
        "e": ["f"],
        # Singleton: g
    }
    dct = {"a", "b", "c", "d", "e", "f", "g"}
    comps = find_components(thes, dct)
    assert [len(c) for c in comps] == [4, 2, 1]
    assert comps[0] == ["a", "b", "c", "d"]
    assert comps[1] == ["e", "f"]
    assert comps[2] == ["g"]


def test_self_loop_is_ignored():
    """A word listing itself as a synonym shouldn't change anything."""
    thes = {"a": ["a", "b"]}
    dct = {"a", "b"}
    comps = find_components(thes, dct)
    assert comps == [["a", "b"]]


def test_summarize_basic():
    comps = [
        ["a", "b", "c", "d", "e", "f", "g"],  # size 7 -> "6-20"
        ["h", "i", "j"],                       # size 3 -> "2-5"
        ["k", "l"],                            # size 2 -> "2-5"
        ["m"],                                 # singleton
        ["n"],                                 # singleton
    ]
    s = summarize(comps)
    assert s["num_components"] == 5
    assert s["num_words"] == 14
    assert s["largest_size"] == 7
    assert s["num_singletons"] == 2
    assert s["size_histogram"] == {
        "1 (singleton)": 2,
        "2-5": 2,
        "6-20": 1,
    }
    # 7 / 14 = 0.5
    assert abs(s["giant_component_ratio"] - 7 / 14) < 1e-9


def test_summarize_empty():
    s = summarize([])
    assert s["num_components"] == 0
    assert s["num_words"] == 0
    assert s["largest_size"] == 0
    assert s["giant_component_ratio"] == 0.0


def test_determinism():
    """Same inputs in different dict orderings -> same output."""
    thes_a = {"b": ["a"], "c": ["d"], "a": []}
    thes_b = {"a": [], "c": ["d"], "b": ["a"]}
    dct = {"a", "b", "c", "d"}
    assert find_components(thes_a, dct) == find_components(thes_b, dct)
