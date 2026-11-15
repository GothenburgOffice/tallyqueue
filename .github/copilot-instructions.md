# TallyQueue project instructions

The shared reference for coding agents and contributors. Copilot reads it directly; `CLAUDE.md`
imports it and adds the Claude Code specifics.

## Shared standards

This repo follows the owner's [WebDev Studio](https://github.com/hkarn/WebDev-Studio) standards
where they fit a small Python service:
[dev environment](https://github.com/hkarn/WebDev-Studio/blob/main/standards/dev-environment.md),
[CI](https://github.com/hkarn/WebDev-Studio/blob/main/standards/ci.md),
[dependencies](https://github.com/hkarn/WebDev-Studio/blob/main/standards/dependencies.md),
[security](https://github.com/hkarn/WebDev-Studio/blob/main/standards/security.md) and
[agent instructions](https://github.com/hkarn/WebDev-Studio/blob/main/standards/agent-instructions.md).
It does not follow the Next.js + PostgreSQL stack standard: this is Python with files on disk, the
same stack as `GothenburgOffice/image-gen-mcp`. This repo's own files stay the authority for what
is implemented here.

## Project

TallyQueue: a to-do list of receipts and invoices waiting to be booked, for a Swedish sole
proprietorship. An MCP server (streamable HTTP) that an AI client adds items to, and a web page
to manage the list, in one container behind an HTTPS reverse proxy. Python 3.13, the MCP Python
SDK, Starlette, Jinja2, no database. Public repo `GothenburgOffice/tallyqueue`,
AGPL-3.0-or-later. See [README](../README.md).

## What it must never become

These are decided. Don't build towards them, and say so if a request would.

- **Not a ledger.** No accounts with balances, no reports, no promise of compliance with
  bokföringslagen. The legal bookkeeping and the archived receipts stay in the user's
  bookkeeping service; a receipt here is a working copy.
- **Files nothing** with Skatteverket.
- **No other people's data.** Items and receipts are stored only for the approved users in
  `TALLYQUEUE_USERS`. If it is ever opened to others, that mode is stateless: import, work,
  export in one session.
- **No bundled BAS chart of accounts.** Its publisher forbids spreading the machine-readable
  chart. The user pastes their own list; the repo holds no list of accounts, in code, tests or
  docs, beyond the handful of numbers the tests need.
- **No advice.** Tool descriptions and page texts call a kontering a proposal the user checks.
  Never word anything as accounting or tax advice.
- **The name is one word**, TallyQueue, and the README says it is not affiliated with
  Skatteverket, Fortnox or Tally Solutions. Keep that disclaimer block near the top.
- **Versions stay on 0.x** (permanent beta).

## Real data stays out

No real receipts, names, amounts, account details or tokens in the repo, issues, pull requests,
tests or logs. Tests and `scripts/smoke.py` build their own made-up files in code and say
"made up" in them. `.gitignore` keeps data folders and receipt and export file types out; don't
add fixtures of those types.

## Environment

Docker-first: the host needs Docker and nothing else. Run commands in the dev container
(`.devcontainer/`, working dir `/app`), or from the host with
`docker compose -f compose.yaml -f compose.dev.yaml run --rm app <command>`.

```sh
ruff format .                 # CI runs: ruff format --check .
ruff check .
python -m pytest -q           # offline: the real app on a local port, a real MCP client
pip-audit --no-deps --disable-pip -r constraints.txt
python scripts/smoke.py http://localhost:3440   # against a running stack; needs TALLYQUEUE_TOKEN
```

The dev stack (`compose.dev.yaml`) serves `http://localhost:3440` with a public, made-up
sign-in (user `dev`, token in that file) and its own data volume. Run the checks before a commit
or a pull request, and report only checks that actually ran.

## Code map

- `tallyqueue/model.py`: what an item holds and every check on it (amounts, dates, kontering
  lines, the conditions for each status, warnings, the pasted list of accounts). Pure functions,
  no files or network. Unit-test any change here (`tests/test_model.py`).
- `tallyqueue/store.py`: items and receipts on disk, one folder per user. An item's id is the
  SHA-256 of its receipt file; adding the same file again returns the existing item unchanged.
  Every function takes the user first and never reads another user's folder. Writes are whole
  files (temporary file, then rename) under one lock.
- `tallyqueue/auth.py`: approved users (`TALLYQUEUE_USERS`, only SHA-256 of each token), web
  sessions in memory. Tokens are 256 random bits and shorter ones are refused; there is no
  sign-in throttle, on purpose (a stranger could use it to lock the owner out).
- `tallyqueue/config.py`: settings from the environment and the Host and Origin checks.
  `PUBLIC_URL` is the one source for links, the allowed host and origin, and the Secure cookie.
- `tallyqueue/server.py`: the MCP tools (`add_item`, `list_items`, `get_item`, `update_item`,
  `list_accounts`), `POST /upload`, `/health`, and `build_app()`, which wraps the app with the
  host check, the token check on `/mcp` and the default response headers.
- `tallyqueue/web.py` with `templates/` and `static/`: the web page. Server-rendered forms.

## Rules that must not regress

- **Every tool and route resolves the user from the request itself** (token or session) and
  passes it to the store. Nothing trusts a user name sent by a client.
- **Status conditions live in `model.status_problems`** and are enforced in
  `store.update_item`, for the MCP tools and the web page alike: prepped needs date, amount,
  currency and a balanced kontering; booked needs the voucher number and the receipt-attached
  tick; skipped needs a reason. A prepped or booked item must stay that complete.
- **Amounts are decimal strings with two decimals**, never floats. An amount that would have
  to be rounded or guessed ("1,250") is refused.
- **A duplicate file never overwrites** what is stored.
- **No JavaScript on the pages.** The page policy is `default-src 'none'` with styles, images
  and frames from this origin only. Templates autoescape; never mark user text as safe.
- **Receipts are served by content type from the file's bytes**, to their owner only. Anything
  that is not a PDF or an image goes out as `text/plain` in a sandbox. SVG is not accepted.
- **Form posts check the origin, then the session, then the CSRF token** (`web._post`).
- **Tokens, session ids and receipt contents never reach the log.** A malformed
  `TALLYQUEUE_USERS` entry is reported by position, not echoed.
- **The production container stays locked down**: non-root, read-only root file system, no
  capabilities, the app port bound to 127.0.0.1 or not published.

## Workflow

- Decisions and planned work are GitHub issues; open questions carry the `decision` label.
  Don't build on an open one. No decision logs or dated reports in the repo.
- Branch from the latest `main`, open the pull request as a draft, and never merge it: the
  owner's merge is the approval, and a push to `main` publishes the image.
- Never apply `good first issue` or `help wanted`; the owner sets those.
- `notes/` and `CLAUDE.local.md` are gitignored, for scratch and session notes.
- The repo files are the authority for versions and commands: `requirements.txt`,
  `constraints.txt`, the Dockerfile and Compose files, and `ci.yml`. Update these instructions
  and the README with any change to them.
