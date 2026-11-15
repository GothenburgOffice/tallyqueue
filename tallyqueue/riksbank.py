# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""Riksbanken's published exchange rates: SEK for one unit of another currency on a date.

This is the one place TallyQueue calls another service, and only when a user asks for a rate.
The call carries a currency code and two dates, nothing about the receipt or the user. It can
be turned off with RIKSBANK_LOOKUP=0.

Riksbanken publishes the rates for information ("indicative only"), so the figure is checked by
the user like every other one here. There is no rate on a weekend or a bank holiday: then the
latest one before the date is used, and the answer says which date it is from.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import urllib.error
import urllib.request
from decimal import Decimal

from . import model

API = 'https://api.riksbank.se/swea/v1'
ENABLED = os.environ.get('RIKSBANK_LOOKUP', '1').strip().lower() not in ('0', 'false', 'no', 'off')
LOOK_BACK_DAYS = 10  # more than the longest run of days without a rate
TIMEOUT = 10
MAX_BYTES = 64 * 1024
MAX_CACHED = 500

_CURRENCY = re.compile(r'[A-Z]{3}')
_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_cache: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()


class RateUnavailable(Exception):
    """No rate could be had. The message is shown to the user as it is."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The address is fixed: an answer that points somewhere else is an error, not followed."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _get(url: str) -> tuple[int, dict, bytes]:
    """(status, headers, body) of one GET. On its own so that tests can stand in for the network."""
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'TallyQueue'})  # noqa: S310
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b''


def rate(currency: str, date: str) -> dict:
    """Riksbanken's rate for the currency on the date, or the latest before it.

    Returns {'currency', 'asked', 'date', 'rate', 'source'}: `date` is the day the rate is from,
    `rate` is text ("11.104", SEK for one unit) and `source` is ready for exchange_rate_source.
    Raises RateUnavailable with a message for the user.
    """
    if not ENABLED:
        raise RateUnavailable('Looking up rates at Riksbanken is turned off on this server')
    currency = (currency or '').strip().upper()
    if not _CURRENCY.fullmatch(currency):
        raise RateUnavailable('Give the currency as a three-letter code such as EUR')
    if currency == 'SEK':
        raise RateUnavailable('The amount is already in SEK: it needs no rate')
    try:
        if not _ISO_DATE.fullmatch((date or '').strip()):
            raise ValueError
        day = datetime.date.fromisoformat(date.strip())
    except ValueError:
        raise RateUnavailable(f'"{date}" is not a date. Write it like 2026-03-31') from None
    today = datetime.date.today()
    if day > today:
        raise RateUnavailable(f'{day} has not come yet: Riksbanken has no rate for it')
    key = (currency, day.isoformat())
    with _lock:
        if key in _cache:
            return dict(_cache[key])

    start = day - datetime.timedelta(days=LOOK_BACK_DAYS)
    try:
        status, headers, body = _get(f'{API}/Observations/SEK{currency}PMI/{start}/{day}')
    except (OSError, ValueError):  # no network, a timeout, a refused connection
        raise RateUnavailable(
            'Riksbanken could not be reached. Give the rate by hand, or try again later'
        ) from None
    if status == 429:
        wait = str({k.lower(): v for k, v in headers.items()}.get('retry-after', '')).strip()
        when = f'in {wait} seconds' if wait.isdigit() else 'in a minute'
        raise RateUnavailable(f'Riksbanken allows only a few lookups a minute. Try again {when}')
    if status == 204:
        raise RateUnavailable(
            f'Riksbanken has no {currency} rate for {day} or the {LOOK_BACK_DAYS} days before it. '
            'Check the currency code and the date'
        )
    if status != 200 or len(body) > MAX_BYTES:
        raise RateUnavailable(
            f'Riksbanken answered with an error ({status}). Give the rate by hand, or try again later'
        )
    found = _latest(body, day)
    if found is None:
        raise RateUnavailable(
            'Riksbanken answered with something that is not a list of rates. Give the rate by hand'
        )
    on, value = found
    source = f'Riksbanken {on}' if on == day else f'Riksbanken {on} (the latest before {day})'
    out = {
        'currency': currency,
        'asked': day.isoformat(),
        'date': on.isoformat(),
        'rate': value,
        'source': source,
    }
    if on == day or day < today:  # today's own rate may still be on its way
        with _lock:
            if len(_cache) >= MAX_CACHED:
                _cache.clear()
            _cache[key] = dict(out)
    return out


def _latest(body: bytes, day: datetime.date) -> tuple[datetime.date, str] | None:
    """The newest (date, rate) in the answer that is not after the day. None when it holds none.

    The rate is read as the digits Riksbanken sent, never as a binary float.
    """
    try:
        rows = json.loads(body, parse_float=Decimal, parse_int=Decimal)
        seen = [
            (datetime.date.fromisoformat(row['date']), model.rate(format(row['value'], 'f')))
            for row in rows
            if isinstance(row['value'], Decimal)
        ]
    except (ValueError, TypeError, KeyError, model.Invalid):
        return None
    seen = [(on, value) for on, value in seen if on <= day and value is not None]
    return max(seen) if seen else None
