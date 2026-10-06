"""Shared quarantine gate for every dataset producer and trainer."""
import json
from pathlib import Path


def excluded_ids(root=Path('/private')):
    path = root / 'quarantine/exclusions.json'
    return set(json.loads(path.read_text(encoding='utf-8')).get('documents', {})) if path.exists() else set()


def assert_training_allowed(root=Path('/private')):
    excluded = excluded_ids(root)
    if not excluded:
        return
    path = root / 'datasets/manifest.json'
    if not path.exists():
        raise RuntimeError('E_DATASET_PROVENANCE_REQUIRED')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if any(m['document'] in excluded for m in manifest):
        raise RuntimeError('E_QUARANTINED_TRAINING_DATA')
