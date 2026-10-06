"""Merge a verified LoRA into the exact local base, offline; verify reload."""
import json
import sys
from pathlib import Path

from .privacy import atomic_json, emit, require_isolation, silent_libraries


def main():
    require_isolation()
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    root = Path('/private')
    selected = (root / 'selected/training.json').exists()
    report = json.loads((root / ('selected/training.json' if selected else 'reports/training.json')).read_text())
    if not report['complete']:
        raise RuntimeError('E_TRAINING_INCOMPLETE')
    with silent_libraries():
        model = AutoModelForCausalLM.from_pretrained('/models/student', local_files_only=True,
            trust_remote_code=False, torch_dtype=torch.float16, device_map={'': 0})
        model = PeftModel.from_pretrained(model, str(root / ('selected/adapter' if selected else 'training/adapter')), local_files_only=True)
        merged = model.merge_and_unload(safe_merge=True)
        destination = root / 'merged'
        merged.save_pretrained(destination, safe_serialization=True, max_shard_size='1GB')
        tokenizer = AutoTokenizer.from_pretrained('/models/student', local_files_only=True)
        tokenizer.save_pretrained(destination)
        inputs = tokenizer('Синтетическая проверка сохранения.', return_tensors='pt').to('cuda')
        with torch.inference_mode():
            before = merged(**inputs).logits[:, -1].float().cpu()
        del merged, model
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        reloaded = AutoModelForCausalLM.from_pretrained(destination, local_files_only=True,
            trust_remote_code=False, torch_dtype=torch.float16, device_map={'': 0})
        with torch.inference_mode():
            after = reloaded(**inputs).logits[:, -1].float().cpu()
        difference = (before - after).abs().max().item()
        if not torch.isfinite(after).all() or difference > .05:
            raise RuntimeError('E_MERGE_RELOAD')
    atomic_json(root / 'reports/merge.json', {'reload_verified': True, 'max_logit_difference': difference,
                 'bytes': sum(p.stat().st_size for p in destination.glob('*.safetensors'))})
    emit('merge_complete', reload_verified=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('failed', code='E_MERGE', error_type=type(error).__name__)
        sys.exit(1)
