# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""What an item holds, and the checks on it. Pure functions: no files and no network.

Nothing here decides how something should be booked. The checks only catch slips: a
malformed date, an amount with three decimals, a kontering that does not balance.
"""

from __future__ import annotations

import datetime
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

STATUSES = ('new', 'prepped', 'booked', 'skipped')
KINDS = ('receipt', 'invoice', 'credit_note', 'other')
FACT_FIELDS = (
    'kind',
    'date',
    'counterparty',
    'description',
    'amount',
    'currency',
    'vat_amount',
    'exchange_rate',
    'amount_sek',
    'exchange_rate_source',
    'invoice_number',
    'due_date',
    'payment_date',
)
# The SEK side of a document in another currency. See settle_sek.
SEK_SIDE = ('exchange_rate', 'amount_sek', 'exchange_rate_source')
MAX_LINES = 40
MAX_ACCOUNTS = 5000

_DATES = ('date', 'due_date', 'payment_date')
_MONEY = ('amount', 'vat_amount', 'amount_sek')
_TEXT = {'counterparty': 200, 'description': 500, 'invoice_number': 100, 'exchange_rate_source': 100}
_ORE = Decimal('0.01')
_RATE_LIMIT = Decimal('100000')
_ACCOUNT = re.compile(r'\d{4,6}')
_CURRENCY = re.compile(r'[A-Z]{3}')
_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_SPACES = re.compile(r"[\s   ']")
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_LIMIT = Decimal('1000000000000')


class Invalid(ValueError):
    """A value the user or the client has to correct. The message is shown as it is."""


def text(value, field: str, max_len: int, *, multiline: bool = False) -> str | None:
    """A trimmed string, or None when it is empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f'{field}: expected text')
    value = _CONTROL.sub('', value.replace('\r\n', '\n').replace('\r', '\n'))
    if not multiline:
        value = ' '.join(value.split())
    value = value.strip()
    if len(value) > max_len:
        raise Invalid(f'{field}: at most {max_len} characters')
    return value or None


def money(value, field: str) -> str | None:
    """An amount as a string with two decimals ("1250.00"), or None when it is empty.

    Takes text only, never a number: an amount is written, not computed, on its way in here.
    The text may use a decimal comma and spaces ("1 250,50"). An amount that would have to be
    rounded is refused, never rounded silently.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f'{field}: write the amount as text, like "1250.00", not as a number')
    try:
        amount = _decimal(value)
    except ValueError:
        raise Invalid(f'{field}: "{value}" is not an amount. Write it like 1250.00') from None
    if amount is None:
        return None
    if abs(amount) >= _LIMIT:
        raise Invalid(f'{field}: "{value}" is not an amount. Write it like 1250.00')
    # Counted in digits, not by value: "1,250" may be meant as 1250 and must not pass as 1.25.
    if amount.as_tuple().exponent < -2:
        raise Invalid(f'{field}: "{value}" has more than two decimals')
    return f'{amount + 0:.2f}'  # + 0 turns -0 into 0


def rate(value, field: str = 'exchange_rate') -> str | None:
    """An exchange rate as text ("11.4235"), or None when it is empty.

    The rate is SEK for one unit of the other currency. Text only, like an amount; up to eight
    decimals, and more than zero.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f'{field}: write the rate as text, like "11.4235", not as a number')
    try:
        number = _decimal(value)
    except ValueError:
        number = Decimal(0)
    if number is None:
        return None
    if not 0 < number < _RATE_LIMIT or number.as_tuple().exponent < -8:
        raise Invalid(
            f'{field}: "{value}" is not a rate. Write SEK for one unit of the currency, like 11.4235'
        )
    return format(number.normalize(), 'f')


def _decimal(value: str) -> Decimal | None:
    """The number written in a text, which may use a decimal comma and spaces ("1 250,50").

    None when the text is empty. ValueError when it is not a number.
    """
    raw = _SPACES.sub('', value).replace('−', '-')
    if not raw:
        return None
    if ',' in raw and '.' in raw:  # the last of the two is the decimal sign
        cut = max(raw.rfind(','), raw.rfind('.'))
        raw = re.sub(r'[.,]', '', raw[:cut]) + '.' + raw[cut + 1 :]
    else:
        raw = raw.replace(',', '.')
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(value) from None
    if not number.is_finite():
        raise ValueError(value)
    return number


