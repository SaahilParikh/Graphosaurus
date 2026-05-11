"""Unit tests for graph_builder.

Covers: depth semantics, dictionary filtering, missing source word, self-loops,
edge deduplication, and normalization. Run with: pytest -q
"""

from __future__ import annotations

import pytest

from graph_builder import build_graph, normalize_inputs


@pytest.fixture
def tiny():
    """A small hand-built thesaurus where we can reason about every edge."""
    thesaurus = {
        "happy": ["joyful", "glad"],
        "joyful": ["happy", "cheerful"],
        "glad": ["happy"],
        "cheerful": ["joyful", "bright"],
        "bright": ["cheerful"],
        # 'ecstatic' is intentionally present in the thesaurus but NOT in the
        # dictionary below -> must be filtered out.
        "ecstatic": ["happy"],
        "happy_self": ["happy_self"],  # self-loop bait
    }
    dictionary = {"happy", "joyful", "glad", "cheerful", "bright", "happy_self"}
    return thesaurus, dictionary


def test_depth_zero_returns_just_the_word(tiny):
    thes, dct = tiny
    g = build_graph("happy", thes, dct, depth=0)
    assert g["word"] == "happy"
    assert g["nodes"] == ["happy"]
    assert g["edges"] == []


def test_depth_one_adds_direct_synonyms(tiny):
    thes, dct = tiny
    g = build_graph("happy", thes, dct, depth=1)
    assert set(g["nodes"]) == {"happy", "joyful", "glad"}
    # Edges are sorted tuples stored as lists.
    assert ["glad", "happy"] in g["edges"]
    assert ["happy", "joyful"] in g["edges"]
    # At depth=1 we do NOT traverse joyful -> cheerful.
    assert "cheerful" not in g["nodes"]


def test_depth_two_does_one_more_hop(tiny):
    thes, dct = tiny
    g = build_graph("happy", thes, dct, depth=2)
    # happy -> joyful -> cheerful should now be included.
    assert "cheerful" in g["nodes"]
    assert ["cheerful", "joyful"] in g["edges"]
    # But cheerful -> bright is one hop too far.
    assert "bright" not in g["nodes"]


def test_depth_three_reaches_bright(tiny):
    thes, dct = tiny
    g = build_graph("happy", thes, dct, depth=3)
    assert "bright" in g["nodes"]
    assert ["bright", "cheerful"] in g["edges"]


def test_synonyms_not_in_dictionary_are_dropped(tiny):
    """'ecstatic' is in thesaurus but not dictionary -> must not appear."""
    thes, dct = tiny
    # Seed from ecstatic (which isn't in the dict) -> empty graph.
    g = build_graph("ecstatic", thes, dct, depth=5)
    assert g["nodes"] == []
    assert g["edges"] == []


def test_source_word_not_in_dictionary_returns_empty():
    g = build_graph("nonexistent", {"nonexistent": ["a"]}, {"a"}, depth=1)
    assert g == {"word": "nonexistent", "depth": 1, "nodes": [], "edges": []}


def test_self_loops_are_ignored(tiny):
    thes, dct = tiny
    g = build_graph("happy_self", thes, dct, depth=2)
    assert g["nodes"] == ["happy_self"]
    assert g["edges"] == []  # no self-edge


def test_edges_are_deduplicated():
    """a <-> b reachable both directions should yield one edge, not two."""
    thes = {"a": ["b"], "b": ["a"]}
    dct = {"a", "b"}
    g = build_graph("a", thes, dct, depth=5)
    assert g["edges"] == [["a", "b"]]


def test_isolated_word_has_node_but_no_edges():
    thes = {"lonely": []}
    dct = {"lonely"}
    g = build_graph("lonely", thes, dct, depth=3)
    assert g["nodes"] == ["lonely"]
    assert g["edges"] == []


def test_negative_depth_rejected():
    with pytest.raises(ValueError):
        build_graph("x", {}, {"x"}, depth=-1)


def test_normalize_inputs_lowercases_and_strips():
    thes = {"  Happy  ": ["Joyful", "GLAD"], "Joyful": ["happy"]}
    dct = ["Happy", "joyful", " Glad "]
    norm_thes, norm_dct = normalize_inputs(thes, dct)
    assert norm_dct == {"happy", "joyful", "glad"}
    assert norm_thes["happy"] == ["glad", "joyful"]  # sorted + deduped
    assert "joyful" in norm_thes


def test_case_insensitive_lookup_after_normalize():
    """End-to-end: mixed-case input -> normalize -> build_graph matches."""
    raw_thes = {"Happy": ["Joyful"], "Joyful": ["Happy"]}
    raw_dct = ["HAPPY", "joyful"]
    thes, dct = normalize_inputs(raw_thes, raw_dct)
    g = build_graph("HaPpY", thes, dct, depth=1)
    assert set(g["nodes"]) == {"happy", "joyful"}
    assert g["edges"] == [["happy", "joyful"]]
