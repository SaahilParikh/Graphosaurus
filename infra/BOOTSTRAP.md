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
  DNS-validated through the zone. **Blocks on validation until the
  domain's NS records at its registrar are updated to point at the zone's
  nameservers** -- see step 5 below.
- GitHub OIDC identity provider (`token.actions.githubusercontent.com`).
- IAM role `PythonGraphsGithubDeployRole` that GitHub Actions assumes. It
  is scoped to **`SaahilParikh/PythonGraphs` on branch `main` only**.
- Three SSM parameters (`/pythongraphs/route53/zone-id`,
  `/pythongraphs/acm/certificate-arn`, `/pythongraphs/iam/ci-role-arn`)
  so the app stack can find these later.

## NS delegation is a manual step

An earlier version of this stack tried to automate
`route53domains:UpdateDomainNameservers`. That only works if the domain
is registered in the **same AWS account** where the stack is deployed.
`parikhsaahil.com` is registered in a different account (or with a
different registrar), so the API call failed with "Domain not found in
account". The attempted automation is gone from `stacks/bootstrap.py`;
you do the NS change yourself in step 5.

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

**Leave this command running.** It will create the zone, OIDC provider,
IAM role, SSM params quickly (~1 min), then hang on `Certificate` in
`CREATE_IN_PROGRESS` until step 5 is done (or time out after ~90 min).

Before you do step 5, pull the NS records from another terminal:

```bash
aws cloudformation describe-stacks \
  --profile personal-b --region us-east-1 \
  --stack-name PythonGraphsBootstrap \
  --query 'Stacks[0].Outputs[?OutputKey==`NameServers`].OutputValue' \
  --output text
```

You'll get a comma-separated list like
`ns-123.awsdns-45.com.,ns-678.awsdns-90.net.,...`. Four names.

### 5. Delegate DNS at the registrar (manual)

Log in to whichever account/registrar owns `parikhsaahil.com`. Replace the
current nameservers (`NS65/66.DOMAINCONTROL.COM`) with the four returned
above.

**If the domain is in a different AWS account via Route53 Domains:**

```bash
# Switch to whichever profile owns the domain:
aws route53domains update-domain-nameservers \
  --profile <domain-owning-profile> \
  --region us-east-1 \
  --domain-name parikhsaahil.com \
  --nameservers \
     Name=ns-XXX.awsdns-YY.com \
     Name=ns-XXX.awsdns-YY.net \
     Name=ns-XXX.awsdns-YY.org \
     Name=ns-XXX.awsdns-YY.co.uk
```

(The 4 names come from the CloudFormation output above. Strip any trailing
`.` before passing.)

**If the domain is registered elsewhere** (Namecheap, GoDaddy, etc.):
update the NS records through that registrar's console.

Verify from a third terminal:

```bash
dig +short parikhsaahil.com NS
# Should return the 4 Route53 NS. May take 5-15 min to propagate.
```

Once DNS is propagated, ACM validation unblocks within a couple of minutes
and the `cdk deploy` from step 4 completes.

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
