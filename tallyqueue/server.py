# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""TallyQueue as a remote MCP server (streamable HTTP), with the web page on the same port.

An AI client finds receipts and invoices, for example in the user's mail through its own
e-mail connector, and adds them here. This server never sees the mail and holds no mail
credentials. It stores the files and the proposals for approved users only (see auth.py).

It is a helper, not a ledger: nothing here books anything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shlex
import time
from typing import Annotated, Literal
from urllib.parse import quote, unquote

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__, auth, config, model, riksbank, store, web

INSTRUCTIONS = """TallyQueue is the user's to-do list of receipts and invoices that are waiting to be booked.
It is a helper, not a ledger: the user does the bookkeeping in their own bookkeeping service, and
checks every figure first.

- An item is one purchase or sale, with the files that document it. For each one you find (for
  example in the user's mail), add one item, and add each of its files to that item. A file
  belongs to one item: adding it again changes nothing.
- Getting a file in: a small text file, such as a saved mail, can go in file_base64 (add_item for
  the first file, add_file for more). Any other file: save it, get a ticket with upload_ticket
  (with item_id for a file of an item you already added) and run the curl command it gives. When
  you cannot run a command, ask the user to add the file in the web page, and find it with
  list_items. Never write out a PDF or an image as base64.
- Which files: those that together document the event. When two documents say the same thing,
  such as an order confirmation and the receipt, keep the one that is the better record of what
  happened (usually the one that shows the payment) and leave the other out. For a purchase in
  another currency, the bank or card statement's record of what was drawn in SEK belongs with the
  receipt: add it as kind payment. Facts and the kontering are on the item, once.
- Facts are what the document says, and nothing else. Leave a fact out rather than guess: no
  payment date unless the document or a statement gives one, and no VAT amount when the document
  shows none. Amounts are strings with two decimals ("1250.00").
- What you conclude yourself is not a fact: how VAT applies, which accounts fit, whether a purchase
  belongs to the business. Put it in the kontering (vat_treatment and note) as a proposal, with
  what you were unsure about. You give no accounting or tax advice, and TallyQueue has no rules
  for VAT or accounts: the user decides.
- The kontering is a proposal for the user to check, in SEK, and it must balance. Propose one for
  every item, also when you are unsure: an empty kontering helps nobody, and what is open goes in
  its note. Treat alike documents alike. Use accounts from list_accounts when the user has
  supplied their list. Its accounts can carry the VAT box, NE row and SRU code from the user's own
  chart: they show where an account's amounts are reported, for instance in a reverse-charge case.
- The list of accounts is the user's own. Add to it with add_accounts only when the user asks you
  to add accounts, never while adding receipts. It works only while the user has switched it on
  in the web page.
- The books are kept in SEK. For a document in another currency, give the total in SEK one of two
  ways: amount_sek as it is, when the receipt or the card or bank statement shows what was drawn;
  or exchange_rate (SEK for one unit of the currency), and amount_sek is worked out from it. When
  neither the receipt nor a statement gives it, get Riksbanken's rate with get_riksbank_rate, for
  the payment date when it is known and otherwise the date on the document. Always say where the
  figure comes from in exchange_rate_source. Never estimate a rate.
- Statuses: an added item is new. Mark it prepped only when the facts and a balanced kontering are
  in place and you have no open question about it; with one, leave it new and say what is open.
  An open question is one only the user can answer, such as whether a purchase belongs to the
  business or which of two VAT treatments applies. A rate from Riksbanken, or an account the
  user has not listed, is not one: say it in the note. Booked means the user booked it in their bookkeeping service: mark it only when the user
  tells you the voucher number. Use skipped, with a
  reason, for what should not be booked.
"""

# What each tool does to the user's data, for clients that ask the user before a tool that writes.
_READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ADDS = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
_CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
_LOOKS_UP = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Makes a short-lived permission, and changes nothing in the user's list.
_TICKET = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
TICKETS = auth.UploadTickets()

# Amounts are text in the schema too, so a client is never invited to send a float.
Amount = str | None


