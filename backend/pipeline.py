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

from .canonical import DEADLINE, DocumentError
from .errors import ProviderError
from .model_document import extract_model_document


class QueueFullError(DocumentError):
    pass


def process_document(content, filename):
    return extract_model_document(content, filename)


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
            connection.send(('error', error.worker_payload() if isinstance(error, ProviderError) else str(error)))
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
        config = {name: os.getenv(name, '') for name in (
            'OPENAI_MODEL', 'OPENAI_API_URL', 'OPENAI_PROXY_URL',
            'OPENAI_REASONING_EFFORT', 'MODEL_DOCUMENT_MAX_OUTPUT_TOKENS',
            'VISION_MAX_PAGES')}
        config['keyFingerprint'] = hashlib.sha256(os.getenv('OPENAI_API_KEY', '').encode()).hexdigest()
        signature = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
        return f'v4:{mode}:{filename}:{signature}:{digest}'

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

    @staticmethod
    def _cacheable(result):
        metadata = result.get('metadata', {})
        verification = metadata.get('verification', {})
        if verification.get('mode') == 'model_direct':
            return metadata.get('cacheEligible') is True
        # Legitimately absent requisites are not a reason to repeat inference.
        # Never retain unavailable/unfinished model or mandatory visual review.
        return (metadata.get('outcome', {}).get('state') != 'unavailable'
                and verification.get('coverageComplete') is True
                and verification.get('reviewCompleted') is True
                and (not verification.get('visionRequired') or verification.get('visionComplete') is True))

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
            try:
                await asyncio.wait_for(asyncio.to_thread(self.connection.send, (content, filename, mode)),
                                       timeout=max(.01, DEADLINE - (time.monotonic() - started)))
            except asyncio.TimeoutError as error:
                self.stop()
                raise DocumentError('Обработка остановлена: превышено время передачи документа') from error
            while not self.connection.poll():
                if time.monotonic() - started > DEADLINE or not self.process.is_alive():
                    self.stop()
                    raise DocumentError(f'Обработка остановлена: превышено {DEADLINE} секунд или завершился рабочий процесс')
                await asyncio.sleep(.05)
            try:
                status, result = await asyncio.wait_for(asyncio.to_thread(self.connection.recv),
                    timeout=max(.01, DEADLINE - (time.monotonic() - started)))
            except asyncio.TimeoutError as error:
                self.stop()
                raise DocumentError('Обработка остановлена: превышено время получения результата') from error
            if status != 'ok':
                if isinstance(result, dict):
                    message = result.pop('message')
                    raise ProviderError(message, **result)
                raise DocumentError(result)
            if mode == 'extract' and self._cacheable(result):
                self._cache_put(key, result)
            result.setdefault('metadata', {})['cacheHit'] = False
            return result
        except (EOFError, OSError) as error:
            self.stop()
            raise DocumentError('Рабочий процесс завершился до окончания обработки; повторите запрос') from error
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


class PipelinePool(Pipeline):
    """Bounded reusable processes, shared result cache and duplicate coalescing.

    A process is owned by one job at a time. Killing a timed-out document cannot
    interrupt another upload. The shared cache never contains unfinished reviews.
    """
    def __init__(self):
        super().__init__()
        count = min(8, max(1, int(os.getenv('EXTRACTION_WORKERS', '3'))))
        self.workers = [Pipeline() for _ in range(count)]
        self.max_pending = max(count, int(os.getenv('EXTRACTION_QUEUE_LIMIT', '8')))
        self.active = 0
        self._jobs = {}
        self._queue = None
        self._event_loop = None

    def _available_workers(self):
        loop = asyncio.get_running_loop()
        if self._event_loop is not loop:
            if self.pending:
                raise RuntimeError('Worker pool is running in another event loop')
            self._event_loop = loop
            self._queue = asyncio.Queue()
            for worker_instance in self.workers:
                self._queue.put_nowait(worker_instance)
        return self._queue

    def stop(self):
        for worker_instance in self.workers:
            worker_instance.stop()

    async def run(self, content, filename, mode='extract'):
        key = self._cache_key(content, filename, mode)
        cached = self._cache_get(key)
        if cached is not None:
            cached.setdefault('metadata', {})['cacheHit'] = True
            return cached
        job = self._jobs.get(key)
        if job is None:
            if self.pending >= self.max_pending:
                raise QueueFullError('Очередь обработки заполнена. Подождите и повторите запрос.')
            queue = self._available_workers()
            self.pending += 1
            job = asyncio.create_task(self._run_job(queue, key, content, filename, mode))
            self._jobs[key] = job
            def completed(task):
                self._jobs.pop(key, None)
                if not task.cancelled():
                    task.exception()
            job.add_done_callback(completed)
        return copy.deepcopy(await asyncio.shield(job))

    async def _run_job(self, queue, key, content, filename, mode):
        worker_instance = None
        started = time.monotonic()
        try:
            try:
                worker_instance = await asyncio.wait_for(queue.get(), self.queue_wait_seconds)
            except asyncio.TimeoutError as error:
                raise QueueFullError('Документ слишком долго ожидал обработки. Повторите запрос.') from error
            self.active += 1
            self.busy = True
            waited_ms = round((time.monotonic() - started) * 1000)
            result = await worker_instance.run(content, filename, mode)
            result.setdefault('metadata', {})['queueWaitMs'] = waited_ms
            if mode == 'extract' and self._cacheable(result):
                self._cache_put(key, result)
            return result
        finally:
            if worker_instance is not None:
                self.active -= 1
                self.busy = self.active > 0
                queue.put_nowait(worker_instance)
            self.pending -= 1
