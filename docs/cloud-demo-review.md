# On-demand AWS demo — review before provisioning

The user chose an AWS target under $5/month. The earlier $15–20 managed RDS
proposal is superseded. Nothing in this document authorizes a cloud apply.

## Concrete design

- One t4g.small ARM64 instance, 16 GB encrypted gp3 root disk, standard CPU credits.
- No inbound network rules, SSH key, NAT gateway, load balancer or managed database.
  A temporary public IPv4 supports outbound package/image/API requests. Control uses SSM.
- PostgreSQL runs on the host's Docker network. Its named volume survives stop/start.
  The application uses a separate database owner role, not the PostgreSQL superuser.
- Bootstrap installs a persistent one-hour stop timer before package installation.
  A normal run finishes early and requests shutdown; the controlling workflow also
  attempts StopInstances in finally/always paths. These are safeguards, not a hard
  account spending cap or proof that a failed OS/control plane will always stop.
- Each run uses an immutable ECR digest and S3 version IDs plus SHA-256 checks.
  Two synthetic source files enter the matcher; ground truth is not published as input.
  Reports and a pg_dump backup go to versioned private S3. SSM output goes to CloudWatch.
- Database checkpoints and source history persist on the disk. The first cloud slice
  runs the deterministic batch only; local interactive agent approval remains separate.
- The disk is retained on instance termination too. Replacements require explicit
  recovery from a backup or reattachment; Terraform does not silently restore a database.
  A retained orphan disk still costs money and must be accounted for during teardown.

## Price estimate, US East 1, September 13 2026

AWS Price List API verified Linux shared t4g.small at $0.0168/hour and gp3 at
$0.08/GB-month. No free-tier credits or commitments are assumed.

| Component | Assumption | Monthly USD |
| --- | --- | ---: |
| Encrypted gp3 disk | 16 GB retained all month | 1.28 |
| Instance | 30 running hours | 0.504 |
| Public IPv4 | 30 running hours at $0.005/hour | 0.15 |
| Core subtotal | Above bounded workload | 1.934 |
| Image, backup, object requests, logs, transfer | Working allowance, verify actual usage | 1.00–2.00 |
| Planning total | Taxes and unrelated account/GitHub usage excluded | 2.93–3.93 |

The storage/log allowance is an assumption, not a measured bill. Keep at most a few
small release images/backups and 30 running hours/month for this estimate. More
sessions, retained disk replacements, growing S3 versions or images can exceed $5.
An always-running instance alone would cost $12.264/month at 730 hours, before the
disk and IPv4. CPU credits are standard so surplus credit charges are disabled.

Pricing references: [EC2](https://aws.amazon.com/ec2/pricing/on-demand/),
[EBS](https://aws.amazon.com/ebs/pricing/), [IPv4](https://aws.amazon.com/vpc/pricing/).
EBS continues billing while stopped: [AWS stop/start behavior](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/how-ec2-instance-stop-start-works.html).

## Release/control pipeline

1. Manually dispatch Publish verified Recon candidate on master. Reusable CI must
   pass first. The recon-release GitHub environment assumes the repository-specific
   publisher role via OIDC; it can push only the intended ECR repo and write input/
   candidate prefixes. It cannot start instances or change infrastructure. Existing
   immutable commit tags are reused on retry, never overwritten.
2. Publish an ARM64 image, run image checks, record the digest, and upload each source
   with its returned S3 version ID. candidate.json records hashes, versions and image.
3. Manually dispatch Run bounded Recon demo with a published commit SHA. A distinct
   recon-demo role can start/stop only the demo instance and invoke only the fixed SSM
   document. Revision input is constrained to a 40-character hexadecimal SHA.
4. The runner waits for cloud-init, downloads/verifies the candidate, runs the batch
   with a stable run ID, uploads reports and backup, and stops. Failure is observable
   through workflow/SSM status. No external notification subscription is configured.
5. Run a previous candidate SHA to exercise code rollback. This preserves the current
   database and is valid only for backward-compatible schemas. Destructive schema
   rollback is not automated. Cloud restore-to-a-new-instance must be exercised after
   provisioning before a shipped-release claim.

## Prepared evidence and remaining gates

Local application and release-contract tests: 37 passed, including backup/restore.
Terraform validate passes and two mocked infrastructure tests pass. The complete
read-only plan contains 25 additions, zero changes and zero deletions. The selected
official AL2023 ARM64 AMI is ami-0a157bd98d97a9589; recheck availability at deployment.
The live account had no existing GitHub OIDC provider when inspected. Recheck before
apply or import any provider created in the meantime; never create a duplicate.

No cloud-init execution, cloud IAM permissions, hosted workflow, image publication,
cloud restore or automatic cloud shutdown has been verified live. Mocks are not
substitutes for those deployment checks. The local PostgreSQL/monitoring drills are
recorded separately in local-operations.md.

Before provisioning, review/approve the 25-resource plan and recurring cost target.
Then configure GitHub environment protections for master and the exact role/bucket/
instance/document variables returned by Terraform. The current personal AWS login
was root; ongoing publishing and demo control use the scoped OIDC roles above.
Terraform bootstrap itself needs a separately authorized administrative session.
The local Terraform state must remain private and backed up; shared automatic
Terraform applies are deliberately not part of these workflows.
