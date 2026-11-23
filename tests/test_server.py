# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end tests: the real server app on a local port, a real MCP client and plain HTTP.

Everything is made up: the users, the tokens, the companies, the amounts and the "receipt"
files, which are built here in code. No real bookkeeping data belongs in this repository.
"""

import asyncio
import base64
import datetime
import hashlib
import html
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib

import pytest

ALICE, BOB = 'tq_made-up-test-token-for-alice-0123456789', 'tq_made-up-test-token-for-bob-9876543210'


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
BASE = f'http://127.0.0.1:{PORT}'
os.environ.update(
    {
        'DATA_DIR': tempfile.mkdtemp(prefix='tallyqueue-test-'),
        'PUBLIC_URL': BASE,
        'MAX_RECEIPT_BYTES': str(200 * 1024),
        'TALLYQUEUE_USERS': ','.join(
            f'{name}:{hashlib.sha256(token.encode()).hexdigest()}'
            for name, token in (('alice', ALICE), ('bob', BOB))
        ),
    }
)

import uvicorn  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

from tallyqueue import model, server, store  # noqa: E402

_srv = uvicorn.Server(uvicorn.Config(server.build_app(), host='127.0.0.1', port=PORT, log_level='warning'))
threading.Thread(target=_srv.run, daemon=True).start()
for _ in range(100):
    try:
        urllib.request.urlopen(f'{BASE}/health', timeout=1)
        break
    except Exception:
        time.sleep(0.1)


# ---------------------------------------------------------------- made-up receipts
def pdf(text: str) -> bytes:
    return f'%PDF-1.4\n% made-up test receipt\n({text})\n%%EOF\n'.encode()


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    header = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', header)
        + chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff'))
        + chunk(b'IEND', b'')
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


HOSTING = pdf('Exempel Hosting AB, invoice 1042, 1250.00 SEK')
FACTS = {
    'kind': 'invoice',
    'date': '2026-10-03',
    'counterparty': 'Exempel Hosting AB',
    'description': 'Web hosting, October',
    'amount': '1250.00',
    'currency': 'SEK',
    'vat_amount': '250.00',
    'invoice_number': '1042',
}
BALANCED = {
    'lines': [
        {'account': '6230', 'debit': '1000.00'},
        {'account': '2640', 'debit': '250.00'},
        {'account': '2440', 'credit': '1250.00', 'text': 'Exempel Hosting AB 1042'},
    ],
    'vat_treatment': '25 % Swedish VAT',
    'note': 'Made-up proposal for a test',
}


# ---------------------------------------------------------------- MCP client
async def _call(tool, args, headers):
    async with (
        create_mcp_http_client(headers=headers) as client,
        streamable_http_client(f'{BASE}/mcp', http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(tool, args)


def call(tool, args=None, token=ALICE):
    """(reply as a dict, error text). One of them is None."""
    result = asyncio.run(_call(tool, args or {}, {'X-API-Key': token}))
    text = result.content[0].text
    return (None, text) if result.is_error else (json.loads(text), None)


def ok(tool, args=None, token=ALICE):
    reply, error = call(tool, args, token)
    assert error is None, error
    return reply


def refused(tool, args=None, token=ALICE):
    reply, error = call(tool, args, token)
    assert error is not None, reply
    return error


# ---------------------------------------------------------------- plain HTTP
def fetch(method, path, body=None, headers=None):
    """(status, headers, body) without following redirects."""
    conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, data


class Browser:
    """Just enough of a browser: keeps the session cookie and sends forms with their CSRF token."""

    def __init__(self):
        self.cookie = ''

    def get(self, path):
        return fetch('GET', path, headers={'Cookie': self.cookie})

    def post(self, path, fields, origin=BASE, csrf=True):
        fields = dict(fields)
        if csrf:
            fields.setdefault('csrf', self.csrf())
        body = urllib.parse.urlencode(fields)
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': self.cookie}
        if origin:
            headers['Origin'] = origin
        status, reply_headers, data = fetch('POST', path, body, headers)
        if 'set-cookie' in reply_headers:
            self.cookie = reply_headers['set-cookie'].split(';')[0]
        return status, reply_headers, data

    def csrf(self):
        match = re.search(rb'name="csrf" value="([^"]+)"', self.get('/accounts')[2])
        return match.group(1).decode() if match else ''

    def sign_in(self, token):
        return self.post('/login', {'token': token}, csrf=False)

    def upload(self, filename, data):
        boundary = 'made-up-boundary-7d1'
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{self.csrf()}\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'.encode()
            + data
            + b'\r\n',
            f'--{boundary}--\r\n'.encode(),
        ]
        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Cookie': self.cookie,
            'Origin': BASE,
        }
        return fetch('POST', '/items', b''.join(parts), headers)


@pytest.fixture(scope='module')
def alice():
    browser = Browser()
    status, headers, _ = browser.sign_in(ALICE)
    assert status == 303 and headers['location'] == '/items'
    return browser


@pytest.fixture(scope='module')
def hosting():
    """The made-up hosting invoice, added over MCP with facts and a kontering."""
    reply = ok(
        'add_item',
        {
            'file_base64': b64(HOSTING),
            'filename': 'exempel-hosting-1042.pdf',
            'source': 'Mail from billing@example.test, 2026-10-03',
            'facts': FACTS,
            'kontering': BALANCED,
        },
    )
    assert reply['created'] is True
    return reply['item']


# ---------------------------------------------------------------- access
def test_health_is_open_and_says_nothing_private():
    status, _, body = fetch('GET', '/health')
    assert status == 200 and json.loads(body)['status'] == 'ok'


@pytest.mark.parametrize(
    ('headers', 'why'),
    [
        ({}, 'No token was sent'),
        ({'X-API-Key': '${TALLYQUEUE_TOKEN}'}, 'only a ${...} placeholder that was not filled in'),
        ({'X-API-Key': 'tq_wrong'}, 'not a TallyQueue token: it is 8 characters long'),
        ({'X-API-Key': 'my-own-password-that-is-long-enough-to-be-one'}, 'it does not start with tq_'),
        ({'X-API-Key': 'tq_' + 'x' * 43}, 'no entry in TALLYQUEUE_USERS is the SHA-256 of it'),
    ],
)
def test_mcp_refuses_requests_without_an_approved_token_and_says_why(headers, why):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    status, _, reply = fetch('POST', '/mcp', body, {'Content-Type': 'application/json', **headers})
    error = json.loads(reply)['error']
    assert status == 401 and error.startswith('unauthorized: ') and why in error
    # the refusal never repeats what was sent
    assert headers.get('X-API-Key', 'nothing sent') not in error


def test_an_unfilled_placeholder_gets_one_answer_wherever_it_comes_in():
    # A client keeps "${TALLYQUEUE_TOKEN}" in its settings and fills it in from the environment.
    # When nothing did, a header, an upload and the sign-in form all say so, in the same words.
    from tallyqueue import auth

    placeholder = '${TALLYQUEUE_TOKEN}'
    why = auth.why_refused(placeholder)
    assert 'only a ${...} placeholder that was not filled in' in why and placeholder not in why
    assert why != auth.why_refused('') and 'not a TallyQueue token' not in why
    assert auth.user_for_token(placeholder) is None

    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    for headers in ({'X-API-Key': placeholder}, {'Authorization': f'Bearer {placeholder}'}):
        for status, _, reply in (
            fetch('POST', '/mcp', body, {'Content-Type': 'application/json', **headers}),
            fetch('POST', '/upload', png(), headers),
        ):
            error = json.loads(reply)['error']
            assert status == 401 and why in error and placeholder not in error, headers
    browser = Browser()
    status, _, page = browser.sign_in(placeholder)
    assert status == 401 and why.encode() in page and placeholder.encode() not in page
    assert not browser.cookie


def test_bearer_token_works_too():
    result = asyncio.run(_call('list_items', {}, {'Authorization': f'Bearer {BOB}'}))
    assert not result.is_error


def test_requests_for_another_host_name_are_refused():
    status, _, _ = fetch('GET', '/health', headers={'Host': 'attacker.example'})
    assert status == 421


def test_only_full_length_tokens_are_accepted():
    # Nothing slows down guessing, so a short hand-made token must never work, even when its
    # SHA-256 is listed in TALLYQUEUE_USERS.
    from tallyqueue import auth

    short = 'tq_hunter2'
    auth.USERS['mallory'] = hashlib.sha256(short.encode()).hexdigest()
    try:
        assert auth.user_for_token(short) is None
        status, _, reply = fetch('POST', '/upload', png(), {'X-API-Key': short})
        assert status == 401 and b'not a TallyQueue token: it is 10 characters long' in reply
        # the sign-in page says the same, so a listed hash does not leave its owner guessing
        status, _, body = Browser().sign_in(short)
        assert status == 401 and b'not a TallyQueue token: it is 10 characters long' in body
    finally:
        del auth.USERS['mallory']
    assert auth.user_for_token(ALICE) == 'alice' and auth.user_for_token(ALICE[3:]) is None
    assert len(auth.new_token()) >= auth.MIN_TOKEN_LENGTH


def test_the_token_command_prints_a_token_and_its_pair():
    env = {k: v for k, v in os.environ.items() if k != 'TALLYQUEUE_USERS'}
    out = subprocess.run(
        [sys.executable, '-m', 'tallyqueue', 'token', 'carol'],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    token = re.search(r'^\s+(tq_\S+)$', out, re.M).group(1)
    assert f'carol:{hashlib.sha256(token.encode()).hexdigest()}' in out


# ---------------------------------------------------------------- MCP tools
def test_add_item_stores_the_receipt_under_its_checksum(hosting):
    assert hosting['id'] == sha(HOSTING)
    assert hosting['status'] == 'new' and hosting['facts'] == FACTS
    assert hosting['kontering_totals'] == {'debit': '1250.00', 'credit': '1250.00', 'balanced': True}
    assert hosting['web_page'] == f'{BASE}/items/{sha(HOSTING)}'
    folder = os.path.join(store.DATA_DIR, 'users', 'alice')
    with open(os.path.join(folder, 'receipts', f'{sha(HOSTING)}.pdf'), 'rb') as f:
        assert f.read() == HOSTING
    assert os.path.exists(os.path.join(folder, 'items', f'{sha(HOSTING)}.json'))


def test_adding_the_same_file_again_changes_nothing(hosting):
    reply = ok(
        'add_item',
        {'file_base64': b64(HOSTING), 'facts': {'amount': '9999.00', 'counterparty': 'Someone Else'}},
    )
    assert reply['created'] is False and 'already in the list' in reply['note']
    assert reply['item']['facts'] == FACTS
    assert ok('list_items', {'status': 'all'})['counts']['all'] == 1


def test_add_item_refuses_bad_input():
    assert 'not valid base64' in refused('add_item', {'file_base64': 'not base64!'})
    assert 'not accepted' in refused('add_item', {'file_base64': b64(b'PK\x03\x04\x00\x00zip')})
    assert 'more than two decimals' in refused(
        'add_item', {'file_base64': b64(pdf('never stored')), 'facts': {'amount': '12.345'}}
    )
    assert (
        ok('list_items', {'status': 'all'})['counts']['all'] <= 1
    )  # nothing was stored by the refused calls


def test_users_never_see_each_other(hosting):
    assert ok('list_items', {'status': 'all'}, BOB)['items'] == []
    assert 'No item' in refused('get_item', {'id': hosting['id']}, BOB)
    assert 'No item' in refused('update_item', {'id': hosting['id'], 'notes': 'mine now'}, BOB)


def test_list_items_filters_and_counts(hosting):
    reply = ok('list_items')
    assert reply['counts']['todo'] == reply['counts']['new'] + reply['counts']['prepped']
    row = next(r for r in reply['items'] if r['id'] == hosting['id'][:12])
    assert row['counterparty'] == 'Exempel Hosting AB' and row['amount'] == '1250.00'
    assert row['kontering'] == 'D 6230 1000.00, D 2640 250.00, C 2440 1250.00'
    assert [r['id'] for r in ok('list_items', {'text': 'hosting ab'})['items']] == [row['id']]
    assert ok('list_items', {'text': 'no such thing'})['items'] == []
    assert ok('list_items', {'status': 'booked'})['items'] == []


def test_the_status_flow_new_prepped_booked():
    data = pdf('Exempel Domain Registry, receipt 77, 149.00 SEK')
    item = ok('add_item', {'file_base64': b64(data), 'filename': 'domain.pdf'})['item']
    ref = item['id'][:10]  # the start of an id is enough
    assert item['missing_for_prepped'] == [
        'Missing facts: date, amount, currency',
        'The kontering needs at least two lines',
    ]
    assert 'Cannot be prepped' in refused('update_item', {'id': ref, 'status': 'prepped'})

    facts = {
        'date': '2026-09-14',
        'amount': '149.00',
        'currency': 'SEK',
        'counterparty': 'Exempel Domain Registry',
    }
    lopsided = {'lines': [{'account': '6230', 'debit': '119.20'}, {'account': '1930', 'credit': '149.00'}]}
    item = ok('update_item', {'id': ref, 'facts': facts, 'kontering': lopsided})
    assert item['kontering_totals']['balanced'] is False and any(
        'does not balance' in w for w in item['warnings']
    )
    assert 'does not balance' in refused('update_item', {'id': ref, 'status': 'prepped'})

    balanced = {
        'lines': [*lopsided['lines'][:1], {'account': '2640', 'debit': '29.80'}, lopsided['lines'][1]]
    }
    item = ok('update_item', {'id': ref, 'kontering': balanced, 'status': 'prepped'})
    assert item['status'] == 'prepped' and item['facts']['counterparty'] == 'Exempel Domain Registry'

    # a prepped item stays complete: an unbalanced kontering is refused, and nothing is saved
    assert 'Set the status back to new' in refused('update_item', {'id': ref, 'kontering': lopsided})
    assert ok('get_item', {'id': ref})['kontering_totals']['balanced'] is True

    assert 'voucher number' in refused('update_item', {'id': ref, 'status': 'booked'})
    assert 'receipt is attached' in refused('update_item', {'id': ref, 'status': 'booked', 'voucher': 'A 7'})
    item = ok('update_item', {'id': ref, 'status': 'booked', 'voucher': 'A 7', 'receipt_attached': True})
    assert item['status'] == 'booked'
    assert item['booking'] == {'voucher': 'A 7', 'receipt_attached': True, 'date': store.today()}
    assert 'voucher number' in refused('update_item', {'id': ref, 'voucher': ''})  # booked keeps its voucher

    history = [event['what'] for event in ok('get_item', {'id': ref})['history']]
    assert history[0] == 'added' and history[-1].endswith('status: prepped to booked')
    assert ok('list_items', {'status': 'booked'})['items'][0]['voucher'] == 'A 7'


def test_skipped_needs_a_reason_and_can_be_reopened():
    item = ok(
        'add_item',
        {'file_base64': b64(b'Tack for ditt kop! Privat lunch 129 kr (made up)'), 'filename': 'mail.txt'},
    )['item']
    assert item['receipt']['media_type'] == 'text/plain'
    assert 'Say why' in refused('update_item', {'id': item['id'], 'status': 'skipped'})
    item = ok('update_item', {'id': item['id'], 'status': 'skipped', 'skip_reason': 'Private purchase'})
    assert item['status'] == 'skipped' and item['skip_reason'] == 'Private purchase'
    assert ok('update_item', {'id': item['id'], 'status': 'new'})['status'] == 'new'


def test_add_item_can_set_prepped_or_skipped_in_the_same_call():
    facts = {'date': '2026-10-07', 'amount': '500.00', 'currency': 'SEK', 'counterparty': 'Exempel Papper AB'}
    lines = {'lines': [{'account': '6110', 'debit': '500.00'}, {'account': '1930', 'credit': '500.00'}]}

    # complete: prepped at once, and the history shows both steps
    receipt = pdf('Exempel Papper AB, receipt 77, 500.00 SEK (made up)')
    reply = ok(
        'add_item', {'file_base64': b64(receipt), 'facts': facts, 'kontering': lines, 'status': 'prepped'}
    )
    assert reply['created'] is True and reply['item']['status'] == 'prepped' and 'status_not_set' not in reply
    history = [event['what'] for event in ok('get_item', {'id': reply['item']['id']})['history']]
    assert history == ['added', 'status: new to prepped']

    # not complete: the file is in the list as new, and the answer says why it is not prepped
    invoice = pdf('Exempel Papper AB, invoice 78, 500.00 SEK (made up)')
    reply = ok('add_item', {'file_base64': b64(invoice), 'facts': facts, 'status': 'prepped'})
    assert reply['created'] is True and reply['item']['status'] == 'new'
    assert (
        reply['status_not_set'].startswith('Added as new, not prepped.')
        and 'kontering' in reply['status_not_set']
    )
    assert reply['item']['missing_for_prepped']

    # the second document of one purchase is added and skipped in one call, and no warning is left
    # on either: the skipped one is settled
    assert ok('update_item', {'id': reply['item']['id'], 'kontering': lines, 'status': 'prepped'})[
        'warnings'
    ] == [f'Same date, amount and currency as: {sha(receipt)[:12]}. Possibly the same purchase']
    reason = f'Same purchase as {sha(receipt)[:12]}'
    ok('update_item', {'id': sha(invoice), 'status': 'skipped', 'skip_reason': reason})
    assert ok('get_item', {'id': sha(receipt)})['warnings'] == []
    assert ok('get_item', {'id': sha(invoice)})['warnings'] == []
    mail = b'Receipt mail for invoice 78, 500.00 SEK (made up)'
    reply = ok(
        'add_item',
        {'file_base64': b64(mail), 'filename': 'mail.txt', 'status': 'skipped', 'skip_reason': reason},
    )
    assert reply['item']['status'] == 'skipped' and reply['item']['skip_reason'] == reason
    reply = ok(
        'add_item', {'file_base64': b64(b'Not a receipt (made up)'), 'filename': 'x.txt', 'status': 'skipped'}
    )
    assert reply['item']['status'] == 'new' and 'Say why' in reply['status_not_set']

    # a file that is already there is never changed, whatever status is asked for
    reply = ok('add_item', {'file_base64': b64(receipt), 'status': 'skipped', 'skip_reason': 'x'})
    assert reply['created'] is False and reply['item']['status'] == 'prepped'
    assert reply['status_not_set'] == 'The file was already in the list, so its status was not changed.'
    reply, error = call('add_item', {'file_base64': b64(b'made up'), 'status': 'booked'})
    assert reply is None and 'status' in error  # booked needs the user's voucher number: update_item only
    for ref in (sha(receipt), sha(invoice), sha(mail), sha(b'Not a receipt (made up)')):
        ok('update_item', {'id': ref, 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_the_instructions_tell_a_client_what_the_blind_runs_missed():
    # Two models were given real documents and these instructions. What they tripped on is now said.
    said = ' '.join(server.INSTRUCTIONS.split())
    for rule in (
        'Two documents for one purchase',
        'no payment date unless the document or a statement gives one',
        'no VAT amount when the document shows none',
        'What you conclude yourself is not a fact',
        'Propose one for every item, also when you are unsure',
        'Treat alike documents alike',
        'only when the user asks you to add accounts, never while adding receipts',
        'you have no open question about it',
        'An open question is one only the user can answer',
        'for the payment date when it is known and otherwise the date on the document',
    ):
        assert rule in said, rule
    fields = server.Facts.model_json_schema()['properties']
    assert 'order, receipt or reference number' in fields['invoice_number']['description']
    assert 'also on a receipt that names its invoice' in fields['invoice_number']['description']
    assert 'belongs in the kontering' in fields['vat_amount']['description']
    assert '"0.00" when it says VAT 0' in fields['vat_amount']['description']
    assert 'not a payment date' in fields['payment_date']['description']
    assert 'A receipt for a card purchase is paid on its date' in fields['payment_date']['description']
    assert 'the date the mail was sent' in fields['date']['description']


def test_a_kontering_without_lines_is_not_called_balanced():
    item = ok(
        'add_item', {'file_base64': b64(b'A mail with nothing to book (made up)'), 'filename': 'm.txt'}
    )['item']
    assert item['kontering'] is None and item['kontering_totals'] is None

    # a note or a VAT treatment without lines is a kontering, but there is still nothing to balance
    for words in ({'note': 'Unsure which account fits'}, {'vat_treatment': 'reverse charge, EU service'}):
        item = ok('update_item', {'id': item['id'], 'kontering': words})
        assert item['kontering']['lines'] == [] and item['kontering'] | words == item['kontering']
        assert item['kontering_totals'] is None
        assert 'The kontering needs at least two lines' in item['missing_for_prepped']

    # one line is something to balance, and it does not
    item = ok(
        'update_item', {'id': item['id'], 'kontering': {'lines': [{'account': '6540', 'debit': '99.00'}]}}
    )
    assert item['kontering_totals'] == {'debit': '99.00', 'credit': '0.00', 'balanced': False}
    ok('update_item', {'id': item['id'], 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_facts_can_be_cleared_and_unknown_fields_are_refused(hosting):
    item = ok('update_item', {'id': hosting['id'], 'facts': {'description': ''}})
    assert 'description' not in item['facts'] and item['facts']['amount'] == '1250.00'
    ok('update_item', {'id': hosting['id'], 'facts': {'description': FACTS['description']}})
    reply, error = call('update_item', {'id': hosting['id'], 'facts': {'colour': 'red'}})
    assert error is not None and reply is None


# ---------------------------------------------------------------- upload
def test_upload_takes_raw_bytes_and_is_idempotent():
    data = png()
    headers = {'X-API-Key': ALICE, 'X-Filename': 'kvitto%20foto.png'}
    status, _, body = fetch('POST', '/upload', data, headers)
    reply = json.loads(body)
    assert status == 201 and reply['created'] is True and reply['id'] == sha(data)
    assert reply['receipt'] == {'filename': 'kvitto foto.png', 'media_type': 'image/png', 'bytes': len(data)}
    status, _, body = fetch('POST', '/upload', data, headers)
    assert status == 200 and json.loads(body)['created'] is False
    assert ok('get_item', {'id': sha(data)})['history'][0]['via'] == 'upload'


def test_upload_refuses_strangers_unknown_types_and_large_files():
    assert fetch('POST', '/upload', png())[0] == 401
    assert fetch('POST', '/upload', b'MZ\x90\x00\x03\x00\x00\x00', {'X-API-Key': ALICE})[0] == 400
    assert fetch('POST', '/upload', b'', {'X-API-Key': ALICE})[0] == 400
    assert fetch('POST', '/upload', b'%PDF-1.4' + b'x' * 210 * 1024, {'X-API-Key': ALICE})[0] == 413


def test_an_upload_ticket_adds_one_file_for_its_user_and_is_then_used_up():
    data = pdf('Exempel Kontorsvaror AB, kvitto 7, 99.00 SEK (made up)')
    reply = ok('upload_ticket', {'filename': "kvitto 7 (o'hara).pdf"})
    ticket = reply['ticket']
    assert (
        ticket.startswith('tqu_') and reply['header'] == 'X-Upload-Ticket' and reply['only_this_file'] is None
    )
    # the command is ready to run, with the file name quoted for the shell
    assert f'-H "X-Upload-Ticket: {ticket}"' in reply['command'] and reply['command'].endswith(
        f'{BASE}/upload'
    )
    assert "@'kvitto 7 (o'\"'\"'hara).pdf'" in reply['command']

    status, _, body = fetch(
        'POST', '/upload', data, {'X-Upload-Ticket': ticket, 'X-Filename': 'kvitto-7.pdf'}
    )
    assert status == 201 and json.loads(body)['id'] == sha(data)
    assert ok('get_item', {'id': sha(data)})['history'][0]['via'] == 'upload'
    assert refused('get_item', {'id': sha(data)}, token=BOB)

    # used once, it is gone, and the refusal never repeats it
    status, _, body = fetch('POST', '/upload', pdf('another made-up file'), {'X-Upload-Ticket': ticket})
    assert status == 401 and b'used up or expired' in body and ticket.encode() not in body
    status, _, body = fetch('POST', '/upload', data, {'X-Upload-Ticket': 'tq_not-a-ticket'})
    assert status == 401 and b'not an upload ticket' in body

    # a ticket is BOB's when BOB's token made it
    bobs = pdf('made-up receipt for bob')
    ticket = ok('upload_ticket', token=BOB)['ticket']
    assert fetch('POST', '/upload', bobs, {'X-Upload-Ticket': ticket})[0] == 201
    assert ok('get_item', {'id': sha(bobs)}, token=BOB)['id'] == sha(bobs)
    assert refused('get_item', {'id': sha(bobs)})


def test_an_upload_ticket_for_one_file_takes_no_other():
    data = pdf('Exempel Frakt AB, faktura 12, 150.00 SEK (made up)')
    ticket = ok('upload_ticket', {'sha256': sha(data).upper()})['ticket']
    status, _, body = fetch(
        'POST', '/upload', pdf('not the file the ticket was for'), {'X-Upload-Ticket': ticket}
    )
    assert status == 403 and b'made for another file' in body
    # a refused try uses the ticket up as well
    assert fetch('POST', '/upload', data, {'X-Upload-Ticket': ticket})[0] == 401
    ticket = ok('upload_ticket', {'sha256': sha(data)})['ticket']
    assert fetch('POST', '/upload', data, {'X-Upload-Ticket': ticket})[0] == 201
    assert 'sha256: expected the 64 hex digits' in refused('upload_ticket', {'sha256': 'abc'})


def test_an_upload_ticket_opens_nothing_but_one_upload():
    ticket = ok('upload_ticket')['ticket']
    # not the MCP endpoint, in either header a token may come in
    assert fetch('POST', '/mcp', b'{}', {'X-API-Key': ticket, 'Content-Type': 'application/json'})[0] == 401
    assert fetch('POST', '/mcp', b'{}', {'Authorization': f'Bearer {ticket}'})[0] == 401
    # not the token's place on /upload, and not the web page's sign-in
    assert fetch('POST', '/upload', png(), {'X-API-Key': ticket})[0] == 401
    status, _, body = Browser().sign_in(ticket)
    assert status == 401 and b'does not start with tq_' in body
    # it was never spent by any of that, and still works for its one upload
    assert (
        fetch('POST', '/upload', pdf('made-up file after the refusals'), {'X-Upload-Ticket': ticket})[0]
        == 201
    )


def test_upload_tickets_expire_and_are_limited_per_user(monkeypatch):
    monkeypatch.setattr(server.TICKETS, 'seconds', -1)
    ticket = ok('upload_ticket')['ticket']
    status, _, body = fetch(
        'POST', '/upload', pdf('made-up file, ticket expired'), {'X-Upload-Ticket': ticket}
    )
    assert status == 401 and b'used up or expired' in body
    monkeypatch.undo()

    monkeypatch.setattr(server.TICKETS, 'per_user', 2)
    first = ok('upload_ticket')['ticket']
    ok('upload_ticket')
    assert 'You have 2 unused upload tickets' in refused('upload_ticket')
    assert ok('upload_ticket', token=BOB)['ticket']  # one user's tickets do not count for another
    assert fetch('POST', '/upload', pdf('made-up file, frees a ticket'), {'X-Upload-Ticket': first})[0] == 201
    assert ok('upload_ticket')['ticket']


# ---------------------------------------------------------------- web page
def test_pages_need_a_sign_in():
    for path in ('/items', f'/items/{sha(HOSTING)}', f'/receipts/{sha(HOSTING)}', '/accounts'):
        status, headers, _ = fetch('GET', path)
        assert status == 303 and headers['location'].startswith('/login?next=%2F'), path
    assert fetch('GET', '/')[1]['location'] == '/login'
    assert fetch('GET', '/login')[0] == 200


def test_sign_in_refuses_a_wrong_token_and_never_redirects_off_site():
    browser = Browser()
    status, _, body = browser.sign_in('tq_wrong')
    assert status == 401 and b'not a TallyQueue token: it is 8 characters long' in body and not browser.cookie
    # Text that is not a token and a token nobody has are two mistakes, and each is named.
    status, _, body = browser.sign_in('correct horse battery staple, forty characters')
    assert status == 401 and b'it does not start with tq_' in body and b'correct horse' not in body
    unknown = 'tq_' + 'x' * 43
    status, _, body = browser.sign_in(unknown)
    assert status == 401 and b'no entry in TALLYQUEUE_USERS is the SHA-256 of it' in body
    assert unknown.encode() not in body and not browser.cookie
    status, headers, _ = browser.post('/login', {'token': ALICE, 'next': '//attacker.example/x'}, csrf=False)
    assert status == 303 and headers['location'] == '/items'
    status, headers, _ = Browser().post('/login', {'token': ALICE, 'next': '/accounts'}, csrf=False)
    assert headers['location'] == '/accounts'


def test_the_session_cookie_is_http_only_and_same_site():
    conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    body = urllib.parse.urlencode({'token': ALICE})
    conn.request(
        'POST', '/login', body, {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': BASE}
    )
    cookie = conn.getresponse().getheader('set-cookie').lower()
    conn.close()
    assert 'httponly' in cookie and 'samesite=lax' in cookie and 'path=/' in cookie


def test_pages_carry_the_security_headers(alice):
    status, headers, body = alice.get('/items')
    assert status == 200
    policy = headers['content-security-policy']
    assert "default-src 'none'" in policy and 'script' not in policy and "frame-ancestors 'none'" in policy
    assert headers['x-content-type-options'] == 'nosniff' and headers['x-frame-options'] == 'DENY'
    assert headers['referrer-policy'] == 'same-origin' and headers['cache-control'] == 'no-store'
    assert b'<script' not in body
    assert b'not a ledger' in body and b'AGPL-3.0-or-later' in body


def test_the_list_and_the_item_page_show_the_item(alice, hosting):
    _, _, body = alice.get('/items?status=all')
    assert b'Exempel Hosting AB' in body and f'/items/{hosting["id"]}'.encode() in body
    status, headers, _ = alice.get(f'/items/{hosting["id"][:8]}')
    assert status == 303 and headers['location'] == f'/items/{hosting["id"]}'
    status, _, body = alice.get(f'/items/{hosting["id"]}')
    assert status == 200 and b'value="1250.00"' in body and b'value="6230"' in body and b'Balances' in body
    assert f'<iframe src="/receipts/{hosting["id"]}"'.encode() in body
    assert alice.get('/items/' + 'f' * 64)[0] == 404


def test_forms_from_elsewhere_or_without_the_csrf_token_are_refused(alice, hosting):
    path = f'/items/{hosting["id"]}/notes'
    assert alice.post(path, {'notes': 'x'}, origin='https://attacker.example')[0] == 403
    assert alice.post(path, {'notes': 'x'}, csrf=False)[0] == 403
    assert alice.post(path, {'notes': 'x', 'csrf': 'made-up'})[0] == 403
    assert ok('get_item', {'id': hosting['id']})['notes'] is None
    status, headers, _ = alice.post(path, {'notes': 'Checked against the made-up bank statement'})
    assert status == 303 and headers['location'].endswith('?msg=saved')
    assert ok('get_item', {'id': hosting['id']})['notes'] == 'Checked against the made-up bank statement'


def test_text_from_a_receipt_is_never_markup(alice):
    nasty = '<script>alert(1)</script>'
    item = ok(
        'add_item',
        {'file_base64': b64(pdf('made-up XSS probe')), 'facts': {'counterparty': nasty}, 'notes': nasty},
    )['item']
    for path in ('/items?status=all', f'/items/{item["id"]}'):
        body = alice.get(path)[2]
        assert nasty.encode() not in body and b'&lt;script&gt;alert(1)&lt;/script&gt;' in body
    ok('update_item', {'id': item['id'], 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_editing_through_the_web_page(alice):
    data = pdf('Exempel Kontorsvaror AB, receipt 5512, 399.00 SEK')
    status, headers, _ = alice.upload('kontorsvaror.pdf', data)
    path = f'/items/{sha(data)}'
    assert status == 303 and headers['location'] == f'{path}?msg=added'
    assert alice.upload('same-file-other-name.pdf', data)[1]['location'] == f'{path}?msg=duplicate'

    # a slip in a fact is reported, and what was typed is still in the form
    form = dict.fromkeys(
        (
            'kind',
            'counterparty',
            'description',
            'vat_amount',
            'amount_sek',
            'invoice_number',
            'due_date',
            'payment_date',
        ),
        '',
    )
    form |= {
        'date': '2026-11-02',
        'amount': '399,00',
        'currency': 'kr',
        'counterparty': 'Exempel Kontorsvaror AB',
    }
    status, _, body = alice.post(f'{path}/facts', form)
    assert status == 422 and b'not a three-letter code' in body and b'value="399,00"' in body
    assert alice.post(f'{path}/facts', form | {'currency': 'sek'})[0] == 303
    assert ok('get_item', {'id': sha(data)})['facts'] == {
        'date': '2026-11-02', 'counterparty': 'Exempel Kontorsvaror AB', 'amount': '399.00', 'currency': 'SEK'
    }  # fmt: skip

    lines = {'account_0': '6110', 'debit_0': '319,20', 'credit_0': '', 'text_0': 'Paper',
             'account_1': '2640', 'debit_1': '79,80', 'credit_1': '', 'text_1': '',
             'account_2': '1930', 'debit_2': '', 'credit_2': '399', 'text_2': '',
             'account_3': '', 'debit_3': '', 'credit_3': '', 'text_3': '',
             'vat_treatment': '25 % Swedish VAT', 'note': ''}  # fmt: skip
    assert alice.post(f'{path}/kontering', lines)[0] == 303
    item = ok('get_item', {'id': sha(data)})
    assert item['kontering_totals'] == {'debit': '399.00', 'credit': '399.00', 'balanced': True}
    assert item['history'][-1] == {'at': item['updated'], 'by': 'alice', 'via': 'web', 'what': 'kontering'}

    assert alice.post(f'{path}/status', {'to': 'prepped'})[0] == 303
    status, _, body = alice.post(f'{path}/kontering', lines | {'credit_2': '400'})
    assert status == 422 and b'does not balance' in body and b'value="400"' in body
    status, _, body = alice.post(f'{path}/status', {'to': 'booked', 'voucher': 'A 31'})
    assert status == 422 and b'receipt is attached' in body
    assert (
        alice.post(f'{path}/status', {'to': 'booked', 'voucher': 'A 31', 'receipt_attached': 'on'})[0] == 303
    )
    assert ok('get_item', {'id': sha(data)})['booking']['voucher'] == 'A 31'
    assert b'voucher <strong>A 31</strong>' in alice.get(path)[2]

    assert alice.post(f'{path}/delete', {})[0] == 422
    status, headers, _ = alice.post(f'{path}/delete', {'confirm': 'on'})
    assert status == 303 and headers['location'] == '/items?msg=deleted'
    assert alice.get(path)[0] == 404
    assert not os.path.exists(os.path.join(store.DATA_DIR, 'users', 'alice', 'receipts', f'{sha(data)}.pdf'))


def test_receipts_are_served_to_their_owner_only(alice, hosting):
    status, headers, body = alice.get(f'/receipts/{hosting["id"]}')
    assert status == 200 and body == HOSTING and headers['content-type'] == 'application/pdf'
    assert headers['x-content-type-options'] == 'nosniff' and headers['x-frame-options'] == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in headers['content-security-policy']
    assert headers['content-disposition'].startswith('inline')
    assert alice.get(f'/receipts/{hosting["id"]}?download')[1]['content-disposition'].startswith('attachment')
    bob = Browser()
    bob.sign_in(BOB)
    assert bob.get(f'/receipts/{hosting["id"]}')[0] == 404
    assert bob.get(f'/items/{hosting["id"]}')[0] == 404


def test_amounts_are_text_and_a_json_number_is_refused(hosting):
    for amount in (1250.5, 1250):
        reply, error = call('update_item', {'id': hosting['id'], 'facts': {'amount': amount}})
        assert reply is None and 'amount' in error and 'string' in error, error
    lines = [{'account': '6230', 'debit': 1250.0}, {'account': '2440', 'credit': '1250.00'}]
    reply, error = call('update_item', {'id': hosting['id'], 'kontering': {'lines': lines}})
    assert reply is None and 'debit' in error
    item = ok('get_item', {'id': hosting['id']})
    assert item['facts']['amount'] == '1250.00' and len(item['kontering']['lines']) == 3


def test_a_web_upload_may_be_as_large_as_the_receipt_limit(alice):
    # The form parser limits the size of text fields, not of the file part: a receipt well over
    # that field limit is stored, and one over MAX_RECEIPT_BYTES is refused before it is read.
    big = pdf('made-up large receipt ' + 'x' * 150 * 1024)
    assert 64 * 1024 < len(big) < store.MAX_RECEIPT_BYTES
    status, headers, _ = alice.upload('large.pdf', big)
    assert status == 303 and headers['location'] == f'/items/{sha(big)}?msg=added'
    assert alice.get(f'/receipts/{sha(big)}')[2] == big
    too_big = pdf('never stored ' + 'x' * (store.MAX_RECEIPT_BYTES + 70 * 1024))
    assert alice.upload('too-large.pdf', too_big)[0] == 413
    assert alice.get(f'/items/{sha(too_big)}')[0] == 404
    ok('update_item', {'id': sha(big), 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_an_svg_is_stored_as_text_and_never_served_as_an_image(alice):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><text>made up</text></svg>'
    for name in ('drawing.svg', 'renamed.txt'):
        assert alice.upload(name, svg)[0] == 303
    item = ok('get_item', {'id': sha(svg)})
    assert item['receipt']['media_type'] == 'text/plain'
    assert os.path.exists(os.path.join(store.DATA_DIR, 'users', 'alice', 'receipts', f'{sha(svg)}.txt'))
    status, headers, body = alice.get(f'/receipts/{sha(svg)}')
    assert status == 200 and body == svg
    assert headers['content-type'] == 'text/plain; charset=utf-8'
    assert 'sandbox' in headers['content-security-policy'] and headers['x-content-type-options'] == 'nosniff'
    assert b'<img src' not in alice.get(f'/items/{sha(svg)}')[2]
    ok('update_item', {'id': sha(svg), 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_an_html_receipt_is_shown_as_text_never_as_a_page(alice):
    page = b'<html><body><script>alert(1)</script>Kvitto 59 kr (made up)</body></html>'
    assert alice.upload('mail.html', page)[0] == 303
    status, headers, body = alice.get(f'/receipts/{sha(page)}')
    assert status == 200 and body == page
    assert (
        headers['content-type'].startswith('text/plain') and 'sandbox' in headers['content-security-policy']
    )
    ok('update_item', {'id': sha(page), 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_the_users_own_accounts(alice, hosting):
    assert ok('list_accounts') == {'count': 0, 'codes': {'vat_box': 0, 'ne_row': 0, 'sru': 0}, 'accounts': []}
    status, _, body = alice.post('/accounts', {'accounts': '1930 Bank\nnot an account line\n'})
    assert status == 422 and b'Line 2' in body and b'not an account line' in body
    source = '#KONTO 1930 "Företagskonto"\n2440 Leverantörsskulder\n2640 Ingående moms\n'
    assert alice.post('/accounts', {'accounts': source})[1]['location'] == '/accounts?msg=accounts'
    # every code field is there, null where the list gives none, so a client sees that they exist
    assert ok('list_accounts')['accounts'][0] == {
        'account': '1930',
        'name': 'Företagskonto',
        'vat_box': None,
        'ne_row': None,
        'sru': None,
    }
    assert ok('list_accounts', token=BOB)['count'] == 0
    warnings = ok('get_item', {'id': hosting['id']})['warnings']
    assert 'Not in your list of accounts: 6230' in warnings
    assert 'Företagskonto'.encode() in alice.get('/accounts')[2]


def test_an_ai_client_adds_accounts_only_while_the_user_has_switched_it_on(alice, monkeypatch):
    new = {
        'accounts': [
            {'account': '7010', 'name': 'Exempelkonto A'},
            {'account': '1930', 'name': 'Another name'},
        ]
    }
    # off until the user switches it on: the client reads the list and cannot change it
    assert 'switched off' in refused('add_accounts', new)
    assert ok('list_accounts')['count'] == 3
    page = alice.get('/accounts')[2].decode()
    assert 'Let the AI client add accounts for one hour' in page and 'Stop now' not in page

    # the user switches it on in the web page, and only for their own list
    assert alice.post('/accounts/client', {'allow': 'on'})[1]['location'] == '/accounts?msg=client_on'
    page = alice.get('/accounts')[2].decode()
    assert 'Stop now' in page and re.search(
        r'On until <strong>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC</strong>', page
    )
    assert 'switched off' in refused('add_accounts', new, token=BOB)

    # it only adds: an account that is there keeps its name, and sending the same again adds nothing
    none = {'vat_box': None, 'ne_row': None, 'sru': None}
    assert ok('add_accounts', new) == {
        'added': [{'account': '7010', 'name': 'Exempelkonto A'} | none],
        'already_there': [{'account': '1930', 'name': 'Företagskonto'} | none],
        'count': 4,
    }
    assert ok('add_accounts', new)['added'] == []
    assert 'the number is 4 to 6 digits' in refused(
        'add_accounts', {'accounts': [{'account': '70', 'name': 'x'}]}
    )
    twice = [{'account': '7020', 'name': 'a'}, {'account': '7020', 'name': 'b'}]
    assert 'in the list twice' in refused('add_accounts', {'accounts': twice})
    assert call('add_accounts', {'accounts': []})[0] is None
    assert ok('list_accounts')['count'] == 4

    # it switches itself off after an hour, and the user can stop it at once
    one = {'accounts': [{'account': '7020', 'name': 'Exempelkonto B'}]}
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=61)
    monkeypatch.setattr(store, 'now', lambda: later.strftime('%Y-%m-%dT%H:%M:%SZ'))
    assert 'switched off' in refused('add_accounts', one)
    monkeypatch.undo()
    assert ok('add_accounts', one)['count'] == 5
    assert alice.post('/accounts/client', {'allow': 'off'})[1]['location'] == '/accounts?msg=client_off'
    assert 'switched off' in refused('add_accounts', new)
    assert alice.post('/accounts/client', {'allow': 'on'}, csrf=False)[0] == 403
    assert 'switched off' in refused('add_accounts', new)
    assert [a['account'] for a in ok('list_accounts')['accounts']] == ['1930', '2440', '2640', '7010', '7020']


async def _tools() -> dict:
    """The tools as a client lists them, by name: what a client sees before it calls any."""
    async with (
        create_mcp_http_client(headers={'X-API-Key': ALICE}) as client,
        streamable_http_client(f'{BASE}/mcp', http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return {tool.name: tool for tool in (await session.list_tools()).tools}


def test_an_accounts_codes_reach_the_ai_client_and_survive_the_web_page(alice, hosting):
    before = ok('list_accounts')['accounts']
    source = (
        '2440 Leverantörsskulder B15 7382\n2640 Ingående moms F48 B14 7381\n6230 Datakommunikation R6 7501\n'
    )
    assert alice.post('/accounts', {'accounts': source})[1]['location'] == '/accounts?msg=accounts'

    # asked about some accounts, the client gets their codes, and hears which are not in the list
    assert ok('list_accounts', {'accounts': ['2640', '1111']}) == {
        'count': 3,
        'codes': {'vat_box': 1, 'ne_row': 3, 'sru': 3},
        'accounts': [
            {'account': '2640', 'name': 'Ingående moms', 'vat_box': 'F48', 'ne_row': 'B14', 'sru': '7381'}
        ],
        'not_in_list': ['1111'],
    }
    # an item shows the user's entries for the accounts in its kontering
    used = ok('get_item', {'id': hosting['id']})['kontering_accounts']
    assert [a['account'] for a in used] == ['2440', '2640', '6230'] and used[1]['vat_box'] == 'F48'
    assert used[0]['vat_box'] is None and used[2] == {
        'account': '6230',
        'name': 'Datakommunikation',
        'vat_box': None,
        'ne_row': 'R6',
        'sru': '7501',
    }

    # the descriptions tell a client the form of each code and how the user gets the list in
    tools = asyncio.run(_tools())
    listing = tools['list_accounts'].description
    assert 'null where' in listing and 'F48' in listing and '"R4/R8"' in listing
    assert '2611;Utgående moms 25 %;B10;B14;7381' in listing and 'SIE' in listing
    assert 'Never work a code out yourself' in tools['add_accounts'].description
    schema = tools['add_accounts'].input_schema['$defs']['Account']['properties']
    assert 'A to I' in schema['vat_box']['description'] and 'four digits' in schema['sru']['description']

    # the page shows the list so that saving it again keeps every code
    page = alice.get('/accounts')[2].decode()
    shown = html.unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>', page, re.S).group(1))
    assert '2640;Ingående moms;F48;B14;7381' in shown
    assert alice.post('/accounts', {'accounts': shown})[0] == 303
    assert (
        ok('list_accounts')['count'] == 3
        and ok('list_accounts', {'accounts': ['2640']})['accounts'][0]['sru'] == '7381'
    )

    # put back the list the other tests use
    assert alice.post('/accounts', {'accounts': '\n'.join(model.account_line(a) for a in before)})[0] == 303
    assert ok('list_accounts')['accounts'] == before


def test_each_tool_says_whether_it_writes_so_a_client_can_ask_first():
    async def tools():
        async with (
            create_mcp_http_client(headers={'X-API-Key': ALICE}) as client,
            streamable_http_client(f'{BASE}/mcp', http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return (await session.list_tools()).tools

    found = {tool.name: tool.annotations for tool in asyncio.run(tools())}
    assert {name: a.read_only_hint for name, a in found.items()} == {
        'add_accounts': False,
        'add_item': False,
        'get_item': True,
        'get_riksbank_rate': True,
        'list_accounts': True,
        'list_items': True,
        'update_item': False,
        'upload_ticket': False,
    }
    # only the rate lookup reaches outside this server, and only update_item replaces what is stored
    assert [name for name, a in found.items() if a.open_world_hint] == ['get_riksbank_rate']
    assert [name for name, a in found.items() if a.destructive_hint] == ['update_item']


def test_another_currency_is_booked_in_sek(hosting):
    data = pdf('Example Software Ltd, invoice 88, 20.00 EUR (made up)')
    facts = {
        'date': '2026-10-19',
        'amount': '20.00',
        'currency': 'EUR',
        'counterparty': 'Example Software Ltd',
    }
    lines = {'lines': [{'account': '6540', 'debit': '228.47'}, {'account': '1930', 'credit': '228.47'}]}
    item = ok('add_item', {'file_base64': b64(data), 'facts': facts, 'kontering': lines})['item']
    ref = item['id']
    assert any('total in SEK is missing' in problem for problem in item['missing_for_prepped'])
    assert 'total in SEK is missing' in refused('update_item', {'id': ref, 'status': 'prepped'})

    # a rate gives the total, and the list shows it
    rate = {'exchange_rate': '11,4235', 'exchange_rate_source': 'Riksbanken 2026-10-19'}
    item = ok('update_item', {'id': ref, 'facts': rate, 'status': 'prepped'})
    assert item['status'] == 'prepped'
    assert (item['facts']['exchange_rate'], item['facts']['amount_sek']) == ('11.4235', '228.47')
    history = ok('get_item', {'id': ref})['history'][-1]['what']
    assert history == 'facts: exchange_rate, amount_sek, exchange_rate_source; status: new to prepped'
    assert next(r for r in ok('list_items')['items'] if r['id'] == ref[:12])['amount_sek'] == '228.47'

    # what the card statement says was drawn replaces it; both at once have to agree
    drawn = {'amount_sek': '231.90', 'exchange_rate_source': 'card statement'}
    item = ok('update_item', {'id': ref, 'facts': drawn})
    assert 'exchange_rate' not in item['facts'] and item['facts']['amount_sek'] == '231.90'
    both = {'exchange_rate': '11.4235', 'amount_sek': '231.90'}
    assert 'Give one of the two' in refused('update_item', {'id': ref, 'facts': both})
    reply, error = call('update_item', {'id': ref, 'facts': {'exchange_rate': 11.4235}})
    assert reply is None and 'exchange_rate' in error  # a JSON number is refused, as for an amount

    # a prepped item stays complete, and an amount in SEK takes no rate
    assert 'total in SEK is missing' in refused('update_item', {'id': ref, 'facts': {'amount_sek': ''}})
    assert 'already in SEK' in refused(
        'update_item', {'id': hosting['id'], 'facts': {'exchange_rate': '11.5'}}
    )
    assert ok('get_item', {'id': ref})['facts']['amount_sek'] == '231.90'


def test_the_item_page_takes_a_rate_or_the_total_in_sek(alice, hosting):
    data = pdf('Example Cloud Inc, receipt 31, 12.00 USD (made up)')
    assert alice.upload('cloud.pdf', data)[0] == 303
    path = f'/items/{sha(data)}'
    form = dict.fromkeys(model.FACT_FIELDS, '') | {'date': '2026-11-05', 'amount': '12.00', 'currency': 'USD'}
    rate = {'exchange_rate': '10,5', 'exchange_rate_source': 'Riksbanken 2026-11-05'}
    assert alice.post(f'{path}/facts', form | rate)[0] == 303
    page = alice.get(path)[2].decode()
    # The total worked out from the rate is shown beside its field, not in it: the field is for
    # a total given as it is.
    assert 'Gives 126.00 SEK' in page and 'value="10.5"' in page
    assert re.search(r'name="amount_sek"[^>]* value=""', page)
    assert '12.00 USD at 10.5 is <strong>126.00 SEK</strong>' in page

    status, _, body = alice.post(f'{path}/facts', form | rate | {'amount_sek': '127,31'})
    assert status == 422 and b'Give one of the two' in body and b'value="127,31"' in body
    drawn = {'amount_sek': '127,31', 'exchange_rate_source': 'card statement'}
    assert alice.post(f'{path}/facts', form | drawn)[0] == 303
    page = alice.get(path)[2].decode()
    assert 'About 10.6092 for one USD' in page and re.search(r'name="amount_sek"[^>]* value="127.31"', page)
    assert ok('get_item', {'id': sha(data)})['facts']['exchange_rate_source'] == 'card statement'

    # an amount in SEK has no such fields
    assert 'name="exchange_rate"' not in alice.get(f'/items/{hosting["id"]}')[2].decode()
    ok('update_item', {'id': sha(data), 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def as_shown(page: str) -> dict:
    """The facts form as a browser sends it back: every field with the value the page put in it."""
    start = page.index('/facts"')
    form = page[start : page.index('</form>', start)]
    sent = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input[^>]* name="(\w+)"[^>]* value="([^"]*)"', form)
    }
    kind = re.search(r'<option value="(\w+)" selected', form)
    return sent | {'kind': kind.group(1) if kind else ''}


def test_the_facts_form_sends_every_field_back_and_only_what_was_typed_counts(alice):
    data = pdf('Example Hosting GmbH, invoice 7, 20.00 EUR (made up)')
    assert alice.upload('hosting-eur.pdf', data)[0] == 303
    path, ref = f'/items/{sha(data)}', sha(data)
    facts = {'date': '2026-10-19', 'amount': '20.00', 'currency': 'EUR'}
    rate = {'exchange_rate': '11.4235', 'exchange_rate_source': 'Riksbanken 2026-10-19'}
    ok('update_item', {'id': ref, 'facts': facts | rate})

    def stored() -> list:
        got = ok('get_item', {'id': ref})['facts']
        return [got.get(name) for name in ('currency', *model.SEK_SIDE)]

    # The page puts the saved rate in its field, so a browser sends it back with every save.
    shown = as_shown(alice.get(path)[2].decode())
    assert set(shown) - {'csrf'} == set(model.FACT_FIELDS) and shown['exchange_rate'] == '11.4235'

    # Another field changes: the rate stays, and the total follows the amount.
    assert alice.post(f'{path}/facts', shown | {'amount': '30.00'})[0] == 303
    assert stored() == ['EUR', '11.4235', '342.71', 'Riksbanken 2026-10-19']

    # The currency changes: the rate was for EUR and is not used for USD, and its total and
    # its source go with it.
    shown = as_shown(alice.get(path)[2].decode())
    assert alice.post(f'{path}/facts', shown | {'currency': 'USD'})[0] == 303
    assert stored() == ['USD', None, None, None]
    page = alice.get(path)[2].decode()
    assert (
        re.search(r'name="exchange_rate"[^>]* value=""', page)
        and 'The amount is in USD and its total in SEK is not given yet' in page
    )

    # The new rate typed in the same edit as the currency is kept.
    ok('update_item', {'id': ref, 'facts': {'currency': 'EUR'} | rate})
    shown = as_shown(alice.get(path)[2].decode())
    new = {'currency': 'USD', 'exchange_rate': '10,5', 'exchange_rate_source': 'card statement'}
    assert alice.post(f'{path}/facts', shown | new)[0] == 303
    assert stored() == ['USD', '10.5', '315.00', 'card statement']

    # The total as it was drawn is typed beside a saved rate: it replaces the rate.
    shown = as_shown(alice.get(path)[2].decode())
    assert shown['exchange_rate'] == '10.5' and shown['amount_sek'] == ''
    assert alice.post(f'{path}/facts', shown | {'amount_sek': '318,40'})[0] == 303
    assert stored() == ['USD', None, '318.40', 'card statement']
    # A rate typed beside a saved total replaces the total. What was drawn stays when only the
    # currency is corrected: it does not depend on the currency.
    shown = as_shown(alice.get(path)[2].decode())
    assert shown['amount_sek'] == '318.40'
    assert alice.post(f'{path}/facts', shown | {'currency': 'EUR'})[0] == 303
    assert stored() == ['EUR', None, '318.40', 'card statement']
    shown = as_shown(alice.get(path)[2].decode())
    assert alice.post(f'{path}/facts', shown | {'exchange_rate': '11.5'})[0] == 303
    assert stored() == ['EUR', '11.5', '345.00', 'card statement']

    # The amount turns out to be in SEK: the form still sends the rate, and it is dropped.
    shown = as_shown(alice.get(path)[2].decode())
    assert alice.post(f'{path}/facts', shown | {'currency': 'SEK'})[0] == 303
    assert stored() == ['SEK', None, None, None]

    # A refused save with SEK typed comes back without the SEK-side fields. Back to EUR from
    # there, the rate is still the saved one: a field the page did not have changes nothing.
    ok('update_item', {'id': ref, 'facts': {'currency': 'EUR'} | rate})
    shown = as_shown(alice.get(path)[2].decode())
    status, _, body = alice.post(f'{path}/facts', shown | {'currency': 'SEK', 'date': '19 October'})
    refused_page = as_shown(body.decode())
    assert status == 422 and refused_page['currency'] == 'SEK' and 'exchange_rate' not in refused_page
    assert alice.post(f'{path}/facts', refused_page | {'currency': 'EUR', 'date': '2026-10-19'})[0] == 303
    assert stored() == ['EUR', '11.4235', '342.71', 'Riksbanken 2026-10-19']

    # A prepped item must keep its total in SEK, so changing its currency is refused until the
    # new rate is given. The page says why the rate in the field no longer counts.
    lines = {'lines': [{'account': '6540', 'debit': '342.71'}, {'account': '1930', 'credit': '342.71'}]}
    ok('update_item', {'id': ref, 'kontering': lines, 'status': 'prepped'})
    shown = as_shown(alice.get(path)[2].decode())
    status, _, body = alice.post(f'{path}/facts', shown | {'currency': 'USD'})
    page = body.decode()
    assert status == 422 and 'total in SEK is missing' in page
    assert 'value="11.4235"' in page and '11.4235 was the rate for EUR' in page
    assert stored() == ['EUR', '11.4235', '342.71', 'Riksbanken 2026-10-19']
    assert 'was the rate for' not in alice.get(path)[2].decode()
    ok('update_item', {'id': ref, 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_the_list_writes_the_kontering_out_and_changes_status(alice, hosting):
    # Runs after the accounts test, so the accounts have names.
    here = '/items?status=todo'
    page = alice.get(here)[2].decode()
    assert 'Leverantörsskulder' in page and 'D 6230' not in page
    assert '1\u202f250.00' in page  # amounts are grouped for reading
    assert f'action="/items/{hosting["id"]}/status"' in page and f'name="back" value="{here}"' in page

    # prepped and then booked straight from the list, coming back to the same tab
    path = f'/items/{hosting["id"]}/status'
    status, headers, _ = alice.post(path, {'to': 'prepped', 'back': here})
    assert status == 303 and headers['location'] == f'{here}&msg=saved'
    row = alice.get(here)[2].decode().split(f'href="/items/{hosting["id"]}"')[1]
    at_hand = row.split('class="state"')[1].split('<details')[0]
    assert 'name="voucher"' in at_hand  # a prepped row has the booking form without opening anything
    booked = {'to': 'booked', 'voucher': 'A 44', 'receipt_attached': 'on', 'back': here}
    assert alice.post(path, booked)[1]['location'] == f'{here}&msg=saved'
    assert ok('get_item', {'id': hosting['id']})['booking']['voucher'] == 'A 44'
    assert f'href="/items/{hosting["id"]}"' not in alice.get(here)[2].decode()
    assert 'A 44' in alice.get('/items?status=booked&q=hosting')[2].decode()

    # a slip goes to the item page with what is wrong; an address that leads elsewhere is ignored
    assert alice.post(path, {'to': 'skipped', 'back': here})[0] == 422
    for elsewhere in ('//attacker.example/items?x', 'https://attacker.example/items?x', '/accounts?x'):
        status, headers, _ = alice.post(path, {'to': 'new', 'back': elsewhere})
        assert status == 303 and headers['location'] == f'/items/{hosting["id"]}?msg=saved'


def test_an_item_stored_before_a_fact_existed_still_reads(alice):
    data = pdf('made-up item from an earlier version')
    item = ok('add_item', {'file_base64': b64(data), 'facts': {'amount': '10.00', 'currency': 'EUR'}})['item']
    path = os.path.join(store.DATA_DIR, 'users', 'alice', 'items', f'{item["id"]}.json')
    with open(path, encoding='utf-8') as f:
        stored = json.load(f)
    for name in ('exchange_rate', 'exchange_rate_source'):
        del stored['facts'][name]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(stored, f)
    assert alice.get(f'/items/{item["id"]}')[0] == 200 and alice.get('/items?status=all')[0] == 200
    assert (
        ok('update_item', {'id': item['id'], 'facts': {'exchange_rate': '11.5'}})['facts']['amount_sek']
        == '115.00'
    )
    ok('update_item', {'id': item['id'], 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_riksbankens_rate_on_a_button_and_as_a_tool(alice, monkeypatch):
    # The network is stood in for: tests never call Riksbanken.
    from tallyqueue import riksbank

    asked = []

    def get(url):
        asked.append(url)
        return 200, {}, b'[{"date":"2026-11-04","value":10.4321},{"date":"2026-11-05","value":10.5}]'

    monkeypatch.setattr(riksbank, '_cache', {})
    monkeypatch.setattr(riksbank, 'ENABLED', True)
    monkeypatch.setattr(riksbank, '_get', get)

    data = pdf('Example Maps Inc, receipt 7, 8.00 USD (made up)')
    assert alice.upload('maps.pdf', data)[0] == 303
    path = f'/items/{sha(data)}'
    button = f'action="{path}/riksbanken"'
    assert button not in alice.get(path)[2].decode()  # nothing to look up before there is a currency
    ok('update_item', {'id': sha(data), 'facts': {'date': '2026-11-05', 'amount': '8.00', 'currency': 'USD'}})
    page = alice.get(path)[2].decode()
    assert button in page and 'name="rate_date" type="date" required value="2026-11-05"' in page

    status, headers, _ = alice.post(f'{path}/riksbanken', {'rate_date': '2026-11-05'})
    assert status == 303 and headers['location'] == f'{path}?msg=rate'
    facts = ok('get_item', {'id': sha(data)})['facts']
    assert (facts['exchange_rate'], facts['amount_sek']) == ('10.5', '84.00')
    assert facts['exchange_rate_source'] == 'Riksbanken 2026-11-05'
    assert asked == ['https://api.riksbank.se/swea/v1/Observations/SEKUSDPMI/2026-10-26/2026-11-05']

    # what Riksbanken cannot give is said on the page, and the facts stay as they were
    monkeypatch.setattr(riksbank, '_get', lambda url: (429, {'Retry-After': '40'}, b''))
    status, _, body = alice.post(f'{path}/riksbanken', {'rate_date': '2026-11-03'})
    assert status == 422 and b'Try again in 40 seconds' in body
    assert alice.post(f'{path}/riksbanken', {'rate_date': '2026-11-05'}, csrf=False)[0] == 403
    assert ok('get_item', {'id': sha(data)})['facts']['exchange_rate'] == '10.5'

    # the AI client can ask for the same rate; asking stores nothing
    reply = ok('get_riksbank_rate', {'currency': 'usd', 'date': '2026-11-05'})
    assert (reply['rate'], reply['source'], reply['unit']) == (
        '10.5',
        'Riksbanken 2026-11-05',
        'SEK for 1 USD',
    )
    assert 'already in SEK' in refused('get_riksbank_rate', {'currency': 'SEK', 'date': '2026-11-05'})

    # turned off: no button, no route, no answer from the tool
    monkeypatch.setattr(riksbank, 'ENABLED', False)
    assert button not in alice.get(path)[2].decode()
    assert alice.post(f'{path}/riksbanken', {'rate_date': '2026-11-05'})[0] == 404
    assert 'turned off' in refused('get_riksbank_rate', {'currency': 'USD', 'date': '2026-11-05'})
    monkeypatch.setattr(riksbank, 'ENABLED', True)
    ok('update_item', {'id': sha(data), 'status': 'skipped', 'skip_reason': 'Test probe, not a receipt'})


def test_static_files_and_sign_out(alice):
    status, headers, _ = fetch('GET', '/static/app.css')
    assert status == 200 and headers['content-type'].startswith('text/css')
    assert fetch('GET', '/static/..%2Fweb.py')[0] == 404 and fetch('GET', '/static/secret.txt')[0] == 404
    assert b'Disallow: /' in fetch('GET', '/robots.txt')[2]
    browser = Browser()
    browser.sign_in(ALICE)
    assert browser.get('/items')[0] == 200
    assert browser.post('/logout', {})[0] == 303
    assert browser.get('/items')[0] == 303
