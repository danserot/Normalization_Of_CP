"""Conservative extraction: missing numbers are null, evidence is cell-specific."""
import math
import re
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from datetime import datetime

FIELDS = {
    'title': ['название', 'тема', 'title'],
    'client': ['клиент', 'заказчик', 'покупатель', 'client', 'customer'],
    'clientContact': ['контакт', 'контакты', 'телефон', 'email', 'e-mail', 'clientContact'],
    'validUntil': ['действительно до', 'срок действия', 'предложение действительно до', 'validUntil'],
    'supplier': ['поставщик', 'исполнитель', 'продавец', 'supplier', 'жеткізуші'],
    'currency': ['валюта', 'currency'], 'vat': ['ндс', 'налог', 'vat'],
    'discount': ['скидка', 'discount'], 'delivery': ['доставка', 'стоимость доставки', 'delivery'],
    'paymentTerms': ['условия оплаты', 'оплата', 'paymentTerms'],
    'deliveryTerms': ['срок поставки', 'сроки поставки', 'условия поставки', 'deliveryTerms'],
    'warranty': ['гарантия', 'гарантийный срок', 'warranty'],
    'documentNumber': ['номер документа', 'номер', '№ документа', 'documentNumber'],
    'documentDate': ['дата документа', 'дата', 'documentDate'],
    'documentTotal': ['итого к оплате', 'всего к оплате', 'к оплате', 'общая сумма', 'итого', 'всего', 'documentTotal'],
}
HEADERS = {
    'name': ['наименование', 'наименование товара', 'наименование товаров работ услуг', 'название', 'товар', 'услуга', 'описание', 'name', 'description', 'item'],
    'quantity': ['количество', 'кол во', 'кол', 'quantity', 'qty'],
    'unit': ['ед изм', 'единица', 'единица измерения', 'ед', 'unit'],
    'unitPrice': ['цена', 'цена за ед', 'цена за единицу', 'цена за единицу без ндс', 'unitprice', 'unit price', 'price'],
    'lineTotal': ['сумма', 'сумма позиции', 'стоимость', 'итого', 'linetotal', 'total', 'amount'],
}


def normalized(value):
    return re.sub(r'[^\w]+', ' ', str(value).casefold()).strip()


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


def evidence(cell, value, method='rule', warning=None):
    return {**asdict(cell), 'value': value, 'excerpt': cell.text, 'verifiedInSource': True,
            'method': f'{cell.method}+{method}', 'confidence': .65 if cell.method == 'ocr' or method == 'model' else .95,
            'warning': warning or ('Проверьте OCR по оригиналу' if cell.method == 'ocr' else None)}


def grouped_rows(source):
    rows = defaultdict(list)
    for cell in source.cells:
        rows[(cell.block, cell.row)].append(cell)
    return rows


