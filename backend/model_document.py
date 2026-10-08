"""Original file -> model-produced proposal. No native parsing or rule fallback."""
from pathlib import Path
import time

from pydantic import ValidationError

from .canonical import DEADLINE, DocumentError
from .document_limits import IMAGE_FORMATS
from .document_input import input_parts, SPREADSHEETS
from .document_models import ModelCell, ModelEvidence, DocumentAnswer
from .document_prompt import INSTRUCTION
from .openai_client import OpenAIError, openai_client, setting_int
from .outcome import extraction_outcome
from .usage import reset_usage, usage_summary



def _value(proposal, path):
    value = proposal
    try:
        for part in path.split('.'):
            if isinstance(value, list):
                if not part.isdecimal():
                    return None
                value = value[int(part)]
            else:
                value = value[part]
        return value
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def populated_paths(value, prefix=''):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in {'notes', 'componentMode'}:
                yield from populated_paths(child, f'{prefix}.{key}' if prefix else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from populated_paths(child, f'{prefix}.{index}')
    elif value is not None and value != '':
        yield prefix


def check_arithmetic(proposal):
    """Checks model values, never fills or replaces them."""
    warnings = []
    for index, item in enumerate(proposal['items']):
        quantity, price, total = item['quantity'], item['unitPrice'], item['lineTotal']
        if all(value is not None for value in (quantity, price, total)):
            if abs(quantity * price - total) > .02 + abs(total) * 1e-8:
                warnings.append(f'Позиция {index + 1}: количество × цена не совпадает с суммой строки')
    totals = [item['lineTotal'] for item in proposal['items']]
    if totals and all(value is not None for value in totals) and proposal['documentTotal'] is not None:
        if not any(proposal[key] for key in ('vat', 'discount', 'delivery')):
            if abs(sum(totals) - proposal['documentTotal']) > .02 + len(totals) * .005:
                warnings.append('Итог документа не совпадает с суммой позиций')
    return warnings


def extract_model_document(content, filename, *, client=None):
    reset_usage()
    started = time.perf_counter()
    parts = input_parts(content, filename)
    prepared = time.perf_counter()
    messages = [{'role': 'system', 'content': INSTRUCTION},
                {'role': 'user', 'content': [*parts, {'type': 'input_text',
                    'text': 'Прочитай приложенный документ и верни полное КП по заданной схеме.'}]}]
    try:
        raw = (client or openai_client).generate(messages, DocumentAnswer.model_json_schema(),
            max_tokens=setting_int('MODEL_DOCUMENT_MAX_OUTPUT_TOKENS', 24000, 1024, 100000),
            remaining=max(0, DEADLINE - (time.perf_counter() - started)), schema_name='commercial_proposal')
        answer = DocumentAnswer.model_validate_json(raw)
    except OpenAIError:
        raise
    except ValidationError:
        raise DocumentError('Модель вернула некорректные данные КП; повторите обработку') from None
    model_finished = time.perf_counter()
    proposal = answer.proposal.model_dump()
    warnings = list(answer.warnings)
    cells = [dict(cell.model_dump(), file=filename, method='model-document',
                  source_method='model-document') for cell in answer.cells]
    by_id = {cell['id']: cell for cell in cells}
    if len(by_id) != len(cells):
        raise DocumentError('Модель вернула повторяющиеся ссылки на источник; повторите обработку')
    proof = {}
    for entry in answer.evidence:
        cell = by_id.get(entry.sourceId)
        value = _value(proposal, entry.field)
        if not cell or entry.excerpt not in cell['text'] or value is None or isinstance(value, (dict, list)):
            warnings.append('Часть ссылок модели не согласуется с её транскрипцией')
            continue
        proof[entry.field] = dict(file=filename, excerpt=entry.excerpt, sourceId=cell['id'],
            block=cell['block'], row=cell['row'], cell=cell['cell'], page=cell['page'], sheet=cell['sheet'],
            value=value, method='model-document', sourceMethod='model-document', verifiedInSource=False,
            warning='Цитата предложена моделью; проверьте по оригиналу')
    arithmetic_issues = check_arithmetic(proposal)
    evidence_issues = list(warnings[len(answer.warnings):])
    missing_proof = set(populated_paths(proposal)) - set(proof)
    if missing_proof:
        evidence_issues.append(f'Модель не приложила цитаты для {len(missing_proof)} значений; сверьте их с оригиналом.')
        warnings.extend(evidence_issues[-1:])
    warnings.extend(arithmetic_issues)
    warnings.append('Данные и цитаты получены моделью. Сверьте важные значения с оригиналом.')
    suffix = Path(filename).suffix.lower()
    if suffix in SPREADSHEETS:
        warnings.append('Сервис читает до первых 1000 строк каждого листа. Полное покрытие Excel/CSV независимо не проверено.')
    if suffix == '.docx':
        warnings.append('Word передаётся как исходный файл; встроенные изображения и оформление сервисом не проверяются.')
    complete = answer.complete and not answer.warnings and not arithmetic_issues and not evidence_issues
    verification = {'mode': 'model_direct', 'reviewCompleted': complete,
        'coverageComplete': False, 'coverageBasis': 'model-report', 'modelReportedComplete': answer.complete,
        'visionUsed': suffix == '.pdf' or suffix in IMAGE_FORMATS,
        'unclaimedRows': [], 'issues': warnings, 'llmCalls': 1,
        'sourceIndependentlyVerified': False}
    outcome = extraction_outcome(proposal, verification)
    outcome['message'] = ('Данные получены моделью. Сверьте их с оригиналом.' if proposal['items'] or proof
                          else 'Модель не нашла данные КП; проверьте оригинал.')
    return {'proposal': proposal, 'metadata': {
        'sourceName': filename, 'parser': 'Прямое извлечение моделью',
        'status': 'parsed' if outcome['state'] != 'unavailable' else 'empty', 'outcome': outcome,
        'apiUsage': usage_summary(), 'warnings': list(dict.fromkeys(warnings)),
        'fieldEvidence': proof, 'sourceCells': cells, 'confidence': 0,
        'confidenceMethod': 'not-measured', 'modelUsed': True, 'llmAttempted': True, 'llmCalls': 1,
        'cacheEligible': complete,
        'architectureVersion': 4, 'provider': 'openai', 'verification': verification,
        'documentStats': {'bytes': len(content), 'format': suffix, 'cells': len(cells)},
        'timingsMs': {'read': round((prepared - started) * 1000), 'parsing': 0,
            'model': round((model_finished - prepared) * 1000),
            'validation': round((time.perf_counter() - model_finished) * 1000),
            'total': round((time.perf_counter() - started) * 1000)},
    }}
