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
    assert any('Not in your list of accounts: 6540' in w for w in found)
    assert any('bbbbbbbbbbbb' in w and 'cccc' not in w for w in found)
    assert model.warnings(item(), [], []) == []


def test_accounts_given_one_by_one_are_checked():
    given = [{'account': ' 2640 ', 'name': '  Ingående   moms '}, {'account': '1930', 'name': ''}]
    assert model.clean_accounts(given) == [
        {'account': '1930', 'name': ''},
        {'account': '2640', 'name': 'Ingående moms'},
    ]
    for bad in (
        [],
        'x',
        [{'account': '19', 'name': 'a'}],
        [{'account': '1930'}, {'account': '1930'}],
        ['1930 Bank'],
    ):
        with pytest.raises(model.Invalid):
            model.clean_accounts(bad)
    with pytest.raises(model.Invalid):
        model.clean_accounts([{'account': '1930', 'name': 'x' * 201}])
    with_codes = [
        {'account': '2614', 'name': 'Utgående moms omvänd', 'vat_box': 'D30', 'ne_row': 'B14', 'sru': None}
    ]
    assert model.clean_accounts(with_codes) == [
        {'account': '2614', 'name': 'Utgående moms omvänd', 'vat_box': 'D30', 'ne_row': 'B14'}
    ]
    with pytest.raises(model.Invalid, match='not a NE row'):
        model.clean_accounts([{'account': '2614', 'name': 'x', 'ne_row': 'Z9'}])
    # a VAT box takes the same shapes from a client as from a pasted line: A to I, or two digits
    with pytest.raises(model.Invalid, match='"Z99" is not a VAT box'):
        model.clean_accounts([{'account': '2614', 'name': 'x', 'vat_box': 'Z99'}])
    assert model.clean_accounts([{'account': '2614', 'name': 'x', 'vat_box': '30'}])[0]['vat_box'] == '30'


def test_a_skipped_item_is_no_twin_to_warn_about():
    # Two documents for one purchase: one is skipped, and the warning has done its work.
    facts = {'date': '2026-10-03', 'amount': '100.00', 'currency': 'EUR'}
    kept = item(facts=facts)
    skipped = item(id='b' * 64, facts=facts, status='skipped', skip_reason='Same purchase as aaaaaaaaaaaa')
    assert model.warnings(kept, [], [kept, skipped]) == []
    assert model.warnings(skipped, [], [kept, skipped]) == []
    # a third document with the same figures that is not skipped is still pointed at
    third = item(id='c' * 64, facts=facts)
    assert any(
        'cccccccccccc' in w and 'bbbb' not in w for w in model.warnings(kept, [], [kept, skipped, third])
    )


# ---------------------------------------------------------------- another currency than SEK
@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        ('11.4235', '11.4235'),
        ('11,4235', '11.4235'),
        ('0,0912', '0.0912'),
        ('10', '10'),
        ('11.50', '11.5'),
        ('', None),
        (None, None),
    ],
)
def test_an_exchange_rate_is_text_for_one_unit_of_the_currency(given, expected):
    assert model.rate(given) == expected


@pytest.mark.parametrize('given', ['0', '-11.4', 'abc', 11.4235, 11, '0.123456789', '100000', 'NaN'])
def test_an_exchange_rate_that_is_not_one_is_refused(given):
    with pytest.raises(Invalid):
        model.rate(given)


EUR = {'amount': '20.00', 'currency': 'EUR'}


def settle(before, **change):
    """The facts after a change, the way the store applies one."""
    cleaned = model.clean_facts(change)
    was = dict.fromkeys(model.FACT_FIELDS) | before
    return model.settle_sek(was, was | cleaned, cleaned)


