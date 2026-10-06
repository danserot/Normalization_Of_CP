"""Fail closed before reading private input; expose only explicitly allowed aggregates."""
import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path

OFFLINE_ENV = {
    'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
    'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1', 'WANDB_DISABLED': 'true',
    'WANDB_MODE': 'disabled', 'COMET_MODE': 'DISABLED', 'TOKENIZERS_PARALLELISM': 'false',
    'HF_HUB_DISABLE_PROGRESS_BARS': '1', 'HF_HOME': '/private/cache',
}


def require_isolation():
    os.environ.update(OFFLINE_ENV)
    # An environment flag alone is NOT an isolation boundary. Docker --network none
    # must provide a namespace with only lo; check both IPv4 and IPv6 interfaces.
    interfaces = {p.name for p in Path('/sys/class/net').iterdir()} if sys.platform == 'linux' else set()
    if interfaces != {'lo'} or not Path('/.dockerenv').exists():
        raise RuntimeError('E_NETWORK_NAMESPACE')
    probes = [('1.1.1.1', 443), ('8.8.8.8', 53)]
    for host, port in probes:
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex((host, port)) == 0:
                raise RuntimeError('E_NETWORK_EGRESS')
    return {'network_mode': 'none', 'interfaces': 1, 'egress_probes_blocked': len(probes)}


@contextlib.contextmanager
def silent_libraries():
    """Suppress native/Python diagnostics which could echo document text or paths."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])


def emit(stage, **counts):
    # Callers pass fixed stage/code strings and numeric counters, never exceptions.
    event = {'stage': stage, 'time': round(time.time()), **counts}
    print(json.dumps(event, ensure_ascii=True), flush=True)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
