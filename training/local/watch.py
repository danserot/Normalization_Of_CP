"""Bounded stage runner: kill a stalled process tree, never print child data."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .privacy import emit, require_isolation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--idle-seconds', type=int, default=900)
    parser.add_argument('--max-seconds', type=int, default=86400)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    require_isolation()
    # Child modules are responsible for aggregate-only output. Do not run an
    # arbitrary third-party trainer here: such stdout is never safe to forward.
    if len(args.command) < 3 or args.command[:2] != ['python', '-m'] or args.command[2] not in (
            'training.local.pipeline', 'training.local.train', 'training.local.evaluate', 'training.local.evaluate_tasks'):
        raise RuntimeError('E_COMMAND')
    with subprocess.Popen(args.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True, text=True, bufsize=1) as child:
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        started = last_progress = time.monotonic()
        while child.poll() is None:
            if time.monotonic() - last_progress > args.idle_seconds or time.monotonic() - started > args.max_seconds:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                emit('watchdog_stopped', code='E_NO_PROGRESS' if time.monotonic() - last_progress > args.idle_seconds else 'E_STAGE_TIMEOUT')
                return 124
            for key, _ in selector.select(timeout=5):
                line = key.fileobj.readline()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # Only our fixed-format aggregate events get forwarded.
                    if not isinstance(event, dict) or 'stage' not in event or 'time' not in event:
                        continue
                    print(json.dumps(event), flush=True)
                    last_progress = time.monotonic()
                except (ValueError, TypeError):
                    pass
        # Read remaining aggregate events after normal completion.
        for line in child.stdout:
            try:
                event = json.loads(line)
                if isinstance(event, dict) and 'stage' in event and 'time' in event:
                    print(json.dumps(event), flush=True)
            except (ValueError, TypeError):
                pass
        return child.returncode


if __name__ == '__main__':
    sys.exit(main())
