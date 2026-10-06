"""Check text/table block separation through the deployed HTTP API."""
import json
import os
import sys

import httpx


def main():
    sample = (
        'Поставщик: ООО Синтетик\n'
        'Наименование\tКоличество\tЦена\n'
        'Тестовый кабель\t2\t100\n'
        'Гарантия: 12 месяцев'
    )
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=480, trust_env=False) as client:
        client.post('/api/login', json={'password': os.environ.get('APP_PASSWORD', '')}).raise_for_status()
        response = client.post('/api/extract', files={
            'file': ('synthetic-mixed.txt', sample.encode('utf-8'), 'text/plain'),
        })
        response.raise_for_status()
        payload = response.json()
        metadata = payload['metadata']
        cells = metadata['sourceCells']
        kinds = {cell.get('kind') for cell in cells}
        text_blocks = {cell['block'] for cell in cells if cell.get('kind') == 'text'}
        table_blocks = {cell['block'] for cell in cells if cell.get('kind') == 'table'}
        report = {
            'http_status': response.status_code,
            'has_text_blocks': 'text' in kinds,
            'has_table_blocks': 'table' in kinds,
            'blocks_are_separate': text_blocks.isdisjoint(table_blocks),
            'model_only_mode': metadata['verification']['mode'] == 'model',
            'model_used': metadata['modelUsed'] is True,
            'outcome': metadata['outcome']['state'],
            'extracted_fields': sum(
                value not in ('', None, [], {})
                for key, value in payload['proposal'].items()
                if key not in ('items', 'additionalFields', 'notes')
            ),
            'extracted_items': len(payload['proposal']['items']),
        }
        print(json.dumps(report), flush=True)
        checks = ('http_status', 'has_text_blocks', 'has_table_blocks',
                  'blocks_are_separate', 'model_only_mode', 'model_used')
        return 0 if all(report[key] for key in checks) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'code': 'E_SYNTHETIC_BLOCK_HTTP'}), flush=True)
        sys.exit(1)
