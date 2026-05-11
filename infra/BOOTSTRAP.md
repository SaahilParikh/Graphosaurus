# Bootstrap: one-time setup

You do this **once**, from your laptop, with admin credentials to the target
AWS account. After this, every push to `main` deploys automatically via
GitHub Actions -- no human in the loop.

The target account is the personal Isengard account `***AWS_ACCOUNT_ID_LEGACY***` in
`us-east-1`. The domain `parikhsaahil.com` is already registered through
Amazon Registrar in that account.

## What the bootstrap creates

Running `cdk deploy PythonGraphsBootstrap` creates:

- Route53 public hosted zone for `parikhsaahil.com` (RETAINed, won't be
  deleted if the stack is destroyed).
- ACM certificate for `parikhsaahil.com` + `www.parikhsaahil.com`,
  DNS-validated through the zone.
- GitHub OIDC identity provider (`token.actions.githubusercontent.com`).
- IAM role `PythonGraphsGithubDeployRole` that GitHub Actions assumes. It
  is scoped to **`SaahilParikh/PythonGraphs` on branch `main` only**.
- Three SSM parameters (`/pythongraphs/route53/zone-id`,
  `/pythongraphs/acm/certificate-arn`, `/pythongraphs/iam/ci-role-arn`)
  so the app stack can find these later.
- A custom resource that calls `route53domains:UpdateDomainNameservers`
  to flip the registrar's NS records from `DOMAINCONTROL.COM` to the
  Route53 nameservers the zone just created. This replaces the "manual NS
  change at the registrar" step with IaC.

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| AWS CLI v2 | auth + sanity checks | https://aws.amazon.com/cli/ |
| Node 18+ | CDK CLI runs on Node | https://nodejs.org/ |
| Python 3.12 | CDK app is Python | your choice |
| Docker | CDK builds the Lambda image locally | https://docker.com/ |

## One-time steps

### 1. Get admin credentials into your shell

For Isengard you typically use `midway` + `isengardcli` or the console's
"Access Keys" option. Any mechanism works as long as `aws sts
get-caller-identity` returns your account (`***AWS_ACCOUNT_ID_LEGACY***`):

```bash
aws sts get-caller-identity
# { "Account": "***AWS_ACCOUNT_ID_LEGACY***", "Arn": "..." }
```

### 2. Install CDK and app deps

```bash
npm install -g aws-cdk@2.170.0
cd infra
pip install -r requirements.txt
```

### 3. Bootstrap the CDK environment in the account

CDK ships assets (the Lambda container image, CloudFormation templates) via
a set of S3/ECR resources it calls its "bootstrap stack". Separate from our
application stacks. Run once per account/region:

```bash
cdk bootstrap aws://***AWS_ACCOUNT_ID_LEGACY***/us-east-1
```

Creates the `CDKToolkit` stack and the roles CI will assume.

### 4. Deploy the bootstrap stack

```bash
cd infra
cdk deploy PythonGraphsBootstrap
```

Expect ~5-10 minutes. The ACM cert validation is the slow part -- it waits
for the DNS-01 validation records to propagate.

The stack outputs (printed at the end) include:

- `CiRoleArn` -- matches the ARN hardcoded in
  `.github/workflows/deploy.yml` (`PythonGraphsGithubDeployRole`). Verify
  they match.
- `NameServers` -- the four Route53 NS records. The registrar delegation
  is automated; this is for your reference.
- `DomainName`, `ZoneId`, `CertificateArn`.

### 5. Verify DNS is delegated

This should be automatic via the `DelegateNameservers` custom resource, but
it's worth confirming:

```bash
dig +short parikhsaahil.com NS
# should now return 4 lines like ns-123.awsdns-45.com.
# NOT NS65.DOMAINCONTROL.COM (what it was before bootstrap).
```

If it still shows DOMAINCONTROL.COM, the custom resource may have failed
(common cause: the domain isn't registered in this account). Fix manually:

```bash
aws route53domains update-domain-nameservers \
  --region us-east-1 \
  --domain-name parikhsaahil.com \
  --nameservers \
     Name=<ns1-from-stack-output> \
     Name=<ns2-from-stack-output> \
     Name=<ns3-from-stack-output> \
     Name=<ns4-from-stack-output>
```

NS propagation across the internet takes minutes to hours; ACM validation
may need to wait.

## After bootstrap: CI takes over

Push to `main` (`git push origin main`). GitHub Actions runs
`.github/workflows/deploy.yml`:

1. Assume `PythonGraphsGithubDeployRole` via OIDC (no stored AWS secrets).
2. `pytest` over the whole project.
3. `cdk deploy PythonGraphsApp` -- builds the Lambda image, pushes to ECR,
   updates CloudFront + Lambda + DNS records.

First successful CI deploy takes ~5-8 minutes (CloudFront propagation is
slow). Subsequent deploys are 1-3 minutes.

## Destroying everything

For completeness. **This deletes DNS and the certificate, which will break
the site.**

```bash
# App first (has references to bootstrap resources)
cd infra
cdk destroy PythonGraphsApp

# Then bootstrap. The hosted zone has RETAIN policy -- you'll need to
# delete it manually through the Route53 console if you actually want
# it gone.
cdk destroy PythonGraphsBootstrap
```

`cdk bootstrap` resources (the `CDKToolkit` stack) stay. They're cheap
and reused across CDK projects; leave them.

## Known gotchas

- **Isengard baselining.** The account needs to be baselined recently or
  some API calls (including `cdk deploy`) can fail with permission errors.
  Re-baseline through the Isengard console if that happens.
- **Amazon Registrar domain ownership.** The NS-delegation custom resource
  assumes the domain is registered in the same account as the bootstrap
  stack. If not, delete that construct from `stacks/bootstrap.py` and do
  the NS change manually at whichever registrar owns the domain.
- **CloudFront updates are slow.** Any change that touches the CloudFront
  distribution (new alias, new cert, behavior change) takes 5-10 minutes
  to propagate globally. Don't panic if the first `cdk deploy` takes a
  while to go green.
- **First ACM cert issuance.** If the domain is brand-new, cert validation
  waits on DNS propagation (can be 15+ minutes the first time). Later
  renewals are instant because validation records stay in place.
