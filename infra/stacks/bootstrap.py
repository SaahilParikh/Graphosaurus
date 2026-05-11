"""Bootstrap stack.

Run once, locally, by a human with admin creds on the target AWS account.
Everything in here is idempotent but not expected to change often:

1. Imports the existing Route53 public hosted zone for the domain. Route53
   Registrar auto-creates the zone when the domain is registered through
   Amazon, so we reuse it rather than creating a duplicate.
2. ACM certificate for the domain (+ www subdomain), DNS-validated via the
   imported zone. Because the zone is already the authoritative NS for the
   domain (set by the registrar at creation), validation succeeds within
   a couple of minutes.
3. GitHub OIDC provider and an IAM role the CI pipeline assumes. No
   long-lived AWS access keys live in GitHub.
4. SSM parameters exposing zone id + cert arn so the app stack can pick
   them up without in-app cross-stack references.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_certificatemanager as acm,
    aws_iam as iam,
    aws_route53 as route53,
    aws_ssm as ssm,
)
from constructs import Construct


# SSM parameter names are the stable contract between the two stacks.
# Changing these requires a coordinated bootstrap + app redeploy.
SSM_ZONE_ID = "/graphosaurus/route53/zone-id"
SSM_CERT_ARN = "/graphosaurus/acm/certificate-arn"
SSM_CI_ROLE_ARN = "/graphosaurus/iam/ci-role-arn"


class BootstrapStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        hosted_zone_id: str,
        github_owner: str,
        github_repo: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Import the existing Route53 hosted zone ---------------------
        # Registering graphosaurus.com through Route53 Registrar already
        # created a zone and pointed the domain's NS records at it. Using
        # from_hosted_zone_attributes means CDK does NOT try to create or
        # delete the zone -- it's externally managed.
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "Zone",
            hosted_zone_id=hosted_zone_id,
            zone_name=domain_name,
        )

        # --- ACM certificate ----------------------------------------------
        # Covers apex + www. DNS-validated through the imported zone.
        # No manual NS step needed: the registrar already delegated the
        # domain to this zone, so ACM's DNS probes resolve correctly.
        certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=domain_name,
            subject_alternative_names=[f"www.{domain_name}"],
            validation=acm.CertificateValidation.from_dns(zone),
        )

        # --- GitHub OIDC --------------------------------------------------
        # The provider is a per-account singleton. Assuming it was created
        # by a previous CDK deploy or another project, we import it. If no
        # provider exists yet, create one via the IAM console or:
        #   aws iam create-open-id-connect-provider \
        #     --url https://token.actions.githubusercontent.com \
        #     --client-id-list sts.amazonaws.com \
        #     --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
        github_oidc = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self,
            "GithubOidc",
            open_id_connect_provider_arn=(
                f"arn:aws:iam::{self.account}:oidc-provider/"
                f"token.actions.githubusercontent.com"
            ),
        )

        # IAM role CI assumes. Locked to this repo's main branch; forks or
        # other branches can't use it to deploy.
        ci_role = iam.Role(
            self,
            "GithubDeployRole",
            role_name="GraphosaurusGithubDeployRole",
            assumed_by=iam.FederatedPrincipal(
                github_oidc.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    },
                    "StringLike": {
                        "token.actions.githubusercontent.com:sub":
                            f"repo:{github_owner}/{github_repo}:ref:refs/heads/main",
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description=f"Assumed by GitHub Actions for {github_owner}/{github_repo} to deploy Graphosaurus app stack",
        )

        # CDK v2 uses a set of assume-able roles created by `cdk bootstrap`
        # (cdk-*-deploy-role-*, cdk-*-file-publishing-role-*, etc.) to do
        # the actual deploy work. Let the CI role assume any of them.
        ci_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/cdk-*",
                ],
            )
        )
        # SSM reads for debugging / output reference in CI logs.
        ci_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/graphosaurus/*",
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

        # --- Outputs ------------------------------------------------------
        CfnOutput(self, "DomainName", value=domain_name)
        CfnOutput(self, "ZoneId", value=zone.hosted_zone_id)
        CfnOutput(self, "CertificateArn", value=certificate.certificate_arn)
        CfnOutput(
            self,
            "CiRoleArn",
            value=ci_role.role_arn,
            description="Matches the ARN hardcoded in .github/workflows/deploy.yml",
        )
