"""Health check and fixed endpoint for the local llama.cpp model."""
import os

import httpx


MODEL_ID = 'local'
MODEL_NAME = os.getenv('LOCAL_MODEL_NAME', 'Qwen2.5-1.5B-Instruct Q4_K_M')
LOCAL_URL = 'http://model:8081'


def model_available():
    if os.getenv('LOCAL_MODEL_ENABLED', 'true').lower() != 'true':
        return False
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=1) as client:
            return client.get(LOCAL_URL + '/health').status_code == 200
    except httpx.HTTPError:
        return False