class Facts(BaseModel):
    """What the document itself says. Leave out what is not on it."""

    model_config = ConfigDict(extra='forbid')

    kind: Literal['receipt', 'invoice', 'credit_note', 'other'] | None = None
    date: str | None = Field(
        None,
        description='The date on the document, YYYY-MM-DD. For a mail with no date of its own, the '
        'date the mail was sent.',
    )
    counterparty: str | None = Field(None, description='The seller on a purchase, the customer on a sale.')
    description: str | None = Field(None, description='What it is for, in a few words.')
    amount: Amount = Field(
        None,
        description='Total including VAT in the document currency, two decimals: "1250.00". '
        'Negative for a credit note.',
    )
    currency: str | None = Field(None, description='Three-letter code: SEK, EUR, USD.')
    vat_amount: Amount = Field(
        None,
        description='VAT as the document shows it, in the document currency: "0.00" when it says '
        'VAT 0, left out when it has no VAT line at all. Under reverse charge the VAT you work out '
        'belongs in the kontering, not here.',
    )
    exchange_rate: str | None = Field(
        None,
        description='Only when the currency is not SEK: SEK for one unit of the currency, as text: '
        '"11.4235". amount_sek is then worked out from it. Leave out when you give amount_sek.',
    )
    amount_sek: Amount = Field(
        None,
        description='Only when the currency is not SEK: the total in SEK as the receipt or the card '
        'statement shows it. Leave out when you give exchange_rate.',
    )
    exchange_rate_source: str | None = Field(
        None,
        description='Where the rate or the total in SEK comes from: "receipt", "card statement", '
        '"Riksbanken 2026-10-19".',
        max_length=100,
    )
    invoice_number: str | None = Field(
        None,
        description='The invoice number when the document carries one, also on a receipt that '
        'names its invoice. Otherwise its order, receipt or reference number.',
    )
    due_date: str | None = Field(None, description='YYYY-MM-DD.')
    payment_date: str | None = Field(
        None,
        description='When it was paid, YYYY-MM-DD, when the document or a statement says so. A '
        'receipt for a card purchase is paid on its date. Leave out otherwise: the date of an '
        'invoice is not a payment date.',
    )


