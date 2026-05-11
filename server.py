"""HTTP server exposing the thesaurus as a lazy neighborhood API.

Design for deployability
------------------------
- Config from env vars -- no hardcoded paths or ports. Easy to override in
  container / Lambda / wherever.
- Stateless: no session state, no on-disk writes. Safe behind an LB, safe to
  horizontally scale, safe to freeze/thaw (Lambda).
- Graceful SIGTERM shutdown so container orchestrators stop cleanly.
- /health endpoint for load balancer health checks.
- Single origin (API + static) for dev. Frontend split to CDN is a later
  problem; when we do it, we add CORS here.

Endpoints
---------
- GET /health                              -> 200 {"ok": true}
- GET /api/search?q=<prefix>&limit=<N>     -> {"query": ..., "matches": [...]}
- GET /api/neighborhood?word=<w>&depth=<k> -> {word, depth, nodes, edges}
- GET /api/word?word=<w>                   -> {word, definitions, etymology, wiktionary_url}
- GET /                                    -> web/index.html
- GET /<static-asset>                      -> web/<asset>  (whitelisted)

Env vars
--------
- PG_HOST        (default: 0.0.0.0)
- PG_PORT        (default: 8000)
- PG_DICTIONARY  (default: sample_data/dictionary.txt)
- PG_THESAURUS   (default: sample_data/thesaurus.json)
- PG_DEFINITIONS (optional; unset -> /api/word returns empty defs)
- PG_MAX_DEPTH   (default: 5) -- caps depth to prevent DoS via deep BFS
- PG_LOG_LEVEL   (default: INFO)
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
import signal
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

from graph_builder import build_graph, normalize_inputs
from main import _load_dictionary, _load_thesaurus


log = logging.getLogger("pythongraphs.server")

# Module-level state, set once at startup. Workers read-only after that.
_STATE: Dict[str, Any] = {}

# In-process etymology cache. Lambda warm instance reuses these, so repeat
# queries for the same word don't hit Wiktionary twice. Simple dict --
# unbounded in theory but 150K words * ~500 bytes each = ~75MB worst case.
# In practice only popular words get fetched; cache stays small.
_ETYMOLOGY_CACHE: Dict[str, Optional[str]] = {}
_ETYMOLOGY_CACHE_LOCK = Lock()

# Static assets under web/ that we serve. Whitelisted to block directory
# traversal; no "../../etc/passwd" surprises.
_STATIC_WHITELIST = {"index.html", "app.js", "app.css"}
_STATIC_DIR = Path(__file__).parent / "web"


# --- Pure helpers (easy to unit test, no HTTP stack) -----------------------

def search_words(words_sorted: List[str], prefix: str, limit: int) -> List[str]:
    """Prefix search over a pre-sorted word list. O(log n + m)."""
    q = (prefix or "").strip().lower()
    if not q:
        return []
    limit = max(1, min(limit, 100))
    lo = bisect.bisect_left(words_sorted, q)
    out: List[str] = []
    for w in words_sorted[lo:]:
        if not w.startswith(q):
            break
        out.append(w)
        if len(out) >= limit:
            break
    return out


def neighborhood_response(
    thesaurus: Dict[str, List[str]],
    dictionary: Set[str],
    word: str,
    depth_raw: Any,
    max_depth: int = 5,
) -> Tuple[int, Dict[str, Any]]:
    """Returns (http_status, body). Pure function of inputs."""
    w = (word or "").strip().lower()
    if not w:
        return 400, {"error": "word is required"}
    try:
        depth = int(depth_raw) if depth_raw is not None else 2
    except (TypeError, ValueError):
        depth = 2
    depth = max(0, min(depth, max_depth))
    graph = build_graph(w, thesaurus, dictionary, depth)
    return 200, graph


# --- Etymology fetching (Wiktionary) ---------------------------------------
#
# We used to parse raw wikitext with regex. That worked for words with
# simple etymologies but produced garbage for common words because
# Wiktionary's etymology sections are full of templates like {{inh|en|enm|
# happy}} that expand into prose like "Middle English happy". Our regex
# couldn't do that expansion -- it just dropped templates or kept their
# worst arg.
#
# Better approach: ask Wiktionary for the PRE-EXPANDED HTML of the
# Etymology section, then strip tags. Two API calls:
#   1. prop=sections   -> list of sections; find the one titled "Etymology"
#   2. prop=text&section=N -> HTML of that section with all templates resolved
# Results cached per-process. Warm Lambda reuses; cold Lambda repays.

from html.parser import HTMLParser as _HTMLParser

_WIKTIONARY_API = "https://en.wiktionary.org/w/api.php"
# We drop the contents of these entirely (noise for etymology text).
_SKIP_TAGS = {"style", "script", "sup"}
_VOID_TAGS = {"img", "br", "hr", "input", "meta", "link", "source", "track"}

# Match the Etymology heading anchor. Wiktionary IDs it as "Etymology",
# "Etymology_1", "Etymology_2", etc. when a word has multiple etymologies.
# MediaWiki's rendered HTML looks like either:
#   <div class="mw-heading mw-heading3"><h3 id="Etymology">Etymology</h3>...
# or the older/mobile form:
#   <h3><span id="Etymology" class="mw-headline">Etymology</span></h3>
# We capture the heading-level digit so we know what terminates the section.
_ETY_HEADING_RE = re.compile(
    r'<h(?P<level>[2-6])[^>]*\sid="Etymology[^"]*"[^>]*>|'
    r'<span[^>]*\sid="Etymology[^"]*"[^>]*>',
    re.IGNORECASE,
)


class _HtmlStripper(_HTMLParser):
    """Collect text content from HTML, suppressing noisy subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        # Stack of booleans -- True means "inside a subtree we're skipping".
        self._skip_stack: list[bool] = []

    def _should_skip(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> bool:
        if tag in _SKIP_TAGS:
            return True
        attrs_d = {k: (v or "") for k, v in attrs}
        cls = attrs_d.get("class", "")
        # Skip footnote markers, "reference" spans, audio players, edit links.
        if "reference" in cls or "mw-editsection" in cls or "audio" in cls:
            return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        parent_skipping = any(self._skip_stack)
        self._skip_stack.append(parent_skipping or self._should_skip(tag, attrs))

    def handle_endtag(self, tag):
        if self._skip_stack:
            self._skip_stack.pop()

    def handle_data(self, data):
        if not any(self._skip_stack):
            self.chunks.append(data)

    def text(self) -> str:
        return "".join(self.chunks)


def _find_etymology_section(sections: List[Dict[str, Any]]) -> Optional[int]:
    """Pick the first Etymology subsection (under a language heading).

    `sections` comes from the MediaWiki API: each is a dict with keys
    'line' (display text), 'toclevel' (1 = language header, 2+ = subsections),
    'index' (section id for the next API call).
    """
    for s in sections:
        line = (s.get("line") or "").strip()
        level = s.get("toclevel", 0)
        # Skip the top-level language headers themselves. Pick the first
        # subsection whose title starts with "Etymology".
        if level > 1 and line.lower().startswith("etymology"):
            idx = s.get("index")
            if idx:
                return idx
    return None


def _slice_etymology_html(html: str) -> Optional[str]:
    """Extract the Etymology subtree from a full Wiktionary HTML page.

    We look for the first heading whose id starts with "Etymology", then
    take everything up to the next heading of same-or-higher level. This
    is more reliable than MediaWiki's section API, which occasionally
    returns content spilling past the etymology (seen on "internet").
    """
    m = _ETY_HEADING_RE.search(html)
    if not m:
        return None

    # Heading element we matched (<h3...> or <span...>). If it was a span,
    # find the enclosing <h?> to know the heading level.
    level_group = m.group("level")
    if level_group:
        level = int(level_group)
    else:
        # Fallback: span case. Look backwards for the <h?> that contains it.
        span_start = m.start()
        lookback = html[max(0, span_start - 200):span_start]
        lm = re.search(r"<h([2-6])[^>]*>$", lookback)
        level = int(lm.group(1)) if lm else 3

    # Content starts at the end of the containing heading element. Find the
    # closing </hN> right after our match, then go from there.
    # Search for </hN> after the match start.
    close_re = re.compile(rf"</h{level}>", re.IGNORECASE)
    after_heading = close_re.search(html, m.start())
    content_start = after_heading.end() if after_heading else m.end()

    # Terminate at the next heading of same-or-higher level (h2, h3... up to
    # and including `level`).
    levels_pat = "".join(str(i) for i in range(2, level + 1))
    end_re = re.compile(rf"<h[{levels_pat}]\b", re.IGNORECASE)
    end_match = end_re.search(html, content_start)
    end = end_match.start() if end_match else len(html)

    return html[content_start:end]


def fetch_etymology(word: str, timeout: float = 3.0) -> Optional[str]:
    """Fetch the Etymology section for `word` from Wiktionary as plain text.

    Returns None if Wiktionary has no entry, no Etymology section, or
    the fetch fails/times out. Cached per-process so repeat queries for
    the same word don't hit Wiktionary twice.
    """
    w = word.strip().lower()
    with _ETYMOLOGY_CACHE_LOCK:
        if w in _ETYMOLOGY_CACHE:
            return _ETYMOLOGY_CACHE[w]

    result: Optional[str] = None
    try:
        # One fetch: full page HTML. We slice the Etymology subtree from
        # it ourselves, which is more accurate than the section API for
        # weird entries (short stubs, alternative-form pages).
        url = (
            f"{_WIKTIONARY_API}?action=parse&page={urllib.parse.quote(w)}"
            "&prop=text&format=json&formatversion=2&disabletoc=true"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Graphosaurus/1.0 (graphosaurus.com)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        html = data.get("parse", {}).get("text", "")
        if html:
            section_html = _slice_etymology_html(html)
            if section_html:
                stripper = _HtmlStripper()
                stripper.feed(section_html)
                text = stripper.text()
                # Collapse whitespace but preserve paragraph breaks.
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = text.strip()
                # Drop the literal "Etymology" word if it slipped in.
                text = re.sub(r"^\s*Etymology\s*\d*\s*\[edit\]?\s*\n?", "", text)
                text = text.strip()
                if len(text) >= 10:
                    result = text
    except Exception as e:  # noqa: BLE001
        log.info("etymology fetch failed for %r: %s", w, e)
        result = None

    with _ETYMOLOGY_CACHE_LOCK:
        _ETYMOLOGY_CACHE[w] = result
    return result


def word_response(
    definitions: Dict[str, List[Dict[str, str]]],
    dictionary: Set[str],
    word: str,
    *,
    fetch_ety: bool = True,
) -> Tuple[int, Dict[str, Any]]:
    """Return the metadata for a single word: defs + etymology + links.

    Kept as a pure function over inputs for testability. `fetch_ety` can be
    set False in tests to avoid network calls.
    """
    w = (word or "").strip().lower()
    if not w:
        return 400, {"error": "word is required"}
    if w not in dictionary:
        return 404, {"error": "word not in dictionary", "word": w}

    body: Dict[str, Any] = {
        "word": w,
        "definitions": definitions.get(w, []),
        "wiktionary_url": f"https://en.wiktionary.org/wiki/{urllib.parse.quote(w)}",
    }
    if fetch_ety:
        body["etymology"] = fetch_etymology(w)
    return 200, body


# --- Request handler -------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # Keep access logs on our logger (default goes to stderr directly).
    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            path = parsed.path

            if path == "/health":
                return self._json(200, {"ok": True})
            if path == "/api/search":
                q = (qs.get("q") or [""])[0]
                try:
                    limit = int((qs.get("limit") or ["10"])[0])
                except ValueError:
                    limit = 10
                matches = search_words(_STATE["words_sorted"], q, limit)
                return self._json(200, {"query": q.strip().lower(), "matches": matches})
            if path == "/api/neighborhood":
                word = (qs.get("word") or [""])[0]
                depth_raw = (qs.get("depth") or [None])[0]
                status, body = neighborhood_response(
                    _STATE["thesaurus"],
                    _STATE["dictionary"],
                    word,
                    depth_raw,
                    _STATE["max_depth"],
                )
                return self._json(status, body)
            if path == "/api/word":
                word = (qs.get("word") or [""])[0]
                status, body = word_response(
                    _STATE["definitions"],
                    _STATE["dictionary"],
                    word,
                )
                return self._json(status, body)
            if path in ("/", "/index.html"):
                return self._static("index.html")
            # Whitelisted static assets only (no directory traversal).
            rel = path.lstrip("/")
            if rel in _STATIC_WHITELIST:
                return self._static(rel)

            return self._json(404, {"error": "not found", "path": path})
        except Exception as e:  # noqa: BLE001 -- catch-all for the request
            log.exception("handler error")
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # --- Response helpers ---

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Loose CORS for now -- frontend is same-origin in dev, but this also
        # lets you hit the API from file:// during UI hacking.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        fp = _STATIC_DIR / name
        if not fp.exists():
            return self._json(404, {"error": "not found", "asset": name})
        data = fp.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(fp.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# --- Entry point -----------------------------------------------------------

def _load_state(dict_path: Path, thes_path: Path, max_depth: int) -> Dict[str, Any]:
    raw_words = _load_dictionary(dict_path)
    raw_thes = _load_thesaurus(thes_path)
    thesaurus, dictionary = normalize_inputs(raw_thes, raw_words)

    # Definitions are optional -- fall back to empty dict if the file isn't
    # there (e.g. sample_data/ dev loop where WordNet hasn't been built).
    defs_path_env = os.environ.get("PG_DEFINITIONS", "")
    definitions: Dict[str, List[Dict[str, str]]] = {}
    if defs_path_env:
        p = Path(defs_path_env)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                definitions = json.load(f)
            log.info("loaded %d words with definitions from %s", len(definitions), p)
        else:
            log.warning("PG_DEFINITIONS=%s not found; /api/word will return empty defs", p)

    return {
        "thesaurus": thesaurus,
        "dictionary": dictionary,
        "words_sorted": sorted(dictionary),
        "definitions": definitions,
        "max_depth": max_depth,
    }


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("PG_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dict_path = Path(os.environ.get("PG_DICTIONARY", "sample_data/dictionary.txt"))
    thes_path = Path(os.environ.get("PG_THESAURUS", "sample_data/thesaurus.json"))
    host = os.environ.get("PG_HOST", "0.0.0.0")
    port = int(os.environ.get("PG_PORT", "8000"))
    max_depth = int(os.environ.get("PG_MAX_DEPTH", "5"))

    log.info("loading thesaurus: dict=%s thesaurus=%s", dict_path, thes_path)
    _STATE.update(_load_state(dict_path, thes_path, max_depth))
    log.info(
        "loaded %d words, %d thesaurus entries, %d with definitions, max_depth=%d",
        len(_STATE["dictionary"]),
        len(_STATE["thesaurus"]),
        len(_STATE["definitions"]),
        max_depth,
    )

    server = ThreadingHTTPServer((host, port), Handler)

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("received signal %d, shutting down", signum)
        # server.shutdown() must be called from another thread; use a daemon.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("serving on http://%s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("server closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
