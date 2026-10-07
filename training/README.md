# Локальное дообучение чтению КП

> Архив локального inference/training. Активное приложение с 2026-10-07 использует OpenAI API и не запускает Qwen/Paddle/llama.cpp. Инструкции ниже сохраняют прежний workflow и данные; они не описывают текущий production runtime. Актуальная архитектура: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

В приложении реализован редактор разметки с превью и экспортом: см. [ANNOTATION.md](ANNOTATION.md).

## Выбор для этого проекта

Текущий baseline семантического слоя — `Qwen/Qwen3-4B-Instruct-2507`; runtime использует GGUF через llama.cpp. Модель получает каноническую структуру документа и возвращает план колонок/полей. Visual layer PaddleOCR-VL обрабатывает сложные PDF отдельно; её веса не входят в этот процесс дообучения.

Стартовый YAML использует исходную Qwen3-4B-Instruct-2507, LoRA rank 16 и 4-битную загрузку bitsandbytes (QLoRA). Результат — LoRA-адаптер. Для обучения нужны исходные веса Hugging Face, а не runtime GGUF. Контекст 8192 — стартовая настройка, а не подтверждённая вместимость GPU: 8 ГБ может быть недостаточно, особенно при одновременном Vision inference. Сначала проверьте память и длину примеров коротким прогоном. Обучение этой конфигурации здесь не выполнялось.

Native text является предпочтительным источником значений; Vision задаёт структуру и связи. Дообучение семантики исправляет выбор поля/колонки, но не потерянные символы. Разделяйте ошибки чтения, fusion, semantic mapping и проверки источника.

## Программы

1. WSL2 + Ubuntu: отдельное Linux-окружение для обучения. Из текущей сессии `wsl --list --quiet` вернул E_ACCESSDENIED, поэтому наличие рабочего дистрибутива не подтверждено.
2. Python 3.11, отдельный venv, Git и CUDA-сборка PyTorch. Драйвер NVIDIA на Windows уже обнаружен. Проверить CUDA внутри окружения, не ориентироваться только на строку CUDA Version в nvidia-smi.
3. LLaMA-Factory: запуск обучения через браузер, оценка, чат и экспорт.
4. TensorBoard: локальные графики train/eval loss и learning rate. Облачный аккаунт не нужен.
5. `nvidia-smi`: память, загрузка и температура GPU.

LM Studio/Ollama для обучения не нужны. llama.cpp остаётся сервером для приложения.

## Данные и разметка

Практическая стартовая цель, не гарантия качества: 200–500 разных реальных КП, затем расширять по ошибкам. 30–50 документов подходят для проверки процесса. Один документ может дать несколько задач: реквизиты, колонки, проверка схемы. Форматы одного и того же КП не считаются независимыми документами.

Для каждого примера сохраните точный system prompt, точный user payload и вручную проверенный ответ assistant. Production-контракт в `backend/universal.py` — `Plan`, вход собирается `plan_payload()`, инструкция — `PLAN_SYSTEM`. Старые `Layout`/`Requisites` сохранены для ранее собранных датасетов и локального инструментария; они не являются новым production-входом.

Ответы Layout содержат isItems, границы строк, колонки, components и extras. Ответы Requisites содержат fields с field/cell/value. Ответы Plan содержат fields/tables/issues. Не смешивайте эти ответы с итоговым proposal API: это разные контракты.

Редактор сохраняет исходные ячейки, координаты, происхождение native/vision и проверенные поля/таблицы. `kp_plan_train/val/test.json` содержат production `Plan`: один компактный пример на документ, mappings вместо сотен объектов items. Старые `kp_train/val/test.json` содержат `Layout`/`Requisites` для совместимости. В manifest указаны `schemaVersion`, `counts` и `planCounts`. Ответ модели становится обучающим ответом только после проверки человеком; личные замечания и human issues не экспортируются в ответы модели.

Пример структуры Alpaca (синтетический пример реквизитов, system нужно заменить точным prompt из приложения):

```json
[
  {
    "system": "Извлеки реквизиты КП. Верни fields с field, cell и точной цитатой value. Не придумывай значения.",
    "instruction": "[[\"c0\",\"Поставщик: ООО Альфа\"]]",
    "input": "",
    "output": "{\"fields\":[{\"field\":\"supplier\",\"cell\":\"c0\",\"value\":\"ООО Альфа\"}]}"
  }
]
```

Разделите документы на train/validation/test, например 80/10/10, до нарезки на примеры. Группируйте дубликаты и шаблоны поставщика: близкие документы нельзя распределять между обучением и тестом. Включите отсутствующие поля, неверное OCR, продолжения таблиц, несколько валют, неоднозначные суммы и инструкции внутри документа. Не выдумывайте данные. Коммерческие данные и контакты обезличивайте согласованно, сохраняя ссылки и цитаты.

