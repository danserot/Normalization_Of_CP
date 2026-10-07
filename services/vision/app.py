"""Isolated HTTP service for the official PaddleOCR-VL 1.6 full pipeline."""
import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.vision import PaddleOCRVisionService, RenderedPage

service = PaddleOCRVisionService()
state = {'ready': False, 'loading': True, 'error': ''}
lock = asyncio.Lock()
executor = None


async def run_on_model_thread(function, *args):
    # Paddle device/context belongs to one persistent thread: initialization
    # and inference must not migrate between arbitrary asyncio pool threads.
    if executor is None:
        raise RuntimeError('Vision worker is not running')
    return await asyncio.get_running_loop().run_in_executor(executor, function, *args)


async def initialize():
    try:
        await run_on_model_thread(service.initialize)
        state['ready'] = True
    except Exception as error:
        state['error'] = type(error).__name__
        logging.getLogger(__name__).exception('PaddleOCR-VL initialization failed')
    finally:
        state['loading'] = False


@asynccontextmanager
async def lifespan(_app):
    global service, executor, lock
    service = PaddleOCRVisionService()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='paddle-document')
    lock = asyncio.Lock()
    state.update(ready=False, loading=True, error='')
    task = asyncio.create_task(initialize())
    try:
        yield
    finally:
        state['ready'] = False
        # Do not abandon a live CUDA initialization/inference thread. Docker's
        # stop grace period remains the hard process-level shutdown bound.
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        executor = None
        state.update(ready=False, loading=False)


app = FastAPI(title='PaddleOCR-VL document structure', lifespan=lifespan)

class PageRequest(BaseModel):
    page: int = Field(ge=1, le=1000)
    image: str = Field(max_length=24_000_000)
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)
    page_width: float = Field(gt=0, allow_inf_nan=False)
    page_height: float = Field(gt=0, allow_inf_nan=False)


class AnalyzeRequest(BaseModel):
    pages: list[PageRequest] = Field(min_length=1, max_length=4)


@app.get('/health')
def health():
    return JSONResponse({'provider': 'paddleocr-vl', 'version': '1.6', **state},
                        status_code=200 if state['ready'] else 503)


@app.post('/analyze')
async def analyze(request: AnalyzeRequest):
    if not state['ready']:
        raise HTTPException(503, 'PaddleOCR-VL is initializing or unavailable')
    if len({p.page for p in request.pages}) != len(request.pages):
        raise HTTPException(422, 'Duplicate page number')
    try:
        pages = [RenderedPage(p.page, base64.b64decode(p.image, validate=True), p.width, p.height,
                              p.page_width, p.page_height) for p in request.pages]
    except ValueError as error:
        raise HTTPException(422, 'Invalid image encoding') from error
    # Cancellation does not release ownership while the GPU thread still runs.
    async with lock:
        task = asyncio.create_task(run_on_model_thread(service.analyze_pages, pages))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except Exception as error:
            logging.getLogger(__name__).exception('PaddleOCR-VL inference failed')
            raise HTTPException(502, 'PaddleOCR-VL inference failed') from error
    return asdict(result)
