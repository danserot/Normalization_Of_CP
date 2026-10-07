"""Run a document through the live OpenAI pipeline; no fixture or model mocks.

Usage: python -m scripts.vision_smoke document.pdf --require-vision
Images and document text are sent to OpenAI and use API quota. Optionally save
source-grounded JSON to a private/ignored output location. --env-file defaults
to .env; configuration checks themselves never make a paid request.
"""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--require-vision', action='store_true')
    parser.add_argument('--require-semantic', action='store_true')
    parser.add_argument('--expected-items', type=int)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    if not os.getenv('OPENAI_API_KEY', '').strip():
        raise SystemExit('OpenAI API не настроен. Укажите OPENAI_API_KEY в .env или окружении; используйте --env-file PATH.')
    if not args.document.is_file():
        raise SystemExit('Документ не найден: ' + str(args.document))
    from backend.pipeline import process_document
    print('LIVE OPENAI SMOKE: document contents are sent to the API; usage is billed to the configured project.')
    result = process_document(args.document.read_bytes(), args.document.name)
    metadata, proposal = result['metadata'], result['proposal']
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.require_vision and not metadata['verification']['visionComplete']:
        raise SystemExit('FAILED: OpenAI Vision did not read all requested pages completely')
    if args.require_semantic and metadata['verification']['mode'] != 'model':
        raise SystemExit('FAILED: OpenAI did not return a valid source-grounded extraction plan')
    if args.expected_items is not None and len(proposal['items']) != args.expected_items:
        raise SystemExit(f'FAILED: expected {args.expected_items} items, got {len(proposal["items"])}')
    cells = {c['id']: c for c in metadata['sourceCells']}
    checked = 0
    def verify(evidence):
        nonlocal checked
        if isinstance(evidence, list):
            for entry in evidence:
                verify(entry)
            return
        if evidence.get('method') == 'calculated':
            for entry in evidence.get('sources', []):
                verify(entry)
            return
        identifier = evidence.get('sourceId', evidence.get('id'))
        if identifier not in cells or evidence.get('excerpt') != cells[identifier]['text']:
            raise SystemExit('FAILED: evidence references unavailable or changed source text')
        checked += 1
    for entry in metadata['fieldEvidence'].values():
        verify(entry)
    print(json.dumps({'provider': metadata.get('provider'), 'outcome': metadata['outcome']['state'],
                      'coverageComplete': metadata['verification']['coverageComplete'],
                      'items': len(proposal['items']), 'evidenceChecked': checked,
                      'vision': metadata['routing']['status'], 'mode': metadata['verification']['mode'],
                      'llmCalls': metadata['llmCalls'], 'timingsMs': metadata['timingsMs'],
                      'warnings': len(metadata['warnings'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
