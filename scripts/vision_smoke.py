"""Run a real PDF through the complete pipeline; no fixture or model mocks.

Usage: python -m scripts.vision_smoke document.pdf --require-vision
Optionally save the source-grounded JSON to a private/ignored output location.
"""
import argparse
import json
from pathlib import Path

from backend.pipeline import process_document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('document', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--require-vision', action='store_true')
    parser.add_argument('--require-semantic', action='store_true')
    parser.add_argument('--expected-items', type=int)
    args = parser.parse_args()
    result = process_document(args.document.read_bytes(), args.document.name)
    metadata, proposal = result['metadata'], result['proposal']
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.require_vision and not metadata['verification']['visionComplete']:
        raise SystemExit('FAILED: official visual pipeline did not analyze all requested pages')
    if args.require_semantic and metadata['verification']['mode'] != 'model':
        raise SystemExit('FAILED: semantic model did not return a valid extraction plan')
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
    print(json.dumps({'items': len(proposal['items']), 'evidenceChecked': checked,
                      'vision': metadata['routing']['status'], 'mode': metadata['verification']['mode'],
                      'llmCalls': metadata['llmCalls'], 'timingsMs': metadata['timingsMs'],
                      'warnings': len(metadata['warnings'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
