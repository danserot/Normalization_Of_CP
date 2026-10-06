FROM readdocument-backend:latest
ENV HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 WANDB_DISABLED=true WANDB_MODE=disabled \
    COMET_MODE=DISABLED TOKENIZERS_PARALLELISM=false HF_HUB_DISABLE_PROGRESS_BARS=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer git cmake build-essential curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
RUN pip install --no-cache-dir transformers==4.57.1 peft==0.17.1 accelerate==1.10.1 \
    bitsandbytes==0.48.1 datasets==4.1.1 safetensors==0.6.2 sentencepiece==0.2.1 pytest==8.3.5
WORKDIR /app
RUN pip install --no-cache-dir lm-format-enforcer==0.11.3
