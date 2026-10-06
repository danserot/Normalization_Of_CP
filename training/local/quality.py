"""Conservative checks; passing them is machine validation, never human truth."""
import hashlib
import re
from collections import defaultdict

from backend.annotation_data import Annotation, source_from_payload, validate_annotation
from backend.rules import HEADERS, grouped_rows, normalized
from backend.universal import numeric_quote

REASONS = {
    'E_PARSE': 'Ошибка чтения документа.', 'E_TIMEOUT': 'Превышено время обработки.',
    'E_SCHEMA': 'Ответ модели не соответствует контракту.',
    'E_GROUNDING': 'Проверьте ссылки, цитаты и границы таблиц.',
    'E_OCR': 'Проверьте качество OCR и полноту извлечения.',
    'E_COLUMNS': 'Роль колонки не подтверждена распознанным заголовком.',
    'E_ROWS': 'В диапазоне товаров есть неоднозначные строки.',
    'E_COVERAGE': 'Возможно пропущена таблица или товарные строки.',
    'E_ARITHMETIC': 'Суммы требуют проверки с учётом НДС, скидки и округления.',
    'E_FIELD_CONFLICT': 'Правила и модель по-разному извлекли реквизит.',
    'E_NO_ITEMS': 'Товарные таблицы не подтверждены.',
    'E_CONTEXT': 'Задание не помещается в контекст; обрезка запрещена.',
    'E_MODEL': 'Модель не завершила обработку.',
    'E_CONVERSION': 'Не завершена локальная конвертация DOC.',
    'E_RESOURCE': 'Недостаточно ресурсов для этапа.',
}


def check_annotation(payload, annotation):
    reasons = set()
    source = source_from_payload(payload, 'document')
    by_id = {c.id: c for c in source.cells}
    rows = grouped_rows(source)
    # Structural validation is shared with human review, but no reviewed flag is
    # written back: machine and human provenance must remain separate.
    candidate = annotation.model_copy(deep=True)
    for table in candidate.tables:
        table.reviewed = True
    try:
        validate_annotation(candidate, payload, 'document', True)
    except ValueError:
        reasons.add('E_GROUNDING')
    if payload.get('parseError') or not source.cells:
        reasons.add('E_PARSE')
    if payload.get('incomplete') or any(c.method == 'ocr' for c in source.cells):
        reasons.add('E_OCR')
    text = source.text()
    if text.count('\ufffd') or sum(ord(c) < 32 and not c.isspace() for c in text):
        reasons.add('E_OCR')
    covered, item_rows, arithmetic_checked = set(), 0, 0
    for table in annotation.tables:
        block_rows = {r: {c.cell: c for c in cs} for (b, r), cs in rows.items() if b == table.block}
        header_cells = [c for c in source.cells if c.block == table.block and c.row < table.firstRow]
        if not table.isItems:
            if any(normalized(c.text) in HEADERS['name'] for cs in block_rows.values() for c in cs.values()):
                reasons.add('E_COVERAGE')
            continue
        roles = [('name', table.nameColumn), ('quantity', table.quantityColumn), ('unit', table.unitColumn)]
        for role, col in roles:
            if col and not any(c.cell == col and normalized(c.text) in HEADERS[role] for c in header_cells):
                reasons.add('E_COLUMNS')
        for component in table.components:
            label_cell = by_id.get(component.labelCell)
            if not label_cell or label_cell.block != table.block or label_cell.row >= table.firstRow or label_cell.cell not in (
                    component.priceColumn, component.totalColumn):
                reasons.add('E_COLUMNS')
            for role, col in [('unitPrice', component.priceColumn), ('lineTotal', component.totalColumn)]:
                if col and not any(c.cell == col and normalized(c.text) in HEADERS[role] for c in header_cells):
                    reasons.add('E_COLUMNS')
        for row, cells in block_rows.items():
            if not table.firstRow <= row <= table.lastRow:
                continue
            covered.add((table.block, row))
            name = cells.get(table.nameColumn)
            if not name or not name.text or numeric_quote(name.text) is not None or re.match(
                    r'(?i)^(итого|всего|ндс|скидка|total|наименование)\b', name.text):
                reasons.add('E_ROWS')
                continue
            item_rows += 1
            quantity = numeric_quote(cells[table.quantityColumn].text) if table.quantityColumn in cells else None
            if table.quantityColumn and (quantity is None or quantity <= 0):
                reasons.add('E_ROWS')
            for component in table.components:
                price = numeric_quote(cells[component.priceColumn].text) if component.priceColumn in cells else None
                total = numeric_quote(cells[component.totalColumn].text) if component.totalColumn in cells else None
                if (component.priceColumn and price is None) or (component.totalColumn and total is None):
                    reasons.add('E_ROWS')
                if quantity is not None and price is not None and total is not None:
                    arithmetic_checked += 1
                    # Only compare like-for-like raw values. Never invent tax or
                    # discount rates to make an inconsistent row pass.
                    tolerance = max(.02, abs(quantity) * .005 + .01)
                    if abs(quantity * price - total) > tolerance:
                        reasons.add('E_ARITHMETIC')
    for key, cells in rows.items():
        if key in covered or len(cells) < 3:
            continue
        if sum(numeric_quote(c.text) is not None for c in cells) >= 2 and any(
                c.text and numeric_quote(c.text) is None for c in cells):
            reasons.add('E_COVERAGE')
    if not item_rows:
        reasons.add('E_NO_ITEMS')
    from backend.universal import Plan, Table, Quote, apply_plan
    from backend.rules import validate
    try:
        plan = Plan(fields=[Quote(field=f.field, cell=f.cell, value=f.value) for f in annotation.fields if f.state == 'found'],
                    tables=[Table(**t.model_dump(exclude={'isItems', 'reviewed'})) for t in annotation.tables if t.isItems], issues=[])
        proposal, proof, warnings, _, _ = apply_plan(source, plan)
        validate(proposal, proof, warnings)
        if any('не сходится' in w or 'отличается от суммы' in w for w in warnings):
            reasons.add('E_ARITHMETIC')
    except ValueError:
        reasons.add('E_SCHEMA')
    draft = payload.get('annotation', {})
    for suggested in draft.get('fields', []):
        if not suggested.get('value'):
            continue
        predicted = next((f for f in annotation.fields if f.field == suggested['field']), None)
        if not predicted or predicted.state != 'found' or predicted.value != suggested['value']:
            reasons.add('E_FIELD_CONFLICT')
    grounded = sum(f.state == 'found' and f.cell in by_id and f.value in by_id[f.cell].text for f in annotation.fields)
    return {'passed': not reasons, 'reasons': sorted(reasons), 'item_rows': item_rows,
            'arithmetic_checked': arithmetic_checked, 'grounded_fields': grounded,
            'human_reviewed': False}


