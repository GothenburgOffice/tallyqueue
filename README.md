# TallyQueue

A to-do list of receipts and invoices that are waiting to be booked. TallyQueue is a Gothenburg
Office in-house accounting helper for a Swedish sole proprietorship (enskild firma) with few
transactions a year.

> [!WARNING]
> **Read this before you use it.**
>
> - **Permanent beta.** TallyQueue stays on 0.x versions. Anything can change or break between
>   versions, and there is no support.
> - **Check every figure yourself.** The dates, amounts, VAT and accounts in TallyQueue are
>   proposals, often written by an AI. They can be wrong. You are responsible for what you book.
> - **Not a ledger.** TallyQueue is not bookkeeping software and does not promise compliance with
>   the Swedish Bookkeeping Act (bokföringslagen). Your legal bookkeeping and the archived
>   receipts stay in a real bookkeeping service. The receipts kept here are working copies.
> - **No advice.** Nothing in TallyQueue or in this repository is accounting, tax or legal advice.
> - **Not affiliated** with Skatteverket, Fortnox or Tally Solutions. "TallyQueue" is one word and
>   has nothing to do with the Tally accounting software.

## What it does

1. An AI client such as Claude reads your mail with its own e-mail connector and finds receipts
   and invoices. TallyQueue never sees your mail and never holds mail credentials.
