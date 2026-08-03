# Security Policy

## Supported versions

Before the first stable release, security fixes target `main`. After releases
begin, only the latest stable release will receive security fixes.

| Version | Status |
| --- | --- |
| `main` / first-release candidate | Supported |
| Stable release | Not published yet |

## Reporting a vulnerability

Do not post credentials, private logs, exploit details, or unredacted user data
in a public Issue.

Use GitHub's private vulnerability report for the official repository:

https://github.com/Ling-ye/AgentStrata/security/advisories/new

If that form is unavailable, email `616202172@qq.com` privately. Do not put
exploit details, secrets, or unredacted evidence in a public Issue.

Include the affected version, deployment mode, impact, reproduction conditions,
and the smallest redacted evidence needed to validate the report. Maintainers
will acknowledge a usable report, assess severity, coordinate a fix, and credit
the reporter if requested.

## Deployment boundary

AgentStrata can run tools, MCP servers, model providers, platform gateways, and
deployment scripts with materially different trust levels. Treat third-party
services and tool schemas as untrusted, use explicit allowlists, isolate secrets
in local environment files, and do not expose management ports beyond loopback
without an independent security review.

Public examples, tests, Issues, and logs must also omit tenant domains, document
tokens, platform account/group IDs, real chat identities, private project names,
and machine-specific paths. Maintainer identity is limited to `Lingye` and
`616202172@qq.com`; deployment identities belong in ignored local files.
