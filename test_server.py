"""Tests for server.py pure helpers.

We don't spin up the HTTP server in tests -- the Handler is thin glue over
`search_words` and `neighborhood_response`, both of which are pure functions.
That keeps tests fast, deterministic, and avoids port conflicts in CI.
"""

from __future__ import annotations

import pytest

from server import search_words, neighborhood_response


# --- search_words ---

def test_search_words_prefix_match():
    words = sorted(["happy", "happier", "happiest", "joyful", "glad", "sad"])
    assert search_words(words, "hap", 10) == ["happier", "happiest", "happy"]


def test_search_words_no_match():
    assert search_words(["happy", "sad"], "xyz", 10) == []


def test_search_words_empty_query():
    assert search_words(["happy", "sad"], "", 10) == []
    assert search_words(["happy", "sad"], "   ", 10) == []


def test_search_words_case_insensitive():
    words = sorted(["happy", "happier"])
    assert search_words(words, "HAP", 10) == ["happier", "happy"]


def test_search_words_respects_limit():
    words = sorted(["a1", "a2", "a3", "a4", "a5"])
    assert search_words(words, "a", 3) == ["a1", "a2", "a3"]


def test_search_words_limit_clamped():
    words = sorted(["a1", "a2"])
    # limit <= 0 clamped up to 1
    assert search_words(words, "a", 0) == ["a1"]
    assert search_words(words, "a", -5) == ["a1"]
    # limit > 100 clamped down to 100 (here we only have 2 words anyway)
    assert search_words(words, "a", 10_000) == ["a1", "a2"]


# --- neighborhood_response ---

@pytest.fixture
def tiny():
    thesaurus = {
        "happy": ["joyful", "glad"],
        "joyful": ["happy", "cheerful"],
        "glad": ["happy"],
        "cheerful": ["joyful"],
    }
    dictionary = {"happy", "joyful", "glad", "cheerful"}
    return thesaurus, dictionary


def test_neighborhood_basic(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "happy", 1)
    assert status == 200
    assert body["word"] == "happy"
    assert body["depth"] == 1
    assert set(body["nodes"]) == {"happy", "joyful", "glad"}


def test_neighborhood_missing_word(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "", 1)
    assert status == 400
    assert "error" in body


def test_neighborhood_whitespace_word(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "   ", 1)
    assert status == 400


def test_neighborhood_default_depth(tiny):
    """When depth is None, server should default to 2."""
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "happy", None)
    assert status == 200
    assert body["depth"] == 2
    # At depth 2: happy -> joyful -> cheerful reachable
    assert "cheerful" in body["nodes"]


def test_neighborhood_depth_capped_by_max(tiny):
    thes, dct = tiny
    # Request depth=10, max_depth=2 -> actual depth should be 2.
    status, body = neighborhood_response(thes, dct, "happy", 10, max_depth=2)
    assert status == 200
    assert body["depth"] == 2


def test_neighborhood_negative_depth_clamped_to_zero(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "happy", -3)
    assert status == 200
    assert body["depth"] == 0
    assert body["nodes"] == ["happy"]
    assert body["edges"] == []


def test_neighborhood_non_integer_depth_falls_back_to_default(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "happy", "not-a-number")
    assert status == 200
    assert body["depth"] == 2  # default


def test_neighborhood_word_not_in_dictionary(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "unknown", 2)
    # build_graph returns a well-formed empty structure, not an error.
    assert status == 200
    assert body["word"] == "unknown"
    assert body["nodes"] == []
    assert body["edges"] == []


def test_neighborhood_word_lowercased(tiny):
    thes, dct = tiny
    status, body = neighborhood_response(thes, dct, "  HaPpY  ", 1)
    assert status == 200
    assert body["word"] == "happy"
