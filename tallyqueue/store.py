# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""Items and receipts on disk. One folder per approved user; nothing is shared between users.

    DATA_DIR/users/<user>/items/<id>.json          the item: facts, proposed kontering, files, status, history
    DATA_DIR/users/<user>/receipts/<sha256>.<ext>  each file of an item as it arrived (a working copy)
    DATA_DIR/users/<user>/accounts.json            the user's own list of accounts, if they gave one
    DATA_DIR/users/<user>/settings.json            what the user has switched on, such as accounts from a client

The item is the unit: one purchase or sale, with its facts and kontering, and the files that
document it, such as a receipt in another currency and the bank's record of what was drawn in
SEK. An item's id is 64 hexadecimal characters that never change: the SHA-256 of its first file
when it was added with one, otherwise random. A file belongs to one item only, so the same file
added twice changes nothing. The files kept here are working copies. The archive that the law
asks for is the one in the user's bookkeeping service.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import threading

from . import model

log = logging.getLogger('tallyqueue')

DATA_DIR = os.environ.get('DATA_DIR', '/data')
MAX_RECEIPT_BYTES = int(os.environ.get('MAX_RECEIPT_BYTES', str(15 * 1024 * 1024)))
MAX_HISTORY = 200
MIN_REF = 8

USER_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
_HEX = re.compile(r'[0-9a-f]+')
_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', 'png', 'image/png'),
    (b'\xff\xd8\xff', 'jpg', 'image/jpeg'),
    (b'GIF87a', 'gif', 'image/gif'),
    (b'GIF89a', 'gif', 'image/gif'),
)
_TEXT_EXTS = ('txt', 'eml', 'html', 'htm', 'md')
ACCEPTED = 'PDF, PNG, JPEG, WebP, GIF, or UTF-8 text such as a saved mail'
_KEEP = object()
_lock = threading.RLock()


class StoreError(ValueError):
    """A request the store refuses. The message is shown to the user as it is."""


class NotFound(StoreError):
    pass


def now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def today() -> str:
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------- paths
def _user_dir(user: str, *parts: str) -> str:
    if not USER_RE.fullmatch(user or ''):
        raise StoreError('Unknown user')
    return os.path.join(DATA_DIR, 'users', user, *parts)


def _item_path(user: str, item_id: str) -> str:
    return _user_dir(user, 'items', f'{item_id}.json')


def file_path(user: str, file: dict) -> str | None:
    """Where one of an item's files is, or None when it is gone."""
    path = _user_dir(user, 'receipts', f'{file["sha256"]}.{file["ext"]}')
    return path if os.path.isfile(path) else None


def find_file(item: dict, sha256: str | None) -> dict | None:
    """The item's file with this SHA-256, or its first file when sha256 is empty."""
    files = item.get('files') or []
    if not sha256:
        return files[0] if files else None
    return next((f for f in files if f['sha256'] == sha256), None)


