"""HTTP smoke + sampled cgroup/RSS memory for the entire application stack.

Run on the Docker host; records container cgroup usage (includes OCR children,
model mmap pages and cache) and process RSS separately. No document leaves localhost.
"""
import argparse
import json
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, check=True, encoding='utf-8')
    return result.stdout


MONITOR = '''import json,time,psutil,os
from pathlib import Path
print(os.getpid(),flush=True)
while True:
    rss=0
    for p in psutil.process_iter():
        try: rss+=p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    print(json.dumps({'time':time.time(),'cgroup':int(Path('/sys/fs/cgroup/memory.current').read_text()),'rss':rss}),flush=True)
    time.sleep(.05)
'''
SHELL_MONITOR = 'echo $$; set -e; while true; do date +%s.%N; cat /sys/fs/cgroup/memory.current; sleep 0.05; done'


def monitor(container, samples, stop, backend=False):
    command = ['docker', 'exec', container] + (['python', '-u', '-c', MONITOR] if backend else ['sh', '-c', SHELL_MONITOR])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    monitor_pid = int(process.stdout.readline())
    try:
        while not stop.is_set():
            line = process.stdout.readline()
            if not line:
                break
            if backend:
                samples.append(json.loads(line))
            else:
                value = process.stdout.readline()
                if value:
                    samples.append({'time': float(line), 'cgroup': int(value)})
    finally:
        # Killing docker.exe alone can leave the exec process running in a container.
        subprocess.run(['docker', 'exec', container, 'sh', '-c', f'kill -TERM {monitor_pid}'], capture_output=True, check=True)
        process.terminate()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    fixture_dir = Path('test-results/fixtures')
    containers = {service: docker('compose', 'ps', '-q', service).strip() for service in ('backend', 'model', 'frontend')}
    assert all(containers.values()), 'All three services must be running'
    samples = {key: [] for key in containers}
    stop = threading.Event()
    threads = [threading.Thread(target=monitor, args=(name, samples[key], stop, key == 'backend'), daemon=True) for key, name in containers.items()]
    for thread in threads:
        thread.start()
    reports = []
    try:
        with httpx.Client(base_url=args.url, timeout=200, trust_env=False) as client:
            health = client.get('/api/health').json()
            assert health['provider'] == 'local' and health['configured'], health
            for name in ('text.pdf', 'scan.pdf', 'offer.xlsx', 'ambiguous.txt'):
                if name == 'ambiguous.txt':
                    content = 'Предложение для ТОО Альфа.\nОплатить в течение 15 дней после получения товара.'.encode()
                else:
                    content = (fixture_dir / name).read_bytes()
                for _ in range(50):
                    if all(samples.values()):
                        break
                    time.sleep(.1)
                before = {key: max(0, len(values) - 1) for key, values in samples.items()}
                started = time.time()
                response = client.post('/api/extract', files={'file': (name, content)})
                ended = time.time()
                time.sleep(.2)  # Include bounding samples for sub-second files.
                assert response.status_code == 200, response.text
                result = response.json()
                if name in ('text.pdf', 'offer.xlsx'):
                    assert result['proposal']['items'][0]['unitPrice'] == 1234.56
                if name != 'ambiguous.txt':
                    assert result['proposal']['client'] == 'ТОО Альфа', result['proposal']
                peaks = {key: max((s['cgroup'] for s in values[before[key]:]), default=0) for key, values in samples.items()}
                # Sum of per-container peaks is a conservative upper bound on sampled simultaneous use.
                reports.append({'file': name, 'seconds': round(ended - started, 2),
                    'containerPeakBytes': peaks, 'sumPeakBytes': sum(peaks.values()),
                    'backendRssPeakBytes': max((s.get('rss', 0) for s in samples['backend'] if started <= s['time'] <= ended), default=0),
                    'modelUsed': result['metadata']['modelUsed'], 'warnings': result['metadata']['warnings'],
                    'items': len(result['proposal']['items']), 'client': result['proposal']['client']})
                cumulative = {key: int(docker('exec', container, 'cat', '/sys/fs/cgroup/memory.peak').strip()) for key, container in containers.items()}
                reports[-1]['cumulativeKernelPeakBytes'] = cumulative
                reports[-1]['sumCumulativeKernelPeakBytes'] = sum(cumulative.values())
                Path(f'test-results/{name}.result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
                assert all(peaks.values()), 'Measurement missing for one of the containers'
                assert sum(peaks.values()) < 3_000_000_000, 'Exceeded 3 GB'
                assert sum(cumulative.values()) < 3_000_000_000, 'Kernel recorded a peak above 3 GB'
            # Persistence roundtrip + same-key retry, using synthetic content only.
            result = json.loads(Path('test-results/text.pdf.result.json').read_text(encoding='utf-8'))
            payload = {'proposal': result['proposal'], 'sources': [result['metadata']]}
            key = str(uuid4())
            saved = client.post('/api/proposals', json=payload, headers={'Idempotency-Key': key})
            assert saved.status_code == 200, saved.text
            retry = client.post('/api/proposals', json=payload, headers={'Idempotency-Key': key}).json()
            assert retry['duplicate'] and retry['id'] == saved.json()['id']
            fetched = client.get('/api/proposals/' + retry['id']).json()
            assert fetched['payload']['proposal'] == payload['proposal']
            print('HTTP extraction + save + read + idempotency: PASS', flush=True)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=3)
        Path('test-results/memory.json').write_text(json.dumps({'sampleIntervalSeconds': .05,
            'method': 'sum of sampled per-container cgroup memory.current peaks; includes model mmap, OCR children, nginx; excludes Docker VM and user browser',
            'cases': reports}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
