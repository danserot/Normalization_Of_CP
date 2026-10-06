"""Small resumable QLoRA loop; no cloud callbacks and no decoded samples in logs."""
import argparse
import gc
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

from .privacy import atomic_json, emit, require_isolation, silent_libraries

ROOT = Path('/private')


def tokenize_example(tokenizer, sample, cutoff):
    messages = [{'role': 'system', 'content': sample['system']},
                {'role': 'user', 'content': sample['instruction']}]
    prefix = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, enable_thinking=False)
    # Same prefix as inference; loss covers only the supervised answer + EOS.
    answer = tokenizer.encode(sample['output'], add_special_tokens=False) + [tokenizer.eos_token_id]
    if len(prefix) + len(answer) > cutoff:
        return None
    return {'input_ids': prefix + answer, 'labels': [-100] * len(prefix) + answer}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--cutoff', type=int, default=2048)
    parser.add_argument('--epochs', type=int, default=2)
    parser.add_argument('--rank', type=int, default=8)
    parser.add_argument('--accumulation', type=int, default=8)
    args = parser.parse_args()
    require_isolation()
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    torch.manual_seed(42)
    random.seed(42)
    if not torch.cuda.is_available():
        raise RuntimeError('E_CUDA')
    output = ROOT / ('smoke' if args.smoke else 'training')
    output.mkdir(exist_ok=True)
    state_path = output / 'state.json'
    if state_path.exists() and not args.resume:
        raise RuntimeError('E_EXISTING_RUN_USE_RESUME')
    state = json.loads(state_path.read_text()) if args.resume and state_path.exists() else None
    base = '/models/student'
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    with silent_libraries():
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            device_map={'': 0}, torch_dtype=dtype, attn_implementation='sdpa',
            quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype))
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True,
                                               gradient_checkpointing_kwargs={'use_reentrant': False})
        if state:
            model = PeftModel.from_pretrained(model, output / state['checkpoint'], is_trainable=True, local_files_only=True)
        else:
            model = get_peft_model(model, LoraConfig(r=args.rank, lora_alpha=args.rank * 2, lora_dropout=.05,
                target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj'], task_type='CAUSAL_LM'))
    model.config.use_cache = False
    data_path = ROOT / 'datasets' / 'kp_train.json'
    fingerprint = hashlib.sha256(data_path.read_bytes()).hexdigest()
    raw = json.loads(data_path.read_text())
    with silent_libraries():
        data = [item for sample in raw if (item := tokenize_example(tokenizer, sample, args.cutoff)) is not None]
    if len(data) < 2:
        raise RuntimeError('E_TOO_FEW_EXAMPLES')
    if state and (state['dataset'] != fingerprint or state['cutoff'] != args.cutoff or state['rank'] != args.rank):
        raise RuntimeError('E_RESUME_MISMATCH')
    if not args.smoke and not (ROOT / 'reports' / 'smoke.json').exists():
        raise RuntimeError('E_SMOKE_REQUIRED')
    if not any((ROOT / 'reports' / name).exists() for name in ('baseline-test.json', 'tasks-baseline.json')):
        raise RuntimeError('E_BASELINE_REQUIRED')
    schedule = []
    for epoch in range(args.epochs if not args.smoke else 1):
        indices = list(range(len(data)))
        random.Random(42 + epoch).shuffle(indices)
        schedule.extend(indices)
    if args.smoke:
        schedule = schedule[:2]
    report_path = ROOT / 'reports' / ('smoke.json' if args.smoke else 'training.json')
    if state and report_path.exists() and state['next_example'] < len(schedule):
        atomic_json(ROOT / 'reports/history' / ('training-step-' + str(state['step']) + '.json'),
                    json.loads(report_path.read_text()))
    accumulation = 1 if args.smoke else args.accumulation
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=5e-5, weight_decay=.01)
    start = state['next_example'] if state else 0
    step = state['step'] if state else 0
    if state:
        bundle = torch.load(output / state['checkpoint'] / 'optimizer.pt', map_location='cpu', weights_only=True)
        optimizer.load_state_dict(bundle['optimizer'])
        torch.set_rng_state(bundle['rng'])
        torch.cuda.set_rng_state(bundle['cuda_rng'])
    emit('training_loaded', examples=len(data), excluded_long=len(raw) - len(data), total_examples=len(schedule),
         resumed_at=start, trainable_parameters=sum(p.numel() for p in model.parameters() if p.requires_grad))
    model.train()
    optimizer.zero_grad(set_to_none=True)
    started = time.monotonic()
    losses, running = [], 0.

    def checkpoint(next_example):
        name = 'checkpoint-' + str(step)
        destination = output / name
        with silent_libraries():
            model.save_pretrained(destination, safe_serialization=True)
            tokenizer.save_pretrained(destination)
            torch.save({'optimizer': optimizer.state_dict(), 'rng': torch.get_rng_state(),
                        'cuda_rng': torch.cuda.get_rng_state()}, destination / 'optimizer.pt')
        atomic_json(state_path, {'checkpoint': name, 'next_example': next_example, 'step': step,
                                'dataset': fingerprint, 'cutoff': args.cutoff, 'rank': args.rank})

    for offset in range(start, len(schedule)):
        batch = {k: torch.tensor([v], device='cuda') for k, v in data[schedule[offset]].items()}
        # Last short accumulation block uses its actual size.
        block_start = offset - (offset - start) % accumulation
        divisor = min(accumulation, len(schedule) - block_start)
        with silent_libraries(), torch.autocast('cuda', dtype=dtype):
            loss = model(**batch).loss
            if not torch.isfinite(loss):
                raise RuntimeError('E_NONFINITE_LOSS')
            (loss / divisor).backward()
        running += loss.detach().float().item()
        if (offset - start + 1) % accumulation == 0 or offset + 1 == len(schedule):
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1., error_if_nonfinite=True)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            mean = running / divisor
            losses.append(mean)
            running = 0.
            progress = {'step': step, 'examples_completed': offset + 1, 'total_examples': len(schedule),
                        'loss': round(mean, 5), 'elapsed_seconds': round(time.monotonic() - started),
                        'gpu_peak_mb': round(torch.cuda.max_memory_allocated() / 1024**2)}
            atomic_json(output / 'progress.json', progress)
            emit('training_progress', **progress)
            if step % 10 == 0 or offset + 1 == len(schedule):
                checkpoint(offset + 1)
        del batch, loss
    adapter = output / 'adapter'
    with silent_libraries():
        model.eval()
        model.save_pretrained(adapter, safe_serialization=True)
        tokenizer.save_pretrained(adapter)
        probe = torch.tensor([data[0]['input_ids'][:32]], device='cuda')
        with torch.inference_mode():
            reference = model(input_ids=probe).logits[:, -1].float().cpu()
        # Reload the actual saved adapter and compare logits on the same base.
        model.load_adapter(str(adapter), adapter_name='reload_check', is_trainable=False)
        model.set_adapter('reload_check')
        with torch.inference_mode():
            reloaded = model(input_ids=probe).logits[:, -1].float().cpu()
        delta = (reference - reloaded).abs().max().item()
        if not torch.isfinite(reloaded).all() or delta > .05:
            raise RuntimeError('E_RELOAD')
        from safetensors.torch import load_file
        tensors = load_file(str(adapter / 'adapter_model.safetensors'))
        if not any('lora_B' in k and t.abs().sum().item() > 0 for k, t in tensors.items()):
            raise RuntimeError('E_UNCHANGED_ADAPTER')
    report = {'complete': True, 'smoke': args.smoke, 'steps': step, 'examples': len(data),
              'excluded_long': len(raw) - len(data), 'epochs': 1 if args.smoke else args.epochs,
              'rank': args.rank, 'learning_rate': 5e-5, 'cutoff': args.cutoff, 'seed': 42,
              'reload_max_logit_difference': delta, 'weights_bytes': (adapter / 'adapter_model.safetensors').stat().st_size,
              'gpu_peak_mb': round(torch.cuda.max_memory_allocated() / 1024**2),
              'seconds': round(time.monotonic() - started), 'mean_train_loss': sum(losses) / len(losses) if losses else None,
              'base_model': json.loads(Path('/models/models.lock.json').read_text())['student'],
              'dataset_sha256': fingerprint, 'human_gold_documents': 0}
    report['dataset_scope'] = json.loads((ROOT / 'reports/dataset.json').read_text()).get('scope', 'whole_document')
    atomic_json(report_path, report)
    emit('training_complete', steps=step, weights_bytes=report['weights_bytes'], reload_verified=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        message = str(error)
        code = message if message.startswith('E_') and len(message) < 40 else 'E_TRAINING'
        for marker, category in [('out of memory', 'E_GPU_MEMORY'), ('CUDA error', 'E_CUDA_RUNTIME'),
                                 ('same device', 'E_DEVICE'), ('dtype', 'E_DTYPE'),
                                 ('grad_fn', 'E_GRADIENT'), ('inplace', 'E_AUTOGRAD')]:
            if marker in message:
                code = category
                break
        import traceback
        line = next((frame.lineno for frame in reversed(traceback.extract_tb(error.__traceback__))
                     if Path(frame.filename).name == 'train.py'), 0)
        failure = {'code': code, 'error_type': type(error).__name__, 'training_code_line': line}
        atomic_json(ROOT / 'reports/training_failure.json', failure)
        emit('failed', **failure)
        sys.exit(1)
