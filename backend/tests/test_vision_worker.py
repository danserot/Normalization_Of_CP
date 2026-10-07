"""No Paddle weights: exercise service lifecycle and model-thread ownership."""
import asyncio
import base64
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.vision import VisionPage, VisionResult
from services.vision import app as module


def request():
    return {'pages': [{'page': 1, 'image': base64.b64encode(b'fixture').decode(),
                       'width': 1, 'height': 1, 'page_width': 10, 'page_height': 10}]}


class FakeVision:
    def __init__(self):
        self.threads = []
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_first = False

    def initialize(self):
        self.threads.append(threading.get_ident())

    def analyze_pages(self, pages):
        self.threads.append(threading.get_ident())
        self.calls += 1
        if self.block_first and self.calls == 1:
            self.entered.set()
            assert self.release.wait(2)
        return VisionResult([VisionPage(p.page) for p in pages])


def test_model_thread_is_stable_and_lifespan_can_restart(monkeypatch):
    fake = FakeVision()
    monkeypatch.setattr(module, 'PaddleOCRVisionService', lambda: fake)
    for _ in range(2):
        with TestClient(module.app) as client:
            for _ in range(100):
                if client.get('/health').status_code == 200:
                    break
                time.sleep(.005)
            assert client.get('/health').json()['ready']
            assert client.post('/analyze', json=request()).status_code == 200
        assert not module.state['ready']
        assert module.executor is None
    assert fake.calls == 2
    assert fake.threads[0] == fake.threads[1]
    assert fake.threads[2] == fake.threads[3]


def test_cancelled_request_keeps_model_ownership(monkeypatch):
    fake = FakeVision()
    fake.block_first = True
    monkeypatch.setattr(module, 'PaddleOCRVisionService', lambda: fake)

    async def exercise():
        async with module.lifespan(module.app):
            while not module.state['ready']:
                await asyncio.sleep(.001)
            first = asyncio.create_task(module.analyze(module.AnalyzeRequest(**request())))
            assert await asyncio.to_thread(fake.entered.wait, 1)
            first.cancel()
            second = asyncio.create_task(module.analyze(module.AnalyzeRequest(**request())))
            await asyncio.sleep(.02)
            assert fake.calls == 1
            fake.release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second)['available']
    asyncio.run(exercise())
    assert len(set(fake.threads)) == 1
