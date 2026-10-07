"""Process-local document accounting; vision threads share a locked ledger.

Each extraction worker processes one document at a time. Only provider counts
are recorded; unknown usage/prices must never be presented as zero cost.
"""
import copy
import threading

_lock = threading.Lock()
_calls = []
_unknown = 0
PRICES = {'gpt-5-mini': (0.25, 0.025, 2.0),
          'gpt-5-mini-2025-08-07': (0.25, 0.025, 2.0)}


def reset_usage():
    global _unknown
    with _lock:
        _calls.clear()
        _unknown = 0


def record_usage(data, model, stage):
    global _unknown
    usage = data.get('usage')
    with _lock:
        if not isinstance(usage, dict) or any(
            not isinstance(usage.get(key), int) or usage[key] < 0
            for key in ('input_tokens', 'output_tokens')
        ):
            _unknown += 1
            return
        incoming, outgoing = usage['input_tokens'], usage['output_tokens']
        cached = (usage.get('input_tokens_details') or {}).get('cached_tokens', 0)
        cached = min(incoming, max(0, cached)) if isinstance(cached, int) else 0
        reasoning = (usage.get('output_tokens_details') or {}).get('reasoning_tokens', 0)
        rates = PRICES.get(model)
        cost = ((incoming - cached) * rates[0] + cached * rates[1] + outgoing * rates[2]) / 1_000_000 if rates else None
        _calls.append({'stage': stage, 'model': model, 'inputTokens': incoming,
                       'cachedInputTokens': cached, 'outputTokens': outgoing,
                       'reasoningTokens': reasoning if isinstance(reasoning, int) else 0,
                       'estimatedCostUsd': cost})


def usage_summary():
    with _lock:
        calls, unknown = copy.deepcopy(_calls), _unknown
    complete = unknown == 0 and all(c['estimatedCostUsd'] is not None for c in calls)
    return {'currency': 'USD', 'pricingDate': '2026-10-07', 'costIsEstimate': True,
            'complete': complete, 'unreportedCalls': unknown, 'calls': calls,
            **{key: sum(c[key] for c in calls) for key in
               ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens')},
            'estimatedCostUsd': sum(c['estimatedCostUsd'] for c in calls) if complete else None}
