"""Single reusable subprocess with a hard deadline and process-tree cleanup."""
import asyncio
import copy
import hashlib
import json
import multiprocessing as mp
import logging
import os
from collections import OrderedDict
import time
import traceback
from pathlib import Path

import psutil

from .extraction import DEADLINE, DocumentError, read_document
from .universal import extract_universal
from .rules import FIELDS, validate
from .outcome import extraction_outcome


class QueueFullError(DocumentError):
    pass


def process_document(content, filename):
    started = time.perf_counter()
    source = read_document(content, filename)
    read_ms = round((time.perf_counter() - started) * 1000)
    model_started = time.perf_counter()
    result, verification = extract_universal(source, content, filename)
    model_ms = round((time.perf_counter() - model_started) * 1000)
    model_used = result is not None
    if result is not None:
        proposal, proof, warnings, used_rows = result
    else:
        proposal = {key: '' for key in FIELDS}
        proposal.update(documentTotal=None, notes='', items=[], additionalFields=[])
        proof, warnings, used_rows = {}, list(source.warnings), set()
    warnings.extend(verification['issues'])
    validation_started = time.perf_counter()
    validate(proposal, proof, warnings)
    validation_ms = round((time.perf_counter() - validation_started) * 1000)
    outcome = extraction_outcome(proposal, verification)
    low_ocr_pages = [page for page in source.ocr_quality
                     if page['meanConfidence'] is None or page['meanConfidence'] < 55]
    if low_ocr_pages:
        outcome['ocrReviewPages'] = [page['page'] for page in low_ocr_pages]
        if outcome['state'] == 'complete':
            outcome['state'] = 'partial'
            outcome['message'] = 'OCR распознал текст неуверенно. Сверьте скан и значения.'
        elif outcome['state'] == 'partial':
            outcome['message'] = 'Данные извлечены частично; также проверьте страницы с низкой оценкой OCR.'
    if outcome['state'] != 'complete':
        warnings.insert(0, outcome['message'])
    return {'proposal': proposal, 'metadata': {
        'sourceName': filename,
        'parser': 'Локальная модель + проверка источников' if model_used else 'Локальная модель: извлечение не выполнено',
        'status': 'empty' if outcome['state'] == 'unavailable' else 'parsed',
        'outcome': outcome,
        'ocrQuality': source.ocr_quality,
        'timingsMs': {'read': read_ms, 'model': model_ms, 'validation': validation_ms,
                      'total': round((time.perf_counter() - started) * 1000)},
        'confidence': round(min((e['confidence'] for e in proof.values() if isinstance(e, dict)), default=0), 2),
        'warnings': list(dict.fromkeys(warnings)), 'fieldEvidence': proof,
        'ocrPages': len(source.ocr_pages), 'ocrPageNumbers': source.ocr_pages,
        'sourceCells': source.public(), 'modelUsed': model_used, 'verification': verification,
    }}


def worker(connection):
    while True:
        try:
            content, filename, mode = connection.recv()
        except EOFError:
            return
        try:
            if mode == 'annotation':
                from .annotation_data import prepare_annotation
                result = prepare_annotation(content, filename)
            else:
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
        self.pending = 0
        self.max_pending = max(1, int(os.getenv('EXTRACTION_QUEUE_LIMIT', '2')))
        self.queue_wait_seconds = max(1, int(os.getenv('EXTRACTION_QUEUE_WAIT_SECONDS', '120')))
        self._lock = asyncio.Lock()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = 32 * 1024 * 1024
        self._cache_entries = 12

    @staticmethod
    def _cache_key(content, filename, mode):
        digest = hashlib.sha256(content).hexdigest()
        return f'{mode}:{filename.casefold()}:{digest}'

    def _cache_get(self, key):
        entry = self._cache.get(key)
        if entry is None:
            return None
        self._cache.move_to_end(key)
        return copy.deepcopy(entry[1])

    def _cache_put(self, key, result):
        try:
            value = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError):
            return
        if len(value) > self._cache_limit // 2:
            return
        previous = self._cache.pop(key, None)
        if previous:
            self._cache_bytes -= previous[0]
        self._cache[key] = (len(value), copy.deepcopy(result))
        self._cache_bytes += len(value)
        while self._cache and (len(self._cache) > self._cache_entries or self._cache_bytes > self._cache_limit):
            _, (size, _) = self._cache.popitem(last=False)
            self._cache_bytes -= size

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

    async def run(self, content, filename, mode='extract'):
        key = self._cache_key(content, filename, mode)
        cached = self._cache_get(key)
        if cached is not None:
            cached.setdefault('metadata', {})['cacheHit'] = True
            return cached
        if self.pending >= self.max_pending:
            raise QueueFullError('Очередь обработки заполнена. Подождите и повторите запрос.')
        self.pending += 1
        owns_worker = False
        try:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=self.queue_wait_seconds)
            except asyncio.TimeoutError as error:
                raise QueueFullError('Документ слишком долго ожидал обработки. Повторите запрос.') from error
            owns_worker = True
            self.busy = True
            if not self.process or not self.process.is_alive():
                self.stop()
                context = mp.get_context('spawn')
                self.connection, child = context.Pipe()
                self.process = context.Process(target=worker, args=(child,), daemon=True)
                self.process.start()
                child.close()
            started = time.monotonic()
            await asyncio.to_thread(self.connection.send, (content, filename, mode))
            while not self.connection.poll():
                if time.monotonic() - started > DEADLINE or not self.process.is_alive():
                    self.stop()
                    raise DocumentError(f'Обработка остановлена: превышено {DEADLINE} секунд или завершился рабочий процесс')
                await asyncio.sleep(.05)
            status, result = await asyncio.to_thread(self.connection.recv)
            if status != 'ok':
                raise DocumentError(result)
            if mode == 'extract' and result.get('metadata', {}).get('outcome', {}).get('state') == 'complete':
                self._cache_put(key, result)
            result.setdefault('metadata', {})['cacheHit'] = False
            return result
        except asyncio.CancelledError:
            if owns_worker:
                self.stop()
                self.busy = False
            raise
        finally:
            if owns_worker:
                self.busy = False
                self._lock.release()
            self.pending -= 1