def iso_date(value, field: str) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        if not _ISO_DATE.fullmatch(value):
            raise ValueError
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise Invalid(f'{field}: "{value}" is not a date. Write it like 2026-03-31') from None


def clean_facts(changes: dict) -> dict:
    """Checked changes to an item's facts. A value of None (or empty text) clears the fact."""
    out = {}
    for key, value in changes.items():
        if key not in FACT_FIELDS:
            raise Invalid(f'Unknown fact "{key}". The facts are: {", ".join(FACT_FIELDS)}')
        if isinstance(value, str) and not value.strip():
            value = None
        if value is None:
            out[key] = None
        elif key in _DATES:
            out[key] = iso_date(value, key)
        elif key in _MONEY:
            out[key] = money(value, key)
        elif key == 'exchange_rate':
            out[key] = rate(value, key)
        elif key == 'currency':
            code = str(value).strip().upper()
            if not _CURRENCY.fullmatch(code):
                raise Invalid(f'currency: "{value}" is not a three-letter code such as SEK or EUR')
            out[key] = code
        elif key == 'kind':
            if value not in KINDS:
                raise Invalid(f'kind: one of {", ".join(KINDS)}')
            out[key] = value
        else:
            out[key] = text(value, key, _TEXT[key])
    return out


def to_sek(amount: str, exchange_rate: str) -> str:
    """The amount at the rate, rounded to öre the usual way (half up)."""
    return f'{(Decimal(amount) * Decimal(exchange_rate)).quantize(_ORE, rounding=ROUND_HALF_UP) + 0:.2f}'


def settle_sek(before: dict, facts: dict, given) -> dict:
    """The facts after a change, with their SEK side in order. `given` names the facts in the change.

    Bookkeeping is in SEK. For a document in another currency the total in SEK comes one of two
    ways, and whoever gives it says which:

    - exchange_rate (SEK for one unit of the currency): amount_sek is worked out from it, and
      worked out again whenever the amount or the rate changes.
    - amount_sek as it is, for example what the card statement says was drawn: no rate is kept.

    So a stored rate and a stored total in SEK never disagree. Where the figure comes from
    (receipt, card statement, Riksbanken) goes in exchange_rate_source. Nothing here looks a
    rate up: it is given, never guessed.
    """
    facts = dict(facts)

    def given_now(name: str) -> bool:
        return name in given and facts.get(name) is not None

    currency = facts.get('currency')
    if currency == 'SEK':
        if any(given_now(name) for name in SEK_SIDE):
            raise Invalid(
                'The amount is already in SEK: leave exchange_rate, amount_sek and exchange_rate_source empty'
            )
        return facts | dict.fromkeys(SEK_SIDE)
    if before.get('currency') not in (None, currency) and not given_now('exchange_rate'):
        facts['exchange_rate'] = None  # the rate belonged to the other currency
    amount = facts.get('amount')
    if given_now('exchange_rate') and given_now('amount_sek'):
        worked_out = to_sek(amount, facts['exchange_rate']) if amount is not None else None
        if worked_out != facts['amount_sek']:
            sum_ = f'{amount} × {facts["exchange_rate"]} = {worked_out}' if amount else 'there is no amount'
            raise Invalid(
                f'amount_sek {facts["amount_sek"]} is not the amount at the exchange_rate ({sum_}). Give one '
                'of the two: amount_sek as the statement shows it, or exchange_rate'
            )
    elif given_now('amount_sek'):
        facts['exchange_rate'] = None  # a total given as it is replaces one worked out from a rate
    elif before.get('exchange_rate') is not None and facts.get('exchange_rate') is None:
        facts['amount_sek'] = None  # it was worked out from the rate that is now gone
    if facts.get('exchange_rate') is not None:
        facts['amount_sek'] = to_sek(amount, facts['exchange_rate']) if amount is not None else None
    had = before.get('exchange_rate') is not None or before.get('amount_sek') is not None
    gone = facts.get('exchange_rate') is None and facts.get('amount_sek') is None
    if had and gone and not given_now('exchange_rate_source'):
        facts['exchange_rate_source'] = None  # nothing is left for it to be the source of
    return facts


