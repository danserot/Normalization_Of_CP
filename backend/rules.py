"""Conservative extraction: missing numbers are null, evidence is cell-specific."""
import math
import re
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from datetime import datetime
from .semantic import FIELD_ALIASES, classify_label, normalize_label

FIELDS = {
    'title': ['название', 'тема', 'title'],
    'client': ['клиент', 'заказчик', 'покупатель', 'client', 'customer'],
    'clientContact': ['контактное лицо', 'контакт', 'контакты', 'телефон', 'email', 'e-mail', 'clientContact'],
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
HEADERS = FIELD_ALIASES


def normalized(value):
    return normalize_label(value)


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
    native = 'native' in cell.method or cell.method in {'openpyxl', 'python-docx'}
    agreement = getattr(cell, 'vision_agreement', None)
    basis = ['exact_source', 'semantic_mapping' if method == 'model' else 'explicit_rule']
    confidence = .90 if native else .76
    if native:
        basis.append('native_text')
    if cell.kind == 'table':
        confidence += .03
        basis.append('table_structure')
    if method != 'model':
        confidence += .02
    if agreement is True:
        confidence += .04
        basis.append('vision_native_agreement')
    elif agreement is False:
        confidence -= .18
        basis.append('vision_native_conflict')
    return {**asdict(cell), 'value': value, 'excerpt': cell.text, 'verifiedInSource': True,
            'sourceId': cell.id, 'method': f'{cell.method}+{method}',
            'confidence': round(min(.99, max(0, confidence)), 3),
            'confidenceKind': 'heuristic', 'confidenceBasis': basis,
            'warning': warning}


def grouped_rows(source):
    rows = defaultdict(list)
    for cell in source.cells:
        rows[(cell.block, cell.row)].append(cell)
    return rows


def alternative_blocks(source, tables=()):
    """Recognize explicitly alternative headers even if a model table is rejected."""
    blocks = {table.block for table in tables if getattr(table, 'componentMode', '') == 'alternative'}
    table_blocks = {cell.block for cell in source.cells if cell.kind == 'table'}
    for block in table_blocks:
        headers = [cell for cells in header_paths(source, block).values() for cell in cells]
        if any(re.search(r'(?i)\b(?:вариант\w*|альтернатив\w*|option\w*|alternative\w*)\b', cell.text)
               for cell in headers):
            blocks.add(block)
    return blocks


def header_paths(source, block, before_row=None):
    """Column header ancestry from real merged anchors, with a bounded fallback.

    The fallback carries only explicit variant headings across their adjacent
    blank cells; other unrelated text never becomes a parent price heading.
    """
    rows = [(row, sorted(cells, key=lambda c: c.cell)) for (name, row), cells in grouped_rows(source).items()
            if name == block]
    name_header = next((row for row, cells in rows if any(
        classify_label(cell.text)[0] == 'name' and parse_number(cell.text) is None for cell in cells)), None)
    def is_numeric(text):
        return parse_number(text) is not None or bool(re.fullmatch(r'[+-]?[\d \u00a0\u202f]+,\d{1,6}', text.strip()))
    first_data = next((row for row, cells in rows if (name_header is None or row > name_header)
                       and any(is_numeric(cell.text) for cell in cells)
                       and any(cell.text and not is_numeric(cell.text) for cell in cells)), None)
    boundary = first_data or before_row or 9
    if before_row and before_row > (name_header or 0):
        boundary = min(boundary, before_row)
    width = max((cell.cell + max(1, getattr(cell, 'colspan', 1)) - 1 for _, cells in rows for cell in cells), default=0)
    paths = defaultdict(list)
    for row, cells in rows:
        if row >= boundary or name_header is not None and row < name_header:
            continue
        by_col = {cell.cell: cell for cell in cells}
        carried = None
        for column in range(1, width + 1):
            cell = by_col.get(column)
            if cell and cell.text and not getattr(cell, 'merged_into', None):
                carried = cell if re.search(r'(?i)\b(?:вариант\w*|option\w*|alternative\w*)\b', cell.text) else None
                for covered in range(column, column + max(1, getattr(cell, 'colspan', 1))):
                    paths[covered].append(cell)
            elif carried and (not cell or not cell.text):
                paths[column].append(carried)
    return {column: list({cell.id: cell for cell in cells}.values()) for column, cells in paths.items()}


def preserve_variant_totals(source, proposal, proof, warnings, blocks):
    """No alternative or component subtotal may become the single document total."""
    if not blocks:
        return
    proposal['documentTotal'] = None
    proof.pop('documentTotal', None)
    for block in blocks:
        totals = []
        for (name, row), cells in grouped_rows(source).items():
            if name != block:
                continue
            label = next((cell for cell in cells if re.match(
                r'(?i)^(?:всего|итого|grand total|total)(?:[\s:,.]|$)', cell.text)), None)
            numeric = [cell for cell in cells if _parse_matrix_number(cell.text) is not None]
            if label and len(numeric) >= 2:
                priority = 3 if re.match(r'(?i)^(?:всего|grand total)', label.text) else 2 if re.search(r'(?i)ндс|vat', label.text) else 1
                totals.append((priority, row, label, numeric))
        if not totals:
            continue
        _, row, label, numeric = max(totals, key=lambda entry: (entry[0], entry[1]))
        paths = header_paths(source, block)
        fields = proposal.setdefault('additionalFields', [])
        for cell in numeric:
            parents = paths.get(cell.cell, [])
            variant = next((parent for parent in parents if re.search(
                r'(?i)\b(?:вариант\w*|option\w*|alternative\w*)\b', parent.text)), None)
            context = variant.text if variant else ' / '.join(parent.text for parent in parents)
            if not context:
                # A missing relationship remains visible in the source table;
                # inventing a variant name would hide the ambiguity.
                continue
            field = {'label': f'Всего — {context}'[:200], 'value': cell.text}
            if field in fields:
                index = fields.index(field)
            else:
                index = len(fields)
                fields.append(field)
            proof[f'additionalFields.{index}.value'] = evidence(cell, cell.text, 'matrix')
            refs = [evidence(label, label.text, 'matrix')]
            refs.extend(evidence(parent, parent.text, 'matrix') for parent in ([variant] if variant else parents))
            proof[f'additionalFields.{index}.label'] = {
                'value': field['label'], 'method': 'calculated', 'verifiedInSource': False,
                'sources': refs, 'file': source.file, 'excerpt': field['label'],
                'confidence': min(ref['confidence'] for ref in refs), 'componentMode': 'alternative',
                'confidenceKind': 'heuristic', 'confidenceBasis': ['variant_header_structure'],
            }
    warnings.append('Альтернативные итоги сохранены отдельно; единый итог документа не выбран')


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
        if isinstance(value, str) and re.search(r'(?i)\[(?:неразборчиво|unreadable|illegible)\]', value):
            return
        if proposal[key] not in ('', None) and proposal[key] != value:
            warnings.append(f'Внутри документа разные значения {key}: требуется проверка')
            existing = proof[key] if isinstance(proof[key], list) else [proof[key]]
            proof[key] = [*existing, evidence(cell, value)]
            return
        proposal[key], proof[key] = value, evidence(cell, value)

    # A company name in the heading is stronger supplier evidence than an
    # arbitrary descriptive paragraph selected by a language model.
    for cell in source.cells[:100]:
        peers = rows.get((cell.block, cell.row), [])
        row_text = ' '.join(peer.text for peer in peers if peer.text)
        role_labels = [*FIELDS['client'], *FIELDS['supplier']]
        is_role_row = any(re.match(r'^' + re.escape(label) + r'(?:\s*[:=]|\s+)', row_text, re.I)
                          for label in role_labels)
        is_labeled_value = any(peer.cell < cell.cell and normalized(peer.text) in {
            normalized(label) for label in FIELDS['client']} for peer in peers)
        if (not proposal['supplier'] and not is_role_row and not is_labeled_value
                and cell.kind != 'table' and len(cell.text) <= 250
                and re.match(r'(?i)^(?:тоо|ооо|ао|ип|llp|ltd)\b\s*\S+', cell.text)):
            set_field('supplier', cell.text, cell)
        if not proposal['title'] and re.match(r'^коммерческое предложение(?:\s|$)', cell.text, re.I):
            set_field('title', cell.text, cell)

    for row_key, cells in rows.items():
        if row_key in used_rows:
            continue
        block, row_no = row_key
        mapping = {}
        for cell in cells:
            key, confidence, method = classify_label(cell.text)
            if key and method in ('alias', 'semantic'):
                mapping[key] = cell.cell
        if 'name' in mapping and any(k in mapping for k in ('quantity', 'unitPrice', 'lineTotal')):
            mappings[block] = mapping
            used_rows.add(row_key)
            continue
        joined = ' '.join(c.text for c in cells if c.text)
        is_total = bool(re.match(r'^(итого|всего|к оплате|общая сумма|subtotal|total)(?:\s|:|$)', joined, re.I))
        if is_total:
            # Requisites below the total are outside the items table. A later
            # table must supply its own header before item parsing resumes.
            mappings.pop(block, None)
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
        nonempty_count = sum(bool(candidate.text) for candidate in cells)
        for index, cell in enumerate(cells):
            for key, labels in FIELDS.items():
                if cell.kind == 'table' and nonempty_count > 2 and key != 'documentTotal':
                    continue
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
    _extract_price_matrix(source, proposal, proof, warnings, used_rows)
    _extract_unknown_fields(source, proposal, proof, used_rows)
    preserve_variant_totals(source, proposal, proof, warnings, alternative_blocks(source))
    return proposal, proof, warnings, used_rows


def _extract_price_matrix(source, proposal, proof, warnings, used_rows):
    """Extract multi-row tables whose numeric columns are alternative cost components."""
    blocks = defaultdict(list)
    for (block, row), cells in grouped_rows(source).items():
        if any(cell.text for cell in cells):
            blocks[block].append((row, sorted(cells, key=lambda cell: cell.cell)))
    for block, rows in blocks.items():
        if any(row_key[0] == block for row_key in used_rows):
            continue
        initial_items = len(proposal['items'])
        header_index = next((i for i, (_, cells) in enumerate(rows)
            if any(classify_label(cell.text)[0] == 'name' for cell in cells)), None)
        if header_index is None:
            continue
        header_row, header_cells = rows[header_index]
        name_cell = next(cell for cell in header_cells if classify_label(cell.text)[0] == 'name')
        data = rows[header_index + 1:]
        first_data = next(((row, cells) for row, cells in data
            if any(c.cell == name_cell.cell and c.text and not re.match(r'^(итого|ндс|всего)', c.text, re.I) for c in cells)
            and sum(parse_number(c.text) is not None for c in cells) >= 2), None)
        if not first_data:
            continue
        header_layers = [(row, cells) for row, cells in rows[header_index:] if row < first_data[0]]
        numeric_columns = [cell.cell for cell in first_data[1] if parse_number(cell.text) is not None and cell.cell != 1]
        if len(numeric_columns) < 2:
            continue
        labels = {}
        carried = {}
        label_cells = {}
        for _, cells in header_layers:
            by_col = {cell.cell: cell for cell in cells}
            last, last_cell = '', None
            for col in range(1, max(numeric_columns) + 1):
                text = by_col.get(col).text.strip() if by_col.get(col) else ''
                if text:
                    last = text
                    last_cell = by_col[col]
                elif last:
                    text = last
                if col in numeric_columns and text and classify_label(text)[0] != 'name':
                    carried.setdefault(col, []).append(text)
                    if last_cell is not None:
                        label_cells.setdefault(col, []).append(last_cell)
        for col in numeric_columns:
            parts = []
            for part in carried.get(col, []):
                if normalized(part) not in {normalized(value) for value in parts}:
                    parts.append(part)
            labels[col] = ' / '.join(parts) or f'Стоимость, колонка {col}'
        if not re.search(r'(?i)(?:стоимост|цена|тариф|price|cost|amount|сумма|смр|тмц)', ' '.join(labels.values())):
            warnings.append(f'Таблица {block}: числовые колонки без подтверждённых заголовков стоимости; требуется semantic mapping')
            continue
        for row, cells in rows:
            if row < first_data[0]:
                continue
            by_col = {cell.cell: cell for cell in cells}
            name = by_col.get(name_cell.cell)
            if not name or not name.text or re.match(r'^(итого|ндс|всего)', name.text, re.I):
                continue
            components = []
            index = len(proposal['items'])
            for col in numeric_columns:
                cell = by_col.get(col)
                value = parse_number(cell.text) if cell else None
                if value is None or value < 0:
                    continue
                component_index = len(components)
                components.append({'label': labels[col], 'unitPrice': None, 'lineTotal': value})
                proof[f'items.{index}.components.{component_index}.lineTotal'] = evidence(cell, value, 'matrix')
                label_sources = [evidence(header, header.text, 'matrix') for header in label_cells.get(col, [])]
                proof[f'items.{index}.components.{component_index}.label'] = {
                    'value': labels[col], 'method': 'calculated', 'verifiedInSource': False,
                    'sources': label_sources, 'formula': ' / '.join(ref['sourceId'] for ref in label_sources),
                    'confidence': min((ref['confidence'] for ref in label_sources), default=0),
                    'confidenceKind': 'heuristic', 'confidenceBasis': ['merged_header_structure'],
                    'componentMode': 'alternative',
                    'excerpt': labels[col], 'file': source.file,
                }
            if not components:
                continue
            proposal['items'].append({'name': name.text, 'quantity': None, 'unit': '',
                'unitPrice': None, 'lineTotal': None, 'components': components,
                'componentMode': 'alternative', 'additionalFields': []})
            proof[f'items.{index}.name'] = evidence(name, name.text, 'matrix')
            used_rows.add((block, row))
        if len(proposal['items']) > initial_items:
            proposal.setdefault('additionalFields', [])
            total_row = next(((row, cells) for row, cells in rows
                if any(c.cell == name_cell.cell and re.match(r'^всего(?:\s|$)', c.text, re.I) for c in cells)), None)
            if total_row:
                proposal['documentTotal'] = None
                proof.pop('documentTotal', None)
                by_col = {cell.cell: cell for cell in total_row[1]}
                for col in numeric_columns:
                    cell = by_col.get(col)
                    value = _parse_matrix_number(cell.text) if cell else None
                    if value is None:
                        continue
                    extra_index = len(proposal['additionalFields'])
                    variant = labels[col].split('/')[0].strip()
                    proposal['additionalFields'].append({'label': f'Всего — {variant}', 'value': cell.text})
                    proof[f'additionalFields.{extra_index}.value'] = evidence(cell, cell.text, 'matrix')
            warnings.append('Таблица содержит альтернативные варианты стоимости; компоненты сохранены раздельно и не суммированы между вариантами')


def _extract_unknown_fields(source, proposal, proof, used_rows):
    """Preserve explicit unfamiliar metadata without asking a model to invent labels."""
    known = {normalized(label) for labels in FIELDS.values() for label in labels}
    known.update(normalized(label) for labels in HEADERS.values() for label in labels)
    for row_key, cells in grouped_rows(source).items():
        if row_key in used_rows:
            continue
        populated = [cell for cell in cells if cell.text]
        if len(populated) > 2:
            continue
        label, value, label_cell, value_cell = '', '', None, None
        if len(populated) == 1:
            cell = populated[0]
            match = re.fullmatch(r'([^:\n]{2,100}):\s*(\S[^\n]*)', cell.text)
            if match:
                label, value = (part.strip() for part in match.groups())
                label_cell = value_cell = cell
        elif len(populated) == 2 and re.search(r'(?i)(?:срок|услови|производств|упаков|адрес|реквизит|delivery|production)', populated[0].text):
            label, value = populated[0].text.rstrip(':').strip(), populated[1].text
            label_cell, value_cell = populated
        if (not label or normalized(label) in known or len(value) > 2000 or parse_number(label) is not None
                or re.match(r'^\d', label) or label.count(',') >= 2
                or ',' in label and len(label.split()) >= 4):
            continue
        fields = proposal.setdefault('additionalFields', [])
        candidate = {'label': label, 'value': value}
        if candidate in fields or len(fields) >= 100:
            continue
        index = len(fields)
        fields.append(candidate)
        proof[f'additionalFields.{index}.label'] = evidence(label_cell, label)
        proof[f'additionalFields.{index}.value'] = evidence(value_cell, value)


def _parse_matrix_number(value):
    parsed = parse_number(value)
    if parsed is not None:
        return parsed
    raw = str(value or '').strip()
    if re.fullmatch(r'\d[\d\s\u00a0\u202f]*[,.]\d{2}', raw):
        try:
            return float(Decimal(re.sub(r'[\s\u00a0\u202f]', '', raw).replace(',', '.')))
        except (InvalidOperation, ValueError):
            return None
    return None


def validate(proposal, proof, warnings):
    def annotate(key, message=None, passed=False):
        entries = proof.get(key, [])
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                continue
            basis = entry.setdefault('confidenceBasis', [])
            flag = 'arithmetic_validated' if passed else 'arithmetic_conflict'
            if flag not in basis:
                basis.append(flag)
                if not passed:
                    entry['confidence'] = round(entry.get('confidence', 0) * .8, 3)
            if message:
                entry['warning'] = message
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
        for field in ('quantity', 'unitPrice', 'lineTotal'):
            value = item.get(field)
            if value is not None and (not math.isfinite(value) or value < 0):
                message = f'Позиция {index + 1}: недопустимое значение {field}'
                warnings.append(message)
                annotate(f'items.{index}.{field}', message)
        for component_index, component in enumerate(item.get('components', [])):
            quantity, price, amount = item['quantity'], component['unitPrice'], component['lineTotal']
            for field in ('unitPrice', 'lineTotal'):
                value = component.get(field)
                if value is not None and (not math.isfinite(value) or value < 0):
                    message = f'Позиция {index + 1}, {component["label"]}: недопустимое значение {field}'
                    warnings.append(message)
                    annotate(f'items.{index}.components.{component_index}.{field}', message)
            if quantity is not None and price is not None and amount is not None:
                key = f'items.{index}.components.{component_index}.lineTotal'
                tolerance = .02 + abs(amount) * 1e-8
                if abs(quantity * price - amount) > tolerance:
                    message = f'Позиция {index + 1}, {component["label"]}: количество × цена отличается от суммы компонента'
                    warnings.append(message)
                    annotate(key, message)
                else:
                    annotate(key, passed=True)
        component_totals = item.get('components') and all(
            component.get('lineTotal') is not None for component in item['components'])
        is_alternative = any(proof.get(f'items.{index}.components.{ci}.label', {}).get('componentMode') == 'alternative'
                             for ci in range(len(item.get('components', []))))
        if item.get('components') and not is_alternative:
            for field in ('unitPrice', 'lineTotal'):
                aggregate = item.get(field)
                values = [component.get(field) for component in item['components']]
                if aggregate is not None and all(value is not None for value in values):
                    if abs(sum(values) - aggregate) > .02 + len(values) * .005:
                        message = f'Позиция {index + 1}: сумма компонентов отличается от явно указанного {field}'
                        warnings.append(message)
                        annotate(f'items.{index}.{field}', message)
        if not component_totals and (item['quantity'] is None or item['unitPrice'] is None):
            warnings.append(f'Позиция {index + 1}: количество или цена отсутствует; заполните вручную')
        if item.get('lineTotal') is not None and item.get('quantity') is not None and item.get('unitPrice') is not None:
            actual = item['quantity'] * item['unitPrice']
            if abs(actual - item['lineTotal']) > .02:
                message = f'Позиция {index + 1}: количество × цена отличается от суммы строки; проверьте НДС/скидку'
                warnings.append(message)
                annotate(f'items.{index}.lineTotal', message)
            else:
                annotate(f'items.{index}.lineTotal', passed=True)
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
            fixed_discount = parse_number(proposal['discount'])
            if discount:
                possibilities.append(raw * (1 - float(discount[1].replace(',', '.')) / 100))
            elif fixed_discount is not None and fixed_discount >= 0:
                possibilities.append(raw - fixed_discount)
            if vat and not re.search(r'включ|в том числе|без ндс', proposal['vat'], re.I):
                possibilities += [v * (1 + float(vat[1].replace(',', '.')) / 100) for v in list(possibilities)]
            possibilities += [v + delivery for v in list(possibilities)]
            tolerance = .02 + len(sums) * .005
            if all(abs(total - v) > tolerance for v in possibilities):
                message = 'Итог документа не сходится с позициями и явно указанными НДС/скидкой/доставкой'
                warnings.append(message)
                annotate('documentTotal', message)
            else:
                annotate('documentTotal', passed=True)
    if not proposal['items']:
        warnings.append('Позиции не распознаны уверенно; проверьте структуру таблицы')
    if not proof:
        warnings.append('Структура не распознана: извлечён только текст, поля нужно заполнить вручную')
