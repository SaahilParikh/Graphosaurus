#!/usr/bin/env bash
# Docker convenience wrapper. Pure bash, no `make` needed.
# Usage: ./run.sh <command>  (run with no args to see the list).

set -euo pipefail

IMAGE="pythongraphs"

build() {
  docker build -t "$IMAGE" .
}

# Build only if the image doesn't exist yet. Use `./run.sh build` to force a
# rebuild after changing Dockerfile or dependencies. For everyday code edits
# with `./run.sh shell`, the bind mount picks up changes with no rebuild.
ensure_built() {
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    build
  fi
}

test_() {  # `test` is a bash builtin; avoid shadowing it.
  ensure_built
  docker run --rm "$IMAGE" pytest -q
}

graphs() {
  ensure_built
  mkdir -p out
  docker run --rm -v "$PWD/out:/app/out" "$IMAGE" \
    python main.py \
      --dict sample_data/dictionary.txt \
      --thesaurus sample_data/thesaurus.json \
      --out out \
      --depth 2 --workers 4
}

components() {
  ensure_built
  mkdir -p out
  docker run --rm -v "$PWD/out:/app/out" "$IMAGE" \
    python find_components.py \
      --dict sample_data/dictionary.txt \
      --thesaurus sample_data/thesaurus.json \
      --out out/components.json
}

bench() {
  ensure_built
  docker run --rm "$IMAGE" python bench.py
}

serve() {
  ensure_built
  # Publish container 8000 -> host 8000. Foreground; Ctrl-C to stop.
  # Bind-mount the project so you can edit web/* and restart without rebuilding.
  echo "Serving at http://localhost:8000 (Ctrl-C to stop)"
  docker run --rm -it \
    -p 8000:8000 \
    -v "$PWD:/app" \
    "$IMAGE" python server.py
}

shell_() {  # avoid shadowing `shell` convention
  ensure_built
  docker run --rm -it -v "$PWD:/app" "$IMAGE" bash
}

clean() {
  rm -rf out __pycache__ .pytest_cache
}

help_() {
  cat <<'EOF'
Usage: ./run.sh <command>

Commands:
  build       Build the Docker image.
  test        Run pytest inside the container (25 tests).
  graphs      Run per-word ego-graph pipeline on sample_data/ -> ./out/.
  components  Run connected-components analysis on sample_data/ -> ./out/components.json.
  bench       Run the synthetic scaling benchmark (slow: ~3 minutes).
  serve       Run the HTTP server on http://localhost:8000 (project bind-mounted).
  shell       Interactive bash inside the container; project bind-mounted.
  clean       Remove generated artifacts on the host (out/, __pycache__, .pytest_cache).
  help        Show this message.
EOF
}

# Dispatch. Aliases map pretty names to the _ suffixed functions where needed.
cmd="${1:-help}"
case "$cmd" in
  test)  shift; test_  "$@" ;;
  shell) shift; shell_ "$@" ;;
  help|-h|--help) help_ ;;
  build|graphs|components|bench|serve|clean)
    shift; "$cmd" "$@" ;;
  *)
    echo "Unknown command: $cmd" >&2
    echo >&2
    help_ >&2
    exit 2 ;;
esac
