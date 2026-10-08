"""Unauthenticated network checks. Never loads or sends API keys/documents."""
import json
import os
import socket
import ssl
from urllib.parse import urlsplit

import httpx


def network_failure(error):
    chain, current = [], error
    while current is not None and len(chain) < 8:
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    detail = str(error).upper()
    reason = ('tls_eof' if 'UNEXPECTED_EOF' in detail else
              'certificate' if 'CERTIFICATE_VERIFY_FAILED' in detail else
              'dns' if 'NAME OR SERVICE' in detail or 'GETADDRINFO' in detail else
              'timeout' if isinstance(error, httpx.TimeoutException) else 'connection')
    return {'classes': chain, 'reason': reason}


def main():
    endpoint = os.getenv('OPENAI_API_URL', 'https://api.openai.com/v1/responses')
    target = urlsplit(endpoint)
    configured_proxy = os.getenv('OPENAI_PROXY_URL', '').strip() or None
    print(json.dumps({'api_host': target.hostname, 'api_path': target.path,
        'configured_proxy': {'host': urlsplit(configured_proxy).hostname,
            'port': urlsplit(configured_proxy).port} if configured_proxy else None,
        'proxy_environment': {name: {'host': urlsplit(value).hostname, 'port': urlsplit(value).port}
            for name, value in os.environ.items() if name.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'}},
        'no_proxy_present': bool(os.getenv('NO_PROXY'))}))
    try:
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)})
        print(json.dumps({'dns_addresses': addresses}))
    except OSError as error:
        print(json.dumps({'dns_error': type(error).__name__}))
    routes = [('direct', False, None), ('environment', True, None)]
    if configured_proxy:
        if urlsplit(configured_proxy).scheme not in {'http', 'https'}:
            print(json.dumps({'configured_proxy_error': 'HTTP or HTTPS proxy required'}))
        else:
            routes.append(('configured_proxy', False, configured_proxy))
    for route, trust_environment, proxy in routes:
        for url in ('https://' + target.hostname + '/v1/models', 'https://example.com'):
            result = {'url': url, 'route': route, 'trust_env': trust_environment}
            try:
                with httpx.Client(trust_env=trust_environment, proxy=proxy,
                                  timeout=8, follow_redirects=False) as client:
                    response = client.get(url)
                    result.update(http_status=response.status_code, tls_reached=True)
            except httpx.HTTPError as error:
                result.update(network_failure(error))
            print(json.dumps(result))
    # No certificate bypass: compare TLS 1.2 while retaining normal verification.
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        with httpx.Client(trust_env=False, verify=context, timeout=8) as client:
            response = client.get('https://' + target.hostname + '/v1/models')
            print(json.dumps({'tls12_http_status': response.status_code}))
    except httpx.HTTPError as error:
        print(json.dumps({'tls12': network_failure(error)}))


if __name__ == '__main__':
    main()
