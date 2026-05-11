#!/usr/bin/env python3
"""CDK app entry. Two stacks:

- PythonGraphsBootstrap: run ONCE locally by a human with admin creds.
    Imports the existing Route53 zone, creates ACM cert, GitHub OIDC trust,
    and CI role. Re-running is idempotent but unnecessary.

- PythonGraphsApp: deployed by CI on every push to main. Creates the
    Lambda, Function URL, CloudFront distribution, and alias records.
    Looks up the zone + cert that Bootstrap created via SSM parameters.

Keeping them separate means CI only needs permissions for the app stack --
which cannot escalate into the bootstrap resources (zone, OIDC provider,
CI role itself).

All account-specific values (account id, hosted zone id) come from env vars
so the code can live in a public repo without leaking infrastructure ids.
See infra/BOOTSTRAP.md for how to set them locally and in GitHub Actions.

NOTE: CloudFormation stack names are kept as PythonGraphs* for historical
reasons. Renaming them requires delete + recreate, which takes the live
site down and cycles the CloudFront domain. Left as-is intentionally.
"""
from __future__ import annotations

import os

from aws_cdk import App, Environment

from stacks.bootstrap import BootstrapStack
from stacks.app import AppStack


# Public-ish config: the domain and the GitHub repo identity. These are
# already visible in live DNS / in the repo URL, so no point "hiding" them.
DOMAIN_NAME = "graphosaurus.com"
GITHUB_OWNER = "SaahilParikh"
GITHUB_REPO = "Graphosaurus"


def _require(name: str) -> str:
    """Fail fast with a clear message if a required env var is missing."""
    v = os.environ.get(name)
    if not v:
        raise SystemExit(
            f"error: environment variable {name} is required. "
            "See infra/BOOTSTRAP.md."
        )
    return v


# Account-specific. Kept out of source so the repo doesn't leak them.
#   CDK_DEFAULT_ACCOUNT   -- AWS account id the stacks deploy into
#   PG_HOSTED_ZONE_ID     -- Route53 zone id for DOMAIN_NAME (pre-existing;
#                            we import it, not create it)
AWS_ACCOUNT = _require("CDK_DEFAULT_ACCOUNT")
AWS_REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")
HOSTED_ZONE_ID = _require("PG_HOSTED_ZONE_ID")

env = Environment(account=AWS_ACCOUNT, region=AWS_REGION)

app = App()

BootstrapStack(
    app,
    "PythonGraphsBootstrap",
    env=env,
    domain_name=DOMAIN_NAME,
    hosted_zone_id=HOSTED_ZONE_ID,
    github_owner=GITHUB_OWNER,
    github_repo=GITHUB_REPO,
)

AppStack(
    app,
    "PythonGraphsApp",
    env=env,
    domain_name=DOMAIN_NAME,
)

app.synth()
