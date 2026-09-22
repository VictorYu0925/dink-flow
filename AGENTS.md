# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.11 DinkUp registration bot with two root-level scripts:

- `dinkup_bot.py` loads authentication, waits until noon, fetches events six days ahead, filters locations and available `fun` divisions, and submits registrations.
- `save_session.py` opens an interactive Chrome session and saves login state to `auth.json`.
- `.github/workflows/schedule-run.yml` schedules execution; `dry-run.yml` provides manual execution.
- `Pipfile` and `Pipfile.lock` define and lock Requests and Playwright dependencies.

There are currently no dedicated source-package, test, or asset directories.

## Build, Test, and Development Commands

Run commands from the repository root with Python 3.11 installed:

- `python -m pip install pipenv` — install the environment manager.
- `pipenv sync` — install locked dependencies.
- `pipenv run playwright install chromium` — install the bot's browser; Linux runners use `--with-deps` for system dependencies.
- `pipenv run python save_session.py` — capture authentication interactively; requires Google Chrome installed locally.
- `pipenv run python dinkup_bot.py` — execute the live registration flow.
- `pipenv run python -m py_compile dinkup_bot.py save_session.py` — check syntax without executing either script.

There is no build step.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for functions and variables, and `UPPER_SNAKE_CASE` for configuration constants. Preserve UTF-8 Chinese comments and log messages. Keep request timeouts explicit and retry counts bounded. No formatter or linter is configured. Update `Pipfile.lock` alongside dependency changes.

## Testing Guidelines

No automated test framework or coverage threshold is configured. Run the syntax check before submitting changes. For new tests, use `tests/test_*.py` and mock HTTP requests, browser authentication, and clock delays. Cover location filtering, division availability, retries, and registration payloads.

Despite its filename, `dry-run.yml` submits real registrations. Do not use it as an isolated test suite.

## Commit & Pull Request Guidelines

History commonly uses `feat:` and `fix:` prefixes, alongside unprefixed summaries. Prefer concise, descriptive messages such as `fix: handle empty event responses`. PRs should explain behavior changes, validation performed, related issues, and any scheduling or registration impact.

## Security & Configuration

Never commit authentication cookies or session contents. `auth.json` is ignored; CI uses the `AUTH_JSON_CONTENT` secret, which takes precedence over the local file. Workflows set `TZ=Asia/Taipei`; local execution uses the system clock. GitHub cron expressions use UTC.