def _write(path: str, data: bytes) -> None:
    """Write the whole file or nothing: a crash never leaves half an item."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_json(path: str, obj) -> None:
    _write(path, (json.dumps(obj, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))


def _read_json(path: str):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _read_item(path: str) -> dict:
    """An item from disk with every fact present: one added in a later version reads as empty.

    An item saved before items had several files kept its one file under "receipt", stored by
    the item's id. It reads as an item with that one file, and is saved that way the next time.
    """
    item = _read_json(path)
    item['facts'] = {name: item['facts'].get(name) for name in model.FACT_FIELDS}
    if 'files' not in item:
        old = item.pop('receipt', None)
        first = (item.get('history') or [{}])[0]
        item['files'] = (
            [
                {
                    'sha256': item['id'],
                    'filename': old['filename'],
                    'ext': old['ext'],
                    'media_type': old['media_type'],
                    'bytes': old['bytes'],
                    'kind': 'document',
                    'added': item.get('created'),
                    'via': first.get('via'),
                }
            ]
            if old
            else []
        )
    return item


# ---------------------------------------------------------------- receipt files
def sniff(data: bytes, filename: str = '') -> tuple[str, str]:
    """(extension, media type) from the file's content. Raises for a type that is not accepted.

    The content decides, not the name. Only PDF and the four raster image types get a type of
    their own. Everything else that is UTF-8 text is "text", whatever it calls itself: a saved
    HTML mail, an XML e-invoice, an SVG. Text is always served back as plain text, so none of
    them can ever run as a page or be drawn as an image from this server.
    """
    for magic, ext, media in _MAGIC:
        if data.startswith(magic):
            return ext, media
    if b'%PDF-' in data[:1024]:
        return 'pdf', 'application/pdf'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    try:
        decoded = data.decode('utf-8')
    except UnicodeDecodeError:
        decoded = '\x00'
    if '\x00' in decoded:
        raise StoreError(f'This file type is not accepted. Send {ACCEPTED}')
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return (ext if ext in _TEXT_EXTS else 'txt'), 'text/plain'


def _display_name(filename: str, ext: str) -> str:
    name = re.sub(r'[\x00-\x1f\x7f]', '', (filename or '').replace('\\', '/').rsplit('/', 1)[-1]).strip()
    return name[:150] or f'receipt.{ext}'


# ---------------------------------------------------------------- files
def _new_file(data: bytes, filename: str, kind: str | None, via: str, stamp: str) -> dict:
    """The record of a file, checked: not empty, not too large, an accepted type, a known kind."""
    if not data:
        raise StoreError('The file is empty')
    if len(data) > MAX_RECEIPT_BYTES:
        raise StoreError(f'The file is larger than {MAX_RECEIPT_BYTES // (1024 * 1024)} MB')
    kind = kind or 'document'
    if kind not in model.FILE_KINDS:
        raise StoreError(f'kind: one of {", ".join(model.FILE_KINDS)}')
    ext, media = sniff(data, filename or '')
    return {
        'sha256': hashlib.sha256(data).hexdigest(),
        'filename': _display_name(filename or '', ext),
        'ext': ext,
        'media_type': media,
        'bytes': len(data),
        'kind': kind,
        'added': stamp,
        'via': via,
    }


def _holder(user: str, sha256: str) -> dict | None:
    """The item that has a file with this SHA-256, if any. Call it under the lock."""
    for item in all_items(user):
        if find_file(item, sha256):
            return item
    return None


def _save_file(user: str, file: dict, data: bytes) -> None:
    _write(_user_dir(user, 'receipts', f'{file["sha256"]}.{file["ext"]}'), data)


# ---------------------------------------------------------------- items
def add_item(
    user: str,
    data: bytes | None,
    filename: str = '',
    *,
    via: str,
    kind: str | None = None,
    source: str | None = None,
    facts: dict | None = None,
    kontering: dict | None = None,
    notes: str | None = None,
) -> tuple[dict, bool]:
    """A new item, with its first file or none yet. Returns (item, created).

    When the file is already in the list, the item that has it comes back unchanged and created
    is False: a second scan of an inbox never overwrites what the user has edited.
    """
    stamp = now()
    file = _new_file(data, filename, kind, via, stamp) if data is not None else None
    cleaned = model.clean_facts(facts or {})
    settled = model.settle_sek({}, {name: cleaned.get(name) for name in model.FACT_FIELDS}, cleaned)
    item = {
        'id': file['sha256'] if file else secrets.token_hex(32),
        'status': 'new',
        'created': stamp,
        'updated': stamp,
        'files': [file] if file else [],
        'source': model.text(source, 'source', 300),
        'facts': settled,
        'kontering': model.clean_kontering(kontering),
        'booking': None,
        'skip_reason': None,
        'notes': model.text(notes, 'notes', 4000, multiline=True),
        'history': [{'at': stamp, 'by': user, 'via': via, 'what': 'added'}],
    }
    with _lock:
        if file:
            holder = _holder(user, file['sha256'])
            if holder:
                return holder, False
        path = _item_path(user, item['id'])
        if os.path.exists(path):  # an id that is taken: never in practice, and never overwritten
            raise StoreError('Try again')
        if file:
            _save_file(user, file, data)
        _write_json(path, item)
    return item, True


def add_file(
    user: str, ref: str, data: bytes, filename: str = '', *, via: str, kind: str | None = None
) -> tuple[dict, bool]:
    """Add a file to an item. Returns (item, added).

    The same file again on the same item changes nothing (added is False). A file that another
    item already has is refused: a file documents one item.
    """
    stamp = now()
    file = _new_file(data, filename, kind, via, stamp)
    with _lock:
        item = get_item(user, ref)
        holder = _holder(user, file['sha256'])
        if holder and holder['id'] == item['id']:
            return item, False
        if holder:
            raise StoreError(
                f'This file is already in another item, {holder["id"][:12]}. A file documents one item.'
            )
        if len(item['files']) >= model.MAX_FILES:
            raise StoreError(f'An item has at most {model.MAX_FILES} files')
        _save_file(user, file, data)
        item['files'] = item['files'] + [file]
        _log(user, item, via, f'file added: {file["filename"]} ({file["kind"]})')
        return item, True


def remove_file(user: str, ref: str, sha256: str, *, via: str) -> dict:
    """Take a file off an item and delete it. A prepped or booked item keeps at least one file."""
    with _lock:
        item = get_item(user, ref)
        file = find_file(item, (sha256 or '').strip().lower()) if sha256 else None
        if not file:
            raise NotFound('No such file in this item')
        item['files'] = [f for f in item['files'] if f is not file]
        problems = model.status_problems(item, item['status'])
        if problems:
            raise StoreError(f'Cannot remove it from a {item["status"]} item: {"; ".join(problems)}.')
        path = file_path(user, file)
        _log(user, item, via, f'file removed: {file["filename"]}')
        if path:
            os.remove(path)
        return item


def _log(user: str, item: dict, via: str, what: str) -> None:
    """Save the item with one more line of history. Call it under the lock."""
    item['updated'] = now()
    entry = {'at': item['updated'], 'by': user, 'via': via, 'what': what}
    item['history'] = (item['history'] + [entry])[-MAX_HISTORY:]
    _write_json(_item_path(user, item['id']), item)


def resolve(user: str, ref: str) -> str:
    """The full id for an id or the start of one (at least 8 characters)."""
    ref = (ref or '').strip().lower()
    if not _HEX.fullmatch(ref) or not MIN_REF <= len(ref) <= 64:
        raise NotFound(f'An item id is {MIN_REF} to 64 hexadecimal characters')
    if len(ref) == 64:
        if os.path.exists(_item_path(user, ref)):
            return ref
        raise NotFound(f'No item {ref[:12]}')
    folder = _user_dir(user, 'items')
    names = os.listdir(folder) if os.path.isdir(folder) else []
    hits = [n[:-5] for n in names if n.startswith(ref) and n.endswith('.json')]
    if not hits:
        raise NotFound(f'No item starts with {ref}')
    if len(hits) > 1:
        raise NotFound(f'{len(hits)} items start with {ref}: give more of the id')
    return hits[0]


def get_item(user: str, ref: str) -> dict:
    return _read_item(_item_path(user, resolve(user, ref)))


def all_items(user: str) -> list[dict]:
    """Every item of the user, newest document date first; items without a date come first."""
    folder = _user_dir(user, 'items')
    items = []
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if not name.endswith('.json'):
            continue
        try:
            items.append(_read_item(os.path.join(folder, name)))
        except (OSError, ValueError, KeyError, AttributeError):
            log.warning('Unreadable item file skipped: %s', name)
    items.sort(key=lambda i: (i['facts'].get('date') or '9999', i['created'], i['id']), reverse=True)
    return items


def search(items: list[dict], status: str | None = None, query: str | None = None) -> list[dict]:
    """Filter by status ('todo' means new and prepped) and by text in the common fields."""
    if status == 'todo':
        items = [i for i in items if i['status'] in ('new', 'prepped')]
    elif status and status != 'all':
        items = [i for i in items if i['status'] == status]
    needle = (query or '').strip().lower()
    if needle:
        items = [i for i in items if needle in _haystack(i)]
    return items


def _haystack(item: dict) -> str:
    facts = item['facts']
    parts = [
        item['id'],
        *(f['filename'] for f in item['files']),
        item.get('source'),
        item.get('notes'),
        (item.get('booking') or {}).get('voucher'),
        *(facts.get(k) for k in ('counterparty', 'description', 'invoice_number', 'date', 'amount')),
    ]
    return '\n'.join(p for p in parts if p).lower()


def counts(items: list[dict]) -> dict[str, int]:
    out = dict.fromkeys(model.STATUSES, 0)
    for item in items:
        out[item['status']] = out.get(item['status'], 0) + 1
    out['todo'] = out['new'] + out['prepped']
    out['all'] = len(items)
    return out


def update_item(
    user: str,
    ref: str,
    *,
    via: str,
    facts: dict | None = None,
    kontering=_KEEP,
    status: str | None = None,
    voucher: str | None = None,
    receipt_attached: bool | None = None,
    skip_reason: str | None = None,
    notes=_KEEP,
) -> dict:
    """Change an item. Only what is given changes; nothing is written when a check fails.

    A status has conditions (see model.status_problems). They are checked on the item as it
    would be after the change, also when the status itself stays the same: a prepped item
    stays complete and a booked item keeps its voucher number.
    """
    with _lock:
        item = get_item(user, ref)
        before = json.dumps(item, sort_keys=True)
        what = []
        if facts:
            cleaned = model.clean_facts(facts)
            was = item['facts']
            item['facts'] = model.settle_sek(was, was | cleaned, cleaned)
            changed = [k for k in model.FACT_FIELDS if was.get(k) != item['facts'].get(k)]
            if changed:
                what.append(f'facts: {", ".join(changed)}')
        if kontering is not _KEEP:
            new_kontering = model.clean_kontering(kontering)
            if new_kontering != item.get('kontering'):
                item['kontering'] = new_kontering
                what.append('kontering')
        if notes is not _KEEP:
            new_notes = model.text(notes, 'notes', 4000, multiline=True)
            if new_notes != item.get('notes'):
                item['notes'] = new_notes
                what.append('notes')
        if voucher is not None or receipt_attached is not None:
            booking = dict(item.get('booking') or {'voucher': None, 'receipt_attached': False, 'date': None})
            if voucher is not None:
                booking['voucher'] = model.text(voucher, 'voucher', 50)
            if receipt_attached is not None:
                booking['receipt_attached'] = bool(receipt_attached)
            if booking != item.get('booking'):
                item['booking'] = booking
                what.append('booking')
        if skip_reason is not None:
            reason = model.text(skip_reason, 'skip_reason', 300)
            if reason != item.get('skip_reason'):
                item['skip_reason'] = reason
                what.append('skip reason')
        old_status = item['status']
        new_status = status or old_status
        if new_status not in model.STATUSES:
            raise StoreError(f'status: one of {", ".join(model.STATUSES)}')
        problems = model.status_problems(item, new_status)
        if problems:
            hint = (
                ''
                if status
                else f' Set the status back to new to save unfinished changes to a {old_status} item.'
            )
            raise StoreError(f'Cannot be {new_status}: {"; ".join(problems)}.{hint}')
        if new_status != old_status:
            item['status'] = new_status
            if new_status == 'booked':
                item['booking']['date'] = today()
            what.append(f'status: {old_status} to {new_status}')
        if json.dumps(item, sort_keys=True) == before:
            return item
        _log(user, item, via, '; '.join(what))
        return item


def delete_item(user: str, ref: str) -> None:
    """Remove an item and its files. The next scan of the inbox will add them again."""
    with _lock:
        item = get_item(user, ref)
        paths = [file_path(user, f) for f in item['files']]
        os.remove(_item_path(user, item['id']))
        for path in paths:
            if path:
                os.remove(path)


# ---------------------------------------------------------------- the user's accounts
def read_accounts(user: str) -> list[dict]:
    path = _user_dir(user, 'accounts.json')
    if not os.path.exists(path):
        return []
    try:
        return _read_json(path)
    except (OSError, ValueError):
        log.warning('Unreadable accounts file for %s', user)
        return []


def write_accounts(user: str, source: str) -> list[dict]:
    """Replace the user's list of accounts with what the pasted text holds."""
    accounts = model.parse_accounts(source)
    with _lock:
        _write_json(_user_dir(user, 'accounts.json'), accounts)
    return accounts