def test_a_rate_gives_the_total_in_sek_rounded_to_ore():
    facts = settle(EUR, exchange_rate='11,4235', exchange_rate_source='Riksbanken 2026-10-19')
    assert (facts['exchange_rate'], facts['amount_sek']) == ('11.4235', '228.47')
    # worked out again when the amount changes; half an öre rounds up (342.705)
    assert settle(facts, amount='30.00')['amount_sek'] == '342.71'
    assert settle(facts, amount='-20.00')['amount_sek'] == '-228.47'  # a credit note keeps its sign
    assert settle(facts, description='Licence')['amount_sek'] == '228.47'
    # when the rate goes, so does what was worked out from it, and the note of its source
    cleared = settle(facts, exchange_rate='')
    assert [cleared[name] for name in model.SEK_SIDE] == [None, None, None]
    # a rate with no amount yet waits for it
    waiting = settle({'currency': 'EUR'}, exchange_rate='11.4235')
    assert waiting['amount_sek'] is None and settle(waiting, amount='20')['amount_sek'] == '228.47'


def test_a_total_in_sek_given_as_it_is_replaces_the_rate():
    with_rate = settle(EUR, exchange_rate='11.4235', exchange_rate_source='Riksbanken 2026-10-19')
    facts = settle(with_rate, amount_sek='231,90', exchange_rate_source='card statement')
    assert (facts['exchange_rate'], facts['amount_sek']) == (None, '231.90')
    assert facts['exchange_rate_source'] == 'card statement'
    assert model.implied_rate(facts) == '11.5950' and model.implied_rate(with_rate) is None
    # a rate given later replaces the total again
    assert settle(facts, exchange_rate='11.5')['amount_sek'] == '230.00'
    # both at once are taken only when they agree to the öre
    assert settle(EUR, exchange_rate='11.4235', amount_sek='228.47')['exchange_rate'] == '11.4235'
    with pytest.raises(Invalid, match='Give one of the two'):
        settle(EUR, exchange_rate='11.4235', amount_sek='231.90')
    with pytest.raises(Invalid, match='there is no amount'):
        settle({'currency': 'EUR'}, exchange_rate='11.4235', amount_sek='231.90')


def test_an_amount_in_sek_has_no_sek_side_and_a_rate_belongs_to_its_currency():
    with pytest.raises(Invalid, match='already in SEK'):
        settle({'amount': '100.00', 'currency': 'SEK'}, exchange_rate='11.5')
    with pytest.raises(Invalid, match='already in SEK'):
        settle({'amount': '100.00', 'currency': 'SEK'}, amount_sek='100.00')
    with_rate = settle(EUR, exchange_rate='11.4235', exchange_rate_source='receipt')
    assert [settle(with_rate, currency='SEK')[name] for name in model.SEK_SIDE] == [None, None, None]
    assert [settle(with_rate, currency='USD')[name] for name in model.SEK_SIDE] == [None, None, None]
    assert settle(with_rate, currency='USD', exchange_rate='10.5')['amount_sek'] == '210.00'
    # what the statement says was drawn stays when the currency is corrected
    drawn = settle(EUR, amount_sek='231.90', exchange_rate_source='card statement')
    assert settle(drawn, currency='USD')['amount_sek'] == '231.90'


def test_prepped_needs_the_total_in_sek_for_another_currency():
    balanced = model.clean_kontering(kontering(('6540', '228.47', None), ('1930', None, '228.47')))
    facts = {'date': '2026-10-19', 'amount': '20.00', 'currency': 'EUR'}
    problems = model.prep_problems(item(facts=facts, kontering=balanced))
    assert len(problems) == 1 and 'total in SEK is missing' in problems[0] and 'EUR' in problems[0]
    assert model.prep_problems(item(facts=facts | {'amount_sek': '228.47'}, kontering=balanced)) == []


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


