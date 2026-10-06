"""Public resource preparation ONLY. Never mount documents or private output here."""
import json
import os
import subprocess
from pathlib import Path

os.environ.update(HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1', HF_HUB_DISABLE_PROGRESS_BARS='1',
                  HF_HUB_DISABLE_XET='1', HF_HUB_DOWNLOAD_TIMEOUT='120')


def main():
    from huggingface_hub import HfApi, snapshot_download
    root = Path('/resources')
    root.mkdir(exist_ok=True)
    lock = root / 'models.lock.json'
    saved = json.loads(lock.read_text()) if lock.exists() else {}
    for role, name in [('student', 'Qwen/Qwen3-1.7B'), ('teacher', 'Qwen/Qwen3-4B-Instruct-2507')]:
        revision = saved.get(role, {}).get('revision') or HfApi().model_info(name).sha
        saved[role] = {'id': name, 'revision': revision, 'license': 'Apache-2.0'}
        lock.write_text(json.dumps(saved, indent=2))
        snapshot_download(name, revision=revision, local_dir=root / role,
                          allow_patterns=['*.json', '*.safetensors', '*.txt', '*.jinja', 'LICENSE', 'README.md'],
                          max_workers=2)
        print(json.dumps({'stage': 'public_download', 'role': role, 'status': 'complete'}), flush=True)
    source = root / 'llama.cpp'
    if not source.exists():
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/ggml-org/llama.cpp.git', str(source)], check=True, timeout=300)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    saved['llama.cpp'] = {'revision': revision}
    lock.write_text(json.dumps(saved, indent=2))
    print(json.dumps({'stage': 'public_download', 'role': 'converter_source', 'status': 'complete'}), flush=True)


if __name__ == '__main__':
    main()