def implied_rate(facts: dict) -> str | None:
    """The rate that a total in SEK given as it is amounts to, to four decimals. For display only."""
    amount, sek = facts.get('amount'), facts.get('amount_sek')
    if facts.get('exchange_rate') or not amount or not sek or Decimal(amount) == 0:
        return None
    return str((Decimal(sek) / Decimal(amount)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))


def clean_kontering(value: dict | None) -> dict | None:
    """A checked kontering, or None when it holds nothing (which clears it).

    Every line has an account and an amount on one side, debit or credit. Rows that are
    completely empty are dropped, so a form can send its spare rows.
    """
    if not value:
        return None
    if not isinstance(value, dict):
        raise Invalid('kontering: expected lines, vat_treatment and note')
    lines = []
    for n, raw in enumerate(value.get('lines') or [], 1):
        if not isinstance(raw, dict):
            raise Invalid(f'Line {n}: expected account, debit or credit, and text')
        account = str(raw.get('account') or '').strip()
        debit = money(raw.get('debit'), f'Line {n} debit')
        credit = money(raw.get('credit'), f'Line {n} credit')
        debit = None if debit is not None and Decimal(debit) == 0 else debit
        credit = None if credit is not None and Decimal(credit) == 0 else credit
        line_text = text(raw.get('text'), f'Line {n} text', 200)
        if not account and debit is None and credit is None and not line_text:
            continue
        if not _ACCOUNT.fullmatch(account):
            raise Invalid(f'Line {n}: the account is 4 to 6 digits, not "{account}"')
        if (debit is None) == (credit is None):
            raise Invalid(f'Line {n} (account {account}): give a debit or a credit amount, one of them')
        if Decimal(debit or credit) < 0:
            raise Invalid(f'Line {n} (account {account}): amounts are positive. Use the other side instead')
        lines.append({'account': account, 'debit': debit, 'credit': credit, 'text': line_text})
    if len(lines) > MAX_LINES:
        raise Invalid(f'kontering: at most {MAX_LINES} lines')
    vat_treatment = text(value.get('vat_treatment'), 'vat_treatment', 200)
    note = text(value.get('note'), 'note', 1000, multiline=True)
    if not lines and not vat_treatment and not note:
        return None
    return {'lines': lines, 'vat_treatment': vat_treatment, 'note': note}


def totals(kontering: dict | None) -> tuple[Decimal, Decimal]:
    """The sum of the debit side and of the credit side."""
    lines = (kontering or {}).get('lines') or []
    debit = sum((Decimal(ln['debit']) for ln in lines if ln.get('debit')), Decimal(0))
    credit = sum((Decimal(ln['credit']) for ln in lines if ln.get('credit')), Decimal(0))
    return debit, credit


def prep_problems(item: dict) -> list[str]:
    """What is missing before the item can be called prepped. Empty when nothing is."""
    problems = []
    facts = item.get('facts') or {}
    missing = [name for name in ('date', 'amount', 'currency') if not facts.get(name)]
    if missing:
        problems.append(f'Missing facts: {", ".join(missing)}')
    if facts.get('currency') not in (None, 'SEK') and not facts.get('amount_sek'):
        problems.append(
            f'The total in SEK is missing (the amount is in {facts["currency"]}): give exchange_rate, '
            'or amount_sek as the card statement shows it'
        )
    lines = (item.get('kontering') or {}).get('lines') or []
    if len(lines) < 2:
        problems.append('The kontering needs at least two lines')
    else:
        debit, credit = totals(item['kontering'])
        if debit != credit:
            problems.append(f'The kontering does not balance: debit {debit:.2f}, credit {credit:.2f}')
    return problems


def booking_problems(item: dict) -> list[str]:
    """What is missing before the item can be called booked."""
    booking = item.get('booking') or {}
    problems = []
    if not booking.get('voucher'):
        problems.append('The voucher number from the bookkeeping service is missing')
    if not booking.get('receipt_attached'):
        problems.append('Confirm that the receipt is attached to the voucher in the bookkeeping service')
    return problems


