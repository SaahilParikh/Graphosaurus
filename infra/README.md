# Infrastructure

Two CDK stacks define the public deployment of Graphosaurus.

## `GraphosaurusBootstrap`

Run **once**, by a human with admin credentials. Creates the persistent /
identity resources:

- ACM certificate (DNS-validated via the existing Route53 zone) for the
  apex + www
- Reference to the existing Route53 hosted zone (imported, not created --
  Route53 Registrar auto-creates this on domain registration)
- GitHub OIDC provider reference + `GraphosaurusGithubDeployRole` (trust
  scoped to `<GITHUB_OWNER>/<GITHUB_REPO>@main`, configured in
  `infra/app.py`)

See **[BOOTSTRAP.md](./BOOTSTRAP.md)** for the exact commands.

## `GraphosaurusApp`

Deployed automatically on every push to `main` via
`.github/workflows/deploy.yml`. Produces the runtime:

- A Lambda function running `server.py` inside a container image (Lambda
  Web Adapter layer translates Lambda events to HTTP)
- A Function URL on that Lambda (IAM auth)
- A CloudFront distribution in front, with OAC signing origin requests and
  our ACM cert
- `A` + `AAAA` alias records for the apex and `www`

The two stacks hand off via SSM parameters under `/pythongraphs/*`.

## Layout

```
infra/
├── app.py                 # CDK app entry: instantiates both stacks
├── cdk.json               # CDK config, feature flags
├── requirements.txt       # Pinned CDK deps
├── BOOTSTRAP.md           # Step-by-step one-time setup
├── README.md              # (this file)
├── lambda/
│   └── Dockerfile         # Lambda container image (server.py + adapter)
└── stacks/
    ├── __init__.py
    ├── bootstrap.py       # GraphosaurusBootstrap stack
    └── app.py             # GraphosaurusApp stack
```

## Why container Lambda instead of zip

Python code + deps would fit in the 250 MB unzipped Lambda limit, but the
container path has three advantages for this project:

1. We get to reuse our existing dev `Dockerfile` layout with minimal
   changes (`infra/lambda/Dockerfile` adds only the Web Adapter copy).
2. `server.py` runs **unchanged** -- no `mangum`, no rewrite to a handler
   function. The Web Adapter forwards Lambda events to whatever HTTP
   server you're running on `$PORT`. One code path for local, Docker, and
   Lambda.
3. CDK's `DockerImageAsset` handles the ECR lifecycle for us: builds,
   tags, pushes, and wires the tag into the Lambda function resource.

Cold starts for a ~150 MB image at 1024 MB memory are typically 1-3s.
Subsequent warm invocations are <50 ms. CloudFront caches the static
frontend aggressively, so most visitors never hit a cold Lambda.

## Why two stacks, not one

Blast-radius isolation. The CI role is scoped so it can only update the
app stack -- it has no permission to delete the hosted zone, the cert, or
itself. A compromised GitHub token can take down the site for ~5 minutes
(bad deploy) but can't permanently hijack DNS or cert.

## Cost estimate (ballpark)

At trivial personal traffic:

- Route53 hosted zone: **$0.50/mo**
- Route53 queries: ~$0 (first 1B queries nearly free)
- CloudFront: ~$0 within the free tier (1 TB / 10M requests / month)
- Lambda: ~$0 within the free tier (1M requests + 400,000 GB-seconds)
- ACM cert: **$0**
- ECR storage: ~$0 (few MB)
- Data transfer: trivial

**Expected total: $0.50 - $2 / month** depending on traffic.

## See also

- [../README.md](../README.md) -- main project README
- [BOOTSTRAP.md](./BOOTSTRAP.md) -- one-time setup instructions
- [../.github/workflows/deploy.yml](../.github/workflows/deploy.yml) -- CI pipeline
