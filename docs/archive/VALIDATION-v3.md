# Проверка миграции OpenAI API — 2026-10-07

Текущая версия использует OpenAI Responses API для визуального чтения и semantic Plan. Предыдущая Qwen/Paddle проверка сохранена отдельно в [архивном отчёте v2](archive/VALIDATION-v2.md); её результаты не подтверждают нынешний OpenAI runtime.

## Что подтверждено

- `docker compose --env-file .env.example config --services` содержит только `backend`, `frontend`.
- Подключение обоих архивных GPU/trained overlays также оставляет только `backend`, `frontend`; локальные model/vision сервисы не создаются.
- Active Compose сохраняет `proposal_data` и парольные настройки; backend имеет исходящий HTTPS доступ. API ключ отсутствует в frontend environment.
- Backend requirements не содержат Paddle, Torch, Transformers, llama.cpp, Tesseract или скачивания весов.

| Проверка | Результат | Что подтверждено |
| --- | --- | --- |
| Финальный полный pytest | 190 passed, 4 skipped; 15,89 s | Native parsers, mocked OpenAI transport/vision/semantic, numeric/Office images, evidence, API, annotation/export, очередь, reusable workers и quota errors. |
| `npm run lint` | Успешна | Frontend static checks. |
| `npm run build` | Успешна | Production TypeScript/Vite build; Windows subprocess запуск потребовал sandbox escalation. |
| Основной Compose и архивные overlays | Успешны | Состав из двух сервисов, сохранение volume и API settings. |
| `git diff --check` для deployment/docs | Успешна | Отсутствие whitespace ошибок в изменённых конфигурациях и документации. |
| Playwright browser workflow с mocked API | Успешна | Параллельная загрузка, источники, PNG preview, отсутствующие данные, HTTP 503, timeout и отсутствие API key. |
| HTTP route + настоящий subprocess + локальный Responses stand-in | Успешна | Upload проходит visual и semantic transport, evidence сохраняется, повторная загрузка даёт cache hit, proposal сохраняется и читается из SQLite. |
| Финальный production Docker build и `up -d --remove-orphans` | Успешны; cached rebuild 8,7 s | Собран последний код с quota handling; запущены только frontend/backend, backend healthy, старый Qwen контейнер удалён из runtime. |
| Production health/models и browser без API mocks | Успешны | Health/models возвращают 200, configured=true; начальный экран UI корректен, 0 console errors/warnings. |
| Настоящий OpenAI API smoke | Ограничен HTTP 429 | Провайдер отвечает `credit_balance_exhausted`, type `insufficient_quota`: недостаточно средств/квоты проекта. Успешного live OCR нет. |

Skipped tests требуют явно включённого внешнего Vision runtime. Автотесты используют mocked API; live inference и OCR accuracy этим не подтверждаются. Финальный полный regression и production rebuild выполнены после quota-specific изменений.

`backend/tests/test_openai_pipeline.py` выполняет настоящий backend upload route, reusable subprocess и HTTP transport к локальному synthetic Responses серверу. Это отличается от подмены frontend API, но не вызывает платный OpenAI и не оценивает качество модели. Дополнительно проверены `store: false`, порядок visual/semantic запросов, source evidence, cache reuse без дополнительных provider запросов и сохранение/чтение предложения.

Numeric regression проверяет, что искажённая цифра в synthetic OpenAI ответе относительно digital PDF не считается завершённым visual review. Сравнение состава цифр не измеряет OCR accuracy и не доказывает совпадение каждого числа/ячейки. Для API PDF native words сохраняются внутри backend, а canonical клетки на распознанных страницах остаются API transcription.

В browser проверке исправлен сбой PNG preview при React StrictMode; отсутствующие item значения используют исходную позицию файла для различения `absent`/`unreadable`/`unverified`. Успешное чтение КП без извлекаемых данных показывает отсутствие данных, а не общий сбой. Скриншоты находятся в ignored `output/playwright/`: `openai-image-results.png`, `openai-api-error.png`, `openai-api-timeout.png`, `openai-not-configured.png`, `openai-no-data.png`.

Дополнительно начальный production UI `http://127.0.0.1:8080` проверен в чистой browser-сессии без перехвата API: `/api/session` и `/api/models` возвращают 200, карточка `OpenAI · gpt-5-mini` сообщает о настройке API, dropzone принимает изображения, исходный preview отсутствует до загрузки, console содержит 0 errors и 0 warnings. Скриншот: `output/playwright/actual-docker-initial.png`. Эта проверка подтверждает production UI и связь с backend, а не успешное чтение документа моделью.

Mount подтверждён: `readdocument_proposal_data` -> `/data`; сохранённая база остаётся в прежнем volume. В одном idle snapshot backend занимал 64,66 MiB, frontend — 14,73 MiB. Это наблюдение после запуска, а не peak-memory или throughput benchmark. Локальные model services в active Docker отсутствуют.

После финального rebuild health снова вернул 200 и `configured=true`. Реальная загрузка native КП через production HTTP API вернула 200 с явно частичным результатом: `outcome.state=partial`, `modelUsed=false`, `llmAttempted=true`, `reviewCompleted=false`, `cacheHit=false`. Предупреждение `insufficient_quota` предлагает проверить баланс, лимиты и проект. Исчерпанная квота не повторяется автоматически; такой непроверенный результат не записывается в cache как успешный review. Один diagnostic round trip занял около двух секунд; это ответ с ошибкой квоты, а не performance benchmark успешного OCR.

## Границы подтверждения

Ключ в `.env` присутствует; health показывает `configured=true`. Настоящие обращения к OpenAI выполнены, но получили HTTP 429 с `credit_balance_exhausted` / `insufficient_quota`. Поэтому причина незавершённого live smoke — недостаток средств/квоты проекта. Проверьте [OpenAI API billing](https://platform.openai.com/settings/organization/billing/), баланс, лимиты расходов и проект, которому принадлежит ключ. Исчерпанная квота определяется отдельно от временного rate limit и не вызывает автоматические повторы.

Успешное live OCR и latency benchmark пока не получены. Mocked HTTP проверки и наличие settings не измеряют OCR accuracy, provider latency или применимые API rate limits. После восстановления квоты требуется live smoke с известными ожидаемыми значениями и сравнение на разнообразных human-reviewed КП.

Сохранённая annotation/training data и предложения не удалялись. Локальные model directories и кеши сохранены как архив, без участия в active Compose.