def test_accounts_keep_the_codes_a_printed_chart_has_after_the_name():
    # Made-up lines in the shape of a printed chart: name, then VAT box, NE row and SRU code,
    # each only when the account has one.
    parsed = model.parse_accounts(
        '1930 Företagskonto B9 7280\n'
        '2611 Utgående moms 25 % B10 B14 7381\n'
        '2618 Vilande utgående moms B14\n'
        '2050 Expansionsfond U2\n'
        '2019 Årets resultat 7300\n'
        '3740 Öresavrundning E42 R4/R8 7400\n'
        '6072 Representation, ej avdragsgill R6, R13 7501\n'
        '8990 Resultat R11 R11 7440\n'
        '4999 Konto 2024\n'
        '2640;Ingående moms;F48;B14;7381\n'
        '3001\tFörsäljning 25 %\tA05\t\t1234\n'
    )
    assert parsed == [
        {'account': '1930', 'name': 'Företagskonto', 'ne_row': 'B9', 'sru': '7280'},
        {'account': '2019', 'name': 'Årets resultat', 'sru': '7300'},
        {'account': '2050', 'name': 'Expansionsfond', 'ne_row': 'U2'},
        {'account': '2611', 'name': 'Utgående moms 25 %', 'vat_box': 'B10', 'ne_row': 'B14', 'sru': '7381'},
        {'account': '2618', 'name': 'Vilande utgående moms', 'ne_row': 'B14'},
        {'account': '2640', 'name': 'Ingående moms', 'vat_box': 'F48', 'ne_row': 'B14', 'sru': '7381'},
        {'account': '3001', 'name': 'Försäljning 25 %', 'vat_box': 'A05', 'sru': '1234'},
        {'account': '3740', 'name': 'Öresavrundning', 'vat_box': 'E42', 'ne_row': 'R4/R8', 'sru': '7400'},
        {'account': '4999', 'name': 'Konto 2024'},
        {'account': '6072', 'name': 'Representation, ej avdragsgill', 'ne_row': 'R6, R13', 'sru': '7501'},
        {'account': '8990', 'name': 'Resultat R11', 'ne_row': 'R11', 'sru': '7440'},
    ]
    # written back as lines, the list reads the same, as the web page shows it to be saved again
    assert model.parse_accounts('\n'.join(model.account_line(a) for a in parsed)) == parsed
    with pytest.raises(Invalid, match='Line 1: "X1" is not a VAT box'):
        model.parse_accounts('2640;Ingående moms;X1;B14;7381\n')
    with pytest.raises(Invalid, match='"Z99" is not a VAT box'):
        model.parse_accounts('2640;Ingående moms;Z99;B14;7381\n')
    with pytest.raises(Invalid, match='not a SRU code'):
        model.parse_accounts('2640;Ingående moms;F48;B14;73\n')


def test_a_sie_files_sru_lines_give_its_accounts_their_codes():
    parsed = model.parse_accounts(
        '#SRU 1930 7280\n#KONTO 1930 "Företagskonto"\n#KONTO 2440 "Leverantörsskulder"\n'
        '#SRU 2440 7382\n#SRU 9999 7000\n'
    )
    assert parsed == [
        {'account': '1930', 'name': 'Företagskonto', 'sru': '7280'},
        {'account': '2440', 'name': 'Leverantörsskulder', 'sru': '7382'},
    ]


def test_a_whole_sie_file_with_vouchers_keeps_its_accounts():
    # The rows of a voucher sit between braces, on lines of their own or after #VER.
    parsed = model.parse_accounts(
        '#FLAGGA 0\n'
        '#SIETYP 4\n'
        '#FNAMN "Exempel Enskild Firma"\n'
        '#KONTO 1930 "Företagskonto"\n'
        '#KONTO 6110 "Kontorsmateriel"\n'
        '#VER A 1 20261102 "Kontorsvaror"\n'
        '{\n'
        '\t#TRANS 6110 {} 399.00\n'
        '\t#TRANS 1930 {} -399.00\n'
        '}\n'
        '#VER A 2 20261103 "Porto" {\n'
        '   #TRANS 6110 {} 50.00\n'
        '   #TRANS 1930 {} -50.00\n'
        '}\n'
    )
    assert parsed == [
        {'account': '1930', 'name': 'Företagskonto'},
        {'account': '6110', 'name': 'Kontorsmateriel'},
    ]
    with pytest.raises(Invalid, match='Line 2'):
        model.parse_accounts('1930 Bank\n{ not an account }\n')