def extract_rules(source):
    proposal = {field: '' for field in FIELDS}
    proposal.update(documentTotal=None, notes=source.text(), items=[])
    proof, warnings = {}, list(source.warnings)
    rows = grouped_rows(source)
    used_rows = set()
    mappings = {}

    def set_field(key, value, cell):
        if value is None or value == '':
            return
        if proposal[key] not in ('', None) and proposal[key] != value:
            warnings.append(f'Внутри документа разные значения {key}: требуется проверка')
            existing = proof[key] if isinstance(proof[key], list) else [proof[key]]
            proof[key] = [*existing, evidence(cell, value)]
            return
        proposal[key], proof[key] = value, evidence(cell, value)

    for row_key, cells in rows.items():
        block, row_no = row_key
        mapping = {}
        for cell in cells:
            for key, labels in HEADERS.items():
                if normalized(cell.text) in labels:
                    mapping[key] = cell.cell
        if 'name' in mapping and any(k in mapping for k in ('quantity', 'unitPrice', 'lineTotal')):
            mappings[block] = mapping
            used_rows.add(row_key)
            continue
        joined = ' '.join(c.text for c in cells if c.text)
        is_total = bool(re.match(r'^(итого|всего|к оплате|общая сумма|subtotal|total)(?:\s|:|$)', joined, re.I))
        is_metadata = any(re.match(r'^' + re.escape(label) + r'(?:\s*[:=]|\s+)', joined, re.I)
                          for labels in FIELDS.values() for label in labels)
        if block in mappings and not is_total and not is_metadata:
            mapping = mappings[block]
            by_col = {c.cell: c for c in cells}
            name_cell = by_col.get(mapping['name'])
            service_row = re.match(r'^(примечание|ндс|скидка|доставка|подпись|страница)(?:\s|:|$)', joined, re.I)
            if name_cell and name_cell.text and not service_row:
                item = {'name': name_cell.text, 'quantity': None, 'unit': '', 'unitPrice': None, 'lineTotal': None}
                index = len(proposal['items'])
                for key, col in mapping.items():
                    cell = by_col.get(col)
                    if not cell or not cell.text:
                        continue
                    value = parse_number(cell.text) if key in ('quantity', 'unitPrice', 'lineTotal') else cell.text
                    if value is None or isinstance(value, float) and value < 0:
                        warnings.append(f'Строка {row_no}: неоднозначное значение {key}: {cell.text}')
                        continue
                    item[key] = value
                    proof[f'items.{index}.{key}'] = evidence(cell, value)
                proposal['items'].append(item)
                used_rows.add(row_key)
                if len(proposal['items']) > 2000:
                    raise ValueError('Превышен лимит 2000 позиций')
                continue
        # Labels may occupy their own cell or precede a value within one cell.
        for index, cell in enumerate(cells):
            for key, labels in FIELDS.items():
                for label in sorted(labels, key=len, reverse=True):
                    match = re.match(r'^' + re.escape(label) + r'(?:\s*[:№=]\s*|\s+)(.+)$', cell.text, re.I)
                    candidate, value_cell = None, cell
                    if match:
                        candidate = match.group(1).strip()
                    elif normalized(cell.text) == normalized(label):
                        value_cell = next((c for c in cells[index + 1:] if c.text), cell)
                        candidate = value_cell.text if value_cell is not cell else None
                    if candidate:
                        value = parse_number(candidate) if key == 'documentTotal' else candidate
                        set_field(key, value, value_cell)
                        break
            if re.match(r'^коммерческое предложение(?:\s|$)', cell.text, re.I):
                set_field('title', cell.text, cell)
                match = re.search(r'№\s*([\w/.-]+)', cell.text)
                if match:
                    set_field('documentNumber', match.group(1), cell)
                match = re.search(r'\bот\s+(\d{2}[./-]\d{2}[./-]\d{4})', cell.text)
                if match:
                    set_field('documentDate', match.group(1), cell)
            currency = re.search(r'(?<!\w)(RUB|USD|EUR|KZT|TRY|₽|₸|руб\.?|тенге)(?!\w)', cell.text, re.I)
            if currency and not proposal['currency']:
                set_field('currency', currency.group(1), cell)
    return proposal, proof, warnings, used_rows


def validate(proposal, proof, warnings):
    for field in ('documentDate', 'validUntil'):
        value = proposal[field]
        if re.fullmatch(r'\d{2}[./-]\d{2}[./-]\d{4}', value):
            try:
                datetime.strptime(re.sub(r'[/-]', '.', value), '%d.%m.%Y')
            except ValueError:
                warning = f'{field}: в источнике некорректная календарная дата'
                warnings.append(warning)
                entries = proof.get(field, [])
                for entry in entries if isinstance(entries, list) else [entries]:
                    entry['warning'] = warning
    for index, item in enumerate(proposal['items']):
        if item['quantity'] is None or item['unitPrice'] is None:
            warnings.append(f'Позиция {index + 1}: количество или цена отсутствует; заполните вручную')
        elif item.get('lineTotal') is not None:
            actual = item['quantity'] * item['unitPrice']
            if abs(actual - item['lineTotal']) > .02:
                message = f'Позиция {index + 1}: количество × цена отличается от суммы строки; проверьте НДС/скидку'
                warnings.append(message)
                if f'items.{index}.lineTotal' in proof:
                    proof[f'items.{index}.lineTotal']['warning'] = message
    total = proposal['documentTotal']
    if proposal['items'] and total is not None:
        sums = [i.get('lineTotal') if i.get('lineTotal') is not None else i['quantity'] * i['unitPrice']
                if i['quantity'] is not None and i['unitPrice'] is not None else None for i in proposal['items']]
        if all(v is not None for v in sums):
            raw = sum(sums)
            possibilities = [raw]
            discount = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*%', proposal['discount'].strip())
            vat = re.search(r'(\d+(?:[.,]\d+)?)\s*%', proposal['vat'])
            delivery = parse_number(proposal['delivery']) or 0
            if discount:
                possibilities.append(raw * (1 - float(discount[1].replace(',', '.')) / 100))
            if vat and not re.search(r'включ|в том числе|без ндс', proposal['vat'], re.I):
                possibilities += [v * (1 + float(vat[1].replace(',', '.')) / 100) for v in list(possibilities)]
            possibilities += [v + delivery for v in list(possibilities)]
            if all(abs(total - v) > .02 for v in possibilities):
                warnings.append('Итог документа не сходится с позициями и явно указанными НДС/скидкой/доставкой')
    if not proposal['items']:
        warnings.append('Позиции не распознаны уверенно; проверьте структуру таблицы')
    if not proof:
        warnings.append('Структура не распознана: извлечён только текст, поля нужно заполнить вручную')
