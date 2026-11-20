# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""The web page: see the list and the receipts, and manage them without an AI client.

Plain server-rendered HTML with forms. There is no JavaScript, so the page's content policy
can forbid scripts altogether. A user signs in with their access token and gets a session
cookie; every form post is checked for its origin and for the session's CSRF token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from decimal import Decimal
from urllib.parse import quote

import anyio
import jinja2
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

from . import __version__, auth, config, model, riksbank, store

sessions = auth.Sessions()
COOKIE = '__Host-tallyqueue' if config.SECURE else 'tallyqueue'
STATIC = os.path.join(os.path.dirname(__file__), 'static')
STATIC_TYPES = {'app.css': 'text/css; charset=utf-8', 'icon.svg': 'image/svg+xml'}
# Browsers keep the stylesheet for an hour. Its address carries a checksum of its content, so
# a changed stylesheet is fetched at once, whether or not the version number moved.
with open(os.path.join(STATIC, 'app.css'), 'rb') as _css:
    STYLE = hashlib.sha256(_css.read()).hexdigest()[:12]
IMAGES = {'png': 'image/png', 'jpg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp'}
MEDIA = {'pdf': 'application/pdf', **IMAGES}
SPARE_ROWS = 4

# No scripts at all. Receipts are shown in a frame or as an image from this same origin.
PAGE_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; frame-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
# A receipt may be framed by our own pages only, and loads nothing itself.
RECEIPT_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'self'"

FLASH = {
    'saved': 'Saved.',
    'added': 'Added to the list.',
    'duplicate': 'That file is already in the list. This is the item; nothing was changed.',
    'deleted': 'The item and its receipt were deleted.',
    'accounts': 'Your list of accounts was saved.',
    'client_on': 'The AI client may now add accounts to your list. It stops by itself after an hour.',
    'client_off': 'The AI client can no longer add accounts.',
    'rate': "Riksbanken's rate was fetched and saved with the facts. Check that it is for the date you want.",
}
TABS = (
    ('todo', 'To do'),
    ('new', 'New'),
    ('prepped', 'Prepped'),
    ('booked', 'Booked'),
    ('skipped', 'Skipped'),
    ('all', 'All'),
)
FACT_LABELS = {
    'kind': 'Kind',
    'date': 'Date on the document',
    'counterparty': 'Counterparty',
    'description': 'What it is for',
    'amount': 'Total incl. VAT',
    'currency': 'Currency',
    'vat_amount': 'VAT (moms)',
    # The SEK side (model.SEK_SIDE) has its own block in the form, shown for another currency.
    'invoice_number': 'Invoice, order or receipt number',
    'due_date': 'Due date',
    'payment_date': 'Paid on',
}

