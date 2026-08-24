# 0007. Public ALB with username/password auth (Tailscale removed)

Status: Accepted (2026-08-23)

## Context

The original design (ADR 0004 era) exposed the web UI only over a
Tailscale tailnet, with `tailscale serve` providing TLS. Operating it
required a tailnet, per-device client installs for the operator, and an
auth key in the boot path whose 90-day expiry created a silent time bomb
on instance replacement. The project owner explicitly decided to remove
the private-network layer in favor of a public, password-protected
endpoint.

## Decision

- Patient access is a public, internet-facing ALB at
  `https://adoc.petabloc.io` (ACM certificate auto-validated in the
  Route53 zone; HTTP redirects to HTTPS). `deploy/cfn/alb.yaml`.
- App authentication is username/password only (owner decision — no
  TOTP, no WAF): scrypt-hashed users provisioned via `adoc user add`,
  constant-time verification with a same-cost path for unknown
  usernames, per-username (5) and per-IP (20) 15-minute lockouts,
  `X-Forwarded-For` trusted only behind the ALB, Secure/HttpOnly
  cookies. `src/adoc/web/users.py`, `security.py`.
- The compute layer has no public ingress of its own: only the ALB's
  security group may reach the app port. Shell access is
  `aws ecs execute-command` only (ADR 0006).

## Consequences

- No VPN/client setup for the operator; works from any browser.
- The login surface is internet-facing and the codebase is public:
  security rests on password strength, the lockout logic, and patch
  discipline — not obscurity. Lockout counters are in-memory and reset
  on task restart (accepted gap).
- TOTP and/or AWS WAF remain available as follow-ups if the threat
  picture changes.
