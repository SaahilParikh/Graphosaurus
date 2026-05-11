# PythonGraphs

Tiny, embarrassingly-parallel program that turns a **dictionary + thesaurus**
into one graph per word. For each word in the dictionary it builds an *ego
graph*: a BFS traversal through thesaurus relationships, up to a configurable
depth, filtered to words that exist in the dictionary.

## Files

| File | Role |
|------|------|
| `graph_builder.py` | Pure graph-building logic. `build_graph(word, thesaurus, dictionary, depth)` returns a plain dict. No I/O, no globals — safe to ship to a worker process. |
| `main.py` | CLI + `ProcessPoolExecutor` driver. Loads inputs, fans out one task per word, writes one JSON file per graph. |
| `test_graph_builder.py` | Pytest suite covering depth semantics, filtering, edge cases. |
| `sample_data/` | `dictionary.txt` + `thesaurus.json` — small enough to eyeball, big enough to be interesting. |

## Input formats

**`dictionary.txt`** — one word per line. Blank lines and `# comments` are ignored.

```
happy
joyful
cheerful
```

**`thesaurus.json`** — JSON object mapping a word to a list of synonyms.

```json
{
  "happy":   ["joyful", "cheerful", "glad"],
  "joyful":  ["happy", "cheerful"],
  "cheerful":["happy", "joyful", "bright"]
}
```

All input is lowercased + stripped once up front by `normalize_inputs`, so
mixed-case input works.

## Output format

One JSON file per word, at `<out_dir>/<word>.json`:

```json
{
  "word": "happy",
  "depth": 2,
  "nodes": ["cheerful", "glad", "happy", "joyful"],
  "edges": [
    ["cheerful", "joyful"],
    ["glad", "happy"],
    ["happy", "joyful"]
  ]
}
```

Edges are undirected (stored as sorted `[a, b]` pairs) and deduplicated.
Everything is sorted for deterministic diffs.

## Usage

### Docker (recommended)

Everything is containerized. Python 3.12 + pytest come with the image; host
needs only Docker.

```bash
./run.sh build        # build the image (one-time, cheap to rebuild)
./run.sh test         # run pytest inside the container
./run.sh graphs       # ego-graph pipeline on sample_data/ -> ./out/
./run.sh components   # connected-components analysis on sample_data/ -> ./out/components.json
./run.sh serve        # HTTP server + web UI on http://localhost:8000
./run.sh shell        # interactive shell in the container, project bind-mounted for edits
./run.sh help         # list all commands
```

Bind mounts: `./run.sh graphs` and `./run.sh components` mount `./out` into
the container so generated JSON ends up on the host, not trapped in the
image. `./run.sh shell` bind-mounts the whole project directory so edits on
the host are live inside the container.

### Native (no Docker)

If you prefer running directly:

```bash
python main.py \
  --dict       sample_data/dictionary.txt \
  --thesaurus  sample_data/thesaurus.json \
  --out        out/ \
  --depth      2 \
  --workers    8
```

Flags:

- `--depth N` — BFS hops from each word. `0` = just the word, `1` = direct
  synonyms, `2` = synonyms-of-synonyms, etc. Default: `1`.
- `--workers N` — parallel worker processes. Default: `os.cpu_count()`.
- `--chunk-size N` — words per task. Default: auto-tuned to ~4 chunks per
  worker. Override only if you're benchmarking or have very uneven per-word
  cost.

## Testing

```bash
pip install pytest
pytest -q
```

## Deployment

Deployed automatically to **[parikhsaahil.com](https://parikhsaahil.com)**
on every push to `main` via GitHub Actions. Two CDK stacks in
[`infra/`](./infra/):

- `PythonGraphsBootstrap` (run once) — Route53 zone, ACM cert, GitHub OIDC
- `PythonGraphsApp` (deployed by CI) — Lambda container + CloudFront +
  Route53 aliases

See [`infra/BOOTSTRAP.md`](./infra/BOOTSTRAP.md) for the one-time setup.
See [`infra/README.md`](./infra/README.md) for the architecture.

## Parallelism rationale

Building one ego graph is fully independent of building another — no shared
mutable state, no cross-word coordination. That makes this a textbook
**embarrassingly parallel** workload.

### Why processes, not threads?

`build_graph` is pure-Python CPU work (dict lookups, set ops, BFS). Under the
GIL, threads would serialize instead of running concurrently. Processes
sidestep the GIL.

### Why chunks, not one task per word?

A single `build_graph` call at depth 1-2 takes well under a millisecond.
`ProcessPoolExecutor`'s per-task overhead (pickle args, enqueue, pickle result
back) is ~50-200µs. At one-word-per-task, **IPC dominates** and parallelism
can even hurt — we measured a 14-worker run running *slower* than a 1-worker
run.

Chunking fixes it: each task processes a batch of words. The worker builds
the graphs AND writes the JSON files itself, so only a small `(count,
errors)` summary crosses the process boundary. Default chunk size targets ~4
chunks per worker, which bounds load imbalance to roughly 25% of total time.

Measured speedup on a 14-core laptop (avg 20 synonyms/word):

| n_words | depth | 1 worker | 14 workers | speedup |
|--------:|------:|---------:|-----------:|--------:|
|  10,000 |   2   |   7.9s   |    2.0s    |  4.0x   |
|  10,000 |   3   | 105s (before chunking) | 16s | 6.4x |
| 100,000 |   2   |    —     |   19s      |   —     |

Sub-linear vs 14x ideal, because JSON encoding + file I/O still serializes
somewhat and the pool startup cost (~0.5s) is unavoidable.

### Why `initializer=` instead of passing inputs per task?

The thesaurus + dictionary are read-only and potentially large. Pickling them
into every submitted task is wasteful. `ProcessPoolExecutor(initializer=...)`
runs a setup function **once per worker** that stashes the data in module
globals; subsequent tasks only ship the chunk of words. Standard "broadcast a
big read-only blob" pattern.

### Why BFS with `visited` but still adding edges unconditionally?

We only expand a word once (via `visited`), but we still add the edge when we
re-encounter a neighbor via a different path. That way the graph faithfully
represents every thesaurus relationship in-scope, without doing redundant
expansion work.

### Failure isolation

A malformed thesaurus entry for one word shouldn't kill the whole batch.
Worker exceptions are caught per-future in `main.py` and logged to stderr;
the rest of the words keep going.
