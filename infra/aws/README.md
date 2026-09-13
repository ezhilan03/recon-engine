# AWS foundation and optional on-demand demo

The user selected an under-$5/month on-demand demo. See
[the concrete deployment and cost review](../../docs/cloud-demo-review.md).

The foundation remains the default. enable_publisher adds scoped GitHub OIDC;
enable_demo adds the private-by-network-policy EC2/PostgreSQL demo with stop controls.
Both are disabled by default. Use a reviewed, pinned ARM64 demo_ami when enabling it.
All infrastructure remains unapplied. Historical foundation verification follows.

# AWS foundation, prepared but not applied

This module creates private versioned S3 artifacts, an immutable ECR repository
with basic scanning and cleanup of old untagged images, and a seven-day CloudWatch
log group. The bucket denies unencrypted transport. It refuses destructive removal
of stored objects/images through force-delete settings. Keep release tags for rollback.

September 13 verification: Terraform 1.16.1, AWS provider 6.64.0; fmt, validate and
mock-provider policy assertions pass. An authenticated plan for the portfolio account
in us-east-1 reports 8 additions, 0 changes and 0 deletions. Nothing was applied.
The binary plan is local and ignored by Git; regenerate before an eventual apply.

```sh
terraform init
terraform validate
terraform test
AWS_PROFILE=portfolio terraform plan -var=account_id=YOUR_ACCOUNT_ID
```

The default is foundation-only. Optional on-demand runtime and publishing code are
now prepared; the cloud deployment review records their verification limits. The current local AWS
session is root; the deployment must use a scoped identity. No Terraform credentials
are stored in configuration. Account restrictions prevent accidental cross-account use.

No monthly price is promised. S3 versions, ECR images, logging and runtime can incur
charges. The user selected local-first development and has not approved a numeric
cloud spending cap. Cost the complete compute/database design before asking for an
apply. A budget alert is not a hard spending cap. Before shared CI applies, bootstrap
remote state with locking and restricted access; the current local backend is only
for the unapplied development plan.

References: https://registry.terraform.io/providers/hashicorp/aws/latest/docs
