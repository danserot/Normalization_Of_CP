"""Convert/quantize private weights with a pinned local converter, without network."""
import os
import re
import subprocess
import sys
from pathlib import Path

from .privacy import atomic_json, emit, require_isolation


def main():
    require_isolation()
    root = Path('/private')
    output = root / 'gguf'
    output.mkdir(exist_ok=True)
    source = Path('/models/llama.cpp')
    env = {**os.environ, 'PYTHONPATH': str(source / 'gguf-py'),
           'LD_LIBRARY_PATH': str(source / 'build/bin')}
    commands = [('hf', [sys.executable, str(source / 'convert_hf_to_gguf.py'), str(root / 'merged'),
                        '--outfile', str(output / 'model-f16.gguf'), '--outtype', 'f16'],
                       ), ('quantize', [str(source / 'build/bin/llama-quantize'), str(output / 'model-f16.gguf'),
                        str(output / 'model-q4_k_m.gguf'), 'Q4_K_M', '2'])]
    for stage, command in commands:
        if stage == 'hf':
            import json
            failure_path = root / 'reports/conversion_failure.json'
            previous = json.loads(failure_path.read_text()) if failure_path.exists() else {}
            f16 = output / 'model-f16.gguf'
            newest_source = max(p.stat().st_mtime for p in (root / 'merged').iterdir() if p.is_file())
            if previous.get('phase') == 'quantize' and f16.exists() and f16.stat().st_mtime >= newest_source:
                with f16.open('rb') as stream:
                    if stream.read(4) == b'GGUF':
                        emit('conversion_resumed', completed_phase='hf')
                        continue
        # Converter logs stay private; expose only known error classes and code
        # line numbers, never the error text or model tensor values.
        log = output / ('conversion-' + stage + '.log')
        with log.open('w') as stream:
            result = subprocess.run(command, env=env, stdout=stream, stderr=stream, timeout=900)
        if result.returncode:
            details = log.read_text(errors='replace')
            category = next((name for name in ('KeyError', 'ValueError', 'AttributeError', 'NotImplementedError',
                                               'ModuleNotFoundError', 'ImportError', 'RuntimeError')
                             if name + ':' in details), 'E_PROCESS')
            lines = re.findall(r'File "[^"]*convert_hf_to_gguf.py", line (\d+)', details)
            atomic_json(root / 'reports/conversion_failure.json', {'phase': stage, 'return_code': result.returncode,
                        'category': category, 'converter_line': int(lines[-1]) if lines else 0})
            emit('conversion_failed', phase=stage, return_code=result.returncode, category=category,
                 converter_line=int(lines[-1]) if lines else 0)
            raise RuntimeError('E_CONVERSION')
    report = {'f16_bytes': (output / 'model-f16.gguf').stat().st_size,
              'q4_bytes': (output / 'model-q4_k_m.gguf').stat().st_size,
              'quality_verified': False}
    atomic_json(root / 'reports/conversion.json', report)
    emit('conversion_complete', **report)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('failed', code='E_CONVERSION', error_type=type(error).__name__)
        sys.exit(1)
