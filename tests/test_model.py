# SPDX-License-Identifier: AGPL-3.0-or-later
"""The checks on facts, konteringar and account lists. All data here is made up."""

import pytest

from tallyqueue import model
from tallyqueue.model import Invalid


@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        ('1250', '1250.00'),
        ('1250.5', '1250.50'),
        ('1 250,50', '1250.50'),
        ('1 250,50', '1250.50'),
        ('1.250,50', '1250.50'),
        ('1,250.50', '1250.50'),
        ('-99,90', '-99.90'),
        ('−99.90', '-99.90'),
        ('-0', '0.00'),
        ('', None),
        ('   ', None),
        (None, None),
    ],
)
def test_money_is_normalised_to_two_decimals(given, expected):
    assert model.money(given, 'amount') == expected


@pytest.mark.parametrize(
    'given', ['12.345', '1,250', 'abc', '12,5,0', 0.1 + 0.2, 1250, 1250.5, True, 'NaN', '1e400', [1]]
)
def test_money_refuses_what_it_would_have_to_guess_or_round(given):
    with pytest.raises(Invalid):
        model.money(given, 'amount')


def test_dates_must_be_real_iso_dates():
    assert model.iso_date('2026-02-28', 'date') == '2026-02-28'
    for bad in ('2026-02-30', '28/2/2026', '20260228', '2026-2-8'):
        with pytest.raises(Invalid):
            model.iso_date(bad, 'date')


def test_facts_are_checked_and_empty_text_clears():
    cleaned = model.clean_facts(
        {
            'date': '2026-10-03',
            'amount': '1 250,00',
            'currency': 'sek',
            'counterparty': '  Exempel   Hosting AB ',
            'description': '',
            'kind': 'invoice',
        }
    )
    assert cleaned == {
        'date': '2026-10-03',
        'amount': '1250.00',
        'currency': 'SEK',
        'counterparty': 'Exempel Hosting AB',
        'description': None,
        'kind': 'invoice',
    }
    for bad in ({'colour': 'red'}, {'currency': 'kronor'}, {'kind': 'bribe'}, {'counterparty': 'x' * 201}):
        with pytest.raises(Invalid):
            model.clean_facts(bad)


def kontering(*lines, **extra):
    return {'lines': [dict(zip(('account', 'debit', 'credit'), ln, strict=True)) for ln in lines], **extra}


def test_kontering_lines_are_checked_and_spare_rows_dropped():
    cleaned = model.clean_kontering(
        kontering(('6230', '1000', None), ('2640', '250,00', ''), ('', '', ''), ('1930', None, '1250'))
    )
    assert [(ln['account'], ln['debit'], ln['credit']) for ln in cleaned['lines']] == [
        ('6230', '1000.00', None),
        ('2640', '250.00', None),
        ('1930', None, '1250.00'),
    ]
    assert model.totals(cleaned) == (1250, 1250)


@pytest.mark.parametrize(
    'lines',
    [
        [('623', '10', None)],  # too short to be an account
        [('62A0', '10', None)],
        [('6230', '10', '10')],  # both sides
        [('6230', None, None)],  # no amount
        [('6230', '-10', None)],  # negative
        [('6230', '10', None)] * (model.MAX_LINES + 1),
    ],
)
def test_kontering_refuses_malformed_lines(lines):
    with pytest.raises(Invalid):
        model.clean_kontering(kontering(*lines))


def test_an_empty_kontering_is_none():
    assert model.clean_kontering(None) is None
    assert model.clean_kontering({'lines': [], 'vat_treatment': ' ', 'note': ''}) is None
    assert model.clean_kontering({'lines': [], 'note': 'unsure'})['note'] == 'unsure'


def item(**changes):
    base = {
        'id': 'a' * 64,
        'status': 'new',
        'facts': dict.fromkeys(model.FACT_FIELDS),
        'kontering': None,
        'booking': None,
        'skip_reason': None,
    }
    base['facts'] |= changes.pop('facts', {})
    return base | changes


def test_prepped_needs_the_key_facts_and_a_balanced_kontering():
    assert len(model.prep_problems(item())) == 2
    facts = {'date': '2026-10-03', 'amount': '1250.00', 'currency': 'SEK'}
    unbalanced = model.clean_kontering(kontering(('6230', '1000', None), ('1930', None, '1250')))
    problems = model.prep_problems(item(facts=facts, kontering=unbalanced))
    assert problems == ['The kontering does not balance: debit 1000.00, credit 1250.00']
    balanced = model.clean_kontering(kontering(('6230', '1250', None), ('1930', None, '1250')))
    assert model.prep_problems(item(facts=facts, kontering=balanced)) == []


def test_booked_needs_a_voucher_number_and_the_receipt_attached():
    assert len(model.status_problems(item(), 'booked')) == 2
    assert (
        len(model.status_problems(item(booking={'voucher': 'A 12', 'receipt_attached': False}), 'booked'))
        == 1
    )
    assert model.status_problems(item(booking={'voucher': 'A 12', 'receipt_attached': True}), 'booked') == []
    assert model.status_problems(item(), 'skipped') == ['Say why the item is skipped']
    assert model.status_problems(item(skip_reason='Private purchase'), 'skipped') == []
    assert model.status_problems(item(), 'new') == []


def test_warnings_point_at_things_worth_a_second_look():
    facts = {'date': '2026-10-03', 'amount': '100.00', 'currency': 'EUR'}
    lines = model.clean_kontering(kontering(('6540', '1100', None), ('1930', None, '1150')))
    this = item(facts=facts, kontering=lines)
    twin = item(id='b' * 64, facts=facts)
    other = item(id='c' * 64, facts=facts | {'amount': '99.00'})
    accounts = [{'account': '1930', 'name': 'Företagskonto'}]
    found = model.warnings(this, accounts, [this, twin, other])
    assert any('does not balance' in w for w in found)
    assert any('amount_sek is empty' in w for w in found)
    assert any('Not in your list of accounts: 6540' in w for w in found)
    assert any('bbbbbbbbbbbb' in w and 'cccc' not in w for w in found)
    assert model.warnings(item(), [], []) == []


def test_accounts_are_parsed_from_plain_lines_and_sie_lines():
    parsed = model.parse_accounts(
        '﻿#FLAGGA 0\n'
        '#KONTO 1930 "Företagskonto"\n'
        '#KONTO 2640 Ingående\n'
        '#KTYP 1930 T\n'
        '\n'
        '6230;Datakommunikation\n'
        '6540\tIT-tjänster\n'
        '2440 Leverantörsskulder\n'
        '2440 Leverantörer\n'
        '3001\n'
    )
    assert parsed == [
        {'account': '1930', 'name': 'Företagskonto'},
        {'account': '2440', 'name': 'Leverantörer'},
        {'account': '2640', 'name': 'Ingående'},
        {'account': '3001', 'name': ''},
        {'account': '6230', 'name': 'Datakommunikation'},
        {'account': '6540', 'name': 'IT-tjänster'},
    ]
    with pytest.raises(Invalid, match='Line 2'):
        model.parse_accounts('1930 Bank\nBank account\n')
