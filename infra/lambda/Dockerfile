# Multi-stage Lambda image.
#
# Stage 1 (builder): installs nltk, downloads WordNet once, runs our
# build_wordbank.py to produce data/thesaurus.json + data/dictionary.txt.
# This stage is cache-friendly: unless nltk version, WordNet version, or
# build_wordbank.py changes, Docker reuses the cached layer and skips
# re-download/re-generation.
#
# Stage 2 (runtime): slim Python image + Lambda Web Adapter + our server
# code + the generated data. No nltk in the runtime image, keeping cold
# start fast.

# ---------- Stage 1: build the wordbank ----------
FROM python:3.12-slim AS builder

# NLTK + its WordNet corpus. Version-pinned for reproducibility.
RUN pip install --no-cache-dir nltk==3.8.1

# Download WordNet into a known dir. Cached as its own layer so it only
# redownloads if this RUN line changes.
ENV NLTK_DATA=/opt/nltk_data
RUN python -c "import nltk; nltk.download('wordnet', download_dir='/opt/nltk_data', quiet=True); nltk.download('omw-1.4', download_dir='/opt/nltk_data', quiet=True)"

WORKDIR /build
COPY scripts/build_wordbank.py ./scripts/
RUN python scripts/build_wordbank.py --out /build/data

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim

# AWS Lambda Web Adapter: intercepts Lambda events and forwards them as
# HTTP requests to the server running on $PORT.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 \
     /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8000

WORKDIR /app

# App code -- runtime stdlib-only, no pip installs needed.
COPY graph_builder.py main.py components.py find_components.py server.py ./
COPY web/ ./web/

# Bring in the generated wordbank from the builder stage.
COPY --from=builder /build/data ./data

# Server config: point at the WordNet-derived data.
ENV PG_DICTIONARY=/app/data/dictionary.txt
ENV PG_THESAURUS=/app/data/thesaurus.json
ENV PG_DEFINITIONS=/app/data/definitions.json
ENV PG_HOST=0.0.0.0
ENV PG_PORT=8000
# With ~150K words, the loaded dict is ~100 MB; BFS stays fast but cold
# start is a couple of seconds. Bump Lambda memory if this feels slow.

CMD ["python", "server.py"]
