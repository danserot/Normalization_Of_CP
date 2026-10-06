"""Local-only compatible server, used when the app runs the verified LoRA directly."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.universal import Layout, Plan, Requisites, inference_schema
from .inference import LocalGenerator
from .privacy import emit, require_isolation, silent_libraries


class LlamaGenerator:
    def __init__(self):
        import httpx
        with httpx.Client(trust_env=False, timeout=1) as client:
            for _ in range(90):
                try:
                    if client.get('http://127.0.0.1:8081/health').status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
        raise RuntimeError('E_SERVER_NOT_READY')

    def __call__(self, system, payload, schema, max_tokens=700):
        import httpx
        with httpx.Client(trust_env=False, timeout=160) as client:
            r = client.post('http://127.0.0.1:8081/v1/chat/completions', json={
                'model': 'local', 'messages': [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}],
                'temperature': 0, 'max_tokens': max_tokens, 'chat_template_kwargs': {'enable_thinking': False},
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'task', 'strict': True,
                    'schema': inference_schema(schema, payload)}}})
            r.raise_for_status()
            answer = r.json()['choices'][0]
            if answer.get('finish_reason') == 'length':
                raise RuntimeError('E_SCHEMA')
            return schema.model_validate_json(answer['message']['content'])

    def memory_mb(self):
        import psutil
        return round(sum(p.memory_info().rss for p in psutil.process_iter() if p.name().startswith('llama')) / 1024**2)


def main():
    require_isolation()
    with silent_libraries():
        generator = LocalGenerator('/models/student', '/private/training/adapter')
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {'status': 'ok'} if self.path == '/health' else {'data': [{'id': 'local'}]})

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 200000:
                    return self.reply(413, {'error': 'E_SIZE'})
                request = json.loads(self.rfile.read(size))
                schema = request['response_format']['json_schema']['schema']
                properties = schema.get('properties', {})
                contract = Layout if 'isItems' in properties else Plan if 'tables' in properties else Requisites
                messages = request['messages']
                with lock, silent_libraries():
                    result = generator(messages[0]['content'], json.loads(messages[1]['content']), contract,
                                       min(1800, int(request.get('max_tokens', 700))))
                self.reply(200, {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': result.model_dump_json()}}]})
            except Exception:
                self.reply(422, {'error': 'E_INFERENCE'})

    emit('local_server_ready')
    ThreadingHTTPServer(('127.0.0.1', 8081), Handler).serve_forever()


if __name__ == '__main__':
    main()
