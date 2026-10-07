# ReadDocument: коммерческие предложения через OpenAI API

React + FastAPI приложение превращает КП в проверяемый JSON и сохраняет предложения в SQLite. Изображения и страницы PDF читает OpenAI Vision, смысл реквизитов и колонок определяет OpenAI через Responses API. Локальные LLM, Paddle и Tesseract в рабочем Docker-окружении не запускаются и не скачиваются.

Отсутствующие данные остаются пустыми или `null`: приложение сообщает, что реквизит не указан в КП. Python сохраняет исходные значения, применяет план ко всем строкам таблиц и проверяет арифметику. Распознанный текст, предупреждения и ссылки на источники доступны для проверки человеком.

## Быстрый запуск

Нужны Docker Desktop с Docker Compose и OpenAI API key. Ключ используется только backend и не попадает в frontend bundle.

1. Если `.env` ещё нет, создайте его из `.env.example`:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Укажите `OPENAI_API_KEY` в `.env`. Существующий `.env` сохраняйте: добавьте новые настройки из примера. Без ключа `POST /api/extract` возвращает `503` с инструкцией настроить `.env`.
3. Запустите приложение:

   ```powershell
   docker compose up -d --build
   ```

4. Откройте `http://127.0.0.1:8080`. Проверка конфигурации: `GET /api/health`; извлечение: `POST /api/extract`, multipart-поле `file`.

`/api/health` показывает конфигурацию провайдера и моделей. Наличие ключа и успешный HTTP health не подтверждают оплату, квоту или качество реального извлечения; это проверяет загрузка документа.

База остаётся в прежнем `proposal_data`. При переходе со старой версии сначала остановите старые `model` и `vision` контейнеры, если они работают, и запустите Compose выше. Сохранённые `models/`, `private_training/`, `training/` и старые model-cache volumes не участвуют в обработке. Не используйте `docker compose down -v`, если нужна сохранённая база.

## Как обрабатывается документ

```text
Документ -> проверка формата и лимитов
  PDF / изображение -> OpenAI Vision -> текст, блоки, таблицы
  DOCX / Excel / текст -> native parser -> точные исходные ячейки
                         |
                  CanonicalDocument
                         |
              OpenAI: план реквизитов и колонок
                         |
               Python: все строки + evidence
                         |
          проверка сумм и пропусков -> JSON + предупреждения
```

| Источник | Обработка |
| --- | --- |
| PDF | Все страницы визуально читаются OpenAI по умолчанию (`VISION_PDF_MODE=always`). Native текст сохраняется для проверки и точных значений. |
| PNG / JPG / JPEG / WEBP / BMP / TIF / TIFF | Изображения, включая TIFF frames, преобразуются в PNG и читаются OpenAI Vision. |
| DOCX | Native текст, таблицы и колонтитулы плюс чтение встроенных растровых изображений OpenAI. |
| XLSX | Native листы, строки, колонки и cached значения плюс встроенные растровые изображения OpenAI. Формулы не пересчитываются. |
| XLS | Native ячейки и OpenAI semantic mapping. Встроенные картинки не проверяются: для них требуется XLSX или PDF, и UI сообщает об ограничении. |
| CSV / TSV / TXT / JSON | Native чтение плюс OpenAI semantic review. JSON должен содержать данные исходного КП. |

Если визуальный API не смог прочитать источник, это явно отражается в ошибке или неполной проверке. Native текст не превращает неудачный Vision в успешное визуальное извлечение. Ошибки ключа, квоты, timeout и ограничений контекста сообщаются отдельно от отсутствующих в КП реквизитов.

Встроенные в Office векторные/неподдерживаемые изображения явно отмечаются как непрочитанные. Лимит одного исходного растрового изображения — 40 миллионов пикселей; число кадров/страниц ограничено `VISION_MAX_PAGES`.

## Скорость и качество

- Обработка PDF-страниц ограниченно параллельна: `OPENAI_VISION_CONCURRENCY=3`. Не требуется загружать веса моделей в RAM/VRAM.
- Вместо генерации JSON для каждой товарной строки OpenAI возвращает компактный semantic Plan; Python применяет mapping к таблице целиком.
- `EXTRACTION_WORKERS=3` позволяет обрабатывать несколько документов; очередь ограничена, а ожидание и обработка имеют отдельные deadlines.
- Завершённые результаты повторных загрузок используют SHA-256 cache с учётом настроек. Ошибки и незавершённые проверки не кешируются как успешные результаты.
- Повторные API запросы ограничены; больше параллелизма не гарантирует меньшую задержку при rate limits. Реальная скорость зависит от длины документа, модели, сети и квоты API.
- Значение принимается при подтверждённой цитате исходной ячейки. Количество/цена не заполняются придуманными `1`/`0`. Альтернативные стоимости сохраняются отдельно, а отсутствующий единый итог не выбирается случайно.
- Confidence — эвристическая оценка качества источников и проверок, а не измеренный процент правильности OCR.

