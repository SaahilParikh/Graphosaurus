"""Bootstrap stack.

Run once, locally, by a human with admin creds on the target AWS account.
Everything in here is idempotent but not expected to change often:

1. Route53 public hosted zone for the domain.
2. ACM certificate for the domain (+ www subdomain), DNS-validated via the
   zone. Will hang on validation until the domain's NS records at its
   registrar are pointed at this zone -- see BOOTSTRAP.md.
3. GitHub OIDC provider and an IAM role the CI pipeline assumes. No
   long-lived AWS access keys live in GitHub.
4. SSM parameters exposing zone id + cert arn so the app stack can pick
   them up without in-app cross-stack references.

Note on NS delegation
---------------------
Earlier versions of this stack tried to automate `route53domains:
UpdateDomainNameservers`. That only works if the domain is registered in
the SAME account this stack deploys into. In our case, parikhsaahil.com is
registered in a different account (or with a non-Route53Domains registrar),
so the API call fails with "Domain not found in account".

The NS delegation step is therefore documented as a manual action in
BOOTSTRAP.md: after `cdk deploy PythonGraphsBootstrap` creates the zone,
read the 4 NS records it emits as CfnOutput, then update them at whichever
registrar owns the domain.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Fn,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_iam as iam,
    aws_route53 as route53,
    aws_ssm as ssm,
)
from constructs import Construct


# SSM parameter names are the stable contract between the two stacks.
# Changing these requires a coordinated bootstrap + app redeploy.
SSM_ZONE_ID = "/pythongraphs/route53/zone-id"
SSM_CERT_ARN = "/pythongraphs/acm/certificate-arn"
SSM_CI_ROLE_ARN = "/pythongraphs/iam/ci-role-arn"


class BootstrapStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        github_owner: str,
        github_repo: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Route53 hosted zone ------------------------------------------
        # RETAIN: if we ever `cdk destroy` this stack by accident, we don't
        # want to blow away DNS and break everything pointing at this zone.
        zone = route53.PublicHostedZone(
            self,
            "Zone",
            zone_name=domain_name,
            comment=f"Managed by PythonGraphsBootstrap for {domain_name}",
        )
        zone.apply_removal_policy(RemovalPolicy.RETAIN)

        # --- ACM certificate ----------------------------------------------
        # Covers apex (parikhsaahil.com) + www. Add more SANs here if we ever
        # serve other subdomains. DNS-validated so no email wrangling.
        #
        # IMPORTANT: this resource will sit in CREATE_IN_PROGRESS until the
        # domain's NS records at its registrar point at this zone. See
        # BOOTSTRAP.md step 5. Typical validation takes 1-5 min once NS is
        # right; can be longer the first time as DNS propagates.
        certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=domain_name,
            subject_alternative_names=[f"www.{domain_name}"],
            validation=acm.CertificateValidation.from_dns(zone),
        )

        # --- GitHub OIDC --------------------------------------------------
        # One OIDC provider per account is reused by all repos. This construct
        # is idempotent but if you already have one in the account, import
        # it with from_open_id_connect_provider_arn(...) instead.
        github_oidc = iam.OpenIdConnectProvider(
            self,
            "GithubOidc",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
            # Thumbprint is AWS's documented constant for the GitHub OIDC
            # provider. It's rotated rarely; check the AWS docs if token
            # validation ever fails suddenly.
            thumbprints=["6938fd4d98bab03faadb97b34396831e3780aea1"],
        )

        # IAM role CI assumes. Locked down to this specific repo's main
        # branch -- forks or other branches can't use it to deploy.
        ci_role = iam.Role(
            self,
            "GithubDeployRole",
            role_name="PythonGraphsGithubDeployRole",
            assumed_by=iam.FederatedPrincipal(
                github_oidc.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    },
                    "StringLike": {
                        # <owner>/<repo>:ref:refs/heads/main  limits it to
                        # main. Widen to `:*` for all branches/PRs if wanted.
                        "token.actions.githubusercontent.com:sub":
                            f"repo:{github_owner}/{github_repo}:ref:refs/heads/main",
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description=f"Assumed by GitHub Actions for {github_owner}/{github_repo} to deploy PythonGraphsApp",
        )

        # The role needs to be able to deploy the app stack via CDK. The
        # cleanest way is to attach the standard CDK deploy role permissions.
        # For a personal project we'll just grant the CDK-published roles
        # (cdk-*-deploy-role-*, cdk-*-file-publishing-role-*, etc.) via
        # assume, which CDK v2 uses to do the actual work.
        ci_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[
                    # The CDK bootstrap stack creates these with known name
                    # patterns. We allow assuming any of them in this account.
                    f"arn:aws:iam::{self.account}:role/cdk-*",
                ],
            )
        )
        # Also allow direct reads of SSM parameters used for cross-stack data,
        # so the pipeline can print them for debugging.
        ci_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/pythongraphs/*",
                ],
            )
        )

        # --- SSM exports for the app stack --------------------------------
        ssm.StringParameter(
            self,
            "ZoneIdParam",
            parameter_name=SSM_ZONE_ID,
            string_value=zone.hosted_zone_id,
            description="Route53 hosted zone id for the app domain",
        )
        ssm.StringParameter(
            self,
            "CertArnParam",
            parameter_name=SSM_CERT_ARN,
            string_value=certificate.certificate_arn,
            description="ACM certificate ARN for the app domain",
        )
        ssm.StringParameter(
            self,
            "CiRoleArnParam",
            parameter_name=SSM_CI_ROLE_ARN,
            string_value=ci_role.role_arn,
            description="IAM role ARN assumed by GitHub Actions CI",
        )

        # --- Human-readable outputs ---------------------------------------
        # The NS values here are the critical output: you MUST copy these
        # into whichever registrar owns parikhsaahil.com so ACM can validate
        # the certificate. See BOOTSTRAP.md step 5.
        CfnOutput(self, "DomainName", value=domain_name)
        CfnOutput(self, "ZoneId", value=zone.hosted_zone_id)
        CfnOutput(
            self,
            "NameServers",
            value=Fn.join(",", zone.hosted_zone_name_servers or []),
            description=(
                "MANUAL: copy these 4 NS records into the domain's registrar "
                "(NS records, not glue). ACM cert is blocked until done."
            ),
        )
        CfnOutput(self, "CertificateArn", value=certificate.certificate_arn)
        CfnOutput(
            self,
            "CiRoleArn",
            value=ci_role.role_arn,
            description="Matches the ARN hardcoded in .github/workflows/deploy.yml",
        )
