"""App stack.

Deployed by CI on every push to main. Produces the runtime resources:

- ECR image asset (built from infra/lambda/Dockerfile, pushed to the
  CDK bootstrap ECR repo).
- Lambda function that runs the image. Wired up with a Function URL.
- CloudFront distribution that sits in front of the Function URL, adds
  TLS via the ACM cert, and aliases our domain.
- Route53 A + AAAA alias records pointing apex + www at CloudFront.

No bootstrap-y resources here: no hosted zone, no OIDC, no cert. Those
are in BootstrapStack and referenced via SSM parameters. That means CI
can redeploy this stack without having permission to touch the persistent
identity/DNS infra.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_ecr_assets as ecr_assets,
    aws_lambda as _lambda,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_ssm as ssm,
)
from constructs import Construct

from stacks.bootstrap import SSM_ZONE_ID, SSM_CERT_ARN


class AppStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Pick up bootstrap outputs ------------------------------------
        # value_for_string_parameter resolves at synth time by doing an
        # SSM GetParameter. The resolved value gets baked into the template.
        zone_id = ssm.StringParameter.value_for_string_parameter(
            self, SSM_ZONE_ID
        )
        cert_arn = ssm.StringParameter.value_for_string_parameter(
            self, SSM_CERT_ARN
        )

        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "Zone",
            hosted_zone_id=zone_id,
            zone_name=domain_name,
        )
        certificate = acm.Certificate.from_certificate_arn(
            self, "Certificate", cert_arn
        )

        # --- Build + push the container image -----------------------------
        # CDK handles the Docker build and ECR push automatically during
        # `cdk deploy`. The image hash is part of the Lambda's configuration,
        # so any code change triggers a Lambda update.
        image = ecr_assets.DockerImageAsset(
            self,
            "ServerImage",
            # Repo root as build context so the Dockerfile can COPY the app
            # source. Dockerfile itself lives under infra/lambda/.
            directory="..",
            file="infra/lambda/Dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
            # Defense in depth: also listed in root .dockerignore. Without
            # these, cdk.out gets copied into itself (ENAMETOOLONG recursion).
            exclude=[
                "**/cdk.out",
                "**/.venv",
                "**/__pycache__",
                "**/.pytest_cache",
                "out/**",
                ".git",
                "Untitled.ipynb",
            ],
        )

        # --- Lambda -------------------------------------------------------
        fn = _lambda.DockerImageFunction(
            self,
            "ServerFn",
            code=_lambda.DockerImageCode.from_ecr(
                repository=image.repository,
                tag_or_digest=image.image_tag,
            ),
            # 1024 MB -> ~1 vCPU allocation -> faster cold starts.
            # Bumping to 2048 shaves ~0.5s off cold but doubles cost.
            memory_size=1024,
            # BFS at depth 5 on real data is still well under a second,
            # but network init + Lambda cold bootstrap can eat time.
            timeout=Duration.seconds(30),
            environment={
                "PG_LOG_LEVEL": "INFO",
                "PG_MAX_DEPTH": "5",
            },
            description=f"Thesaurus graph server for {domain_name}",
        )

        # Function URL with IAM auth. We tried AuthType=NONE first; this
        # account's AWS Organization has an SCP that denies public Lambda
        # URLs (AuthType=NONE), so we use IAM auth and route through
        # CloudFront, which signs requests via Origin Access Control. This
        # is the more secure pattern anyway -- only CloudFront can invoke
        # Lambda, not random internet clients hitting the raw URL.
        fn_url = fn.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=_lambda.InvokeMode.BUFFERED,
        )

        # --- CloudFront Origin Access Control ----------------------------
        # OAC lets CloudFront sign origin requests with SigV4. Unlike OAI
        # (S3-only, legacy), OAC works for Lambda function URL origins.
        lambda_oac = cloudfront.FunctionUrlOriginAccessControl(
            self,
            "LambdaOAC",
            signing=cloudfront.Signing.SIGV4_ALWAYS,
        )

        # --- CloudFront ---------------------------------------------------
        # The Function URL isn't enough by itself: it doesn't do custom
        # domains or edge caching. CloudFront adds both. Also absorbs a lot
        # of cheap traffic that never hits Lambda. With OAC, CloudFront
        # signs origin requests so Lambda only accepts traffic from this
        # distribution.
        distribution = cloudfront.Distribution(
            self,
            "CDN",
            comment=f"PythonGraphs frontend + API for {domain_name}",
            domain_names=[domain_name, f"www.{domain_name}"],
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin.with_origin_access_control(
                    fn_url,
                    origin_access_control=lambda_oac,
                ),
                viewer_protocol_policy=cloudfront
                    .ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # Default: let CloudFront pick sensible caching for the
                # static HTML/JS/CSS responses.
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                # Required for OAC signing: the origin request policy must
                # exclude the Host header (CloudFront must sign with its
                # own host, not the viewer's).
                origin_request_policy=cloudfront
                    .OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            additional_behaviors={
                # API calls are dynamic (depth param, autocomplete). Don't
                # cache them -- a shared cache would leak one user's depth
                # choice to another. Forward everything to Lambda every time.
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin.with_origin_access_control(
                        fn_url,
                        origin_access_control=lambda_oac,
                    ),
                    viewer_protocol_policy=cloudfront
                        .ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    origin_request_policy=cloudfront
                        .OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
                # /health is dynamic (could be used by uptime checks) but
                # cheap to call. Don't cache.
                "/health": cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin.with_origin_access_control(
                        fn_url,
                        origin_access_control=lambda_oac,
                    ),
                    viewer_protocol_policy=cloudfront
                        .ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront
                        .OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            },
            # IPv4 + IPv6. AAAA record below assumes this is enabled.
            enable_ipv6=True,
            # Cheapest price class: US + Canada + Europe edges. Expand to
            # PRICE_CLASS_ALL later if we care about APAC/SA latency.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )

        # --- DNS aliases --------------------------------------------------
        # Apex A/AAAA for parikhsaahil.com
        route53.ARecord(
            self,
            "ApexA",
            zone=zone,
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(distribution)
            ),
        )
        route53.AaaaRecord(
            self,
            "ApexAAAA",
            zone=zone,
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(distribution)
            ),
        )
        # Subdomain www.parikhsaahil.com for people who type it out of habit.
        route53.ARecord(
            self,
            "WwwA",
            zone=zone,
            record_name="www",
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(distribution)
            ),
        )
        route53.AaaaRecord(
            self,
            "WwwAAAA",
            zone=zone,
            record_name="www",
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(distribution)
            ),
        )

        # --- Outputs ------------------------------------------------------
        CfnOutput(self, "SiteUrl", value=f"https://{domain_name}")
        CfnOutput(self, "FunctionUrl", value=fn_url.url)
        CfnOutput(self, "CloudFrontDomain", value=distribution.domain_name)
