# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""Items and receipts on disk. One folder per approved user; nothing is shared between users.

    DATA_DIR/users/<user>/items/<sha256>.json      the item: facts, proposed kontering, status, history
    DATA_DIR/users/<user>/receipts/<sha256>.<ext>  the receipt file as it arrived (a working copy)
    DATA_DIR/users/<user>/accounts.json            the user's own list of accounts, if they gave one

An item's id is the SHA-256 of its receipt file, so the same file added twice is one item.
The receipt kept here is a working copy. The archive that the law asks for is the one in the
user's bookkeeping service.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
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


def receipt_path(user: str, item: dict) -> str | None:
    """Where the item's receipt file is, or None when it is gone."""
    path = _user_dir(user, 'receipts', f'{item["id"]}.{item["receipt"]["ext"]}')
    return path if os.path.isfile(path) else None


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


# ---------------------------------------------------------------- receipt files
def sniff(data: bytes, filename: str = '') -> tuple[str, str]:
    """(extension, media type) from the file's content. Raises for a type that is not accepted.

    The content decides, not the name. Text is always served back as plain text, so an HTML
    receipt can never run as a page from this server.
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


# ---------------------------------------------------------------- items
def add_item(
    user: str,
    data: bytes,
    filename: str = '',
    *,
    via: str,
    source: str | None = None,
    facts: dict | None = None,
    kontering: dict | None = None,
    notes: str | None = None,
) -> tuple[dict, bool]:
    """Store a receipt file as a new item. Returns (item, created).

    When the same file is already in the list, the existing item comes back unchanged and
    created is False: a second scan of an inbox never overwrites what the user has edited.
    """
    if not data:
        raise StoreError('The file is empty')
    if len(data) > MAX_RECEIPT_BYTES:
        raise StoreError(f'The file is larger than {MAX_RECEIPT_BYTES // (1024 * 1024)} MB')
    ext, media = sniff(data, filename or '')
    item_id = hashlib.sha256(data).hexdigest()
    cleaned = model.clean_facts(facts or {})
    stamp = now()
    item = {
        'id': item_id,
        'status': 'new',
        'created': stamp,
        'updated': stamp,
        'receipt': {
            'filename': _display_name(filename or '', ext),
            'ext': ext,
            'media_type': media,
            'bytes': len(data),
        },
        'source': model.text(source, 'source', 300),
        'facts': {name: cleaned.get(name) for name in model.FACT_FIELDS},
        'kontering': model.clean_kontering(kontering),
        'booking': None,
        'skip_reason': None,
        'notes': model.text(notes, 'notes', 4000, multiline=True),
        'history': [{'at': stamp, 'by': user, 'via': via, 'what': 'added'}],
    }
    with _lock:
        path = _item_path(user, item_id)
        if os.path.exists(path):
            return _read_json(path), False
        _write(_user_dir(user, 'receipts', f'{item_id}.{ext}'), data)
        _write_json(path, item)
    return item, True


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
    return _read_json(_item_path(user, resolve(user, ref)))


def all_items(user: str) -> list[dict]:
    """Every item of the user, newest document date first; items without a date come first."""
    folder = _user_dir(user, 'items')
    items = []
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if not name.endswith('.json'):
            continue
        try:
            items.append(_read_json(os.path.join(folder, name)))
        except (OSError, ValueError):
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
        item['receipt']['filename'],
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
            changed = [k for k, v in cleaned.items() if item['facts'].get(k) != v]
            item['facts'].update(cleaned)
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
        item['updated'] = now()
        item['history'] = (
            item['history'] + [{'at': item['updated'], 'by': user, 'via': via, 'what': '; '.join(what)}]
        )[-MAX_HISTORY:]
        _write_json(_item_path(user, item['id']), item)
        return item


def delete_item(user: str, ref: str) -> None:
    """Remove an item and its receipt file. The next scan of the inbox will add it again."""
    with _lock:
        item = get_item(user, ref)
        receipt = receipt_path(user, item)
        os.remove(_item_path(user, item['id']))
        if receipt:
            os.remove(receipt)


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
