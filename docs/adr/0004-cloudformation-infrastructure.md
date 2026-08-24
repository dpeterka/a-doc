# 0004. AWS infrastructure as CloudFormation, no console-created resources

Status: Accepted (compute-target sections superseded by ADR 0006 — ECS Fargate + EFS replaced the EC2 node; access sections superseded by ADR 0007 — public ALB replaced Tailscale. Networking also superseded in the same direction: `network.yaml`'s single public subnet became two AZ-spread public subnets, because ADR 0007's public ALB requires at least two subnets in different Availability Zones; this is a networking-layer consequence of that pivot, not an independent decision. The IaC principle itself stands.)

## Context

The operator (this project's user) is an experienced infrastructure
architect and has decided infrastructure should be reviewed like code:
every AWS resource change should go through a PR and a change set, the
same way an application code change goes through review and CI. The
deployment target itself is simple — a single always-on EC2 node, since
SQLite + git-as-database want one persistent instance rather than a fleet
— but "simple deployment" and "unmanaged/console-created resources" are
independent axes, and this project chooses IaC even at small scale for
auditability and reproducibility (a rebuilt instance should restore to a
known-working state, which is only possible if every resource that instance
depends on is declared somewhere).

## Decision

All AWS resources are defined as CloudFormation stacks under `deploy/cfn/`:
`network.yaml` (VPC, one public subnet, a security group with no inbound
rules), `backup.yaml` (versioned, SSE-KMS-encrypted S3 bucket + lifecycle
rule), `instance.yaml` (the EC2 node — IAM role, encrypted gp3 EBS volumes,
SSM access, no SSH key pair), and `ci.yaml` (an OIDC-federated IAM role
GitHub Actions assumes to deploy the others). Stacks cross-reference each
other via `Fn::ImportValue` against exported Outputs rather than manually
threaded shell parameters, so `deploy.yml` only needs to know stack names.
No resource is ever created by hand in the console; every change is a PR
that updates a template, deployed via `aws cloudformation deploy` (a change
set) from CI (or locally, for the one-time `ci.yaml` bootstrap — see that
template's header comment for the chicken-and-egg reasoning).

Access to the running instance is via AWS SSM (control plane) and Tailscale
(the application itself) only; there is no public ingress and no SSH key
material anywhere in the stack.

## Consequences

- Every infrastructure change is reviewable and revertible the same way a
  code change is — `git log` on `deploy/cfn/` is the infrastructure change
  history.
- The one necessary exception is `ci.yaml`'s first deployment, which must
  be manual (it creates the role that later deployments, including of
  itself, assume) — documented in README.md and in the template.
- A rebuilt/replaced EC2 instance restores itself via
  `deploy/install.sh` + a tested restore from the backup S3 bucket, which
  PLAN.md calls out as a release gate — infrastructure-as-code makes that
  restore reproducible rather than "whatever the last admin did by hand."
