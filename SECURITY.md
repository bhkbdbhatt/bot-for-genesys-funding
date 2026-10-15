# Security Policy

GenesysPluginSponsorBot is a local developer-relations tool: a single operator
runs it against accounts they own, using credentials they supply. It has no
server component, no multi-tenant surface and no inbound network listener except
the optional loopback web wizard. The security work is therefore mostly about
**not leaking your own credentials** and **not being mistaken for a spam bot**.

## Supported versions

Security fixes are applied to the latest release on the default branch.

| Version | Supported |
| ------- | --------- |
| 1.x     | ✅        |
| < 1.0   | ❌        |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately
through GitHub's [private vulnerability reporting][report] (the *Security* tab →
*Report a vulnerability*), or email the maintainer at `security@example.com`
(replace with your real contact before publishing).

Include the version, a description, and the smallest reproduction you can manage.
You can expect an acknowledgement within a few days. There is no bug-bounty
programme, but credit is gladly given in `CHANGELOG.md` unless you prefer to stay
anonymous.

## Scope

In scope:

- Credential disclosure (secrets written to disk, logs, outbox files or the web
  UI). Secrets are read only from environment variables; see the `*_TOKEN` /
  `*_KEY` / `*_PASSWORD` list in the README.
- The optional local web wizard: authentication bypass, privilege escalation
  between roles, session or CSRF weaknesses, clickjacking and the localhost/host
  guard.
- Signature and idempotency handling in the generic webhook publisher.
- Anything that lets a crafted prospect, article or config value cause code
  execution, path traversal or SSRF against an unintended host.

Out of scope:

- The behaviour of third-party platforms you configure it to publish to.
- Social outcomes (a post being removed, an account being rate-limited) that
  follow from running an automation tool; see *Ethics and compliance* in the
  README.
- Vulnerabilities in `pyyaml` or `requests` — report those upstream.

## Threat model

The threat table lives in the README (§ *Security*), with the design rationale in
`ARCHITECTURE.md` (§ *Cross-cutting invariants*). In short: secrets stay in the
environment and are redacted from logs; the web wizard binds to loopback with
password + TOTP auth, a role gate and a Host/CSRF guard; state is written
atomically so a crash cannot corrupt a ledger; outbound traffic is rate-limited
and account-level publications are gated behind a human moving an item to
`approved`.

[report]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability
