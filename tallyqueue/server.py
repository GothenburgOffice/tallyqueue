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
import json
import os
from typing import Annotated, Literal
from urllib.parse import unquote

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import TextContent
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__, auth, config, model, riksbank, store, web

INSTRUCTIONS = """TallyQueue is the user's to-do list of receipts and invoices that are waiting to be booked.
It is a helper, not a ledger: the user does the bookkeeping in their own bookkeeping service, and
checks every figure first.

- For each receipt or invoice you find (for example in the user's mail), add it once: add_item with
  the file, or POST the file to /upload and then update_item. An item's id is the SHA-256 of its
  file, so adding the same file again changes nothing.
- Give only facts that are on the document. Leave a fact out rather than guess. Amounts are
  strings with two decimals ("1250.00").
- The kontering is a proposal for the user to check, in SEK, and it must balance. Use accounts
  from list_accounts when the user has supplied their list. Put what you were unsure about in
  kontering.note. You give no accounting or tax advice.
- The books are kept in SEK. For a document in another currency, give the total in SEK one of two
  ways: amount_sek as it is, when the receipt or the card or bank statement shows what was drawn;
  or exchange_rate (SEK for one unit of the currency), and amount_sek is worked out from it. When
  neither the receipt nor a statement gives it, get Riksbanken's rate with get_riksbank_rate. Always
  say where the figure comes from in exchange_rate_source. Never estimate a rate.
- Statuses: new, then prepped (facts and a balanced kontering are in place), then booked (the user
  booked it in their bookkeeping service). Mark an item booked only when the user tells you the
  voucher number and that the receipt is attached there. Use skipped, with a reason, for what
  should not be booked.
"""

# Amounts are text in the schema too, so a client is never invited to send a float.
Amount = str | None


class Facts(BaseModel):
    """What the document itself says. Leave out what is not on it."""

    model_config = ConfigDict(extra='forbid')

    kind: Literal['receipt', 'invoice', 'credit_note', 'other'] | None = None
    date: str | None = Field(None, description='The date on the document, YYYY-MM-DD.')
    counterparty: str | None = Field(None, description='The seller on a purchase, the customer on a sale.')
    description: str | None = Field(None, description='What it is for, in a few words.')
    amount: Amount = Field(
        None,
        description='Total including VAT in the document currency, two decimals: "1250.00". '
        'Negative for a credit note.',
    )
    currency: str | None = Field(None, description='Three-letter code: SEK, EUR, USD.')
    vat_amount: Amount = Field(None, description='VAT on the document, in the document currency.')
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
    invoice_number: str | None = None
    due_date: str | None = Field(None, description='YYYY-MM-DD.')
    payment_date: str | None = Field(None, description='When it was paid, YYYY-MM-DD, if known.')


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
        'file': item['receipt']['filename'],
    }
    return {k: v for k, v in row.items() if v is not None}


def _view(user: str, item: dict, *, history: bool = False) -> dict:
    debit, credit = model.totals(item['kontering'])
    out = {
        'id': item['id'],
        'status': item['status'],
        'facts': {k: v for k, v in item['facts'].items() if v is not None},
        'kontering': item['kontering'],
        'kontering_totals': {'debit': f'{debit:.2f}', 'credit': f'{credit:.2f}', 'balanced': debit == credit},
        'booking': item['booking'],
        'skip_reason': item['skip_reason'],
        'notes': item['notes'],
        'source': item['source'],
        'receipt': {k: item['receipt'][k] for k in ('filename', 'media_type', 'bytes')},
        'created': item['created'],
        'updated': item['updated'],
        'missing_for_prepped': model.prep_problems(item) if item['status'] == 'new' else [],
        'warnings': model.warnings(item, store.read_accounts(user), store.all_items(user)),
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
    description=f"""Add a receipt or invoice to the user's list of things to book. Returns the item.

The file is required: {store.ACCEPTED}. Its SHA-256 is the item's id. If the same file is already in the
list, nothing changes and the existing item comes back with created=false, so scanning an inbox twice
is safe. To change an existing item, use update_item.

Send the file base64-encoded in file_base64. For a file too large to send that way, upload the raw
bytes instead and then call update_item with the id from the reply:
  curl -fsS -X POST --data-binary @invoice.pdf -H "X-API-Key: <token>" -H "X-Filename: invoice.pdf" {config.PUBLIC_URL}/upload

facts: only what the document says (date, counterparty, amount, currency, VAT). Leave out what is not
there; never guess. For another currency than SEK, also give exchange_rate or amount_sek, with
exchange_rate_source (see update_item). kontering: a proposal in SEK that balances, for the user to
check; use accounts from list_accounts when there are any. source: where the document came from, such
as the sender, date and subject of the mail.""",
    structured_output=False,
)
async def add_item(
    file_base64: Annotated[str, Field(description='The file, base64-encoded.', min_length=1)],
    ctx: Context,
    filename: Annotated[str | None, Field(description='The file name, e.g. invoice-1042.pdf.')] = None,
    source: Annotated[str | None, Field(description='Where it came from.', max_length=300)] = None,
    facts: Annotated[Facts | None, Field(description='What the document says.')] = None,
    kontering: Annotated[Kontering | None, Field(description='Proposed booking, in SEK.')] = None,
    notes: Annotated[
        str | None, Field(description='Anything else the user should know.', max_length=4000)
    ] = None,
) -> list:
    user = _user(ctx)
    raw = file_base64.strip()
    if raw.startswith('data:') and ',' in raw:
        raw = raw.split(',', 1)[1]
    try:
        data = base64.b64decode(''.join(raw.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ToolError('file_base64 is not valid base64. Send the file bytes, base64-encoded.') from None
    item, created = await _store(
        store.add_item,
        user,
        data,
        filename or '',
        via='mcp',
        source=source,
        facts=_given(facts),
        kontering=_given(kontering),
        notes=notes,
    )
    view = await _store(_view, user, item)
    note = (
        None
        if created
        else 'This file was already in the list. Nothing was changed; use update_item to change it.'
    )
    return [_text({'created': created, 'note': note, 'item': view})]


@mcp.tool(
    description="""List the user's items, newest document first, with how many there are in each status.
status: todo (new and prepped, the default), new, prepped, booked, skipped or all. text narrows to items
whose counterparty, description, invoice number, file name, notes or voucher number contain it. Rows are
short and show the first 12 characters of the id, which is enough for get_item and update_item.""",
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
missing before it can be prepped, warnings, and its history. id is the item's id or its first 8 or more
characters. Read an item before changing it.""",
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
  currency is not SEK, and the kontering balances with at least two lines. It means "ready for the
  user to book", not that it is correct: the user checks it.
- booked: the user has booked it in their bookkeeping service. Send voucher (the voucher number there,
  e.g. "A 12") and receipt_attached=true (the receipt is attached to that voucher there). Only do this
  when the user tells you both. Never guess a voucher number.
- skipped: it should not be booked (private purchase, not a receipt, same purchase as another item).
  Send skip_reason.
- new: back to the start, for unfinished work.

A prepped or booked item must stay complete: to save unfinished changes, set status to new as well.""",
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
        bool | None, Field(description='The receipt is attached to the voucher in the bookkeeping service.')
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
TallyQueue does not include the BAS chart of accounts.""",
    structured_output=False,
)
async def list_accounts(ctx: Context) -> list:
    accounts = await _store(store.read_accounts, _user(ctx))
    return [_text({'count': len(accounts), 'accounts': accounts})]


@mcp.tool(
    description="""Riksbanken's published exchange rate for a currency on a date: SEK for one unit of it.

Use it for a document in another currency than SEK, and only when neither the document nor a card or
bank statement gives a rate or the total in SEK. date: the payment date when it is known, otherwise
the date on the document. On a weekend or a holiday the answer is the latest rate before the date,
and says which date it is from.

Nothing is stored by this call. Give the answer to update_item: rate as facts.exchange_rate and source
as facts.exchange_rate_source. Riksbanken publishes its rates for information only; the user checks
the figure. Riksbanken allows only a few lookups a minute, so look up one rate per document.""",
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
    """
    user = auth.user_for_headers(request.headers)
    if not user:
        return JSONResponse({'error': 'unauthorized: send your token as X-API-Key'}, status_code=401)
    try:
        data = await read_body(request, store.MAX_RECEIPT_BYTES)
    except TooLarge:
        return JSONResponse({'error': 'the file is too large'}, status_code=413)
    filename = unquote(request.headers.get('x-filename', ''))
    try:
        item, created = await anyio.to_thread.run_sync(
            lambda: store.add_item(user, data, filename, via='upload')
        )
    except (store.StoreError, model.Invalid) as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    reply = {
        'id': item['id'],
        'created': created,
        'status': item['status'],
        'receipt': {k: item['receipt'][k] for k in ('filename', 'media_type', 'bytes')},
        'web_page': f'{config.PUBLIC_URL}/items/{item["id"]}',
        'next': 'Call update_item with this id to add the facts and a proposed kontering.'
        if created
        else 'This file was already in the list. Nothing was changed.',
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
            return await refuse(401, 'unauthorized: send your TallyQueue token as X-API-Key')
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
