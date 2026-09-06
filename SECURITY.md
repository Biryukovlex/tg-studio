# Security policy

## Supported versions

Security fixes are applied to the latest revision of the `main` branch. Until
the project publishes versioned releases, older commits are not supported.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability and do not include
real credentials, Telegram messages, sessions, or database exports in a report.

Use GitHub's private vulnerability reporting for this repository. If that
option is unavailable, contact the repository owner privately through the
contact method on the owner's GitHub profile and ask for a secure reporting
channel. Include:

- the affected commit and deployment mode;
- a minimal reproduction using synthetic data;
- the expected and observed impact;
- any suggested mitigation, without accessing data you do not own.

You should receive an acknowledgement within seven days. Timelines for a fix
and coordinated disclosure depend on severity and reproducibility.

## Operator security

TG Studio is self-hosted software that holds a Telegram user session and may
send selected channel context to an LLM provider. Operators are responsible for
protecting `.env`, database backups, provider keys, and the encryption key;
restricting network access; applying updates; and reviewing the privacy and
deployment runbooks before enabling real provider or web-search traffic.
