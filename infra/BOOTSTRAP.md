# Bootstrap: one-time setup

One-time human work on a laptop or dev box with admin credentials to the
target AWS account. After this, every push to `main` deploys automatically
via GitHub Actions. No further human in the loop.

Domain expected: a domain already registered through Route53 Registrar
(which auto-creates the hosted zone). No manual NS step needed.

All account-specific values (AWS account id, Route53 zone id) are passed in
via environment variables locally and via GitHub Actions secrets in CI.
None are hardcoded in the repo.

## What the bootstrap creates

`cdk deploy GraphosaurusBootstrap` creates:

- ACM certificate for the domain + `www.` subdomain, DNS-validated through
  the (already-existing) Route53 zone. Validation usually completes in
  1-2 minutes.
- GitHub OIDC identity provider reference (imports the one in your account;
  create one manually if missing -- see troubleshooting below).
- IAM role `GraphosaurusGithubDeployRole` that GitHub Actions assumes. It
  is scoped to `<GITHUB_OWNER>/<GITHUB_REPO>` on `main` only (values set
  in `infra/app.py`).
- Three SSM parameters (`/pythongraphs/route53/zone-id`,
  `/pythongraphs/acm/certificate-arn`, `/pythongraphs/iam/ci-role-arn`)
  so the app stack can pick these up later.

## Prerequisites

| Tool | Why |
|---|---|
| AWS CLI v2 | auth + sanity checks |
| Node 20+ | CDK CLI runs on Node |
| Python 3.12 | CDK app is Python |
| Docker | CDK builds the Lambda image locally |

## Required environment variables

| Variable | Purpose |
|---|---|
| `CDK_DEFAULT_ACCOUNT` | AWS account id the stacks deploy into |
| `PG_HOSTED_ZONE_ID` | Route53 zone id for the site domain (pre-existing) |
| `CDK_DEFAULT_REGION` | Optional. Defaults to `us-east-1` (required for CloudFront cert). |

Set them in your shell before running `cdk` commands, e.g.:
```
export CDK_DEFAULT_ACCOUNT=<AWS_ACCOUNT_ID>
export PG_HOSTED_ZONE_ID=<Z...>
```

## One-time steps

### 1. Configure an AWS profile

Any profile format works (SSO, IAM user keys, instance profile) as long as
`aws sts get-caller-identity --profile <name>` returns the target account.
Example SSO-backed profile:

```
[profile personal]
sso_session = personal
sso_account_id = <AWS_ACCOUNT_ID>
sso_role_name = AdministratorAccess
region = us-east-1

[sso-session personal]
sso_start_url = https://<your-sso-portal>.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
```

Refresh with `aws sso login --profile personal` when creds expire.

Sanity check:
```
aws sts get-caller-identity --profile personal
```

### 2. Install CDK CLI and Python deps

```
npm install -g aws-cdk@2.170.0
cd infra
python3 -m pip install -r requirements.txt --user
```

### 3. Bootstrap the CDK environment in the account

```
export AWS_PROFILE=personal
export CDK_DEFAULT_ACCOUNT=<AWS_ACCOUNT_ID>
export PG_HOSTED_ZONE_ID=<Z...>
cdk bootstrap aws://${CDK_DEFAULT_ACCOUNT}/us-east-1
```

Creates the `CDKToolkit` CloudFormation stack (staging S3 bucket, ECR,
roles). Safe to re-run.

### 4. Deploy the bootstrap stack

```
cd infra
cdk deploy GraphosaurusBootstrap
```

Expected duration: ~1-2 minutes.

Stack outputs: `CiRoleArn`, `ZoneId`, `CertificateArn`, `DomainName`.

### 5. Set GitHub Actions secrets

CI needs the account id and zone id to deploy. Set them as GitHub
repository secrets so they don't live in the workflow file:

1. In GitHub, go to `Settings` -> `Secrets and variables` -> `Actions`.
2. Add these repository secrets:
   | Name | Value |
   |---|---|
   | `AWS_ACCOUNT_ID` | your AWS account id |
   | `HOSTED_ZONE_ID` | your Route53 zone id (from step 4 output `ZoneId`) |

Or via `gh` CLI:
```
gh secret set AWS_ACCOUNT_ID --body '<AWS_ACCOUNT_ID>'
gh secret set HOSTED_ZONE_ID --body '<Z...>'
```

### 6. Push to main; CI takes over

From then on, `git push origin main` triggers
`.github/workflows/deploy.yml` which:

1. Assumes `GraphosaurusGithubDeployRole` via GitHub OIDC (no stored AWS
   credentials in GitHub).
2. Runs `pytest` over the whole project.
3. Runs `cdk deploy GraphosaurusApp` -- builds the Lambda image, pushes to
   ECR, updates CloudFront + Lambda + Route53 aliases.

First app-stack deploy: ~5 minutes. Subsequent deploys: 1-3 minutes.

## Known gotchas

- **GitHub OIDC provider import.** The stack imports
  `arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com`.
  If your account doesn't have one yet, create it first:
  ```
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
  ```
  Then re-run `cdk deploy GraphosaurusBootstrap`.
- **Lambda + CloudFront OAC 403.** If you see "Forbidden. For
  troubleshooting Function URL authorization issues..." the Lambda needs
  BOTH `lambda:InvokeFunctionUrl` AND `lambda:InvokeFunction` granted to
  `cloudfront.amazonaws.com`. CDK's `FunctionUrlOrigin.with_origin_access_control`
  adds only the first; the second is added manually in `stacks/app.py`
  (`fn.add_permission("AllowCloudFrontInvokeFunction", ...)`).
  Don't remove that line.
- **GitHub repo rename.** The OIDC trust policy scopes to
  `repo:<owner>/<repo>:ref:refs/heads/main`. If you rename the repo on
  GitHub, update `GITHUB_REPO` in `infra/app.py` and redeploy
  `GraphosaurusBootstrap`, otherwise CI role assumption will fail.
- **AWS Organizations SCPs.** If deploying into an org account, check for
  SCPs that block public Lambda URLs (`AuthType=NONE`). We use
  `AuthType=AWS_IAM` + CloudFront OAC to sidestep this; works even with
  strict org policies.

## Destroying everything

```
cd infra
cdk destroy GraphosaurusApp
cdk destroy GraphosaurusBootstrap
```

The Route53 hosted zone is imported (not created), so it stays intact. The
`CDKToolkit` bootstrap resources stay too -- cheap and shared across any
CDK project in the account.
