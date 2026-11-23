# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end check of a running TallyQueue server, as an MCP client and as a browser would.

  python scripts/smoke.py http://127.0.0.1:3440            add made-up items, edit them, read the pages
  python scripts/smoke.py http://127.0.0.1:3440 --verify   after a restart: are they still there?

The access token is read from TALLYQUEUE_TOKEN. Everything the script adds is made up and says
so; run it against a test or development server, not against your real list.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


def made_up_pdf(lines: list[str]) -> bytes:
    """A small, valid one-page PDF with these lines of text."""
    text = ' '.join(f'({ln.replace("(", "[").replace(")", "]")}) Tj T*' for ln in lines)
    stream = f'BT /F1 12 Tf 50 780 Td 18 TL {text} ET'.encode('latin-1')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length %d >>\nstream\n%s\nendstream' % (len(stream), stream),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out, offsets = b'%PDF-1.4\n', []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n%s\nendobj\n' % (number, body)
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    out += b''.join(b'%010d 00000 n \n' % offset for offset in offsets)
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return out


HOSTING = made_up_pdf(
    [
        'MADE-UP TEST INVOICE - not a real document',
        'Exempel Hosting AB',
        'Invoice 1042, 2026-10-03',
        'Web hosting, October: 1000.00 SEK',
        'VAT 25 %: 250.00 SEK',
        'Total: 1250.00 SEK',
    ]
)
SOFTWARE = made_up_pdf(
    [
        'MADE-UP TEST RECEIPT - not a real document',
        'Example Software Ltd',
        'Receipt 88213, 2026-10-19',
        'Subscription, one month: 20.00 EUR',
    ]
)
STATEMENT = (
    b'MADE-UP TEST CARD STATEMENT LINE - not a real document\nExample Software Ltd, EUR 20.00, SEK 228.47\n'
)
LUNCH = b'MADE-UP TEST MAIL - not a real document\n\nTack for ditt kop!\nLunch 129,00 kr\n'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, data=None, headers=None, method=None):
    """(status, headers, body); redirects are returned, not followed."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def check(condition, what):
    print(('ok    ' if condition else 'FAIL  ') + what)
    if not condition:
        sys.exit(1)


async def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('url', help='the server address, e.g. http://127.0.0.1:3440')
    parser.add_argument(
        '--verify', action='store_true', help='only check that the items of an earlier run are there'
    )
    args = parser.parse_args()
    base = args.url.rstrip('/')
    token = os.environ.get('TALLYQUEUE_TOKEN', '')
    if not token:
        sys.exit('Set TALLYQUEUE_TOKEN to an access token of the server.')
    hosting_id, software_id = (hashlib.sha256(d).hexdigest() for d in (HOSTING, SOFTWARE))

    status, _, body = request(f'{base}/health')
    check(
        status == 200 and json.loads(body)['status'] == 'ok',
        f'/health answers ({json.loads(body)["version"]})',
    )
    check(
        request(f'{base}/mcp', b'{}', {'Content-Type': 'application/json'})[0] == 401,
        '/mcp refuses a request without a token',
    )

    async with (
        create_mcp_http_client(headers={'X-API-Key': token}) as client,
        streamable_http_client(f'{base}/mcp', http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        async def call(tool, **arguments):
            result = await session.call_tool(tool, arguments)
            text = result.content[0].text
            check(not result.is_error, f'{tool}' if not result.is_error else f'{tool}: {text}')
            return json.loads(text)

        tools = sorted(t.name for t in (await session.list_tools()).tools)
        check(
            tools
            == [
                'add_accounts',
                'add_file',
                'add_item',
                'get_item',
                'get_riksbank_rate',
                'list_accounts',
                'list_items',
                'update_item',
                'upload_ticket',
            ],
            f'tools: {", ".join(tools)}',
        )

        if args.verify:
            item = await call('get_item', id=hosting_id)
            check(
                item['status'] == 'prepped' and item['facts']['amount'] == '1250.00',
                'the prepped item survived',
            )
            item = await call('get_item', id=software_id)
            check(
                item['status'] == 'new' and item['facts']['amount_sek'] == '228.47',
                'the uploaded item survived, with its total in SEK',
            )
            status, _, body = request(f'{base}/upload', HOSTING, {'X-API-Key': token})
            check(
                status == 200 and json.loads(body)['created'] is False,
                'its receipt file is still known by checksum',
            )
            item = await call('get_item', id=software_id)
            check(
                [f['kind'] for f in item['files']] == ['document', 'payment'],
                'both files of the uploaded item survived',
            )
            return

        added = await call(
            'add_item',
            file_base64=base64.b64encode(HOSTING).decode(),
            filename='exempel-hosting-1042.pdf',
            source='Made-up mail from billing@example.test, 2026-10-03',
            facts={
                'kind': 'invoice',
                'date': '2026-10-03',
                'counterparty': 'Exempel Hosting AB',
                'description': 'Web hosting, October (made up)',
                'amount': '1250.00',
                'currency': 'SEK',
                'vat_amount': '250.00',
                'invoice_number': '1042',
            },
            kontering={
                'lines': [
                    {'account': '6230', 'debit': '1000.00'},
                    {'account': '2640', 'debit': '250.00'},
                    {'account': '2440', 'credit': '1250.00'},
                ],
                'vat_treatment': '25 % Swedish VAT',
                'note': 'Made-up proposal from the smoke test',
            },
        )
        check(added['item']['id'] == hosting_id, 'the item id is the SHA-256 of the file')
        again = await call('add_item', file_base64=base64.b64encode(HOSTING).decode())
        check(again['created'] is False, 'the same file again adds nothing')
        item = await call('update_item', id=hosting_id[:12], status='prepped')
        check(item['status'] == 'prepped', 'a complete item can be marked prepped')

        # The way a client without the token uploads: a one-use ticket for this one file.
        ticket = (await call('upload_ticket', sha256=software_id, filename='software-88213.pdf'))['ticket']
        upload = {'X-Upload-Ticket': ticket, 'X-Filename': 'software-88213.pdf'}
        status, _, body = request(f'{base}/upload', SOFTWARE, upload)
        check(
            status in (200, 201) and json.loads(body)['id'] == software_id,
            'POST /upload takes the raw file with an upload ticket',
        )
        check(request(f'{base}/upload', SOFTWARE, upload)[0] == 401, 'the ticket is used up after one upload')
        # The card statement's line for it: what was drawn in SEK, a second file of the same item.
        paid = await call(
            'add_file',
            id=software_id,
            file_base64=base64.b64encode(STATEMENT).decode(),
            filename='card-statement.txt',
            kind='payment',
        )
        check(
            [f['kind'] for f in paid['item']['files']] == ['document', 'payment'],
            'a payment record is a second file of the same item',
        )
        item = await call(
            'update_item',
            id=software_id,
            facts={
                'date': '2026-10-19',
                'counterparty': 'Example Software Ltd',
                'amount': '20.00',
                'currency': 'EUR',
            },
        )
        check(
            item['status'] == 'new' and item['missing_for_prepped'], 'an item without a kontering stays new'
        )
        item = await call(
            'update_item',
            id=software_id,
            facts={'exchange_rate': '11.4235', 'exchange_rate_source': 'Riksbanken 2026-10-19 (made up)'},
        )
        check(item['facts']['amount_sek'] == '228.47', 'a rate gives the total in SEK')

        lunch = await call('add_item', file_base64=base64.b64encode(LUNCH).decode(), filename='lunch.txt')
        item = await call(
            'update_item', id=lunch['item']['id'], status='skipped', skip_reason='Private purchase (made up)'
        )
        check(item['status'] == 'skipped', 'a private purchase can be skipped')

        listing = await call('list_items')
        check(
            {hosting_id[:12], software_id[:12]} <= {row['id'] for row in listing['items']},
            'list_items shows the to-do items',
        )

    # The web page, as a browser would use it.
    check(request(f'{base}/items')[0] == 303, 'the list asks for a sign-in')
    form = urllib.parse.urlencode({'token': token}).encode()
    status, headers, _ = request(f'{base}/login', form, {'Origin': base})
    cookie = (headers.get('Set-Cookie') or '').split(';')[0]
    check(status == 303 and cookie, 'signing in with the token gives a session cookie')
    status, headers, body = request(f'{base}/items?status=all', headers={'Cookie': cookie})
    check(status == 200 and b'Exempel Hosting AB' in body, 'the list page shows the item')
    check(
        'script' not in headers.get('Content-Security-Policy', 'script'), 'the page policy allows no scripts'
    )
    status, headers, body = request(
        f'{base}/items/{hosting_id}/files/{hosting_id}', headers={'Cookie': cookie}
    )
    check(
        status == 200 and body == HOSTING and headers['Content-Type'] == 'application/pdf',
        'the receipt comes back unchanged',
    )
    check(
        request(f'{base}/items/{hosting_id}/files/{hosting_id}')[0] == 303,
        'a receipt is not served without a sign-in',
    )
    print('All checks passed.')


if __name__ == '__main__':
    asyncio.run(main())
