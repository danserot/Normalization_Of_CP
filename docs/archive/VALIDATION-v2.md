# Проверка архитектуры v2 — 2026-10-06

Этот отчёт отделяет проверенный код и пользовательский workflow от качества
реального model inference. Рефакторинг не развёрнут поверх существующих
production-контейнеров; исходные локальные изменения и база предложений сохранены.

## Автоматические проверки

| Проверка | Результат | Что подтверждено |
| --- | --- | --- |
| Полный pytest в Linux Docker, без сети | 153 passed, 4 skipped | Native parsers, canonical/fusion, grounding, executor, API, annotation/export, lifecycle Vision worker. |
| Самостоятельная сборка `backend/Dockerfile.test` | Успешна | Не требует предварительно собранного backend image. |
| `npm run lint`, `npm run build` | Успешны | TypeScript/frontend production build. |
| CPU/GPU Compose config и `git diff --check` | Успешны | Конфигурация разбирается; runtime обоих GPU-сервисов одновременно этим не подтверждён. |
| 500 строк синтетической таблицы | 1 mocked semantic call, 500 позиций | Компактный Plan применяется Python ко всей таблице, а не только sampleRows. |

Четыре skipped tests требуют явно включённого реального Vision (`TEST_VISION`).
Contract tests не загружают Paddle weights и не измеряют OCR accuracy.
После последнего полного запуска отдельно перепроверены fusion/worker tests:
19 passed. Предупреждения зависимостей — deprecated Starlette/AnyIO/PyMuPDF API.

## Предоставленный DARA PDF

Native extraction: одна страница, одна таблица, 83 cells, 253 native words.
Многострочные объединённые headers корректно включают обязательный visual review.

Подтверждено в реальном HTTP/browser workflow:

- Четыре услуги «Монтаж системы вентиляции» для блоков 1–4.
- Для каждой услуги видны четыре компонента СМР/ТМЦ, с МБОР/без МБОР.
- `ТОО "EN Construct"` — поставщик; оно не заполняет отсутствующего клиента.
- Количество и единичная цена отсутствуют: `null`, а не придуманные `1`/`0`.
- Дата, номер и срок действия не заполняются строкой итога/суммой.
- Альтернативные общие суммы 142 190 193,14 и 135 418 886,31 сохранены отдельно;
  случайный единый `documentTotal` не выбран.
- Редактирование, подтверждение и сохранение сохраняют nullable значения и
  компоненты; GET сохранённого предложения возвращает четыре услуги.
- В annotation workspace видны оригинальный PDF и выбираемые source cells.
  Автоматическая разметка не объявлена human-reviewed ground truth.

Screenshot: `output/playwright/dara-service-costs-v2.png`.
Тестовая база и PDF-derived JSON находятся только в ignored `runtime/qa-v2`.

## Настоящий semantic inference

GGUF Qwen3-4B-Instruct-2507 Q4_K_M скачан; размер и SHA-256 совпали с README.
На CPU с context 8192 DARA занял около 234 секунд, один model call.
Вернулся schema-valid Plan, но mapping таблицы содержал неподтверждённые названия.
Python отклонил этот mapping, сохранил четыре native/rules услуги и альтернативные
суммы. Проверены 80 evidence entries на неизменённые source ID/excerpts.

Это проверка транспорта, реального GGUF и защит, а не подтверждение высокой
точности Qwen. `reviewCompleted=false`, `semanticReviewRequired=true`.
Спекулятивные `Plan.issues` теперь явно помечены как непроверенные замечания модели.
Редкие длинные реквизиты могут занимать несколько native cells: отдельная quote
по-прежнему проверяется против одной исходной ячейки, без выдуманного продолжения.

## Настоящий Paddle runtime: проверка не завершена

Собран GPU image с официальными Paddle 3.2.1, PaddleOCR 3.6.0 и PaddleX 3.6.1.
Официальные PP-DocLayoutV3/PaddleOCR-VL-1.6 weights загрузились; readiness вернула
200. Реальный первый завершившийся predict выявил отличие `.json`: внутри были
`PaddleOCRVLBlock`, а не только словари. Адаптер исправлен по установленному
официальному коду, добавлена regression-проверка обоих представлений.

После исправления успешный полный `PDF → vision → fusion` smoke пока не получен:
повторный inference не завершался в установленный timeout. В последнем изолированном
запуске диагностическое снятие стека показало `paddle.nn.functional.linear`, затем
процесс завершился с SIGSEGV/exit 139, без OOM. Диагностический сигнал мог вызвать
само падение; это не доказательство причины первоначального незавершённого inference
или несовместимости CUDA. Экспериментальный signal handler удалён из приложения.

Readonly проверка установленного PaddleX показала, что предупреждение о пустом
`paddle_dynamic` config bucket не доказывает потерю GPU device: factory затем
применяет `normalize_engine_config(device=...)` и `PaddleDynamicEngine._apply_device`.
Несуществующий публичный `vl_rec_engine_config` не добавлялся как «исправление».
Для выяснения исходной причины нужны реальные tensor place/dtype и GPU metrics
во время inference; метрики после выхода процесса этого не показывают.

Поэтому `--require-vision` закономерно завершился ошибкой. Native данные не потеряны:
результат явно `fallback`, `visionComplete=false`, `coverageComplete=false`.
Нельзя заявлять, что полное качество Vision подтверждено.

Оборудование: 16 GiB host RAM, около 7,75 GiB RAM + 2 GiB swap доступны Docker/WSL,
RTX 3070 Laptop с 8 GiB VRAM. Одновременная загрузка 32k-context Qwen и Vision
ранее привела к OOM semantic QA-контейнера. В Compose загрузки теперь сериализованы,
но это не увеличивает физически доступную память и не доказывает достаточность
лимитов для двух работающих моделей. Глобальные WSL/driver настройки не менялись.

Следующий критерий приёмки — успешный `scripts.vision_smoke --require-vision
--require-semantic` на целевом runtime, затем human-reviewed benchmark разнообразных
КП. До этого GPU deployment и качество полного visual pipeline остаются
неподтверждёнными; допустимая деградация явно отражается в API/UI.

## Native performance smoke

`scripts/benchmark.py` запущен с выключенными моделями на 13 файлах `examples/mock_kp`:
10 обработаны частично, 3 отклонены; 9 имеют подтверждённое покрытие строк.
Из трёх ошибок две — audit JSON, не являющиеся исходными КП, одна — scan PDF без
Vision. P50 22,5 ms, P95 57 ms. Это latency/fallback smoke, не процент точности.
Результаты: `runtime/qa-v2/benchmark-native.json` и `.csv`.

## Непокрытый scope

DOCX visual rendering, standalone image uploads, пересчёт Excel формул,
калиброванная accuracy confidence и полноценная оценка OCR на размеченном корпусе
не реализованы. Native DOCX/XLSX и text-only semantic training остаются рабочими;
export не обучает visual модель и не запускает fine-tuning автоматически.