env = jinja2.Environment(
    loader=jinja2.PackageLoader('tallyqueue', 'templates'),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _money(value) -> str:
    """An amount for reading, not for a form field: "12 500.00", with thin spaces that do not break."""
    return f'{Decimal(value):,.2f}'.replace(',', '\u202f') if value else ''


env.filters['money'] = _money


def _page(template: str, session: auth.Session | None, status_code: int = 200, **context) -> Response:
    context.setdefault('flash', None)
    context.setdefault('error', None)
    html = env.get_template(template).render(
        session=session, version=__version__, style=STYLE, source_url=config.SOURCE_URL, **context
    )
    return HTMLResponse(html, status_code=status_code, headers={'Content-Security-Policy': PAGE_POLICY})


def _message(session, status_code: int, title: str, text: str) -> Response:
    return _page('message.html', session, status_code, title=title, text=text)


def _redirect(path: str) -> Response:
    return RedirectResponse(path, status_code=303)


def _session(request: Request) -> auth.Session | None:
    return sessions.get(request.cookies.get(COOKIE))


def _to_login(request: Request) -> Response:
    target = request.url.path + (f'?{request.url.query}' if request.url.query else '')
    return _redirect('/login' if target == '/' else f'/login?next={quote(target, safe="")}')


def _safe_next(target: str | None) -> str:
    """Only a path on this site; anything else could send the user elsewhere after sign-in."""
    target = target or ''
    if target.startswith('/') and not target.startswith('//') and '\\' not in target and '\n' not in target:
        return target
    return '/items'


def _origin_ok(request: Request) -> bool:
    """A form post must come from one of our own pages."""
    origin = request.headers.get('origin')
    if origin is None:  # some browsers leave it out on same-origin posts
        return request.headers.get('sec-fetch-site') in (None, 'same-origin')
    return config.origin_allowed(origin, request.headers.get('host', ''))


async def _post(request: Request, *, max_bytes: int = 256 * 1024, max_field: int = 64 * 1024):
    """The signed-in session and the form of a post, or the response that refuses it."""
    session = _session(request)
    if not session:
        return None, None, _to_login(request)
    if not _origin_ok(request):
        return None, None, _message(session, 403, 'Refused', 'This form was not sent from TallyQueue itself.')
    declared = request.headers.get('content-length', '')
    if not declared.isdigit():
        return None, None, _message(session, 411, 'Refused', 'The request has no length.')
    if int(declared) > max_bytes:
        return None, None, _message(session, 413, 'Too large', 'That is more than this form accepts.')
    form = await request.form(max_files=1, max_fields=400, max_part_size=max_field)
    if not hmac.compare_digest(str(form.get('csrf', '')), session.csrf):
        return (
            None,
            None,
            _message(session, 403, 'Refused', 'The form has expired. Go back, reload and try again.'),
        )
    return session, form, None


async def _run(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------- sign-in
async def home(request: Request) -> Response:
    return _redirect('/items') if _session(request) else _redirect('/login')


async def login_form(request: Request) -> Response:
    target = _safe_next(request.query_params.get('next'))
    if _session(request):
        return _redirect(target)
    return _page('login.html', None, next=target, open=bool(auth.USERS))


async def login(request: Request) -> Response:
    if not _origin_ok(request):
        return _message(None, 403, 'Refused', 'This form was not sent from TallyQueue itself.')
    form = await request.form(max_files=0, max_fields=10, max_part_size=4096)
    target = _safe_next(str(form.get('next', '')))
    user = auth.user_for_token(str(form.get('token', '')).strip())
    if not user:
        return _page(
            'login.html', None, 401, next=target, open=bool(auth.USERS), error='That token is not approved.'
        )
    response = _redirect(target)
    response.set_cookie(
        COOKIE,
        sessions.create(user),
        max_age=int(sessions.max_seconds),
        path='/',
        secure=config.SECURE,
        httponly=True,
        # Lax, not Strict: a link to an item from a chat client must open while signed in.
        # Posts from other sites still carry no cookie.
        samesite='lax',
    )
    return response


async def logout(request: Request) -> Response:
    session, _form, refusal = await _post(request)
    if refusal:
        return refusal
    sessions.drop(request.cookies.get(COOKIE))
    response = _redirect('/login')
    response.delete_cookie(COOKIE, path='/', secure=config.SECURE, httponly=True, samesite='lax')
    return response


# ---------------------------------------------------------------- the list
async def items(request: Request) -> Response:
    session = _session(request)
    if not session:
        return _to_login(request)
    status = request.query_params.get('status', 'todo')
    if status not in dict(TABS):
        status = 'todo'
    query = request.query_params.get('q', '').strip()[:100]
    everything = await _run(store.all_items, session.user)
    accounts = await _run(store.read_accounts, session.user)
    shown = store.search(everything, status, query)
    return _page(
        'items.html',
        session,
        items=shown,
        counts=store.counts(everything),
        account_names={a['account']: a['name'] for a in accounts},
        prep_problems={i['id']: model.prep_problems(i) for i in shown if i['status'] == 'new'},
        # Where a status change made in the list comes back to.
        here=f'/items?status={status}' + (f'&q={quote(query, safe="")}' if query else ''),
        tabs=TABS,
        status=status,
        query=query,
        max_mb=store.MAX_RECEIPT_BYTES // (1024 * 1024),
        accepted=store.ACCEPTED,
        flash=FLASH.get(request.query_params.get('msg', '')),
    )


async def add(request: Request) -> Response:
    session, form, refusal = await _post(request, max_bytes=store.MAX_RECEIPT_BYTES + 64 * 1024)
    if refusal:
        return refusal
    upload = form.get('file')
    if not hasattr(upload, 'read') or not upload.filename:
        return _message(session, 400, 'No file', 'Choose a receipt or invoice file to add.')
    data = await upload.read()
    try:
        item, created = await _run(store.add_item, session.user, data, upload.filename, via='web')
    except (store.StoreError, model.Invalid) as e:
        return _message(session, 400, 'Not added', str(e))
    return _redirect(f'/items/{item["id"]}?msg={"added" if created else "duplicate"}')


# ---------------------------------------------------------------- one item
def _draft_lines(form) -> list[dict]:
    lines, n = [], 0
    while f'account_{n}' in form and n < model.MAX_LINES + SPARE_ROWS:
        lines.append(
            {k: str(form.get(f'{k}_{n}', '')).strip() for k in ('account', 'debit', 'credit', 'text')}
        )
        n += 1
    return lines


def _sek_fields(saved: dict) -> dict:
    """What the facts form puts in its SEK-side fields for the saved facts.

    A total in SEK that was worked out from a rate is shown beside its field, not in it: the
    field is for a total given as it is.
    """
    return {
        'exchange_rate': saved['exchange_rate'] or '',
        'amount_sek': '' if saved['exchange_rate'] else saved['amount_sek'] or '',
        'exchange_rate_source': saved['exchange_rate_source'] or '',
    }


async def _item_page(request: Request, session: auth.Session, item: dict, **context) -> Response:
    everything = await _run(store.all_items, session.user)
    accounts = await _run(store.read_accounts, session.user)
    draft_kontering = context.pop('draft_kontering', None)
    kontering = draft_kontering or item['kontering'] or {}
    lines = [dict(ln) for ln in kontering.get('lines') or []]
    debit, credit = model.totals(item['kontering'])
    status_code = 422 if context.get('error') else 200
    draft_facts = context.pop('draft_facts', None)
    facts = draft_facts or item['facts']
    saved = item['facts']
    currency = (facts.get('currency') or '').strip().upper()
    # A save that was refused comes back with what was sent. A rate that was left as it was
    # while the currency was changed is still in its field then, and no longer counts.
    rate_was_for = (
        saved['currency']
        if draft_facts
        and saved['currency']
        and saved['exchange_rate']
        and draft_facts.get('exchange_rate') == saved['exchange_rate']
        and currency != saved['currency']
        else None
    )
    return _page(
        'item.html',
        session,
        status_code,
        item=item,
        facts=facts,
        sek={
            'shown': currency != 'SEK',
            'field': facts.get('amount_sek') or '' if draft_facts else _sek_fields(saved)['amount_sek'],
            'worked_out': saved['amount_sek'] if saved['exchange_rate'] and not draft_facts else None,
            'implied': None if draft_facts else model.implied_rate(saved),
            'rate_was_for': rate_was_for,
        },
        edit_kontering=item['status'] == 'new' or draft_kontering is not None,
        # Riksbanken's rate can be fetched for the saved currency, on a date the user picks.
        lookup=riksbank.ENABLED and saved['currency'] not in (None, 'SEK'),
        fact_labels=FACT_LABELS,
        kinds=model.KINDS,
        lines=lines + [{'account': '', 'debit': '', 'credit': '', 'text': ''}] * SPARE_ROWS,
        vat_treatment=kontering.get('vat_treatment') or '',
        kontering_note=kontering.get('note') or '',
        totals={'debit': f'{debit:.2f}', 'credit': f'{credit:.2f}', 'balanced': debit == credit},
        accounts=accounts,
        account_names={a['account']: a['name'] for a in accounts},
        prep_problems=model.prep_problems(item),
        warnings=model.warnings(item, accounts, everything),
        has_receipt=store.receipt_path(session.user, item) is not None,
        preview='image' if item['receipt']['ext'] in IMAGES else 'frame',
        flash=FLASH.get(request.query_params.get('msg', '')),
        **context,
    )


async def item(request: Request) -> Response:
    session = _session(request)
    if not session:
        return _to_login(request)
    ref = request.path_params['id']
    try:
        found = await _run(store.get_item, session.user, ref)
    except store.StoreError as e:
        return _message(session, 404, 'Not found', str(e))
    if ref != found['id']:
        return _redirect(f'/items/{found["id"]}')
    return await _item_page(request, session, found)


async def change(request: Request) -> Response:
    session, form, refusal = await _post(request)
    if refusal:
        return refusal
    ref, action = request.path_params['id'], request.path_params['action']
    draft: dict = {}
    try:
        if action == 'facts':
            sent = {name: str(form.get(name, '')).strip() for name in model.FACT_FIELDS}
            draft['draft_facts'] = sent
            # A browser sends every field back, touched or not. On the SEK side that matters:
            # a rate that is given is kept, also when the currency changes. So a SEK-side
            # field that still holds what the page put in it, or that the page did not have,
            # is not part of the change, and model.settle_sek decides what becomes of it.
            current = await _run(store.get_item, session.user, ref)
            untouched = {
                name
                for name, shown in _sek_fields(current['facts']).items()
                if name not in form or sent[name] == shown
            }
            changes = {'facts': {name: value for name, value in sent.items() if name not in untouched}}
        elif action == 'kontering':
            draft['draft_kontering'] = {
                'lines': _draft_lines(form),
                'vat_treatment': str(form.get('vat_treatment', '')),
                'note': str(form.get('note', '')),
            }
            changes = {'kontering': draft['draft_kontering']}
        elif action == 'notes':
            changes = {'notes': str(form.get('notes', ''))}
        elif action == 'status':
            to = str(form.get('to', ''))
            changes = {'status': to}
            if to == 'booked':
                changes |= {
                    'voucher': str(form.get('voucher', '')),
                    'receipt_attached': 'receipt_attached' in form,
                }
            elif to == 'skipped':
                changes['skip_reason'] = str(form.get('skip_reason', ''))
        elif action == 'riksbanken' and riksbank.ENABLED:
            # The rate for the item's saved currency. It is saved like a rate typed by hand, so
            # the total in SEK is worked out from it and the source says where it is from.
            current = await _run(store.get_item, session.user, ref)
            found = await _run(
                riksbank.rate, current['facts']['currency'] or '', str(form.get('rate_date', ''))
            )
            changes = {'facts': {'exchange_rate': found['rate'], 'exchange_rate_source': found['source']}}
        elif action == 'delete':
            if 'confirm' not in form:
                raise store.StoreError(
                    'Tick the box to confirm that the item and its receipt should be deleted'
                )
            await _run(store.delete_item, session.user, ref)
            return _redirect('/items?msg=deleted')
        else:
            return _message(session, 404, 'Not found', 'There is no such action.')
        await _run(store.update_item, session.user, ref, via='web', **changes)
    except store.NotFound as e:
        return _message(session, 404, 'Not found', str(e))
    except (store.StoreError, model.Invalid, riksbank.RateUnavailable) as e:
        try:
            current = await _run(store.get_item, session.user, ref)
        except store.StoreError:
            return _message(session, 404, 'Not found', str(e))
        return await _item_page(request, session, current, error=str(e), **draft)
    # A status change made in the list goes back to the list, on the same tab and search.
    back = str(form.get('back', ''))
    if back.startswith('/items?') and _safe_next(back) == back:
        return _redirect(f'{back}&msg=saved')
    return _redirect(f'/items/{ref}?msg={"rate" if action == "riksbanken" else "saved"}')


async def receipt(request: Request) -> Response:
    """The receipt file itself, for the signed-in user only.

    The type comes from the file's content when it was added. Anything that is not a PDF or an
    image is sent as plain text in a sandbox, so a saved HTML mail is shown as text, never run.
    """
    session = _session(request)
    if not session:
        return _to_login(request)
    try:
        found = await _run(store.get_item, session.user, request.path_params['id'])
    except store.StoreError as e:
        return _message(session, 404, 'Not found', str(e))
    path = store.receipt_path(session.user, found)
    if not path:
        return _message(session, 404, 'Not found', 'The receipt file for this item is missing.')
    media = MEDIA.get(found['receipt']['ext'])
    policy = RECEIPT_POLICY if media else f'{RECEIPT_POLICY}; sandbox'
    disposition = 'attachment' if 'download' in request.query_params else 'inline'
    return FileResponse(
        path,
        media_type=media or 'text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f"{disposition}; filename*=UTF-8''{quote(found['receipt']['filename'])}",
            'Content-Security-Policy': policy,
            'X-Frame-Options': 'SAMEORIGIN',
            'Cache-Control': 'private, no-store',
        },
    )


# ---------------------------------------------------------------- the user's accounts
async def accounts(request: Request) -> Response:
    session = _session(request)
    if not session:
        return _to_login(request)
    current = await _run(store.read_accounts, session.user)
    source = '\n'.join(f'{a["account"]} {a["name"]}'.rstrip() for a in current)
    return _page(
        'accounts.html',
        session,
        source=source,
        count=len(current),
        client_until=await _run(store.accounts_from_client_until, session.user),
        flash=FLASH.get(request.query_params.get('msg', '')),
    )


async def save_accounts(request: Request) -> Response:
    session, form, refusal = await _post(request, max_bytes=1024 * 1024, max_field=1024 * 1024)
    if refusal:
        return refusal
    source = str(form.get('accounts', ''))
    try:
        await _run(store.write_accounts, session.user, source)
    except (store.StoreError, model.Invalid) as e:
        until = await _run(store.accounts_from_client_until, session.user)
        return _page('accounts.html', session, 422, source=source, count=0, client_until=until, error=str(e))
    return _redirect('/accounts?msg=accounts')


async def accounts_from_client(request: Request) -> Response:
    """The user's own switch for letting an AI client add accounts. Off until the user turns it on."""
    session, form, refusal = await _post(request)
    if refusal:
        return refusal
    allowed = form.get('allow') == 'on'
    await _run(store.allow_accounts_from_client, session.user, allowed)
    return _redirect(f'/accounts?msg={"client_on" if allowed else "client_off"}')


# ---------------------------------------------------------------- static
async def static(request: Request) -> Response:
    name = request.path_params['name']
    if name not in STATIC_TYPES:
        return PlainTextResponse('Not found', status_code=404)
    return FileResponse(
        os.path.join(STATIC, name),
        media_type=STATIC_TYPES[name],
        headers={'Cache-Control': 'public, max-age=3600', 'Content-Security-Policy': "default-src 'none'"},
    )


async def robots(request: Request) -> Response:
    return PlainTextResponse('User-agent: *\nDisallow: /\n')


ROUTES = (
    ('/', ['GET'], home),
    ('/login', ['GET'], login_form),
    ('/login', ['POST'], login),
    ('/logout', ['POST'], logout),
    ('/items', ['GET'], items),
    ('/items', ['POST'], add),
    ('/items/{id}', ['GET'], item),
    ('/items/{id}/{action}', ['POST'], change),
    ('/receipts/{id}', ['GET'], receipt),
    ('/accounts', ['GET'], accounts),
    ('/accounts', ['POST'], save_accounts),
    ('/accounts/client', ['POST'], accounts_from_client),
    ('/static/{name}', ['GET'], static),
    ('/robots.txt', ['GET'], robots),
)
