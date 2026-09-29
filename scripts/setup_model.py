"""One-time public weight download. Never called by application startup."""
import hashlib
from pathlib import Path
from urllib.request import urlopen

REVISION = '91cad51170dc346986eccefdc2dd33a9da36ead9'
FILENAME = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
SHA256 = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
URL = f'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/{REVISION}/{FILENAME}'


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


if __name__ == '__main__':
    folder = Path(__file__).resolve().parents[1] / 'models'
    folder.mkdir(exist_ok=True)
    target = folder / FILENAME
    if target.exists() and digest(target) == SHA256:
        print('Model already verified:', target.name)
    else:
        partial = target.with_suffix('.partial')
        with urlopen(URL, timeout=120) as response, partial.open('wb') as file:
            while chunk := response.read(1024 * 1024):
                file.write(chunk)
        if digest(partial) != SHA256:
            raise RuntimeError('Model checksum mismatch; refusing to install')
        partial.replace(target)
        print('Verified:', target.name, target.stat().st_size, 'bytes')
