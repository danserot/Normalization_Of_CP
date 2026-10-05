"""Single reusable subprocess with a hard deadline and process-tree cleanup."""
import asyncio
import multiprocessing as mp
import logging
import time
import traceback
from pathlib import Path

import psutil

from .extraction import DEADLINE, DocumentError, read_document
from .universal import extract_universal
from .rules import extract_rules, validate


def process_document(content, filename):
    source = read_document(content, filename)
    result, verification = extract_universal(source, content, filename)
    model_used = result is not None
    proposal, proof, warnings, used_rows = result if result is not None else extract_rules(source)
    warnings.extend(verification['issues'])
    validate(proposal, proof, warnings)
    return {'proposal': proposal, 'metadata': {
        'sourceName': filename, 'parser': 'Универсальная модель + проверка источников' if model_used else 'Резервные локальные правила',
        'status': 'parsed' if proof else 'empty',
        'confidence': round(min((e['confidence'] for e in proof.values() if isinstance(e, dict)), default=0), 2),
        'warnings': list(dict.fromkeys(warnings)), 'fieldEvidence': proof,
        'ocrPages': len(source.ocr_pages), 'ocrPageNumbers': source.ocr_pages,
        'sourceCells': source.public(), 'modelUsed': model_used, 'verification': verification,
    }}


def worker(connection):
    while True:
        try:
            content, filename = connection.recv()
        except EOFError:
            return
        try:
            result = process_document(content, filename)
            connection.send(('ok', result))
        except DocumentError as error:
            connection.send(('error', str(error)))
        except Exception as error:
            suffix = Path(filename).suffix.upper().lstrip('.')
            # Keep diagnostic frames, without exception text or document contents.
            logging.getLogger(__name__).error('Extraction failed: format=%s error=%s stack=%s',
                suffix, type(error).__name__, ''.join(traceback.format_tb(error.__traceback__)))
            connection.send(('error', f'Не удалось обработать {suffix}. Подробности ошибки записаны в журнал сервера'))
        finally:
            content = None
            result = None


class Pipeline:
    def __init__(self):
        self.process = None
        self.connection = None
        self.busy = False

    def stop(self):
        if self.process:
            try:
                parent = psutil.Process(self.process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                parent.kill()
                self.process.join(timeout=2)
            except psutil.NoSuchProcess:
                pass
            self.process = None
        if self.connection:
            self.connection.close()
            self.connection = None

    async def run(self, content, filename):
        if not self.process or not self.process.is_alive():
            self.stop()
            context = mp.get_context('spawn')
            self.connection, child = context.Pipe()
            self.process = context.Process(target=worker, args=(child,), daemon=True)
            self.process.start()
            child.close()
        started = time.monotonic()
        try:
            await asyncio.to_thread(self.connection.send, (content, filename))
            while not self.connection.poll():
                if time.monotonic() - started > DEADLINE or not self.process.is_alive():
                    self.stop()
                    raise DocumentError(f'Обработка остановлена: превышено {DEADLINE} секунд или завершился рабочий процесс')
                await asyncio.sleep(.05)
            status, result = await asyncio.to_thread(self.connection.recv)
            if status != 'ok':
                raise DocumentError(result)
            return result
        except asyncio.CancelledError:
            self.stop()
            raise
