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

_ETY_RE = re.compile(
    r"={2,}\s*Etymology[^=]*={2,}\s*\n(.*?)(?=\n={2,}|\Z)", re.DOTALL
)
_TEMPLATE_RE = re.compile(r"\{\{([^{}]*(?:\{\{[^{}]*\}\}[^{}]*)*)\}\}")
_WIKILINK_PIPE_RE = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_wikitext(text: str) -> str:
    """Rough wikitext -> plain text. Not perfect; good enough for etymology."""
    text = _REF_RE.sub("", text)
    # Remove templates (repeat to handle nested ones that first pass skipped)
    for _ in range(3):
        text = _TEMPLATE_RE.sub("", text)
    text = _WIKILINK_PIPE_RE.sub(r"\1", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_etymology(word: str, timeout: float = 3.0) -> Optional[str]:
    """Fetch the Etymology section for `word` from Wiktionary wikitext.

    Returns a plain-text etymology paragraph, or None if Wiktionary has no
    entry or no etymology section. Cached per-process; safe to call repeatedly.
    Network failures return None silently -- never blocks the UI.
    """
    w = word.strip().lower()
    with _ETYMOLOGY_CACHE_LOCK:
        if w in _ETYMOLOGY_CACHE:
            return _ETYMOLOGY_CACHE[w]

    result: Optional[str] = None
    try:
        url = (
            "https://en.wiktionary.org/w/api.php?"
            f"action=parse&page={urllib.parse.quote(w)}"
            "&prop=wikitext&format=json&formatversion=2"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Graphosaurus/1.0 (graphosaurus.com)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        wikitext = data.get("parse", {}).get("wikitext", "")
        if wikitext:
            m = _ETY_RE.search(wikitext)
            if m:
                result = _clean_wikitext(m.group(1))
                # Some pages have an empty etymology section or one that
                # just points elsewhere. Drop if trivially short.
                if result and len(result) < 10:
                    result = None
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