class KonteringLine(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account: str = Field(description='Account number, 4 digits (up to 6).')
    debit: Amount = Field(None, description='Amount in SEK on the debit side, or leave out.')
    credit: Amount = Field(None, description='Amount in SEK on the credit side, or leave out.')
    text: str | None = None


class Kontering(BaseModel):
    """A proposed booking: lines that balance, in SEK. The user checks it before booking."""

    model_config = ConfigDict(extra='forbid')

    lines: list[KonteringLine] = Field(default_factory=list)
    vat_treatment: str | None = Field(
        None, description='How VAT is handled, in words: "25 % Swedish VAT", "reverse charge, EU service".'
    )
    note: str | None = Field(None, description='Why these accounts, and what you were unsure about.')


class Account(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account: str = Field(description='Account number, 4 digits (up to 6).')
    name: str = Field(description="Its name in the user's chart of accounts.", max_length=200)
    vat_box: str | None = Field(
        None, description="The box in the VAT return (momsruta) the user's chart gives it, such as B10 or 48."
    )
    ne_row: str | None = Field(
        None, description="The row in the NE appendix (bokslut) the user's chart gives it, such as B9 or R6."
    )
    sru: str | None = Field(None, description="The SRU code the user's chart gives it, such as 7280.")


def _user(ctx: Context) -> str:
    """The approved user behind this call. The token is checked on every call."""
    user = auth.user_for_headers(ctx.headers or {})
    if not user:
        raise ToolError(
            'This connection is not approved. Send your TallyQueue token as "X-API-Key: <token>".'
        )
    return user


def _text(obj) -> TextContent:
    return TextContent(type='text', text=json.dumps(obj, indent=1, ensure_ascii=False))


def _given(value: BaseModel | None) -> dict | None:
    """Only the fields the caller sent, so that leaving one out never clears it."""
    return None if value is None else value.model_dump(exclude_unset=True)


def _lines(kontering: dict | None) -> str | None:
    """A kontering on one line: "D 5420 1000.00, D 2640 250.00, C 1930 1250.00"."""
    lines = (kontering or {}).get('lines') or []
    parts = [f'{"D" if ln["debit"] else "C"} {ln["account"]} {ln["debit"] or ln["credit"]}' for ln in lines]
    return ', '.join(parts) or None


def _row(item: dict) -> dict:
    facts = item['facts']
    row = {
        'id': item['id'][:12],
        'status': item['status'],
        'date': facts['date'],
        'counterparty': facts['counterparty'],
        'amount': facts['amount'],
        'currency': facts['currency'],
        'amount_sek': facts['amount_sek'],
        'vat_amount': facts['vat_amount'],
        'kontering': _lines(item['kontering']),
        'voucher': (item['booking'] or {}).get('voucher'),
        'files': [f['filename'] for f in item['files']],
    }
    return {k: v for k, v in row.items() if v is not None}


def _file_out(file: dict) -> dict:
    """One of an item's files as a client sees it."""
    return {k: file.get(k) for k in ('sha256', 'filename', 'kind', 'media_type', 'bytes', 'added')}


def _decode(file_base64: str) -> bytes:
    raw = file_base64.strip()
    if raw.startswith('data:') and ',' in raw:
        raw = raw.split(',', 1)[1]
    try:
        return base64.b64decode(''.join(raw.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ToolError('file_base64 is not valid base64. Send the file bytes, base64-encoded.') from None


FileKind = Annotated[
    Literal['document', 'payment', 'other'] | None,
    Field(
        description='What the file is. document (the default): the receipt, invoice or credit note. '
        "payment: the bank or card statement's record of the payment, such as what was drawn in SEK. "
        'other: anything else that documents the item.'
    ),
]


def _view(user: str, item: dict, *, history: bool = False) -> dict:
    debit, credit = model.totals(item['kontering'])
    lines = (item['kontering'] or {}).get('lines') or []
    has_lines = bool(lines)
    accounts = store.read_accounts(user)
    used = {ln['account'] for ln in lines}
    out = {
        'id': item['id'],
        'status': item['status'],
        'facts': {k: v for k, v in item['facts'].items() if v is not None},
        'kontering': item['kontering'],
        # Nothing to balance without lines, whether there is no kontering or one with only a note
        # or a VAT treatment: 0.00 against 0.00 would read as "balanced".
        'kontering_totals': {'debit': f'{debit:.2f}', 'credit': f'{credit:.2f}', 'balanced': debit == credit}
        if has_lines
        else None,
        'booking': item['booking'],
        'skip_reason': item['skip_reason'],
        'notes': item['notes'],
        'source': item['source'],
        'files': [_file_out(f) for f in item['files']],
        'created': item['created'],
        'updated': item['updated'],
        'missing_for_prepped': model.prep_problems(item) if item['status'] == 'new' else [],
        # The user's own entries for the accounts in the kontering, with their codes (VAT box, NE
        # row, SRU code) when the user's chart has them.
        'kontering_accounts': [a for a in accounts if a['account'] in used],
        'warnings': model.warnings(item, accounts, store.all_items(user)),
        'web_page': f'{config.PUBLIC_URL}/items/{item["id"]}',
    }
    if history:
        out['history'] = item['history']
    return out


async def _store(fn, *args, **kwargs):
    """Run a store call off the event loop; its refusals become tool errors the client can read."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except (store.StoreError, model.Invalid) as e:
        raise ToolError(str(e)) from None


mcp = MCPServer(
    name='tallyqueue',
    title='TallyQueue: receipts and invoices to book',
    version=__version__,
    instructions=INSTRUCTIONS,
)


@mcp.tool(
    description=f"""Add an item, one purchase or sale, to the user's list of things to book. Returns the item.

file_base64: its first file, when that is small, such as a saved mail as text ({store.ACCEPTED}). If
that file is already in the list, nothing changes and the item that has it comes back with
created=false, so scanning an inbox twice is safe. A PDF or an image of any real size never goes here:
leave file_base64 out, and add the file with a ticket from upload_ticket (with this item's id), or
start with the upload and add the facts with update_item. Add more files with add_file or
upload_ticket. To change an existing item, use update_item.

facts: only what the document says (date, counterparty, amount, currency, VAT). Leave out what is not
there; never guess. For another currency than SEK, also give exchange_rate or amount_sek, with
exchange_rate_source (see update_item). kontering: a proposal in SEK that balances, for the user to
check; use accounts from list_accounts when there are any. source: where the document came from, such
as the sender, date and subject of the mail.

status: leave it out and the item is new. prepped, or skipped with skip_reason, can be set in the same
call; update_item says what each needs. If the status cannot be set, the item is still added as new,
and status_not_set in the answer says why.""",
    annotations=_ADDS,
    structured_output=False,
)
async def add_item(
    ctx: Context,
    file_base64: Annotated[
        str | None, Field(description='The first file, base64-encoded: only a small one.', min_length=1)
    ] = None,
    filename: Annotated[str | None, Field(description='The file name, e.g. invoice-1042.pdf.')] = None,
    file_kind: FileKind = None,
    source: Annotated[str | None, Field(description='Where it came from.', max_length=300)] = None,
    facts: Annotated[Facts | None, Field(description='What the document says.')] = None,
    kontering: Annotated[Kontering | None, Field(description='Proposed booking, in SEK.')] = None,
    notes: Annotated[
        str | None, Field(description='Anything else the user should know.', max_length=4000)
    ] = None,
    status: Annotated[
        Literal['prepped', 'skipped'] | None,
        Field(description='Leave out for new. prepped or skipped can be set as the item is added.'),
    ] = None,
    skip_reason: Annotated[str | None, Field(description='Why it is skipped.', max_length=300)] = None,
) -> list:
    user = _user(ctx)
    data = _decode(file_base64) if file_base64 else None
    item, created = await _store(
        store.add_item,
        user,
        data,
        filename or '',
        via='mcp',
        kind=file_kind,
        source=source,
        facts=_given(facts),
        kontering=_given(kontering),
        notes=notes,
    )
    # The status is a second step on the stored item, with the same conditions as in update_item.
    # When it is refused, the item stays added as new: the file is in the list either way.
    status_not_set = None
    if status and created:
        ref = item['id']
        try:
            item = await anyio.to_thread.run_sync(
                lambda: store.update_item(user, ref, via='mcp', status=status, skip_reason=skip_reason)
            )
        except (store.StoreError, model.Invalid) as e:
            status_not_set = f'Added as new, not {status}. {e}'
    elif status:
        status_not_set = 'The file was already in the list, so its status was not changed.'
    view = await _store(_view, user, item)
    note = (
        None
        if created
        else 'This file was already in the list. Nothing was changed; use update_item to change it.'
    )
    reply = {'created': created, 'note': note, 'item': view}
    if status_not_set:
        reply['status_not_set'] = status_not_set
    return [_text(reply)]


@mcp.tool(
    description="""Add a small file, base64-encoded, to an item that is already in the list: for example
the bank's notice of a card payment, saved as text. Returns the item.

Only for a small file. A PDF or an image goes with a ticket from upload_ticket, with item_id. Add a
file only when it adds to what the item's other files show; see the server instructions on which
files to keep. The same file again changes nothing; a file that another item has is refused.""",
    annotations=_ADDS,
    structured_output=False,
)
async def add_file(
    id: Annotated[str, Field(description="The item's id, or its first 8 or more characters.")],
    file_base64: Annotated[str, Field(description='The file, base64-encoded.', min_length=1)],
    ctx: Context,
    filename: Annotated[str | None, Field(description='The file name.')] = None,
    kind: FileKind = None,
) -> list:
    user = _user(ctx)
    item, added = await _store(
        store.add_file, user, id, _decode(file_base64), filename or '', via='mcp', kind=kind
    )
    view = await _store(_view, user, item)
    return [_text({'added': added, 'item': view})]


@mcp.tool(
    description="""List the user's items, newest document first, with how many there are in each status.
status: todo (new and prepped, the default), new, prepped, booked, skipped or all. text narrows to items
whose counterparty, description, invoice number, file name, notes or voucher number contain it. Rows are
short and show the first 12 characters of the id, which is enough for get_item and update_item.""",
    annotations=_READS,
    structured_output=False,
)
async def list_items(
    ctx: Context,
    status: Annotated[Literal['todo', 'new', 'prepped', 'booked', 'skipped', 'all'], Field()] = 'todo',
    text: Annotated[str | None, Field(description='Only items containing this text.', max_length=100)] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> list:
    user = _user(ctx)
    items = await _store(store.all_items, user)
    found = store.search(items, status, text)
    return [
        _text(
            {
                'counts': store.counts(items),
                'showing': f'{min(len(found), limit)} of {len(found)} ({status})',
                'items': [_row(i) for i in found[:limit]],
            }
        )
    ]


@mcp.tool(
    description="""One item in full: facts, proposed kontering with totals, status, booking details, what is
missing before it can be prepped, warnings, and its history. kontering_accounts has the user's own
entries for the accounts in the kontering, with their VAT box, NE row and SRU code when the user's chart
has them. id is the item's id or its first 8 or more characters. Read an item before changing it.""",
    annotations=_READS,
    structured_output=False,
)
async def get_item(
    id: Annotated[str, Field(description='Item id, or the start of it.', min_length=8, max_length=64)],
    ctx: Context,
) -> list:
    user = _user(ctx)
    item = await _store(store.get_item, user, id)
    return [_text(await _store(_view, user, item, history=True))]


@mcp.tool(
    description="""Change an item: its facts, proposed kontering, status or notes. Only what you send changes.

facts: send only the facts to change; an empty string clears one. kontering replaces the whole proposal:
every line has an account and either debit or credit, in SEK, and the two sides should balance; an
empty list of lines clears it.

Another currency than SEK: give facts.exchange_rate (SEK for one unit, e.g. "11.4235"; amount_sek is
worked out from it), or facts.amount_sek as the card or bank statement shows it. Not both, unless they
agree to the öre. Say where it comes from in facts.exchange_rate_source ("receipt", "card statement",
"Riksbanken 2026-10-19"). With no rate on the receipt or a statement, call get_riksbank_rate.

status:
- prepped: the facts date, amount and currency are in place, the total in SEK is known when the
  currency is not SEK, and the kontering balances with at least two lines. Set it only when you
  have no open question about the item; with a doubt, leave it new. It means "ready for the user
  to book", not that it is correct: the user checks it.
- booked: the user has booked it in their bookkeeping service. Send voucher (the voucher number there,
  e.g. "A 12"). Only do this when the user tells you the number. Never guess a voucher number.
  receipt_attached=true says the user has archived the files, in the bookkeeping service or in a
  folder of their own; send it only when the user says so. It is not needed for booked.
- skipped: it should not be booked (private purchase, not a receipt, same purchase as another item).
  Send skip_reason.
- new: back to the start, for unfinished work.

A prepped or booked item must stay complete: to save unfinished changes, set status to new as well.""",
    annotations=_CHANGES,
    structured_output=False,
)
async def update_item(
    id: Annotated[str, Field(description='Item id, or the start of it.', min_length=8, max_length=64)],
    ctx: Context,
    facts: Annotated[Facts | None, Field(description='Facts to change.')] = None,
    kontering: Annotated[Kontering | None, Field(description='The whole proposed booking, in SEK.')] = None,
    status: Annotated[Literal['new', 'prepped', 'booked', 'skipped'] | None, Field()] = None,
    voucher: Annotated[
        str | None, Field(description='Voucher number in the bookkeeping service.', max_length=50)
    ] = None,
    receipt_attached: Annotated[
        bool | None,
        Field(
            description='The user has archived the files, in the bookkeeping service or a folder of their own. '
            'Optional, only when the user says so.'
        ),
    ] = None,
    skip_reason: Annotated[str | None, Field(description='Why it is skipped.', max_length=300)] = None,
    notes: Annotated[str | None, Field(description='Replaces the notes.', max_length=4000)] = None,
) -> list:
    user = _user(ctx)
    changes = {'facts': _given(facts), 'status': status, 'voucher': voucher}
    changes |= {'receipt_attached': receipt_attached, 'skip_reason': skip_reason}
    if kontering is not None:
        changes['kontering'] = _given(kontering)
    if notes is not None:
        changes['notes'] = notes
    item = await _store(store.update_item, user, id, via='mcp', **changes)
    return [_text(await _store(_view, user, item))]


@mcp.tool(
    description="""The user's own list of accounts (number and name), if they have supplied one in the web
page. Use these account numbers in a kontering. An empty list means the user has not supplied any:
propose accounts as usual and say in kontering.note that they are unchecked against the user's chart.
TallyQueue does not include the BAS chart of accounts.

An account can carry the codes the user's own chart gives it, as their bookkeeping program has them:
vat_box, the box in the VAT return (momsruta) its amounts are reported in; ne_row, the row in the NE
appendix (bokslut); sru, the SRU code. They say where an account's amounts end up, which helps to
see what an account is for, such as which VAT box a reverse-charge account feeds. They are the
user's data, not a rule: which accounts fit a document stays a proposal the user checks.

accounts: only these account numbers, such as the ones in a kontering. Leave it out for the whole
list.""",
    annotations=_READS,
    structured_output=False,
)
async def list_accounts(
    ctx: Context,
    accounts: Annotated[
        list[str] | None, Field(description='Only these account numbers.', max_length=100)
    ] = None,
) -> list:
    found = await _store(store.read_accounts, _user(ctx))
    total = len(found)
    if accounts:
        wanted = {a.strip() for a in accounts}
        found = [a for a in found if a['account'] in wanted]
        missing = sorted(wanted - {a['account'] for a in found})
        return [_text({'count': total, 'accounts': found, 'not_in_list': missing})]
    return [_text({'count': total, 'accounts': found})]


@mcp.tool(
    description="""Add accounts to the user's own list of accounts.

Only when the user asks you, in so many words, to add accounts to their list. Never as part of adding
or changing receipts, and never to make a kontering pass a check: an account that is not in the list
is a warning for the user, not something for you to fix.

It works only while the user has switched it on in the web page, under Accounts, and it switches itself
off again after an hour. While it is off this call is refused. Then tell the user so; do not ask them to
switch it on unless adding accounts is what they asked for.

It only adds. An account number that is already in the list keeps its name and codes and comes back
under already_there; changing or removing accounts is done in the web page. Use the numbers and names the user
gives you or points you to, such as the chart in their bookkeeping service.""",
    annotations=_ADDS,
    structured_output=False,
)
async def add_accounts(
    accounts: Annotated[
        list[Account], Field(description='The accounts to add.', min_length=1, max_length=500)
    ],
    ctx: Context,
) -> list:
    user = _user(ctx)
    added, already = await _store(
        store.add_accounts, user, [a.model_dump(exclude_none=True) for a in accounts]
    )
    count = len(await _store(store.read_accounts, user))
    return [_text({'added': added, 'already_there': already, 'count': count})]


@mcp.tool(
    description="""Riksbanken's published exchange rate for a currency on a date: SEK for one unit of it.

Use it for a document in another currency than SEK, and only when neither the document nor a card or
bank statement gives a rate or the total in SEK. date: the payment date when the document or a
statement gives it (for a card purchase, the day of the purchase), otherwise the date on the
document. On a weekend or a holiday the answer is the latest rate before the date, and says which
date it is from.

Nothing is stored by this call. Give the answer to update_item: rate as facts.exchange_rate and source
as facts.exchange_rate_source. Riksbanken publishes its rates for information only; the user checks
the figure. Riksbanken allows only a few lookups a minute, so look up one rate per document.""",
    annotations=_LOOKS_UP,
    structured_output=False,
)
async def get_riksbank_rate(
    currency: Annotated[str, Field(description='Three-letter code: EUR, USD.', min_length=3, max_length=3)],
    date: Annotated[str, Field(description='The date the rate is for, YYYY-MM-DD.')],
    ctx: Context,
) -> list:
    _user(ctx)
    try:
        found = await anyio.to_thread.run_sync(lambda: riksbank.rate(currency, date))
    except riksbank.RateUnavailable as e:
        raise ToolError(str(e)) from None
    return [_text(found | {'unit': f'SEK for 1 {found["currency"]}'})]


# ---------------------------------------------------------------- HTTP routes
@mcp.custom_route('/health', methods=['GET'])
async def health(request: Request) -> Response:
    return JSONResponse({'status': 'ok', 'version': __version__})


@mcp.tool(
    description="""A one-use ticket to upload one file to POST /upload, for when you have the file on disk
and can run a command such as curl, but not the user's token. With item_id, the file is added to that
item; without, it makes a new item.

This is the way to add a file of any real size. add_item takes the file as base64 in the call, which
suits only a small text receipt: never write out a PDF or an image as base64. When you can neither
run curl nor send the file some other way, ask the user to add the file in the web page instead,
then find it with list_items.

The reply has the ticket and the command to run. The ticket is good for one upload within a few
minutes: it is used up by the first try, also one that is refused, so take a new ticket for each
file and for each new try. Give sha256, the SHA-256 of the file as 64 hex digits (sha256sum or
shasum -a 256), when you can: then only that exact file is taken, so a ticket that is seen by
anyone else is of no use to them. Keep the ticket out of anything you write for the user.

The upload replies with the item's id; add the facts and a proposed kontering with update_item. The
same file again adds nothing: it returns the item that is there. A file that another item has is
refused when it is meant for item_id.""",
    annotations=_TICKET,
    structured_output=False,
)
async def upload_ticket(
    ctx: Context,
    sha256: Annotated[
        str | None,
        Field(description='The SHA-256 of the file, 64 hex digits. Then only that file is taken.'),
    ] = None,
    filename: Annotated[
        str | None,
        Field(description="The file's name, for the command in the reply.", max_length=200),
    ] = None,
    item_id: Annotated[
        str | None,
        Field(
            description='The item to add the file to, or its first 8 or more characters. Leave out for a new item.'
        ),
    ] = None,
    kind: FileKind = None,
) -> list:
    user = _user(ctx)
    digest = sha256.strip().lower() if sha256 else None
    target = (await _store(store.get_item, user, item_id))['id'] if item_id else None
    try:
        ticket, expires = TICKETS.create(user, digest, target, kind)
    except ValueError as e:
        raise ToolError(str(e)) from None
    name = (filename or '').strip() or 'receipt.pdf'
    quoted = shlex.quote(name)
    command = (
        f'curl -fsS -X POST --data-binary @{quoted} '
        f'-H "X-Upload-Ticket: {ticket}" -H "X-Filename: {quote(name)}" {config.PUBLIC_URL}/upload'
    )
    return [
        _text(
            {
                'ticket': ticket,
                'header': 'X-Upload-Ticket',
                'url': f'{config.PUBLIC_URL}/upload',
                'expires': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires)),
                'only_this_file': digest,
                'to_item': target,
                'command': command,
                'next': 'Run the command where the file is.'
                + ('' if target else ' The reply has the new item id: then update_item.'),
            }
        )
    ]


class TooLarge(Exception):
    pass


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refused as soon as it is known to be larger than the limit."""
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > limit:
        raise TooLarge
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise TooLarge
        chunks.append(chunk)
    return b''.join(chunks)


@mcp.custom_route('/upload', methods=['POST'])
async def upload(request: Request) -> Response:
    """Add a receipt file as a new item: the raw bytes in the body, the token in X-API-Key.

    For clients that have the file on disk and a shell, where base64 in a tool call is clumsy.
    Instead of the token, an upload ticket from the upload_ticket tool in X-Upload-Ticket: it is
    spent by this request whatever comes of it, and takes only its own file when it names one.
    """
    ticket = (request.headers.get(auth.TICKET_HEADER) or '').strip()
    only = None
    if ticket:
        found = TICKETS.spend(ticket)
        if not found:
            return JSONResponse({'error': f'unauthorized: {auth.ticket_refused(ticket)}'}, status_code=401)
        user, only, target, kind = found.user, found.sha256, found.item, found.kind
    else:
        # With the token, a script may name the item and the kind itself.
        target = (request.headers.get('x-item-id') or '').strip() or None
        kind = (request.headers.get('x-file-kind') or '').strip() or None
        user = auth.user_for_headers(request.headers)
        if not user:
            why = auth.why_refused(auth.token_from_headers(request.headers))
            return JSONResponse(
                {
                    'error': f'unauthorized: {why} Send your TallyQueue token as X-API-Key, '
                    'or an upload ticket from the upload_ticket tool as X-Upload-Ticket.'
                },
                status_code=401,
            )
    try:
        data = await read_body(request, store.MAX_RECEIPT_BYTES)
    except TooLarge:
        return JSONResponse({'error': 'the file is too large'}, status_code=413)
    if only and hashlib.sha256(data).hexdigest() != only:
        return JSONResponse(
            {
                'error': 'This ticket was made for another file: the SHA-256 of what was sent is not '
                'the one given for the ticket. The ticket is used up; get a new one for this file.'
            },
            status_code=403,
        )
    filename = unquote(request.headers.get('x-filename', ''))
    try:
        if target:
            item, created = await anyio.to_thread.run_sync(
                lambda: store.add_file(user, target, data, filename, via='upload', kind=kind)
            )
        else:
            item, created = await anyio.to_thread.run_sync(
                lambda: store.add_item(user, data, filename, via='upload', kind=kind)
            )
    except store.NotFound as e:
        return JSONResponse({'error': str(e)}, status_code=404)
    except (store.StoreError, model.Invalid) as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    sha256 = hashlib.sha256(data).hexdigest()
    if target:
        nxt = (
            'The file was added to the item.'
            if created
            else 'The item already has this file. Nothing was changed.'
        )
    elif created:
        nxt = 'Call update_item with this id to add the facts and a proposed kontering.'
    else:
        nxt = 'This file was already in the list. Nothing was changed.'
    reply = {
        'id': item['id'],
        'created': created,
        'status': item['status'],
        'file': _file_out(store.find_file(item, sha256) or {}),
        'files': len(item['files']),
        'web_page': f'{config.PUBLIC_URL}/items/{item["id"]}',
        'next': nxt,
    }
    return JSONResponse(reply, status_code=201 if created else 200)


for _path, _methods, _handler in web.ROUTES:
    mcp.custom_route(_path, methods=_methods)(_handler)


# ---------------------------------------------------------------- app
# Sent with every response that does not set the header itself. Pages add their own policy.
BASE_HEADERS = {
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'same-origin',
    'x-frame-options': 'DENY',
    'x-robots-tag': 'noindex, nofollow',
    'cache-control': 'no-store',
    'content-security-policy': "default-src 'none'; frame-ancestors 'none'",
    'permissions-policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
}
if config.SECURE:
    BASE_HEADERS['strict-transport-security'] = 'max-age=31536000'


def build_app():
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host='0.0.0.0',
        # A base64 file is a third larger than the file; leave room for the rest of the call.
        max_request_body_size=store.MAX_RECEIPT_BYTES * 4 // 3 + 256 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=['127.0.0.1:*', 'localhost:*', config.PUBLIC_HOST],
            allowed_origins=['http://127.0.0.1:*', 'http://localhost:*', config.PUBLIC_URL],
        ),
    )

    async def guarded(scope, receive, send):
        if scope['type'] != 'http':
            return await app(scope, receive, send)
        headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope['headers']}

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                present = {k.lower() for k, _ in message.get('headers', [])}
                extra = [
                    (k.encode(), v.encode()) for k, v in BASE_HEADERS.items() if k.encode() not in present
                ]
                message = {**message, 'headers': [*message.get('headers', []), *extra]}
            await send(message)

        async def refuse(status: int, error: str):
            await JSONResponse({'error': error}, status_code=status)(scope, receive, send_with_headers)

        if not config.host_allowed(headers.get('host', '')):
            return await refuse(421, 'this server does not answer to that host name')
        # The MCP endpoint is for approved users only. The tools check the token again.
        on_mcp = scope['path'] == '/mcp' or scope['path'].startswith('/mcp/')
        if on_mcp and not auth.user_for_headers(headers):
            why = auth.why_refused(auth.token_from_headers(headers))
            return await refuse(401, f'unauthorized: {why} Send your TallyQueue token as X-API-Key.')
        await app(scope, receive, send_with_headers)

    return guarded


def main():
    import uvicorn

    if not auth.USERS:
        print('TALLYQUEUE_USERS is empty: nobody can sign in. See README, "Run it".', flush=True)
    options = {
        'host': os.environ.get('HOST', '0.0.0.0'),
        'port': int(os.environ.get('PORT', '8000')),
        'proxy_headers': False,
        'server_header': False,
        'log_level': 'info',
        # No request log: a search in the list would put names from the bookkeeping in it.
        'access_log': False,
    }
    if os.environ.get('TALLYQUEUE_RELOAD') == '1':  # development: restart when the source changes
        uvicorn.run(
            'tallyqueue.server:build_app', factory=True, reload=True, reload_dirs=['tallyqueue'], **options
        )
    else:
        uvicorn.run(build_app(), **options)
