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
add fixtures of those types. `.dockerignore` lists the same patterns, so a local receipt never
reaches an image layer: add a pattern to both files (`tests/test_repo.py` compares them).

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
  sessions in memory. Tokens are 256 random bits and start with `tq_`; anything else is
  refused, also when its SHA-256 is listed. A refusal says why (`auth.why_refused`: nothing
  sent, a `${…}` placeholder not filled in, not a token, or a token no entry was made from),
  in the same words on `/mcp`, `/upload` and the sign-in page, and never repeats what was
  sent. There is no sign-in throttle, on purpose (a stranger could use it to lock the owner
  out).
- `tallyqueue/config.py`: settings from the environment and the Host and Origin checks.
  `PUBLIC_URL` is the one source for links, the allowed host and origin, and the Secure cookie.
- `tallyqueue/server.py`: the MCP tools (`add_item`, `list_items`, `get_item`, `update_item`,
  `list_accounts`, `add_accounts`, `get_riksbank_rate`), `POST /upload`, `/health`, and `build_app()`, which
  wraps the app with the host check, the token check on `/mcp` and the default response headers.
- `tallyqueue/riksbank.py`: Riksbanken's published rate for a currency on a date, fetched on
  request and remembered in memory.
- `tallyqueue/web.py` with `templates/` and `static/`: the web page. Server-rendered forms.
  `templates/_macros.html` holds what the list and the item page share: the kontering written
  out as a voucher, and the status forms. The stylesheet's address carries a checksum of its
  content, so a change to `app.css` reaches browsers without a version bump.

## Rules that must not regress

- **Every tool and route resolves the user from the request itself** (token or session) and
  passes it to the store. Nothing trusts a user name sent by a client.
- **Status conditions live in `model.status_problems`** and are enforced in
  `store.update_item`, for the MCP tools and the web page alike: prepped needs date, amount,
  currency, the total in SEK when the currency is another, and a balanced kontering; booked
  needs the voucher number and the receipt-attached tick; skipped needs a reason. A prepped or
  booked item must stay that complete.
- **Amounts are decimal strings with two decimals**, never floats: in the tool schema, in
  `model.money` and on disk. A JSON number is refused, and so is an amount that would have to
  be rounded or guessed ("1,250"). An exchange rate is text too (`model.rate`).
- **The SEK side of another currency lives in `model.settle_sek`**, which the store applies to
  every change of facts. The total in SEK is either worked out from a given `exchange_rate`
  (the one place an amount is computed: amount × rate, rounded half up to öre) or given as it
  is in `amount_sek`, and then no rate is kept. A stored rate and a stored total never
  disagree, and an amount in SEK has neither. A rate is never estimated: it is given, with its
  source in `exchange_rate_source`. A rate belongs to its currency: it is dropped when the
  currency changes, unless the same change gives the new one.
- **In the facts form, only what was typed counts as given.** A browser sends every field back,
  so `web.change` leaves a SEK-side field out of the change when it still holds what the page
  put in it (`web._sek_fields`). Otherwise the old currency's rate would count as given for
  the new one.
- **`riksbank.py` is the only code that calls another service**, and only when a user asks: the
  button on the item's page or the `get_riksbank_rate` tool. The address is fixed but for a
  checked currency code and two dates; redirects are not followed; the rate is read as the
  digits sent, never as a float; `RIKSBANK_LOOKUP=0` turns it off. Nothing about a receipt or a
  user goes out. Tests stand in for the network (`riksbank._get`) and never call Riksbanken.
- **Every status can be changed by hand in the web page**, in the list and on the item's page,
  through the same forms (`templates/_macros.html`). The AI client is never the only way.
- **What an AI client is told lives in `server.INSTRUCTIONS`, the `Facts` field descriptions and
  the tool descriptions**, and it keeps facts apart from conclusions. A fact is what the document
  says; how VAT applies, which accounts fit and whether a purchase belongs to the business are
  proposals in the kontering. An item is prepped only when the client has no open question about
  it. These texts were changed after two models were run blind on real documents: before
  rewording them, think about what a client that sees nothing else will do.
- **A skipped item is settled.** It is no twin for the "possibly the same purchase" warning, and
  gets no such warning itself (`model.warnings`).
- **An AI client proposes a kontering for every item**, also when it is unsure, and treats alike
  documents alike. An empty kontering is not a way to say "I was unsure": the note is.
- **An AI client cannot change the list of accounts, unless the user has switched that on.** The
  switch is the user's own, in the web page; it lasts `store.ACCOUNTS_FROM_CLIENT_MINUTES` and
  then turns itself off (`store.accounts_from_client_until`). `add_accounts` only adds: it never
  renames or removes. Never make it part of adding receipts, and never widen it to replace the
  list. Each tool carries annotations that say whether it writes, so a client can ask the user
  first.
- **A duplicate file never overwrites** what is stored.
- **No JavaScript on the pages.** The page policy is `default-src 'none'` with styles, images
  and frames from this origin only. Templates autoescape; never mark user text as safe.
- **Receipts are served by content type from the file's bytes**, to their owner only. Anything
  that is not a PDF or a PNG, JPEG, WebP or GIF image goes out as `text/plain` in a sandbox.
  That includes HTML, XML and SVG: they are stored as text and never served as a page or an
  image. Don't add `image/svg+xml` or `text/html` to the served types.
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
