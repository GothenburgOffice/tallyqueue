# SPDX-License-Identifier: AGPL-3.0-or-later
"""Looking a rate up at Riksbanken, with the network stood in for.

The answers here are made up, in the shape the real service gives: a list of dated values as JSON
numbers, 204 when it has nothing, 429 with Retry-After when asked too often.
"""

import datetime
import re

import pytest

from tallyqueue import riksbank
from tallyqueue.riksbank import RateUnavailable

WEEK = (
    b'[{"date":"2026-10-15","value":11.1765},{"date":"2026-10-16","value":11.145},'
    b'{"date":"2026-10-19","value":11.104}]'
)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(riksbank, '_cache', {})
    monkeypatch.setattr(riksbank, 'ENABLED', True)


def answer(monkeypatch, status=200, body=WEEK, headers=None):
    """Stand in for the network. Returns the list of addresses that were asked for."""
    asked = []

    def get(url):
        asked.append(url)
        return status, headers or {}, body

    monkeypatch.setattr(riksbank, '_get', get)
    return asked


def test_the_rate_for_a_day_riksbanken_has(monkeypatch):
    asked = answer(monkeypatch)
    assert riksbank.rate(' eur ', '2026-10-19') == {
        'currency': 'EUR',
        'asked': '2026-10-19',
        'date': '2026-10-19',
        'rate': '11.104',
        'source': 'Riksbanken 2026-10-19',
    }
    # the address is fixed but for the currency code and the two dates
    assert asked == ['https://api.riksbank.se/swea/v1/Observations/SEKEURPMI/2026-10-09/2026-10-19']
    # a rate that is already known is not asked for again
    assert riksbank.rate('EUR', '2026-10-19')['rate'] == '11.104' and len(asked) == 1


def test_a_weekend_takes_the_latest_rate_before_it_and_says_so(monkeypatch):
    answer(monkeypatch)
    found = riksbank.rate('EUR', '2026-10-18')
    assert (found['date'], found['rate']) == ('2026-10-16', '11.145')
    assert found['source'] == 'Riksbanken 2026-10-16 (the latest before 2026-10-18)'


@pytest.mark.parametrize(
    ('sent', 'expected'),
    [(b'0.06251', '0.06251'), (b'11.10', '11.1'), (b'6.3E-05', '0.000063'), (b'10', '10')],
)
def test_the_rate_is_the_digits_riksbanken_sent_never_a_float(monkeypatch, sent, expected):
    answer(monkeypatch, body=b'[{"date":"2026-10-19","value":' + sent + b'}]')
    assert riksbank.rate('JPY', '2026-10-19')['rate'] == expected


@pytest.mark.parametrize(
    ('status', 'headers', 'body', 'said'),
    [
        (204, {}, b'', 'has no EUR rate for 2026-10-19 or the 10 days before it'),
        (429, {'Retry-After': '51'}, b'', 'Try again in 51 seconds'),
        (429, {}, b'', 'Try again in a minute'),
        (500, {}, b'', 'error (500)'),
        (302, {'Location': 'https://elsewhere.example/'}, b'', 'error (302)'),
        (200, {}, b'{"statusCode": 200}', 'not a list of rates'),
        (200, {}, b'[{"date":"2026-10-19","value":"11.104"}]', 'not a list of rates'),
        (200, {}, b'[{"date":"2026-10-19","value":-1.5}]', 'not a list of rates'),
        (200, {}, b'<html>', 'not a list of rates'),
        pytest.param(200, {}, b'[' + b' ' * riksbank.MAX_BYTES + b']', 'error (200)', id='too large'),
    ],
)
def test_what_riksbanken_cannot_give_is_said_plainly(monkeypatch, status, headers, body, said):
    answer(monkeypatch, status, body, headers)
    with pytest.raises(RateUnavailable, match=re.escape(said)):
        riksbank.rate('EUR', '2026-10-19')


@pytest.mark.parametrize(
    ('currency', 'date'),
    [
        ('SEK', '2026-10-19'),
        ('EURO', '2026-10-19'),
        ('E1R', '2026-10-19'),
        ('', '2026-10-19'),
        ('EUR', '19/10/2026'),
        ('EUR', '2026-10-19/../x'),
        ('EUR', ''),
        ('EUR', (datetime.date.today() + datetime.timedelta(days=1)).isoformat()),
    ],
)
def test_nothing_is_asked_for_a_currency_or_a_date_that_cannot_have_a_rate(monkeypatch, currency, date):
    asked = answer(monkeypatch)
    with pytest.raises(RateUnavailable):
        riksbank.rate(currency, date)
    assert asked == []


def test_no_network_and_turned_off(monkeypatch):
    def down(url):
        raise OSError('made-up network failure')

    monkeypatch.setattr(riksbank, '_get', down)
    with pytest.raises(RateUnavailable, match='could not be reached'):
        riksbank.rate('EUR', '2026-10-19')
    asked = answer(monkeypatch)
    monkeypatch.setattr(riksbank, 'ENABLED', False)
    with pytest.raises(RateUnavailable, match='turned off'):
        riksbank.rate('EUR', '2026-10-19')
    assert asked == []


def test_todays_rate_is_asked_for_again_until_it_is_published(monkeypatch):
    today = datetime.date.today()
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    asked = answer(monkeypatch, body=f'[{{"date":"{yesterday}","value":11.2}}]'.encode())
    assert riksbank.rate('EUR', today.isoformat())['date'] == yesterday
    riksbank.rate('EUR', today.isoformat())
    assert len(asked) == 2
