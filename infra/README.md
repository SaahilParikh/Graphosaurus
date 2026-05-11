# Infrastructure (planned)

This directory is a placeholder for the deployment IaC. The application code
is written to be deployable today; the IaC to actually deploy it will land
here when we're ready to publish.

## Deployment phases

### Phase 1 — local Docker (current)

`./run.sh serve` runs the whole stack on `http://localhost:8000` via Docker.
No AWS involved. The server is 12-factor-compliant — config via env vars,
stateless, logs to stdout, `/health` endpoint — so moving it to any runtime
is a config change, not a code change.

### Phase 2 — static pre-generated graphs on S3 + CloudFront

Cheapest path to public hosting. On deploy, run `main.py` on real data
(WordNet), upload `out/*.json` to an S3 bucket, serve the `web/` frontend
from the same or a sibling bucket behind CloudFront. No always-on compute.

Tradeoff: depth is fixed at generation time. Good enough if we pick one
sensible default (likely depth=2).

```
  Users
    |
    v
  CloudFront
    |     \
    |      \
    v       v
  S3 (web/)  S3 (graphs/{word}.json)
```

Regeneration = one CI job: run the pipeline, sync to S3, invalidate the
relevant CloudFront paths.

### Phase 3 — dynamic API on Lambda (optional)

If the depth slider is important in production (not just dev), we keep the
Python server but run it serverless. API Gateway in front of a Lambda that
bundles the thesaurus. The static frontend is still Phase-2-style on S3/CF.

```
  Users -> CloudFront -> S3 (web/)
                      -> API Gateway -> Lambda (server.py) -> [thesaurus in bundle]
```

Lambda cold start is the main concern; thesaurus up to ~500MB fits in a
Lambda package or comes from S3 at init time.

## Tooling choice

AWS CDK (Python) when we get there. Keeps language consistent with the app,
has good ergonomics for stacks like S3 + CloudFront + Lambda + API GW.
Deploy target: personal Isengard account.

## What NOT to do

- Do not use S3 as a key-value lookup backend for an on-demand API server.
  S3 GET latency is 50-200ms; the in-memory dict we have today does lookups
  in microseconds. If we outgrow RAM on one box, move to DynamoDB or LMDB,
  not S3 per-key.
- Do not put the whole thesaurus in a single S3 object and fetch it on every
  request. That defeats the "lazy" part. Per-word files (Phase 2) or
  in-memory on the server (Phase 1/3) are the two real options.
- Do not add a graph DB (Neo4j/Neptune) until we've actually outgrown an
  in-memory dict, which we won't at WordNet scale.