def signatures(payload):
    cells = payload.get('cells', [])
    text = ' '.join(normalized(c['text']) for c in cells if c['text'])
    tokens = re.sub(r'\d+', '#', text).split()
    shingles = {hashlib.sha256(' '.join(tokens[i:i + 5]).encode()).hexdigest()[:16]
                for i in range(max(0, len(tokens) - 4))}
    supplier = next((f['value'] for f in payload.get('annotation', {}).get('fields', [])
                     if f['field'] == 'supplier' and f.get('value')), '')
    return {'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'shingles': sorted(shingles),
            'supplier': hashlib.sha256(normalized(supplier).encode()).hexdigest() if supplier else ''}


def grouped_partition(records, target=50):
    """Union duplicates, supplier families and near templates BEFORE any labels."""
    keys = sorted(records)
    parent = {k: k for k in keys}
    shingle_sets = {k: set(records[k]['signature']['shingles']) for k in keys}
    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for i, left in enumerate(keys):
        a = records[left]['signature']
        sa = shingle_sets[left]
        for right in keys[i + 1:]:
            b = records[right]['signature']
            sb = shingle_sets[right]
            same = a['text_hash'] == b['text_hash'] or bool(a['supplier'] and a['supplier'] == b['supplier'])
            similar = not same and bool(sa and sb and min(len(sa), len(sb)) / max(len(sa), len(sb)) >= .72
                                       and len(sa & sb) / len(sa | sb) >= .72)
            if same or similar:
                parent[root(right)] = root(left)
    groups = defaultdict(list)
    for k in keys:
        groups[root(k)].append(k)
    # Deterministic best-fit groups; large families stay intact. Do not cut a
    # family to hit exactly 50. Preserve a training set even for tiny corpora.
    remaining = sorted(groups, key=lambda k: hashlib.sha256(k.encode()).hexdigest())
    assignment = {}
    for split in ('test', 'val'):
        count = 0
        while len(remaining) > 1 and count < target:
            group = min(remaining, key=lambda k: abs(target - count - len(groups[k])))
            if count and abs(target - count) <= abs(target - count - len(groups[group])):
                break
            remaining.remove(group)
            for k in groups[group]:
                assignment[k] = {'group': group, 'split': split}
            count += len(groups[group])
    for group in remaining:
        for k in groups[group]:
            assignment[k] = {'group': group, 'split': 'train'}
    return assignment
