from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.usage import record_usage, reset_usage, usage_summary


def test_usage_prices_cached_input_and_does_not_double_count_reasoning():
    reset_usage()
    record_usage({'usage': {'input_tokens': 1000, 'output_tokens': 200,
                 'input_tokens_details': {'cached_tokens': 400},
                 'output_tokens_details': {'reasoning_tokens': 100}}}, 'gpt-5-mini', 'vision')
    result = usage_summary()
    assert result['estimatedCostUsd'] == pytest.approx(0.00056)
    assert result['outputTokens'] == 200
    assert result['reasoningTokens'] == 100
    assert result['complete']
    reset_usage()
    assert usage_summary()['inputTokens'] == 0


def test_parallel_pages_and_unknown_usage():
    reset_usage()
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: record_usage({'usage': {'input_tokens': 10,
             'output_tokens': 20}}, 'gpt-5-mini', 'vision'), range(20)))
    assert usage_summary()['inputTokens'] == 200
    assert len(usage_summary()['calls']) == 20
    record_usage({}, 'gpt-5-mini', 'semantic')
    assert usage_summary()['estimatedCostUsd'] is None
    assert not usage_summary()['complete']


def test_unknown_model_does_not_use_mini_price():
    reset_usage()
    record_usage({'usage': {'input_tokens': 10, 'output_tokens': 20}}, 'other-model', 'semantic')
    assert usage_summary()['estimatedCostUsd'] is None