Распакуйте ZIP редактора в `training/data`. Для нового YAML используются `kp_plan_train` и `kp_plan_val`, зарегистрированные в `dataset_info.json`. Test не используйте для подбора параметров. Готового датасета здесь нет; examples/mock_kp — регрессия, не достаточная обучающая выборка.

## Подготовка окружения и запуск

Команды ниже выполняются в Ubuntu/WSL. Сначала установите подходящую CUDA-сборку PyTorch по официальному селектору https://pytorch.org/get-started/locally/ в этом venv. CUDA Toolkit отдельно обычно не нужен для готового wheel; может понадобиться при компиляции расширений.

```bash
python3.11 -m venv ~/venvs/kp-training
source ~/venvs/kp-training/bin/activate
# Установить PyTorch по официальному селектору, затем:
git clone --depth 1 https://github.com/hiyouga/LlamaFactory.git ~/LlamaFactory
cd ~/LlamaFactory
pip install -e .
pip install bitsandbytes tensorboard
pip check
python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"
llamafactory-cli version
git rev-parse HEAD
pip freeze > ~/kp-training-requirements.txt
```

При CUDA unavailable не запускайте обучение. Не устанавливайте драйвер Linux поверх драйвера WSL. Совместимость конкретных установленных пакетов здесь не проверена; сохраните commit и pip freeze для воспроизводимости.

Скопируйте каталог training в Linux-файловую систему, например ~/kp-training, добавьте размеченные данные. После проверки CUDA:

```bash
cd ~/kp-training
llamafactory-cli train qwen_kp_qlora.yaml
```

Перед полным запуском сделайте пробный прогон в копии YAML: max_steps: 10, eval_strategy: "no", save_strategy: "no", отдельный output_dir. Проверьте, что вход и полный ответ помещаются в cutoff_len. При OOM уменьшайте контекст только если все выбранные примеры полностью помещаются, иначе используйте больше памяти или отложите длинные примеры. Молчаливая обрезка source references недопустима. Не запускайте GPU-инференс одновременно с обучением.

Для интерфейса с графиком loss:

```bash
llamafactory-cli webui
```

Откройте адрес из терминала. Выберите Qwen3-4B-Instruct-2507, template `qwen3_nothink`, SFT, LoRA, 4-bit bitsandbytes, rank 16, batch 1, accumulation 16, cutoff 8192, learning rate 0.0001, 2 эпохи. Шаблон зарегистрирован в официальном [LLaMA-Factory](https://github.com/hiyouga/LlamaFactory/blob/main/src/llamafactory/data/template.py); версия установленного инструмента должна его поддерживать. Укажите каталог dataset_dir с dataset_info.json. YAML и UI — два способа одного запуска.

В другом терминале того же venv:

```bash
cd ~/kp-training
tensorboard --logdir output --host 127.0.0.1 --port 6006
```

Откройте http://localhost:6006. В отдельном терминале `nvidia-smi -l 2` показывает GPU. Падение train loss означает обучение на примерах, но не доказывает улучшение чтения новых КП. Если train loss падает, а eval loss растёт, проверьте переобучение и ранние checkpoints.

## Проверка результата и подключение

Сравните базовую и обученную модель на одинаковом отложенном тесте: валидность JSON, точность field/cell/value, границы таблиц и колонки, число неподтверждённых значений, итоговая точность позиций, время документа. Сохраняйте проверки источников в backend. Цель — уменьшить реальные ошибки; loss и несколько удачных ответов недостаточны.

Существующий scripts/quality_audit.py полезен для регрессии форматов, но дополните его новыми отложенными реальными документами. Измерьте скорость и качество после финального квантования тоже.

После обучения: экспортировать и слить LoRA с ТОЧНОЙ исходной моделью в неквантованном формате через LLaMA-Factory/PEFT, преобразовать результат в GGUF средствами llama.cpp, квантовать Q4_K_M и запускать отдельным сервером с alias local. Для слияния нужна дополнительная RAM/диск. Не сливайте адаптер с текущим Q4 GGUF и не заменяйте исходный файл до сравнения. Обновление Compose и экспорт автоматизированным скриптом в этот набор не входят.

Обучение и установка программ обучения не выполнялись. Добавлены редактор разметки с сохранением и экспортом, инструкция, стартовый YAML и регистрация данных. Для обучения нужны проверенный датасет, рабочее CUDA-окружение и короткий пробный прогон.

## Официальные источники

- https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507
- https://github.com/hiyouga/LlamaFactory/blob/main/src/llamafactory/data/template.py
- https://huggingface.co/docs/peft/developer_guides/quantization
- https://llamafactory.readthedocs.io/en/latest/getting_started/data_preparation.html
- https://llamafactory.readthedocs.io/en/latest/getting_started/webui.html
- https://llamafactory.readthedocs.io/en/latest/advanced/monitor.html
