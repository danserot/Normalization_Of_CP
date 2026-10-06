"""Fast source-grounded labels for unambiguous tables; uncertain cases stay drafts."""
import collections
import json
import re
import sys
from pathlib import Path

from backend.annotation_data import Annotation, MarkedField, MarkedTable, source_from_payload
from backend.rules import FIELDS, HEADERS, grouped_rows, normalized
from backend.universal import Component, catalog, metadata_cells, numeric_quote
from .inference import explicit_fields
from .pipeline import ROOT, load
from .privacy import atomic_json, emit, require_isolation, silent_libraries
from .quality import check_annotation
from .exclusions import excluded_ids


def rule_annotation(payload, group):
    source = source_from_payload(payload, 'document')
    rows = grouped_rows(source)
    tables = []
    for block in catalog(source):
        block_rows = {r: cs for (b, r), cs in rows.items() if b == block['block']}
        if max(c.cell for cs in block_rows.values() for c in cs) < 2:
            continue
        header_matches = []
        for number, cells in block_rows.items():
            roles = {role: [c for c in cells if normalized(c.text) in names] for role, names in HEADERS.items()}
            if len(roles['name']) == 1 and (len(roles['unitPrice']) == 1 or len(roles['lineTotal']) == 1):
                header_matches.append((number, roles))
        table = {'block': block['block'], 'reviewed': False, 'isItems': False,
                 'firstRow': block['firstRow'], 'lastRow': block['lastRow'], 'nameColumn': 0,
                 'quantityColumn': 0, 'unitColumn': 0, 'components': [], 'extras': []}
        if len(header_matches) == 1:
            header_row, roles = header_matches[0]
            if any(len(cells) > 1 for cells in roles.values()):
                tables.append(MarkedTable(**table))
                continue
            col = lambda role: roles[role][0].cell if roles[role] else 0
            candidates = []
            for number, cells in block_rows.items():
                if number <= header_row:
                    continue
                by_col = {c.cell: c for c in cells}
                name = by_col.get(col('name'))
                if not name or not name.text or numeric_quote(name.text) is not None or re.match(
                        r'(?i)^(итого|всего|ндс|скидка|total|подпись)\b', name.text):
                    continue
                numeric_cols = [col(role) for role in ('quantity', 'unitPrice', 'lineTotal') if col(role)]
                if numeric_cols and all(c in by_col and numeric_quote(by_col[c].text) is not None for c in numeric_cols):
                    candidates.append(number)
            if candidates:
                label = (roles['unitPrice'] or roles['lineTotal'])[0]
                used = {col(role) for role in roles if col(role)}
                headers = block_rows[header_row]
                extras = [{'label': c.text, 'labelCell': c.id, 'column': c.cell}
                          for c in headers if c.cell not in used and c.text]
                table.update(isItems=True, firstRow=min(candidates), lastRow=max(candidates),
                    nameColumn=col('name'), quantityColumn=col('quantity'), unitColumn=col('unit'),
                    components=[Component(label=label.text, labelCell=label.id,
                                priceColumn=col('unitPrice'), totalColumn=col('lineTotal')).model_dump()], extras=extras)
        tables.append(MarkedTable(**table))
    metadata = metadata_cells(source, [t for t in tables if t.isItems])
    by_id = {c.id: c for c in metadata}
    quotes = explicit_fields(metadata)
    fields = []
    for key in FIELDS:
        quote = quotes.get(key)
        if quote:
            fields.append(MarkedField(field=key, state='found', cell=quote.cell, value=quote.value))
            continue
        # Existing parser suggestions are allowed only when the quote is exact
        # and the SAME source cell explicitly identifies the role. No guesses
        # from filenames, an unlabeled company name or a manager's signature.
        suggestion = next((f for f in payload['annotation']['fields'] if f['field'] == key), {})
        cell = by_id.get(suggestion.get('cell'))
        value = suggestion.get('value', '')
        label = cell.text.split(':', 1)[0] if cell else ''
        if cell and ':' in cell.text and value and value in cell.text and normalized(label) in (
                normalized(alias) for alias in FIELDS[key]):
            fields.append(MarkedField(field=key, state='found', cell=cell.id, value=value))
        else:
            fields.append(MarkedField(field=key, state='missing'))
    return Annotation(fields=fields, tables=tables, group=group)


def main():
    require_isolation()
    assignment = load(ROOT / 'partition.json')['documents']
    excluded = excluded_ids()
    assignment = {k: v for k, v in assignment.items() if k not in excluded}
    counts = collections.Counter()
    reasons = collections.Counter()
    accepted_by_split = collections.Counter()
    for identifier, group in assignment.items():
        destination = ROOT / 'labels' / (identifier + '.json')
        old = load(destination, {})
        if old.get('status') == 'auto_validated':
            counts['auto_validated'] += 1
            accepted_by_split[group['split']] += 1
            continue
        payload = load(ROOT / 'parsed' / (identifier + '.json'))
        with silent_libraries():
            annotation = rule_annotation(payload, group['group'])
            quality = check_annotation(payload, annotation)
        status = 'auto_validated' if quality['passed'] else 'needs_review'
        if old:
            atomic_json(ROOT / 'label_history' / identifier / 'before_rules.json', old)
        record = {'status': status, 'human_reviewed': False, 'method': 'explicit_source_rules',
                  'attempts': old.get('attempts', 0), 'annotation': annotation.model_dump(), 'quality': quality,
                  'inference_code_sha256': old.get('inference_code_sha256', ''), 'seconds': 0}
        # Keep a model's draft in the review queue when conservative rules cannot
        # certify this document; preserve all previous outputs in history.
        if not quality['passed'] and old.get('annotation'):
            record = old
            status = old['status']
        atomic_json(destination, record)
        counts[status] += 1
        if status == 'auto_validated':
            accepted_by_split[group['split']] += 1
        else:
            reasons.update(record['quality']['reasons'])
    report = {'statuses': dict(counts), 'accepted_by_split': dict(accepted_by_split), 'reason_codes': dict(reasons),
              'human_reviewed': 0}
    atomic_json(ROOT / 'reports/fast_labels.json', report)
    atomic_json(ROOT / 'reports/labeling.json', dict(counts))
    emit('fast_labels_complete', **report)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        emit('failed', code='E_FAST_LABELS')
        sys.exit(1)
