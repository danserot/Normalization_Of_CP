# ReadDocument: извлечение КП моделью

Исходный файл отправляется в Responses API. Модель возвращает готовое КП: реквизиты, товары, прочитанные фрагменты и цитаты. Локальные парсеры, OCR, правила извлечения и semantic Plan в рабочем пути не вызываются.

Python проверяет JSON, допустимость чисел и арифметику. Отсутствующие значения не заполняются. При ошибке API возвращается ошибка, а не результат резервных правил.

## Запуск

1. Создайте `.env` из `.env.example`, если его ещё нет. Существующий файл сохраняйте.
2. Укажите `OPENAI_API_KEY` и `OPENAI_MODEL`.
3. Выполните `docker compose up -d --build`.
4. Откройте http://127.0.0.1:8080.

Предложения и разметка остаются в прежнем volume `proposal_data`. Не удаляйте volume, если нужна база.

## Передача документов

| Формат | Передача |
| --- | --- |
| PDF, DOCX, XLSX, XLS, CSV, TSV, TXT, JSON | Полные исходные байты через `input_file`, без локального извлечения текста и колонок. |
| PNG, JPG, WEBP, BMP, TIFF | Через `input_image`; изображения преобразуются в PNG, все TIFF frames передаются без OCR. |

Передача полного файла не гарантирует полное чтение. По [официальной документации](https://developers.openai.com/api/docs/guides/file-inputs) PDF обрабатывается с текстом и изображениями страниц, DOCX — как текст без встроенных изображений, таблицы — до первых 1000 строк каждого листа. Ограничения показаны в результате. Формулы Excel локально не пересчитываются. Для проверки оформления Word используйте PDF.

`proposal` — готовое КП, `cells` — транскрипция модели, `evidence` — её ссылки и цитаты. Все цитаты имеют `verifiedInSource=false`: они не подтверждены независимым парсером. `coverageComplete=false`, отдельно сохраняется `modelReportedComplete`. Важные значения необходимо сверять с оригиналом.

Новые загрузки редактора разметки используют тот же модельный путь. Роли таблиц подтверждает человек. Существующие записи и экспорт сохранены; автоматическая разметка остаётся черновиком.

## Настройки

- `OPENAI_MODEL=gpt-5-mini`: модель для всего извлечения.
- `OPENAI_PROXY_URL`: необязательный HTTP/HTTPS-прокси для запросов backend к API. В Docker используйте `host.docker.internal` вместо `localhost`, если прокси запущен на компьютере.
- `MODEL_DOCUMENT_MAX_OUTPUT_TOKENS=24000`: лимит готового КП и транскрипции. Обрезанный ответ отклоняется.
- `OPENAI_TIMEOUT_SECONDS=120`, `EXTRACTION_TIMEOUT_SECONDS=300`: deadlines.
- `EXTRACTION_WORKERS=3`, `EXTRACTION_QUEUE_LIMIT=8`: workers и очередь.
- `MAX_FILE_SIZE_MB=25`: лимит оригинала.
- `VISION_MAX_PAGES=50`: лимит кадров многокадровых изображений.

Существующий `.env` может менять defaults. Старые `SEMANTIC_*` и параметры PDF-render больше не управляют рабочим извлечением. Учёт токенов и стоимости сохранён. Кешируются согласованные завершённые ответы с цитатами; кеш не является независимой проверкой.

Ключ используется только backend. Документы передаются внешнему API; установлен `store:false`. Health подтверждает конфигурацию, а не доступность сети или качество модели.

## Проверка соединения

Если соединение с API обрывается, backend возвращает HTTP 503 и безопасные поля `error.code`, `error.stage`, `error.retryable`. Ошибки формата документа возвращаются отдельно, с HTTP 422. Интерфейс показывает причину рядом с файлом и позволяет повторить только неудачные загрузки.

Для проверки сети без API-ключа и документов:

```powershell
.venv/Scripts/python scripts/api_diagnostics.py
Get-Content -Raw -Encoding utf8 scripts/api_diagnostics.py | docker compose exec -T backend python -
```

Скрипт проверяет DNS, HTTPS к API и контрольному сайту, затем TLS 1.2 с проверкой сертификата. HTTP 401 при запросе `/v1/models` без ключа подтверждает доступность API. Сброс TLS до HTTP-ответа означает проблему соединения; он не подтверждает ошибку ключа или файла. Для VPN нужен доступ из backend, включая Docker. После настройки прокси пересоздайте backend: `docker compose up -d backend`.

## Проверки

```powershell
.venv/Scripts/python -m pytest backend/tests/test_model_document.py backend/tests/test_request_schema.py backend/tests/test_provider_errors.py backend/tests/test_annotation_architecture.py backend/tests/test_openai_transport.py backend/tests/test_usage.py backend/tests/test_universal.py backend/tests/test_semantic_plan.py -q
npm run lint
npm run build
```

Тесты используют MockTransport и синтетические ответы. Они не измеряют качество реальной модели. Старые тесты native/Plan-архитектуры сохранены, их fallback-ожидания не являются контрактом нового режима.

[Архитектура](docs/ARCHITECTURE.md), [результаты проверки](docs/VALIDATION.md). Legacy-модули и локальное обучение остаются для истории и старых данных, но не извлекают новые КП.