2. The client hands each one to TallyQueue over [MCP](https://modelcontextprotocol.io): the file,
   the facts read from it (date, counterparty, amount, currency, VAT) and a proposed *kontering*
   (accounts and VAT treatment).
3. TallyQueue keeps the list. An item is identified by the checksum of its receipt file, so
   scanning the same inbox again adds nothing twice.
4. You open the list in the web page, look at each receipt, correct the proposal and book it in
   your bookkeeping service.
5. You mark the item as booked, with the voucher number it got there and a tick that the receipt
   was attached there.

An item is `new`, then `prepped` (checked and ready to book), then `booked`. An item that should
not be booked, such as a private purchase, is `skipped`.

The books are kept in SEK, so a receipt in another currency needs its total in SEK before it can
be prepped. It is given one of two ways, by the AI client or by you in the web page:

- the **exchange rate** (SEK for one unit of the currency), and TallyQueue works the total out,
  rounded to öre;
- the **total in SEK as it is**, for example what the card statement says was drawn.

Beside it goes where the figure comes from: the receipt, the card statement, or Riksbanken's rate
for the date when neither of the first two has it.

A rate belongs to its currency. When you correct the currency, the rate and the total worked out
from it are dropped, unless you give the new rate in the same edit. A total given as it was
drawn stays.

TallyQueue can fetch Riksbanken's rate, and only when asked: a button on the item's page, with a
date you pick, and a tool for the AI client. On a weekend or a holiday it takes the latest rate
before the date and says which date that was. Three things to know:

- Riksbanken publishes its rates for information and calls them "indicative only". Whether that
  rate is the right one for your books is yours to decide, like every figure here.
- The request goes from your server to `api.riksbank.se` and carries the currency code and
  dates, nothing about the receipt. Riksbanken logs the address the request comes from. This is
  the only call TallyQueue makes to another service; `RIKSBANK_LOOKUP=0` turns it off.
- Riksbanken allows only a few lookups a minute. TallyQueue remembers the rates it has fetched.

## What it does not do

- It is not a ledger and keeps no accounts, balances or reports.
- It files nothing with Skatteverket.
- It does not include the BAS chart of accounts. You paste the accounts you use yourself.
- It is not a service for other people's data. The list and the receipts are stored only for the
  users the operator has approved, at first only the owner.

## Run it

You need Docker and an HTTPS reverse proxy (see [Behind Caddy](#behind-caddy)). Nothing else is
installed on the host.

```bash
cp .env.example .env
```

1. In `.env`, set `PUBLIC_URL` to the address you will use, for example
   `https://tallyqueue.example.com`.
2. Make an access token for yourself. It is shown once: keep it in a password manager.

   ```bash
   docker compose -f compose.images.yaml run --rm app python -m tallyqueue token owner
   ```

   Put the `owner:…` pair it prints in `TALLYQUEUE_USERS` in `.env`. The server keeps only that
   SHA-256, never the token. To approve another user later, add a pair, separated by a comma.
   With `TALLYQUEUE_USERS` empty, nobody can sign in.

   Only a token made by this command works. It starts with `tq_`; a password of your own is
   refused even when its SHA-256 is in `TALLYQUEUE_USERS`. A refused sign-in says what was
   wrong: nothing sent, a `${…}` placeholder that was not filled in, text that is not a token,
   or a token that no entry was made from.
3. Start it:

   ```bash
   docker compose -f compose.images.yaml up -d
   ```

`compose.images.yaml` pulls the published image `ghcr.io/gothenburgoffice/tallyqueue`. To build
from a checkout instead, use `compose.yaml` (`docker compose up -d --build`).

The container runs as a non-root user with a read-only file system and no capabilities. Items
and receipts are in the `data` volume. The app listens on `127.0.0.1:3440` only, so it is
reachable from the internet only through your reverse proxy.

### Behind Caddy

With a Caddy you already run on the host, add this site and reload Caddy:

```
tallyqueue.example.com {
	request_body {
		max_size 25MB
	}
	reverse_proxy 127.0.0.1:3440
}
```

On a host with no reverse proxy, add `compose.caddy.yaml`. It runs Caddy in a second container,
which gets the certificate for the host in `PUBLIC_URL` by itself; the name must point at the
machine, and ports 80 and 443 must reach it. The app port is then not published at all.

```bash
docker compose -f compose.images.yaml -f compose.caddy.yaml up -d
```

### Settings

| Variable | Meaning |
|---|---|
| `PUBLIC_URL` | The address users type: HTTPS, no path. Links, the Host and Origin checks and the Secure cookie come from it |
| `TALLYQUEUE_USERS` | Approved users as `name:sha256` pairs, comma-separated |
| `APP_PORT` | Port on the host that the reverse proxy forwards to (default 3440) |
| `MAX_RECEIPT_BYTES` | Largest receipt file (default 15 MB) |
| `SOURCE_URL` | Where users of your server get its source, if you run a changed version |
| `RIKSBANK_LOOKUP` | `0` turns off fetching exchange rates from Riksbanken (default on) |
| `TALLYQUEUE_IMAGE` | A pinned image for `compose.images.yaml`, such as a `sha-…` tag |

## The web page

Open `PUBLIC_URL` and sign in with your token. The list shows what is left to do, with each
proposed kontering written out as a voucher: account, its name from your list, debit and credit.

You change an item's status by hand, in the list or on the item's own page: prepped, booked
(with the voucher number, and a tick that the receipt is attached to it in your bookkeeping
service) or skipped (with a reason), and back to new. A prepped item has the booking form right
in its row, so after booking something you type the voucher number, tick the box and are done.

The item's page shows the receipt beside its status, the kontering and the facts, which you can
correct. For another currency than SEK the facts take the exchange rate or the total in SEK, and
under them a button fetches Riksbanken's rate for a date. You can also add a receipt file by
hand, and delete an item.

Under **Accounts** you paste the accounts you use, one per line (`1930 Företagskonto`). A SIE
file can be pasted as it is; only its `#KONTO` and `#SRU` lines are read. TallyQueue then warns
when a kontering uses an account that is not in your list, and the AI client can ask for the list.

An account can also keep the codes your bookkeeping service gives it: the VAT box (momsruta), the
NE row (bokslut) and the SRU code. Paste them after the name as a printed chart has them
(`2611 Utgående moms 25 % B10 B14 7381`), or as five columns separated by semicolons
(`2611;Utgående moms 25 %;B10;B14;7381`) where a line could be read two ways. The AI client gets
them with the accounts, so it can see which VAT box an account feeds, for instance in a
reverse-charge case. They are your data, kept as given: TallyQueue draws no conclusions from them.

The AI client cannot change the list, unless you switch that on. On the same page, "Let the AI
client add accounts for one hour" lets it add accounts when you ask it to, for example from the
chart in your bookkeeping service. It can only add: an account that is in the list keeps its
name and nothing is removed. The switch turns itself off after an hour, so the client never
adds accounts while it is only handling receipts.

The pages use no JavaScript.

## Connect an AI client

The MCP endpoint is `PUBLIC_URL/mcp` (streamable HTTP). The client sends your token in the
header `X-API-Key` (or `Authorization: Bearer`).

**Claude (Desktop app, claude.ai): a custom connector.** In Settings → Connectors → Add custom
connector, give the URL `https://tallyqueue.example.com/mcp`, choose **No sign-in**, and add the
request header `X-API-Key` with your token as its value.

**Claude Code in a terminal:**

```bash
claude mcp add --transport http --scope user tallyqueue https://tallyqueue.example.com/mcp --header 'X-API-Key: ${TALLYQUEUE_TOKEN}'
```

The single quotes store the placeholder, and `claude` fills it in from the environment variable
`TALLYQUEUE_TOKEN`.

### Tools

| Tool | What it does |
|---|---|
| `add_item` | Adds a receipt or invoice file, with the facts read from it and a proposed kontering, as new or straight to prepped or skipped. The same file again changes nothing |
| `list_items` | The list, by status (`todo` is new and prepped) or by text, with a count per status |
| `get_item` | One item in full, with what is missing, warnings and its history, and your entries for the accounts in its kontering |
| `update_item` | Changes the facts, the proposed kontering, the status or the notes |
| `list_accounts` | Your own list of accounts, if you have pasted one, with their VAT box, NE row and SRU code. Can be asked about some accounts only |
| `add_accounts` | Adds accounts to your list. Refused unless you have switched it on in the web page, which lasts an hour |
| `get_riksbank_rate` | Riksbanken's rate for a currency on a date. Stores nothing; the client gives the rate to `update_item` |
| `upload_ticket` | A one-use ticket for uploading one file to `/upload`, for a client that can run `curl` but does not hold your token |

The server's instructions and the tool descriptions tell the client the rules:

- Facts are only what the document says: no payment date unless the document or a statement
  gives one, no VAT amount when the document shows none. Amounts are text with two decimals.
- What the client concludes itself, such as how VAT applies or which accounts fit, is not a fact.
  It goes in the kontering as a proposal, with what the client was unsure about.
- Every item gets a proposed kontering, also when the client is unsure: what is open goes in its
  note, and alike documents are treated alike. It is in SEK and balances. For another currency
  the client gives the exchange rate or the total in SEK, with its source.
- Two documents for one purchase, such as an invoice and its receipt, are both added. One carries
  the facts and the kontering, and the other is skipped with a reason that names it.
- An item is marked prepped only when the client has no open question about it, and booked only
  when you give the voucher number.

A client can add and change items but not delete them.

Then ask, for example: *"Go through my mail from October, find receipts and invoices for my
business, and add each one to TallyQueue with its facts and a proposed kontering."*

### How the file gets from the mail to TallyQueue

The client has to send the file itself, and how depends on the client:

- **A client with a shell**, such as Claude Code, saves the attachment and uploads the raw file.
  Your token stays in the connector: the client asks `upload_ticket` for a one-use ticket and
  runs the command it gets back. The reply has the item's id, and `update_item` adds the facts.

  ```bash
  curl -fsS -X POST --data-binary @invoice.pdf -H "X-Upload-Ticket: tqu_…" -H "X-Filename: invoice.pdf" https://tallyqueue.example.com/upload
  ```

  A ticket is good for one upload within ten minutes, and is used up by the first try. When
  the client gives the file's SHA-256 for the ticket, only that file is taken, so a ticket seen
  by anyone else is of no use to them. It opens nothing else: not the list, not the web page.
  Tickets are kept in memory and a restart drops them. A script of your own can still send the
  token itself, as `X-API-Key`.

- **A chat client without a shell** sends the file base64-encoded in `add_item`. That works for
  a short text receipt; for a PDF of ordinary size it is slow and can fail. Add those by hand in
  the web page, and let the client find them with `list_items`.

The client must be able to reach your server: a sandbox that may only call some hosts cannot
upload, and the web page is the way in from there.

## Your data

- Everything is in the `data` volume, as ordinary files: `users/<name>/items/<sha256>.json` (one
  item each, readable as text), `users/<name>/receipts/<sha256>.<ext>` (the file as it arrived),
  `users/<name>/accounts.json` and `users/<name>/settings.json` (what you have switched on). Back
  up the volume to back up TallyQueue.
- Accepted files: PDF, PNG, JPEG, WebP, GIF, and UTF-8 text such as a saved mail.
- Nothing is encrypted at rest. Nothing about your receipts or items is sent anywhere. The only
  outgoing request is the Riksbanken rate lookup, when you ask for it, and it carries only the
  currency code and dates; `RIKSBANK_LOOKUP=0` turns it off.
- The receipts here are working copies. The archive that the law asks for is the one in your
  bookkeeping service: an item is not `booked` until you tick that the receipt is attached there.

## Development

Docker is all the host needs. The dev stack serves `http://localhost:3440` from the source in
this folder, restarts when it changes, and keeps its made-up data in its own volume. Its sign-in
is public on purpose (user `dev`; the token is in `compose.dev.yaml`).

```bash
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

The checks, as CI runs them:

```bash
docker compose -f compose.yaml -f compose.dev.yaml run --rm app sh -c "ruff format --check . && ruff check . && python -m pytest -q"
```

The tests start the real server on a local port and talk to it with a real MCP client and plain
HTTP. `scripts/smoke.py` does the same against a running stack; CI runs it through Caddy and
again after a restart. Opening the folder in VS Code offers the dev container
(`.devcontainer/`), which adds git, `gh` and Claude Code's settings volume.

### Dependencies

`requirements.txt` lists what the code imports, at exact versions. `constraints.txt` pins
everything those pull in, so each build installs the same set. After changing
`requirements.txt`, renew the pins and run the checks:

```bash
sh scripts/renew-constraints.sh
```

## Not in this version

Ideas for later, none of them promised: SIE 4I export, Excel or CSV export, cross-check figures
for the VAT return and the NE appendix, reading a receipt back over MCP, and a stateless mode for
people who are not approved users (import, work and export in one session, nothing stored).

## Your data stays out of this repository

Real bookkeeping data and receipts never belong here. The `.gitignore` keeps data folders and
common receipt and export file types out, and the tests use made-up data only. Please keep it
that way in issues and pull requests too: no real receipts, names, amounts or account details.

## License

Copyright © 2026 Gothenburg Office.

TallyQueue is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version (`AGPL-3.0-or-later`). It is distributed
without any warranty. See [LICENSE](LICENSE).

If you run a changed version as a network service, the license requires you to offer its users
the source of your version. The pages link to the source in their footer; set `SOURCE_URL` to
yours.
