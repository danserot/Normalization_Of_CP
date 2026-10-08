"""Pure stored-cell catalog helpers for human review and archived exports."""
import json
from collections import defaultdict
from .numeric_values import numeric_quote


def grouped_rows(source):
    rows = defaultdict(list)
    for cell in source.cells:
        rows[(cell.block, cell.row)].append(cell)
    return rows


def requisite_batches(cells):
    """Shared by production inference and reviewed dataset export."""
    batch, length = [], 0
    for cell in cells:
        if not cell.text:
            continue
        row = [cell.id, cell.text]
        size = len(json.dumps(row, ensure_ascii=False))
        if batch and length + size > 2500:
            yield batch
            batch, length = [], 0
        batch.append(row)
        length += size
    if batch:
        yield batch


def metadata_cells(source, tables):
    """Keep requisites above/below a table on the same spreadsheet/PDF block."""
    ranges = {}
    for table in tables:
        ranges.setdefault(table.block, []).append((table.firstRow, table.lastRow))
    return [c for c in source.cells if not any(
        first <= c.row <= last for first, last in ranges.get(c.block, ()))]


def catalog(source):
    """Compact samples preserve block/column identity and cross-page headers."""
    blocks = {}
    for (block, row), cells in grouped_rows(source).items():
        blocks.setdefault(block, []).append({'row': row, 'cells': [
            [c.id, c.cell, c.text] for c in cells if c.text]})
    result = []
    for block, rows in blocks.items():
        # All small blocks; larger tables retain header and boundary examples.
        text_rows = [row for row in rows if row['cells'] and all(numeric_quote(c[2]) is None for c in row['cells'])]
        headers = [row for index, row in enumerate(rows[:-1]) if len(row['cells']) >= 2
                   and all(numeric_quote(c[2]) is None for c in row['cells'])
                   and sum(numeric_quote(c[2]) is not None for c in rows[index + 1]['cells']) >= 2]
        numeric_rows = [row for row in rows if sum(numeric_quote(c[2]) is not None for c in row['cells']) >= 1]
        sample = rows if len(rows) <= 12 else sorted(
            {row['row']: row for row in rows[:5] + text_rows[:6] + headers[:6]
             + numeric_rows[:2] + numeric_rows[len(numeric_rows) // 2:len(numeric_rows) // 2 + 1]
             + rows[-3:]}.values(), key=lambda row: row['row'])
        result.append({'block': block, 'rowCount': len(rows), 'firstRow': rows[0]['row'],
                       'lastRow': rows[-1]['row'], 'rows': sample})
    return result
