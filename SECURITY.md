# Security Policy

Batonkeep orchestrates your own AI provider plans and API keys on a backend you run, so it
holds **credentials** and runs **agent processes**. We take reports seriously and appreciate
responsible disclosure.

## Reporting a vulnerability

**Please do not open a public issue, discussion, or pull request for a security problem.**

Report it privately through either channel:

- **GitHub** (preferred) — go to the repository's **Security** tab → **Report a vulnerability**
  (GitHub private vulnerability reporting). This keeps the report and our discussion private
  until a fix ships.
- **Email** — `security@batonkeep.com`. If you'd like, encrypt sensitive details and we'll
  coordinate a secure channel in our first reply.

Please include enough to reproduce: affected version/commit, configuration (deployment mode,
credential mode), steps, and impact. A proof of concept helps but is not required.

## What to expect

- **Acknowledgement** within **3 business days**.
- An initial assessment and severity triage within **7 business days**.
- Coordinated disclosure: we'll agree on a timeline with you, fix in private, then publish a
  GitHub Security Advisory crediting you (unless you prefer to stay anonymous).

## Supported versions

Batonkeep is pre-1.0 and ships from `main`. Security fixes land on `main` and in the next
tagged release; we do not backport to older `0.x` tags. Always run the latest release.

| Version | Supported |
|---|---|
| latest `0.x` release / `main` | ✅ |
| older `0.x` releases | ❌ (upgrade) |

## Scope notes

Batonkeep is **self-hosted and single-tenant by default**: you control the host, the network
exposure, and the credentials. Reports most useful to us include credential handling and
storage, the agent execution / sandbox boundary, the privilege-separation model
(`batond` vs `sandbox`), authentication (the app-auth gate), the publish/share surface, and
the in-UI console. Issues that require an attacker to already have host or root access to the
machine running the backend are generally out of scope.
