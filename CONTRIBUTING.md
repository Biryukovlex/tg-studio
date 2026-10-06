# Contributing to TGhost

Thank you for helping improve TGhost. The project combines Telegram data
collection, analytics, and an evidence-aware writing agent, so changes must
preserve privacy, tenant isolation, and source provenance.

## Before opening a change

- Search existing issues and keep each pull request focused on one concern.
- Never include real Telegram content, session strings, API keys, database
  exports, provider payloads, or screenshots containing private channel data.
- Use synthetic fixtures in tests. Redact logs before attaching them to an
  issue or pull request.
- For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of
  opening a public issue.

## Local development

TGhost requires Python 3.12+ (enforced by `pyproject.toml`), Node.js 22+,
and PostgreSQL 16 for integration tests. The default unit suite does not
connect to Telegram or OpenRouter.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -r requirements-dev.txt
npm --prefix studio-frontend ci --ignore-scripts
```

Run the standard checks before submitting:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -m postgres  # needs TEST_POSTGRES_URL on a disposable database
.venv/bin/python -m compileall -q app scripts alembic tests
.venv/bin/python -m pip check
.venv/bin/python scripts/check_dependency_licenses.py
.venv/bin/python scripts/check_lock_consistency.py
.venv/bin/python -m ruff check app scripts tests alembic
.venv/bin/python -m mypy app
npm --prefix studio-frontend run typecheck
npm --prefix studio-frontend run lint
npm --prefix studio-frontend run build
npm --prefix studio-frontend audit --audit-level=high
docker compose config --quiet
git diff --check
```

PostgreSQL integration tests are opt-in and expect a disposable database. See
the relevant test modules for their environment variables and setup commands.

## Pull-request expectations

- Add or update tests for behavioral changes.
- Update the README, API contract, runbooks, or milestone checklist when the
  change affects users, operators, privacy, data flow, or architecture.
- Keep generated Studio assets in sync by running the frontend build.
- Preserve workspace scoping and enforce authentication and CSRF protection on
  mutations.
- Validate evidence identifiers and links; a successful model response or
  saved draft alone does not prove a factual claim.
- Keep incomplete product paths behind the relevant feature flag.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Unless you explicitly state otherwise, contributions intentionally submitted
for inclusion in TGhost are provided under the
[Apache License 2.0](LICENSE).

### Privacy checks

Keep runtime credentials, Telegram sessions, collected content, provider
payloads and local agent configuration out of commits and test fixtures.
Use invented channels/posts and credentials that cannot authenticate to a
real account. Screenshots require an explicit publication review.

CI checks tracked file paths with `python scripts/check_repository_privacy.py`
and scans Git history (including tests) with checksum-verified Gitleaks.
`.gitleaks.toml` permits only exact reviewed synthetic values. Do not exclude
entire test directories or general credential patterns to silence a finding.
