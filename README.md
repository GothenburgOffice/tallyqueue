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

## What it does not do

- It is not a ledger and keeps no accounts, balances or reports.
- It files nothing with Skatteverket.
- It does not include the BAS chart of accounts. You paste the accounts you use yourself.
- It is not a service for other people's data. The list and the receipts are stored only for the
  users the operator has approved, at first only the owner.

## Status

The first version, an MCP server and a web page for the list, is being reviewed in the
repository's first pull request. This page will describe how to run it once that is merged.

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
the source of your version.
