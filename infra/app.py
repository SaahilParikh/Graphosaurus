#!/usr/bin/env python3
"""CDK app entry. Two stacks:

- PythonGraphsBootstrap: run ONCE locally by a human with admin creds.
    Creates the Route53 zone, ACM cert, GitHub OIDC trust, CI role, and
    delegates NS records at the registrar. Re-running is idempotent but
    unnecessary.

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
DOMAIN_NAME = "parikhsaahil.com"
GITHUB_OWNER = "SaahilParikh"
GITHUB_REPO = "PythonGraphs"

# Personal Isengard account. Region is us-east-1 because:
#   1) CloudFront viewer certs *must* be in us-east-1
#   2) Route53 Domains APIs are only in us-east-1
# So putting both stacks there avoids cross-region coordination.
AWS_ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT", "***AWS_ACCOUNT_ID_LEGACY***")
AWS_REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

env = Environment(account=AWS_ACCOUNT, region=AWS_REGION)

app = App()

BootstrapStack(
    app,
    "PythonGraphsBootstrap",
    env=env,
    domain_name=DOMAIN_NAME,
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
