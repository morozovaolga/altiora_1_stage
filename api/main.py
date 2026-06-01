"""FastAPI — единственный операторский интерфейс к пайплайну Document AI.

Загрузка файла, ingest (layout + YOLO / нативные таблицы), структурные чанки.
"""
import json
import shutil
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# Добавляем корень проекта в sys.path, чтобы работали абсолютные импорты скриптов
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from scripts import app_settings
from scripts.text_sanitize import sanitize_utf8_json
from api.services.extract_service import process_extract

app = FastAPI(
    title="Altiora ETL API", 
    description="Микросервис для распознавания и структурного чанкования документов"
)

_katex_dir = ROOT / "katex"
if _katex_dir.is_dir():
    app.mount("/katex", StaticFiles(directory=str(_katex_dir)), name="katex")

_static_dir = ROOT / "api" / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

_ingested_dir = ROOT / "data" / "ingested"
if _ingested_dir.is_dir():
    app.mount("/ingested", StaticFiles(directory=str(_ingested_dir)), name="ingested")


TEMPLATE_PATH = ROOT / "api" / "templates" / "index.html"

@app.get("/", response_class=HTMLResponse)

def index():
    """Отдает веб-интерфейс в браузере (без кэша, чтобы правки шаблона были видны сразу)."""
    return HTMLResponse(
        content=TEMPLATE_PATH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )

def cleanup_source(source_id: str):
    """Удаляет только сырые загруженные файлы. Извлеченный текст и чанки остаются в базе."""
    try:
        shutil.rmtree(ROOT / "data" / "api_uploads" / source_id, ignore_errors=True)
    except Exception as e:
        print(f"Ошибка фоновой очистки {source_id}: {e}")

@app.delete("/cleanup")
def cleanup_api_data():
    """Удаляет все файлы, загруженные через веб-интерфейс (начинающиеся с API-)."""
    deleted = 0
    for folder in ["api_uploads", "ingested", "chunks_structural", "chunks"]:
        p = ROOT / "data" / folder
        if p.exists():
            for item in p.glob("API-*"):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                deleted += 1
    return {"status": "ok", "deleted_items": deleted}

# Ленивая инициализация тяжелой модели YOLO, чтобы API загружался мгновенно
yolo_extractor = None

def get_yolo_extractor():
    global yolo_extractor
    if yolo_extractor is None:
        from scripts.ingest_yolo_doctr import YoloDoctrExtractor
        yolo_extractor = YoloDoctrExtractor()
    return yolo_extractor


@app.on_event("startup")
def preload_models_on_startup():
    """Прогревает тяжелую модель при старте, чтобы не ждать первый запрос."""
    if not app_settings.PRELOAD_YOLO_ON_STARTUP:
        return
    try:
        get_yolo_extractor()
    except Exception as e:
        # Ошибку логируем, но API поднимаем: для dev важнее не блокировать старт.
        print(f"Предзагрузка YOLO не удалась: {e}")

@app.post("/extract")
def extract_document(
    file: UploadFile = File(...), 
    force_yolo: bool = Form(True),  # устар.: маршрут задаётся APIArgs(True, …); из формы используйте parser_mode
    parser_mode: str | None = Form(None),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    source_id = f"API-{uuid.uuid4().hex[:8]}"
    
    upload_dir = ROOT / "data" / "api_uploads" / source_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Используем безопасное имя файла (LibreOffice путается из-за точек в названиях типа 16.11.2021)
    upload_name = file.filename or "document"
    safe_ext = Path(upload_name).suffix.lower()
    file_path = upload_dir / f"document{safe_ext}"
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        result = process_extract(
            root=ROOT,
            source_id=source_id,
            file_path=file_path,
            upload_dir=upload_dir,
            parser_mode=parser_mode,
            filename=upload_name,
            get_yolo_extractor=get_yolo_extractor,
        )
        background_tasks.add_task(cleanup_source, source_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documents")
def list_documents():
    """Возвращает список ID всех обработанных документов."""
    chunks_dir = ROOT / "data" / "chunks_structural"
    if not chunks_dir.exists():
        return {"documents": []}
    
    # Получаем имена файлов без расширения .jsonl
    docs = [f.stem for f in chunks_dir.glob("*.jsonl")]
    return {"documents": sorted(docs)}

@app.get("/documents/{source_id}")
def get_document_chunks(source_id: str):
    """Возвращает чанки конкретного документа для отображения в интерфейсе."""
    chunks_file = ROOT / "data" / "chunks_structural" / f"{source_id}.jsonl"
    if not chunks_file.exists():
        raise HTTPException(status_code=404, detail="Документ не найден в базе")
        
    with open(chunks_file, "r", encoding="utf-8") as f:
        chunks = [json.loads(line) for line in f if line.strip()]

    return sanitize_utf8_json({"source_id": source_id, "chunks": chunks})