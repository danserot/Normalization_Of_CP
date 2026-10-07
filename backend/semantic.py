"""Fast offline label classification: aliases first, cached character n-grams second."""
import math, os, re, unicodedata
from collections import Counter
from functools import lru_cache

FIELD_ALIASES = {
    'name': ['наименование', 'наименование товара', 'наименование работ', 'наименование услуг', 'товар', 'услуга', 'работы', 'описание', 'product', 'item', 'name', 'description'],
    'quantity': ['количество', 'кол во', 'кол-во', 'количество товара', 'объем', 'объём', 'шт', 'qty', 'quantity'],
    'unit': ['ед изм', 'единица измерения', 'единица', 'ед', 'unit', 'uom'],
    'unitPrice': ['цена', 'цена ед', 'цена за ед', 'цена за единицу', 'стоимость единицы', 'стоимость 1 ед', 'тариф', 'unitprice', 'unit price', 'unit cost', 'price'],
    'lineTotal': ['сумма', 'сумма позиции', 'общая сумма', 'всего', 'стоимость', 'итого', 'linetotal', 'line total', 'total', 'amount'],
}
SEMANTIC_PROTOTYPES = {
    'name': ['название продукции', 'описание позиции', 'product description'],
    'quantity': ['объем поставки', 'число единиц', 'quantity of goods'],
    'unit': ['мера товара', 'единицы поставки', 'measurement unit'],
    'unitPrice': ['стоимость за единицу продукции', 'тариф за одну единицу', 'price per item'],
    'lineTotal': ['полная стоимость позиции', 'итог по строке', 'total amount'],
}

def normalize_label(text):
    value = unicodedata.normalize('NFKC', str(text or '')).casefold().replace('ё', 'е')
    value = re.sub(r'[‐‑‒–—―−]+', '-', value)
    value = re.sub(r'[/.:]+', ' ', value)
    value = re.sub(r'[^\w\s-]+', ' ', value, flags=re.UNICODE)
    value = re.sub(r'[-\s]+', ' ', value).strip()
    return value

NORMALIZED_ALIASES = {field: frozenset(normalize_label(v) for v in values) for field, values in FIELD_ALIASES.items()}

@lru_cache(maxsize=4096)
def _vector(text):
    wrapped = f'  {normalize_label(text)}  '
    grams = Counter(wrapped[i:i + 3] for i in range(max(0, len(wrapped) - 2)))
    norm = math.sqrt(sum(value * value for value in grams.values())) or 1
    return {key: value / norm for key, value in grams.items()}

def _similarity(left, right):
    a, b = _vector(left), _vector(right)
    return sum(value * b.get(key, 0) for key, value in a.items())

@lru_cache(maxsize=4096)
def classify_label(text):
    label = normalize_label(text)
    for field, aliases in NORMALIZED_ALIASES.items():
        if label in aliases: return field, 0.99, 'alias'
    candidates = [(max(_similarity(label, prototype) for prototype in (*FIELD_ALIASES[field], *SEMANTIC_PROTOTYPES[field])), field) for field in FIELD_ALIASES]
    score, field = max(candidates)
    high = float(os.getenv('SEMANTIC_HIGH_THRESHOLD', '0.82'))
    low = float(os.getenv('SEMANTIC_LOW_THRESHOLD', '0.68'))
    if score >= high: return field, min(0.97, round(score, 3)), 'semantic'
    if score >= low: return field, round(score, 3), 'review'
    return None, round(score, 3), 'unknown'
