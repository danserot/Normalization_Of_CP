FROM python:3.12-slim
ARG PADDLE_PACKAGE=paddlepaddle
ARG PADDLE_INDEX=https://www.paddlepaddle.org.cn/packages/stable/cpu/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "${PADDLE_PACKAGE}==3.2.1" -i "${PADDLE_INDEX}"
COPY services/vision/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/vision.py ./backend/vision.py
COPY services/vision/app.py ./services/vision/app.py
EXPOSE 8090
CMD ["uvicorn", "services.vision.app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
