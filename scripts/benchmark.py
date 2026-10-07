"""Benchmark supported proposals through OpenAI; documents leave this device.

This live benchmark uses API quota and reports actual completeness separately
from request latency. It does not measure OCR accuracy without reviewed answers.
"""
import argparse, csv, json, os, statistics, sys, time
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path, default=Path('benchmark_results.json'))
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    if not os.getenv('OPENAI_API_KEY', '').strip():
        raise SystemExit('OpenAI API не настроен. Укажите OPENAI_API_KEY в .env или окружении; используйте --env-file PATH.')
    if not args.folder.is_dir():
        raise SystemExit('Каталог с документами не найден: ' + str(args.folder))
    from backend.extraction import FORMATS
    from backend.pipeline import process_document
    print('LIVE OPENAI BENCHMARK: documents are sent to the API; usage is billed to the configured project.')
    rows = []
    for path in sorted(p for p in args.folder.rglob('*') if p.is_file() and p.suffix.lower() in FORMATS):
        started = time.perf_counter()
        try:
            result = process_document(path.read_bytes(), path.name)
            metadata = result['metadata']
            timing = metadata['timingsMs']
            rows.append({'filename': str(path), 'format': path.suffix.lower(),
                'totalMs': timing['total'], 'parseMs': timing['parsing'],
                'semanticMs': timing['semantic'], 'llmMs': timing['llm'],
                'renderMs': timing.get('render', 0), 'visionMs': timing.get('vision', 0),
                'fusionMs': timing.get('fusion', 0), 'llmCalls': metadata['llmCalls'],
                'outcome': metadata['outcome']['state'],
                'visionStatus': metadata.get('routing', {}).get('status', 'native'),
                'coverageComplete': metadata['verification']['coverageComplete'],
                'reviewCompleted': metadata['verification']['reviewCompleted'],
                'items': len(result['proposal']['items']),
                'warnings': len(metadata['warnings']), 'success': True, 'error': ''})
        except Exception as error:
            rows.append({'filename': str(path), 'format': path.suffix.lower(),
                'totalMs': round((time.perf_counter() - started) * 1000), 'parseMs': None,
                'semanticMs': None, 'llmMs': None, 'llmCalls': 0, 'warnings': 0,
                'renderMs': None, 'visionMs': None, 'fusionMs': None,
                'outcome': 'error', 'visionStatus': None, 'coverageComplete': False,
                'reviewCompleted': False, 'items': 0,
                'success': False, 'error': f'{type(error).__name__}: {error}'})
    durations = sorted(row['totalMs'] for row in rows if row['success'])
    report = {'summary': {'documents': len(rows), 'success': sum(r['success'] for r in rows),
        'failure': sum(not r['success'] for r in rows),
        'complete': sum(r['outcome'] == 'complete' for r in rows),
        'partial': sum(r['outcome'] == 'partial' for r in rows),
        'coverageVerified': sum(r['coverageComplete'] for r in rows),
        'latencyP50Ms': round(statistics.median(durations), 2) if durations else None,
        'latencyP95Ms': durations[max(0, int(len(durations) * .95) - 1)] if durations else None}, 'documents': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with args.output.with_suffix('.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys() if rows else ['filename'])
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps(report['summary'], ensure_ascii=False))

if __name__ == '__main__': main()
