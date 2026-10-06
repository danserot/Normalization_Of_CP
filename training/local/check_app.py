"""Run inside the backend container. Emit only aggregates; synthetic HTTP input."""
import json
import os
import socket
import sys
import time
from pathlib import Path


def main():
    # The app uses an internal Docker network, unlike training's network=none.
    if not Path('/.dockerenv').exists():
        raise RuntimeError('E_CONTAINER_REQUIRED')
    for host, port in [('1.1.1.1', 443), ('8.8.8.8', 53)]:
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex((host, port)) == 0:
                raise RuntimeError('E_EGRESS')
    import httpx
    with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, timeout=480) as client:
        login = client.post('/api/login', json={'password': os.environ.get('APP_PASSWORD', '')})
        login.raise_for_status()
        health = client.get('/api/health')
        health.raise_for_status()
        queue = client.get('/api/annotations')
        queue.raise_for_status()
        count = len(queue.json()['documents'])
        text = ('Поставщик: ООО Синтетика\nВалюта: RUB\n'
                'Наименование\tКоличество\tЕд. изм.\tЦена\tСумма\n'
                'Тестовый товар\t2\tшт\t100.00\t200.00')
        started = time.monotonic()
        response = client.post('/api/extract', files={'file': ('synthetic-check.txt', text.encode(), 'text/plain')})
        response.raise_for_status()
        result = response.json()
        items = result['proposal']['items']
        values_ok = len(items) == 1 and items[0]['name'] == 'Тестовый товар' and items[0]['quantity'] == 2 and items[0]['unitPrice'] == 100
        fields_ok = result['proposal']['supplier'] == 'ООО Синтетика' and result['proposal']['currency'] == 'RUB'
        report = {'health_ok': health.json()['status'] == 'ok', 'model_available': health.json()['configured'],
                  'queue_documents': count, 'synthetic_http_status': response.status_code,
                  'synthetic_items_correct': values_ok, 'model_used': result['metadata']['modelUsed'],
                  'synthetic_item_count': len(items),
                  'synthetic_fields_correct': fields_ok,
                  'seconds': round(time.monotonic() - started, 2), 'egress_probes_blocked': 2}
        print(json.dumps(report), flush=True)
        if not values_ok or not fields_ok:
            return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'code': 'E_APP_CHECK'}), flush=True)
        sys.exit(1)
