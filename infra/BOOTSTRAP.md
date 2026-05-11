# Bootstrap: one-time setup

One-time human work on a laptop or dev box with admin credentials to the
target AWS account. After this, every push to `main` deploys automatically
via GitHub Actions. No further human in the loop.

Target account: `***AWS_ACCOUNT_ID***` in `us-east-1`. Domain `graphosaurus.com`
is already registered through Amazon Registrar in that account, so Route53
Registrar has already auto-created the hosted zone and delegated DNS --
no manual NS step.

## What the bootstrap creates

`cdk deploy PythonGraphsBootstrap` creates:

- ACM certificate for `graphosaurus.com` + `www.graphosaurus.com`,
  DNS-validated through the (already-existing) Route53 zone. Validation
  usually completes in 1-2 minutes.
- GitHub OIDC identity provider (imported from existing if already in
  account; otherwise the stack will fail with a clear "EntityAlreadyExists"
  error in the other direction -- see troubleshooting below).
- IAM role `PythonGraphsGithubDeployRole` that GitHub Actions assumes. It
  is scoped to `SaahilParikh/PythonGraphs` on `main` only.
- Three SSM parameters (`/pythongraphs/route53/zone-id`,
  `/pythongraphs/acm/certificate-arn`, `/pythongraphs/iam/ci-role-arn`)
  so the app stack can pick these up later.

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| AWS CLI v2 | auth + sanity checks | https://aws.amazon.com/cli/ |
| Node 20+ | CDK CLI runs on Node | https://nodejs.org/ or nvm |
| Python 3.12 | CDK app is Python | your choice |
| Docker | CDK builds the Lambda image locally | https://docker.com/ |

## One-time steps

### 1. Configure an AWS profile

If you already have a profile for this account (`aws configure sso`-based
or IAM user keys), use it via `--profile <name>` in the commands below or
`export AWS_PROFILE=<name>`.

On the dev box this project was deployed from, the profile is
`personal`, backed by AWS IAM Identity Center:

```
[profile personal]
sso_session = personal
sso_account_id = ***AWS_ACCOUNT_ID***
sso_role_name = AdministratorAccess
region = us-east-1

[sso-session personal]
sso_start_url = https://<your-sso-portal>.awsapps.com/start
sso_region = us-east-1
```

Refresh creds with `aws sso login --profile personal` when they expire
(~8 hours by default).

Sanity check:
```
aws sts get-caller-identity --profile personal
# Account: ***AWS_ACCOUNT_ID***
```

### 2. Install CDK CLI and Python deps

```
npm install -g aws-cdk@2.170.0
cd infra
python3 -m pip install -r requirements.txt --user
# or use a venv if you prefer
```

### 3. Bootstrap the CDK environment in the account

```
export AWS_PROFILE=personal
cdk bootstrap aws://***AWS_ACCOUNT_ID***/us-east-1
```

Creates (or updates) the `CDKToolkit` CloudFormation stack with the staging
S3 bucket, ECR repo, and roles CDK uses. Safe to re-run.

### 4. Deploy the bootstrap stack

```
cd infra
cdk deploy PythonGraphsBootstrap
```

Expected duration: ~1-2 minutes (ACM cert validates quickly since DNS was
already delegated at domain registration time).

Stack outputs include:
- `CiRoleArn` -- must match the ARN hardcoded in
  `.github/workflows/deploy.yml` (`PythonGraphsGithubDeployRole` in account
  `***AWS_ACCOUNT_ID***`).
- `DomainName`, `ZoneId`, `CertificateArn` -- for reference.

### 5. Push to main; CI takes over

From then on, `git push origin main` triggers
`.github/workflows/deploy.yml` which:

1. Assumes `PythonGraphsGithubDeployRole` via GitHub OIDC (no stored AWS
   secrets in GitHub).
2. Runs `pytest` over the whole project.
3. Runs `cdk deploy PythonGraphsApp` -- builds the Lambda image, pushes to
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
  Then re-run `cdk deploy PythonGraphsBootstrap`.
- **Lambda + CloudFront OAC 403.** If you see "Forbidden. For
  troubleshooting Function URL authorization issues..." the Lambda needs
  BOTH `lambda:InvokeFunctionUrl` AND `lambda:InvokeFunction` granted to
  `cloudfront.amazonaws.com`. CDK's `FunctionUrlOrigin.with_origin_access_control`
  adds only the first; the second is added manually in
  `stacks/app.py` (`fn.add_permission("AllowCloudFrontInvokeFunction", ...)`).
  Don't remove that line.
- **AWS Organizations SCPs.** If deploying into an org account, check for
  SCPs that block public Lambda URLs (`AuthType=NONE`). We use
  `AuthType=AWS_IAM` + CloudFront OAC to sidestep this; works even with
  strict org policies.
- **`cdk bootstrap` requires admin.** The `CDKToolkit` stack creates IAM
  roles; the bootstrap command needs permissions to create them. Admin
  (or a carefully-scoped bootstrap role) is the usual answer.

## Destroying everything

For completeness. **This deletes the cert and DNS records, breaking the site.**

```
cd infra
cdk destroy PythonGraphsApp
cdk destroy PythonGraphsBootstrap
```

The Route53 hosted zone is NOT created by our stacks (we import it), so it
stays intact. `cdk bootstrap` resources stay too -- cheap and shared.
