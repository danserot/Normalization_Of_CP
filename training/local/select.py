"""Preserve a candidate chosen on validation only; never auto-deploy on train loss."""
import shutil
import sys

from .pipeline import ROOT, load
from .privacy import atomic_json, emit, require_isolation


def main():
    require_isolation()
    current = load(ROOT / 'reports/tasks-trained.json')
    earlier = [load(p) for p in (ROOT / 'reports/history').glob('tasks-trained-*.json')]
    earlier = next((r for r in earlier if r['val'].get('schema_valid_rate') == 1), None)
    training = load(ROOT / 'reports/history/training-step-4.json')
    if not earlier or not training or current['val'].get('schema_valid_rate') != 0:
        raise RuntimeError('E_SELECTION_REVIEW_REQUIRED')
    # This bounded experiment had two candidates. Selection criterion was fixed
    # to validation validity/columns/rows; test metrics do not enter selection.
    source = ROOT / 'training/checkpoint-4'
    destination = ROOT / 'selected/adapter'
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and path.name != 'optimizer.pt':
            shutil.copyfile(path, destination / path.name)
    atomic_json(ROOT / 'selected/training.json', training)
    atomic_json(ROOT / 'selected/evaluation.json', earlier)
    report = {'checkpoint_step': 4, 'epochs': training['epochs'], 'adapter': 'selected/adapter',
              'validation_schema_valid_rate': earlier['val']['schema_valid_rate'],
              'validation_column_agreement': earlier['val']['column_agreement'],
              'deploy_recommended': False, 'reason': 'E_NO_LAYOUT_IMPROVEMENT',
              'scope': 'experimental_layout_only'}
    atomic_json(ROOT / 'reports/selection.json', report)
    emit('selection_complete', **{k: v for k, v in report.items() if k != 'adapter'})


if __name__ == '__main__':
    try:
        main()
    except Exception:
        emit('failed', code='E_SELECTION')
        sys.exit(1)