## Настройки

Полный перечень: [.env.example](.env.example). Основные значения:

| Настройка | По умолчанию | Назначение |
| --- | --- | --- |
| `OPENAI_MODEL` | `gpt-5-mini` | Semantic Plan. |
| `OPENAI_VISION_MODEL` | пусто | Использует `OPENAI_MODEL`; можно отдельно выбрать модель с поддержкой изображений и Structured Outputs. |
| `OPENAI_API_URL` | `https://api.openai.com/v1/responses` | Responses endpoint; также допускается API base URL. |
| `OPENAI_TIMEOUT_SECONDS` | `120` | Бюджет одного API вызова с ожиданием слота и повторами, внутри общего deadline. |
| `OPENAI_MAX_RETRIES` | `2` | Максимум повторов HTTP 429/5xx в оставшемся бюджете. |
| `OPENAI_MAX_CONCURRENCY` / `OPENAI_VISION_CONCURRENCY` | `4` / `3` | Ограничение одновременно выполняемых API/visual запросов в рабочем процессе. |
| `OPENAI_VISION_MAX_OUTPUT_TOKENS` | `14000` | Ограничение ответа visual extraction. |
| `OPENAI_REASONING_EFFORT` | `minimal` | Настройка модели для небольшой задержки; должна поддерживаться выбранной моделью. |
| `SEMANTIC_MODEL_MODE` | `always` | Semantic review каждого КП; в Compose закреплён режим API. |
| `SEMANTIC_CONTEXT_CHARS` / `SEMANTIC_MAX_OUTPUT_TOKENS` | `60000` / `8192` | Лимиты semantic request. |
| `VISION_PDF_MODE` | `always` | Визуальное чтение всех PDF-страниц. |
| `VISION_RENDER_DPI` / `VISION_MAX_SIDE` | `144` / `2400` | Разрешение и ограничение размера изображения страницы. |
| `VISION_MAX_PAGES` | `50` | Ограничение страниц; превышение не считается полной проверкой. |
| `EXTRACTION_TIMEOUT_SECONDS` | `300` | Общий deadline обработки одного документа. |
| `EXTRACTION_WORKERS` / `EXTRACTION_QUEUE_LIMIT` | `3` / `8` | Рабочие процессы и bounded queue. |
| `EXTRACTION_QUEUE_WAIT_SECONDS` | `120` | Максимальное ожидание свободного worker. |
| `MAX_FILE_SIZE_MB` | `25` | Лимит файла. |

144 DPI и 2400 px — стартовые настройки проекта. Для мелкого текста увеличивайте разрешение, оценивая качество на своих КП и время ответа. Параллелизм увеличивайте после измерения нагрузки; каждый worker имеет собственные API лимиты.

Документы и изображения передаются OpenAI. Запросы используют `store: false`; этот параметр не означает отсутствие всех служебных retention policies. Подробности API: [Responses](https://platform.openai.com/docs/api-reference/responses), [изображения](https://platform.openai.com/docs/guides/images-vision), [Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs), [работа с данными](https://platform.openai.com/docs/guides/your-data).

## Локальная разработка и проверки

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements-test.txt
.venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --env-file .env
npm ci
npm run dev
```

```powershell
docker build -f backend/Dockerfile.test -t readdocument-backend-test .
docker run --rm --network none readdocument-backend-test
npm run lint
npm run build
docker compose --env-file .env.example config --services
```

Contract tests используют mocked HTTP и не расходуют API quota. Для реального smoke нужен настроенный ключ и документ с известными ожидаемыми значениями:

```powershell
.venv/Scripts/python -m scripts.vision_smoke path/to/proposal.pdf --require-vision --require-semantic --expected-items 4
```

Фактические результаты и ограничения: [docs/VALIDATION.md](docs/VALIDATION.md). Подробные решения и критерии качества: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Разметка и архив локального inference

Рабочее место разметки сохраняет оригинал, ячейки и выбранные evidence; существующая база и training data остаются. Локальное обучение не запускается при загрузке документа или экспорте. Старые Qwen/Paddle/GGUF инструкции, `services/vision/` и training artifacts сохранены как архив прежней архитектуры; текущий Compose ими не пользуется.

`docker-compose.gpu.yml` и `docker-compose.trained.yml` теперь пустые compatibility overlays: их подключение не запускает модели и не требует GPU. Архивные отчёты: [архитектура v2](docs/archive/ARCHITECTURE-v2.md), [проверка v2](docs/archive/VALIDATION-v2.md). Инструкции `training/` описывают исторический local training workflow.
