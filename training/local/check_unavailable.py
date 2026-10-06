"""Real local HTTP request containing only this invented fixture."""
import json
import os
import sys
import httpx


def main():
    sample = ('Поставщик: ООО Синтетика\n'
              'Наименование\tКоличество\tЦена\n'
              'Тестовая гайка\t2\tпо запросу')
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=480, trust_env=False) as client:
        client.post('/api/login', json={'password': os.environ.get('APP_PASSWORD', '')}).raise_for_status()
        response = client.post('/api/extract', files={'file': ('synthetic-unknown-price.txt', sample.encode(), 'text/plain')})
        response.raise_for_status()
        data = response.json()
        outcome = data['metadata'].get('outcome', {})
        explicit = outcome.get('state') in ('partial', 'unavailable') and bool(outcome.get('unavailable'))
        no_invented_price = all(item.get('unitPrice') is None for item in data['proposal']['items'])
        report = {'http_status': response.status_code, 'explicit_unavailable_result': explicit,
                  'unknown_price_not_invented': no_invented_price, 'state': outcome.get('state')}
        print(json.dumps(report), flush=True)
        return 0 if explicit and no_invented_price else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'code': 'E_SYNTHETIC_HTTP'}), flush=True)
        sys.exit(1)
