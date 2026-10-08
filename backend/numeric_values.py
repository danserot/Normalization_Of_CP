"""Numeric validation only; never extracts or fills proposal values."""
import math
import re
from decimal import Decimal, InvalidOperation


def parse_number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    raw = str(value).strip()
    raw = re.sub(r'(?i)(?:RUB|USD|EUR|KZT|TRY|руб\.?|тенге|тг\.?|₽|₸|\$|€)', '', raw).strip()
    # A number must occupy the entire cell. Never turn a date or SKU into money.
    if not re.fullmatch(r'[+-]?\d[\d\s\u00a0\u202f.,]*', raw):
        return None
    if re.search(r'\d[\s\u00a0\u202f]+\d', raw) and not re.fullmatch(r'[+-]?\d{1,3}(?:[\s\u00a0\u202f]+\d{3})+(?:[,.]\d{1,2})?', raw):
        return None
    raw = re.sub(r'[\s\u00a0\u202f]', '', raw)
    if ',' in raw and '.' in raw:
        decimal = ',' if raw.rfind(',') > raw.rfind('.') else '.'
        thousands = '.' if decimal == ',' else ','
        whole, fraction = raw.rsplit(decimal, 1)
        if not re.fullmatch(r'[+-]?\d{1,3}(?:' + re.escape(thousands) + r'\d{3})+', whole) or len(fraction) not in (1, 2):
            return None
        raw = whole.replace(thousands, '') + '.' + fraction
    elif ',' in raw or '.' in raw:
        sep = ',' if ',' in raw else '.'
        parts = raw.split(sep)
        # 1,234 / 1.234 could be a decimal or a thousands group: ask a human.
        if len(parts) != 2 or len(parts[1]) not in (1, 2):
            return None
        raw = raw.replace(',', '.')
    try:
        result = float(Decimal(raw))
        return result if math.isfinite(result) else None
    except (InvalidOperation, ValueError):
        return None


def numeric_quote(text):
    value = parse_number(text)
    if value is not None:
        return value
    # Explicit comma decimals in estimates may have up to six fractional digits.
    # Accept malformed PDF spacing only with an explicit decimal separator.
    raw = text.strip()
    if re.fullmatch(r'[+]?[\d \u00a0\u202f]+,\d{1,6}', raw):
        value = float(re.sub(r'[ \u00a0\u202f]', '', raw).replace(',', '.'))
        return value if math.isfinite(value) else None
    return None
