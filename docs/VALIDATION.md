# Проверка миграции OpenAI API — 2026-10-07

Текущая версия использует OpenAI Responses API для визуального чтения и semantic Plan. Предыдущая Qwen/Paddle проверка сохранена отдельно в [архивном отчёте v2](archive/VALIDATION-v2.md); её результаты не подтверждают нынешний OpenAI runtime.

## Что подтверждено

- `docker compose --env-file .env.example config --services` содержит только `backend`, `frontend`.
- Подключение обоих архивных GPU/trained overlays также оставляет только `backend`, `frontend`; локальные model/vision сервисы не создаются.
- Active Compose сохраняет `proposal_data` и парольные настройки; backend имеет исходящий HTTPS доступ. API ключ отсутствует в frontend environment.
- Backend requirements не содержат Paddle, Torch, Transformers, llama.cpp, Tesseract или скачивания весов.

| Проверка | Результат | Что подтверждено |
| --- | --- | --- |
| Полный pytest текущей версии | 168 passed, 4 skipped | Native parsers, mocked OpenAI transport/vision/semantic, evidence, API, annotation/export, очередь и reusable workers. |
| `npm run lint` | Успешна | Frontend static checks. |
| `npm run build` | Успешна | Production TypeScript/Vite build; Windows subprocess запуск потребовал sandbox escalation. |
| Основной Compose и архивные overlays | Успешны | Состав из двух сервисов, сохранение volume и API settings. |
| `git diff --check` для deployment/docs | Успешна | Отсутствие whitespace ошибок в изменённых конфигурациях и документации. |

Skipped tests требуют явно включённого внешнего Vision runtime. Автотесты используют mocked API; live inference и OCR accuracy этим не подтверждаются. Проверка production Docker build и реального HTTP workflow с локальным mocked Responses endpoint выполняется отдельно; её результаты добавляются после завершения.

## Границы подтверждения

Реальный платный OpenAI extraction на этой машине не выполнялся: в текущем `.env` нет `OPENAI_API_KEY`. Mocked HTTP проверки и наличие settings не измеряют OCR accuracy, provider latency или применимые API rate limits. После добавления ключа требуется live smoke с известными ожидаемыми значениями и сравнение на разнообразных human-reviewed КП.

Сохранённая annotation/training data и предложения не удалялись. Локальные model directories и кеши сохранены как архив, без участия в active Compose.