# An AI client can read the list of accounts but not change it, unless the user switches that on.
# The switch turns itself off: adding accounts is asked for on purpose, never part of everyday use.
ACCOUNTS_FROM_CLIENT_MINUTES = 60


def accounts_from_client_until(user: str) -> str | None:
    """Until when an AI client may add accounts for this user, or None when it may not."""
    path = _user_dir(user, 'settings.json')
    if not os.path.exists(path):
        return None
    try:
        until = _read_json(path).get('accounts_from_client_until')
    except (OSError, ValueError, AttributeError):
        log.warning('Unreadable settings file for %s', user)
        return None
    return until if isinstance(until, str) and until > now() else None


def allow_accounts_from_client(user: str, allowed: bool) -> str | None:
    """Switch it on for ACCOUNTS_FROM_CLIENT_MINUTES, or off. Returns until when it is on."""
    until = None
    if allowed:
        end = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=ACCOUNTS_FROM_CLIENT_MINUTES)
        until = end.strftime('%Y-%m-%dT%H:%M:%SZ')
    with _lock:
        _write_json(_user_dir(user, 'settings.json'), {'accounts_from_client_until': until})
    return until


def add_accounts(user: str, accounts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Add the accounts an AI client sends: (added, already there). Only while the user allows it.

    It only adds. An account that is in the list keeps its name and codes; changing and removing
    accounts is done in the web page.
    """
    new = model.clean_accounts(accounts)
    with _lock:
        if not accounts_from_client_until(user):
            raise StoreError(
                'Adding accounts is switched off. The user switches it on in the web page, under '
                'Accounts, when they want accounts added. Do not ask them to unless they asked for that.'
            )
        current = {a['account']: a for a in read_accounts(user)}
        added = [a for a in new if a['account'] not in current]
        already = [current[a['account']] for a in new if a['account'] in current]
        if len(current) + len(added) > model.MAX_ACCOUNTS:
            raise StoreError(f'At most {model.MAX_ACCOUNTS} accounts')
        if added:
            merged = current | {a['account']: a for a in added}
            _write_json(_user_dir(user, 'accounts.json'), [merged[n] for n in sorted(merged)])
    return added, already
