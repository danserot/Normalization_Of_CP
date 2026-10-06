"""Certify individual self-contained Layout tasks; never certify their documents."""
import collections
import json
import sys

from backend.annotation_data import Annotation, MarkedField, source_from_payload
from backend.rules import FIELDS
from backend.universal import LAYOUT_SYSTEM, Layout, catalog
from .fast_labels import rule_annotation
from .pipeline import ROOT, load
from .privacy import atomic_json, emit, require_isolation, silent_libraries
from .quality import check_annotation


def certified_layouts(payload, group):
    if payload.get('incomplete') or payload.get('parseError'):
        return []
    annotation = rule_annotation(payload, group)
    blocks = {b['block']: b for b in catalog(source_from_payload(payload, 'document'))}
    examples = []
    for table in annotation.tables:
        if not table.isItems:
            continue
        cells = [c for c in payload['cells'] if c['block'] == table.block]
        # All rows in this original block are checked, including rows outside
        # the proposed item range. Unrelated document fields are not targets
        # of a Layout task and must not be trained as "missing".
        isolated = {'cells': cells, 'annotation': {'fields': []}}
        candidate = Annotation(fields=[MarkedField(field=f, state='missing') for f in FIELDS],
                               tables=[table], group=group)
        quality = check_annotation(isolated, candidate)
        if not quality['passed']:
            continue
        block = blocks[table.block]
        # A self-contained block with its own visible explicit headers needs
        # no previous layout. Do not create labels for headerless continuations.
        answer = Layout.model_validate(table.model_dump(exclude={'block', 'reviewed'}))
        examples.append({'system': LAYOUT_SYSTEM,
            'instruction': json.dumps({**block, 'previousLayout': None}, ensure_ascii=False, separators=(',', ':')),
            'input': '', 'output': answer.model_dump_json()})
    return examples


def main():
    require_isolation()
    datasets = {'train': [], 'val': [], 'test': []}
    manifest, documents = [], collections.Counter()
    for identifier, group in load(ROOT / 'partition.json')['documents'].items():
        payload = load(ROOT / 'parsed' / (identifier + '.json'))
        with silent_libraries():
            examples = certified_layouts(payload, group['group'])
        if not examples:
            continue
        split = group['split']
        manifest.append({'document': identifier, **group, 'start': len(datasets[split]), 'count': len(examples),
                         'provenance': 'auto_validated_layout_only', 'human_reviewed': False})
        documents[split] += 1
        datasets[split].extend(examples)
    for split, examples in datasets.items():
        atomic_json(ROOT / 'datasets' / ('kp_' + split + '.json'), examples)
    atomic_json(ROOT / 'datasets/manifest.json', manifest)
    report = {'documents': sum(documents.values()), 'documents_by_split': dict(documents),
              'examples': {k: len(v) for k, v in datasets.items()}, 'human_reviewed': 0,
              'train_tasks': {'Layout': len(datasets['train'])},
              'scope': 'self_contained_layout_only',
              'evaluation_limitation': 'pseudo_labels_are_not_ground_truth; requisites_not_supervised'}
    atomic_json(ROOT / 'reports/dataset.json', report)
    emit('task_export_complete', **report)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        emit('failed', code='E_TASK_EXPORT')
        sys.exit(1)
