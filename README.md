# Altiora: поставка первого этапа

Папка содержит файлы, относящиеся к первому этапу ТЗ: анализ исходных данных, выбор модели и подхода, ETL-пайплайн, тестирование на данных заказчика и API.

## Состав поставки

- `api/main.py` - FastAPI-прототип для загрузки документов и получения структурированных блоков.
- `scripts/ingest_router.py` - основной маршрутизатор обработки документов.
- `scripts/ingest_yolo_doctr.py` - основной локальный OCR/layout-пайплайн.
- `scripts/ingest_docling.py` - альтернативный парсер для точечных сценариев.
- `scripts/chunk_structural.py` - разбиение извлеченного текста на структурные блоки.
- `scripts/analyze_source_formats.py` - анализ входных форматов из реестра.
- `requirements.txt` - Python-зависимости.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore` - запуск сервиса в Docker.
- `docs/tz_delivery/` - документация в границах ТЗ.
- `docs/api/API.md` - описание HTTP API, полей чанков, **маршрута `route`**, таблиц и склейки между страницами.
- `docs/FAQ.md` - краткий FAQ для заказчика (плашка `route`, таблицы и rowspan, склейка таблиц на двух страницах).
- `docs/README_INGEST_ROUTER.md` - описание ingest-маршрутизатора.

## Что не включено

В поставку не включены рабочие данные, локальные артефакты обработки, Excel-реестры, экспериментальные RAG/eval-скрипты, архивы и служебные файлы разработки. Эти материалы относятся к внутренней разработке или последующим этапам и могут быть переданы по запросу заказчика.

## Запуск API в Docker (подробно)

Нужны установленные **Docker** и **Docker Compose**. Все команды ниже выполняются **из корня этой папки** (`altiora_1_stage`), где лежат `Dockerfile` и `docker-compose.yml`.

### Основная команда

```powershell
docker compose up --build api
```

### Другие команды

```powershell
# Только собрать образ сервиса api, не запускать
docker compose build api

# Запуск без пересборки (быстрее, если образ уже актуален)
docker compose up api

# Остановить и убрать контейнеры/сеть проекта (том с HF-кешем по умолчанию сохранится)
docker compose down

# Если при up ошибка: имя контейнера уже занято — удалить старый контейнер
docker rm -f altiora-api
```

### Docling в Docker

**Docling — не отдельный микросервис**, а Python-библиотека в том же образе (`requirements.txt`). Для обычной работы достаточно основного сервиса **`api`**:

```powershell
docker compose up --build api
```

Откройте **`http://localhost:8000`**, загрузите документ и в форме выберите режим парсера **«Docling»** — поведение то же, что при запуске `uvicorn` из PowerShell без Docker.

**Опционально** можно поднять **второй экземпляр** API с Docling по умолчанию (профиль `docling`, другой порт). Это тот же `Dockerfile` и тот же UI; отличаются только `ALTIORA_DEFAULT_PARSER=docling` и порт на хосте:

```powershell
docker compose --profile docling up --build api-docling
```

После запуска: **`http://localhost:8001`**, контейнер **`altiora-api-docling`**. В выпадающем списке парсера по-прежнему доступны **Авто**, **YOLO** и **Docling**.

Для типовой поставки заказчику **отдельный контейнер не обязателен** — достаточно `api` на порту 8000. Профиль `docling` имеет смысл, если нужен отдельный URL/процесс с Docling по умолчанию (например, выделенный стенд для тяжёлых PDF, пока основной API на 8000 остаётся на «Авто»/YOLO). Оба контейнера делят одну машину и память; изоляции по RAM это не даёт.

**LibreOffice** для конвертации Office→PDF уже в Docker-образе. При запуске без Docker на Windows LibreOffice на хосте нужно установить отдельно.


## Запуск без Docker

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api.main:app --host 0.0.0.0 --port 8000
```
