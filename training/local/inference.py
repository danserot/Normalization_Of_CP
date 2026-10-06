"""Local CUDA inference with JSON grammar and exact production task payloads."""
import json
import re
import time

from backend.annotation_data import Annotation, MarkedField, MarkedTable, source_from_payload
from backend.rules import FIELDS, normalized
from backend.universal import LAYOUT_SYSTEM, REQUISITES_SYSTEM, Layout, Quote, Requisites, catalog, metadata_cells, requisite_batches, numeric_quote, inference_schema


class LocalGenerator:
    def __init__(self, path, adapter=None):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        if not torch.cuda.is_available():
            raise RuntimeError('E_CUDA')
        self.torch = torch
        self.teacher = path.endswith('/teacher')
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        self.tokenizer_data = build_token_enforcer_tokenizer_data(self.tokenizer)
        self.model = AutoModelForCausalLM.from_pretrained(path, local_files_only=True, trust_remote_code=False,
            torch_dtype=torch.float16, device_map={'': 0}, attn_implementation='sdpa',
            quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.float16))
        if adapter:
            from peft import PeftModel
            self.model = PeftModel.from_pretrained(self.model, adapter, local_files_only=True)
        self.model.eval()
        self.generated_tokens = 0
        self.calls = 0
        self.last_output = ''

    def memory_mb(self):
        return round(self.torch.cuda.max_memory_allocated() / 1024**2)

    def __call__(self, system, payload, schema, max_tokens=700):
        from lmformatenforcer import JsonSchemaParser, CharacterLevelParserConfig
        from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn
        if self.teacher:
            # Extra teacher instructions do not alter the student's production
            # payload or target contract. The teacher is explicitly told the
            # schema; a token grammar alone does not explain its semantics.
            system += '\nВерни один JSON-объект по этой схеме: ' + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            if schema is Layout:
                system += ('\nЕсли в блоке есть хотя бы одна товарная строка с названием и ценой/количеством, '
                    'isItems=true, даже если выше или ниже также есть реквизиты. firstRow/lastRow ограничивают '
                    'только товарные строки. Для каждого label копируй точный текст ячейки заголовка, '
                    'не расширяй сокращения. extras содержит только дополнительные колонки внутри '
                    'товарной таблицы, не реквизиты документа; при их отсутствии extras=[]. '
                    'Каждый объект components содержит и priceColumn, и totalColumn; отсутствующая '
                    'колонка равна 0. column в extras всегда >=1. Не добавляй выдуманные колонки.')
        messages = [{'role': 'system', 'content': system}]
        if self.teacher:
            if schema is Layout:
                example_input = {'block': 'table-example', 'rowCount': 2, 'firstRow': 1, 'lastRow': 2,
                    'rows': [{'row': 1, 'cells': [['c0', 1, 'Наименование'], ['c1', 2, 'Количество'],
                                               ['c2', 3, 'Цена'], ['c3', 4, 'Сумма']]},
                             {'row': 2, 'cells': [['c4', 1, 'Синтетическая гайка'], ['c5', 2, '3'],
                                               ['c6', 3, '10'], ['c7', 4, '30']]}], 'previousLayout': None}
                example_output = {'isItems': True, 'firstRow': 2, 'lastRow': 2, 'nameColumn': 1,
                                  'quantityColumn': 2, 'unitColumn': 0, 'components': [
                                      {'label': 'Цена', 'labelCell': 'c2', 'priceColumn': 3, 'totalColumn': 4}], 'extras': []}
            else:
                example_input = [['c40', 'Покупатель: ООО Тестовый покупатель'], ['c41', 'Поставщик: ООО Учебный продавец']]
                example_output = {'fields': [{'field': 'client', 'cell': 'c40', 'value': 'ООО Тестовый покупатель'},
                                            {'field': 'supplier', 'cell': 'c41', 'value': 'ООО Учебный продавец'}]}
            messages.extend([{'role': 'user', 'content': json.dumps(example_input, ensure_ascii=False)},
                             {'role': 'assistant', 'content': json.dumps(example_output, ensure_ascii=False)}])
        messages.append({'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))})
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = self.tokenizer(text, return_tensors='pt').to('cuda')
        if inputs.input_ids.shape[1] + max_tokens > 8192:
            raise RuntimeError('E_CONTEXT')
        grammar = build_transformers_prefix_allowed_tokens_fn(self.tokenizer_data, JsonSchemaParser(inference_schema(schema, payload),
            config=CharacterLevelParserConfig(force_json_field_order=True, max_consecutive_whitespaces=2)))
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=max_tokens,
                max_time=150, prefix_allowed_tokens_fn=grammar, pad_token_id=self.tokenizer.eos_token_id)
        generated = output[0, inputs.input_ids.shape[1]:]
        self.generated_tokens += len(generated)
        self.calls += 1
        self.last_output = self.tokenizer.decode(generated, skip_special_tokens=True)
        try:
            return schema.model_validate_json(self.last_output)
        except ValueError:
            raise RuntimeError('E_SCHEMA') from None


def annotate(generator, payload, group):
    source = source_from_payload(payload, 'document')
    tables, quotes, previous, tasks, conflicts = [], {}, None, [], set()
    started = time.monotonic()
    for block in catalog(source):
        if time.monotonic() - started > 600:
            raise RuntimeError('E_TIMEOUT')
        if max(c.cell for c in source.cells if c.block == block['block']) < 2:
            continue
        user = {**block, 'previousLayout': previous}
        layout = generator(LAYOUT_SYSTEM, user, Layout, 500)
        tasks.append({'task': 'Layout', 'input': user, 'output': layout.model_dump()})
        tables.append(MarkedTable(block=block['block'], reviewed=False, **layout.model_dump()))
        if layout.isItems:
            previous = layout.model_dump()
    for batch in requisite_batches(metadata_cells(source, [t for t in tables if t.isItems])):
        if time.monotonic() - started > 600:
            raise RuntimeError('E_TIMEOUT')
        answer = generator(REQUISITES_SYSTEM, batch, Requisites, 700)
        tasks.append({'task': 'Requisites', 'input': batch, 'output': answer.model_dump()})
        for quote in answer.fields:
            if quote.field not in FIELDS:
                raise RuntimeError('E_SCHEMA')
            if quote.field in quotes:
                if quotes[quote.field].value != quote.value:
                    conflicts.add(quote.field)
            else:
                quotes[quote.field] = quote
    if getattr(generator, 'teacher', False):
        explicit = explicit_fields(metadata_cells(source, [t for t in tables if t.isItems]))
        added = 0
        for key, quote in explicit.items():
            if key not in quotes:
                quotes[key] = quote
                added += 1
        tasks.append({'task': 'ExplicitSourceRules', 'added_fields': added})
    fields = [MarkedField(field=key, state='pending') if key in conflicts else
              MarkedField(field=key, state='found', cell=quotes[key].cell, value=quotes[key].value)
              if key in quotes else MarkedField(field=key, state='missing') for key in FIELDS]
    return Annotation(fields=fields, tables=tables, group=group,
                      issues=['E_FIELD_CONFLICT'] if conflicts else []), tasks


def explicit_fields(cells):
    """Recover only unique literal `known label: value` pairs missed by teacher.

    No inference from filenames, branding, nearby text, manager names or arithmetic.
    Conflicting values are left unresolved for human review.
    """
    candidates = {}
    aliases = {normalized(alias): key for key, values in FIELDS.items() for alias in values}
    for cell in cells:
        match = re.fullmatch(r'([^:\n]{1,60}):\s*(\S[^\n]*)', cell.text)
        if not match:
            continue
        key = aliases.get(normalized(match[1]))
        value = match[2].strip()
        if not key or key == 'documentTotal' and numeric_quote(value) is None:
            continue
        candidates.setdefault(key, []).append(Quote(field=key, cell=cell.id, value=value))
    return {key: values[0] for key, values in candidates.items() if len({v.value for v in values}) == 1}
