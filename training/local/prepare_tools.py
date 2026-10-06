"""Public build phase: mount public resources only, never confidential files."""
import json
import subprocess
from pathlib import Path


root = Path('/resources')
source = root / 'llama.cpp'
if not source.exists():
    subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/ggml-org/llama.cpp.git', str(source)],
                   check=True, timeout=300)
revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
(root / 'tools.lock.json').write_text(json.dumps({'llama.cpp': revision}, indent=2))
subprocess.run(['cmake', '-S', str(source), '-B', str(source / 'build'), '-DGGML_NATIVE=OFF',
                '-DGGML_CUDA=OFF', '-DLLAMA_CURL=OFF', '-DLLAMA_BUILD_TESTS=OFF', '-DLLAMA_BUILD_EXAMPLES=OFF'],
               check=True, timeout=120)
subprocess.run(['cmake', '--build', str(source / 'build'), '--target', 'llama-quantize', '-j', '2'],
               check=True, timeout=1200)
print(json.dumps({'stage': 'public_tools', 'status': 'complete'}), flush=True)