def status_problems(item: dict, status: str) -> list[str]:
    """Why the item cannot have this status, if anything."""
    if status == 'prepped':
        return prep_problems(item)
    if status == 'booked':
        return booking_problems(item)
    if status == 'skipped' and not item.get('skip_reason'):
        return ['Say why the item is skipped']
    return []


def warnings(item: dict, accounts: list[dict], others: list[dict]) -> list[str]:
    """Things worth a second look. They never stop a save."""
    out = []
    kontering = item.get('kontering') or {}
    lines = kontering.get('lines') or []
    if lines:
        debit, credit = totals(kontering)
        if debit != credit:
            out.append(f'The kontering does not balance: debit {debit:.2f}, credit {credit:.2f}')
        if accounts:
            known = {a['account'] for a in accounts}
            unknown = sorted({ln['account'] for ln in lines} - known)
            if unknown:
                out.append(f'Not in your list of accounts: {", ".join(unknown)}')
    # A skipped item is settled: it is no twin to warn about, and needs no such warning itself.
    if item.get('status') != 'skipped':
        twins = [o['id'][:12] for o in others if o.get('status') != 'skipped' and _same_purchase(item, o)]
        if twins:
            out.append(f'Same date, amount and currency as: {", ".join(twins)}. Possibly the same purchase')
    return out


def _same_purchase(a: dict, b: dict) -> bool:
    if a['id'] == b['id']:
        return False
    fa, fb = a.get('facts') or {}, b.get('facts') or {}
    return all(fa.get(k) and fa.get(k) == fb.get(k) for k in ('date', 'amount', 'currency'))


# The codes a chart of accounts can carry beside a name, as the user's bookkeeping program has
# them: the box in the VAT return (momsruta, "B10" or "05"), the row in the NE appendix (bokslut,
# "B9", "R6, R13" or "R4/R8") and the SRU code ("7280"). They are the user's own, kept as given,
# and only said back: TallyQueue draws no conclusion from them.
ACCOUNT_CODES = ('vat_box', 'ne_row', 'sru')
_CODE = {
    'vat_box': re.compile(r'[A-Z]?\d{2}'),
    'ne_row': re.compile(r'[BRU]\d{1,2}(?:(?:, ?|/)[BRU]\d{1,2})*'),
    'sru': re.compile(r'\d{4}'),
}
_CODE_NAMES = {'vat_box': 'VAT box', 'ne_row': 'NE row', 'sru': 'SRU code'}


def _account(number: str, name, codes: dict, where: str) -> dict:
    """One account, checked: its number, its name and the codes it has, each only when given."""
    out = {'account': number, 'name': text(name or '', f'{where} name', 200) or ''}
    for key in ACCOUNT_CODES:
        value = _SPACES.sub(' ', str(codes.get(key) or '')).strip()
        if not value:
            continue
        if not _CODE[key].fullmatch(value):
            raise Invalid(f'{where}: "{value[:20]}" is not a {_CODE_NAMES[key]}')
        out[key] = value
    return out


def clean_accounts(accounts) -> list[dict]:
    """Accounts given one by one, as number and name and any codes, checked. Each number once."""
    if not isinstance(accounts, list) or not accounts:
        raise Invalid('accounts: expected a list of accounts, each with its number and name')
    out: dict[str, dict] = {}
    for n, raw in enumerate(accounts, 1):
        if not isinstance(raw, dict):
            raise Invalid(f'Account {n}: expected account and name')
        number = str(raw.get('account') or '').strip()
        if not re.fullmatch(r'\d{4,6}', number):
            raise Invalid(f'Account {n}: the number is 4 to 6 digits, like "1930"')
        if number in out:
            raise Invalid(f'Account {n}: {number} is in the list twice')
        out[number] = _account(number, raw.get('name'), raw, f'Account {n}')
    return [out[number] for number in sorted(out)]


