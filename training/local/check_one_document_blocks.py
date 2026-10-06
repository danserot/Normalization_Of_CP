"""Inspect one private document offline and emit aggregate block counts only."""
import json
import sys
from pathlib import Path

from backend.extraction import FORMATS, read_document
from .privacy import require_isolation, silent_libraries


def main():
    isolation = require_isolation()
    candidates = sorted(
        (path for path in Path('/input').rglob('*')
         if path.is_file() and path.suffix.lower() in FORMATS),
        key=lambda path: (path.suffix.lower() != '.pdf', path.suffix.lower()),
    )
    if not candidates:
        print(json.dumps({'code': 'E_NO_DOCUMENT'}), flush=True)
        return 1
    path = candidates[0]
    with silent_libraries():
        source = read_document(path.read_bytes(), 'private' + path.suffix.lower())
    blocks = {}
    for cell in source.cells:
        blocks.setdefault(cell.block, cell.kind)
    report = {
        'isolated': isolation['network_mode'] == 'none',
        'format': path.suffix.lower(),
        'text_blocks': sum(kind == 'text' for kind in blocks.values()),
        'table_blocks': sum(kind == 'table' for kind in blocks.values()),
        'text_cells': sum(cell.kind == 'text' for cell in source.cells),
        'table_cells': sum(cell.kind == 'table' for cell in source.cells),
        'ocr_pages': len(source.ocr_pages),
    }
    print(json.dumps(report), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'code': 'E_PRIVATE_BLOCK_CHECK'}), flush=True)
        sys.exit(1)
