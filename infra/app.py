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
"""
from __future__ import annotations

import os

from aws_cdk import App, Environment

from stacks.bootstrap import BootstrapStack
from stacks.app import AppStack


# Single source of truth for these config values. If you fork the project,
# change these (and the account ID) and everything else follows.
DOMAIN_NAME = "graphosaurus.com"
# The existing hosted zone id. Route53 Registrar auto-creates this when a
# domain is registered through Amazon. We import it instead of creating a
# new one to avoid duplicates (and to reuse the NS records the registrar
# already wired up at the domain).
HOSTED_ZONE_ID = "***HOSTED_ZONE_ID***"
GITHUB_OWNER = "SaahilParikh"
GITHUB_REPO = "PythonGraphs"

# Personal AWS account. Region is us-east-1 because CloudFront viewer certs
# must be in us-east-1, so putting both stacks there avoids cross-region
# coordination.
AWS_ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT", "***AWS_ACCOUNT_ID***")
AWS_REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

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