_ACCOUNT_LINE = re.compile(r'(\d{4,6})(?:[\s;,:=\-–]+(.*))?')
_SIE_KONTO = re.compile(r'#KONTO\s+(\d{4,6})\s+(?:"((?:[^"\\]|\\.)*)"|(\S.*))')
_SIE_SRU = re.compile(r'#SRU\s+(\d{4,6})\s+(\d{4})')
# Codes at the end of a line, in the order a printed chart has them: VAT box, NE row, SRU code.
# Only these shapes are taken as codes, so an ordinary name is left whole. An SRU code is read
# here only when it starts with 7 or 8, as the NE appendix's do; in columns any 4 digits are one.
# "B10" can be either a VAT box or an NE row: alone it is read as the NE row, which nearly every
# account has, and as a VAT box only when an NE row follows it.
_TRAILING_CODES = re.compile(
    r'(?P<name>.*?)'
    r'(?:\s+(?P<vat_box>[A-I]\d{2}(?=\s+[BRU]\d)|[AC-I]\d{2}))?'
    r'(?:\s+(?P<ne_row>[BRU]\d{1,2}(?:(?:, ?|/)[BRU]\d{1,2})*))?'
    r'(?:\s+(?P<sru>[78]\d{3}))?'
)


def _codes_at_end(rest: str) -> tuple[str, dict]:
    """A name followed by codes, as a printed chart has them, split into the two."""
    m = _TRAILING_CODES.fullmatch(rest)
    if not m or not m.group('name').strip():
        return rest, {}
    return m.group('name'), {k: m.group(k) for k in ACCOUNT_CODES if m.group(k)}


def parse_accounts(source: str) -> list[dict]:
    """The user's own list of accounts, from pasted text.

    One account per line: "1930 Företagskonto", "1930;Företagskonto", or a SIE line
    (#KONTO 1930 "Företagskonto"). Other lines that start with # are skipped, and so are the
    braces around a voucher's rows, so a whole SIE file can be pasted. A SIE file's #SRU lines
    give the accounts their SRU codes.

    The codes can follow the name, as a printed chart has them: "1930 Företagskonto B9 7280"
    (VAT box, NE row and SRU code, each when the account has one). For a line where that could be
    read two ways, give five columns separated by semicolons or tabs:
    "2611;Utgående moms 25 %;B10;B14;7381", with an empty column for a code the account has not.
    """
    accounts: dict[str, dict] = {}
    sru: dict[str, tuple[int, str]] = {}
    for n, line in enumerate(source.splitlines(), 1):
        line = line.strip().lstrip('﻿')
        if not line or line in ('{', '}'):
            continue
        if line.startswith('#'):
            m = _SIE_KONTO.fullmatch(line)
            if m:
                name = m.group(2) if m.group(2) is not None else m.group(3)
                old = accounts.get(m.group(1), {})
                codes = {k: old[k] for k in ACCOUNT_CODES if k in old}
                accounts[m.group(1)] = _account(m.group(1), name.replace('\\"', '"'), codes, f'Line {n}')
            m = _SIE_SRU.fullmatch(line)
            if m:
                sru[m.group(1)] = (n, m.group(2))
            continue
        for sep in ('\t', ';'):
            columns = [c.strip() for c in line.split(sep)]
            if len(columns) >= 5 and re.fullmatch(r'\d{4,6}', columns[0]):
                name = sep.join(columns[1:-3])
                codes = dict(zip(ACCOUNT_CODES, columns[-3:], strict=True))
                accounts[columns[0]] = _account(columns[0], name, codes, f'Line {n}')
                break
        else:
            m = _ACCOUNT_LINE.fullmatch(line)
            if not m:
                raise Invalid(f'Line {n}: expected an account number and its name, like "1930 Företagskonto"')
            name, codes = _codes_at_end(m.group(2) or '')
            accounts[m.group(1)] = _account(m.group(1), name, codes, f'Line {n}')
    for number, (n, code) in sru.items():
        if number in accounts:
            accounts[number] = _account(
                number, accounts[number]['name'], accounts[number] | {'sru': code}, f'Line {n}'
            )
    if len(accounts) > MAX_ACCOUNTS:
        raise Invalid(f'At most {MAX_ACCOUNTS} accounts')
    return [accounts[number] for number in sorted(accounts)]


def account_line(account: dict) -> str:
    """An account written as one line that parse_accounts reads back the same."""
    if any(account.get(k) for k in ACCOUNT_CODES):
        return ';'.join([account['account'], account['name'], *(account.get(k, '') for k in ACCOUNT_CODES)])
    return f'{account["account"]} {account["name"]}'.rstrip()
