"""Hardware/model test with entirely synthetic input; aggregate output only."""
import argparse
import sys
import time
import traceback
from pathlib import Path

from .privacy import atomic_json, emit, require_isolation, silent_libraries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', default='teacher')
    args = parser.parse_args()
    require_isolation()
    from backend.annotation_data import prepare_annotation
    from .inference import LocalGenerator, annotate
    from .quality import check_annotation
    payload = prepare_annotation(('Поставщик: ООО Синтетика\n'
        'Наименование\tКоличество\tЕд. изм.\tЦена\tСумма\n'
        'Тестовый товар\t2\tшт\t100.00\t200.00').encode(), 'synthetic.txt')
    started = time.monotonic()
    with silent_libraries():
        generator = LocalGenerator('/models/' + args.role)
    emit('probe_loaded', gpu_mb=generator.memory_mb(), seconds=round(time.monotonic() - started))
    try:
        with silent_libraries():
            annotation, tasks = annotate(generator, payload, 'synthetic')
            quality = check_annotation(payload, annotation)
    except Exception:
        # This module only constructs the fixed synthetic fixture above. It never
        # reads /input, parsed records, labels or datasets.
        emit('synthetic_probe_diagnostic', synthetic_response=generator.last_output)
        raise
    report = {'role': args.role, 'gpu_peak_mb': generator.memory_mb(), 'seconds': round(time.monotonic() - started),
              'tasks': len(tasks), 'quality_passed': quality['passed'], 'reason_codes': quality['reasons']}
    if not quality['passed']:
        emit('synthetic_probe_diagnostic', synthetic_annotation=annotation.model_dump())
    atomic_json(Path('/private/reports') / (args.role + '-probe.json'), report)
    emit('probe_complete', **report)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Only library/code locations, never exception values or source lines.
        emit('failed', code='E_PROBE', error_type=type(error).__name__,
             frames=[{'function': f.name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)[-4:]])
        sys.exit(1)
