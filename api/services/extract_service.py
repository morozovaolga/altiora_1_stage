"""Сервисные функции для обработки /extract."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from scripts import app_settings
from scripts.chunk_structural import chunk_source_structural
from scripts.ingest_router import (
    DoclingExtractor,
    LightExtractor,
    decide_route,
    detect_document_profile,
    excel_or_csv_workbook_to_text_pages,
    resolve_soffice_path,
    write_api_native_ingest_no_soffice,
)
from scripts.text_sanitize import sanitize_utf8_json


class APIArgs:
    def __init__(self, force_yolo: bool, parser_mode: str):
        self.force_light = False
        self.force_heavy = force_yolo
        self.parser = parser_mode


def _validate_parser_mode(parser_mode: str | None) -> str:
    default_parser = os.getenv("ALTIORA_DEFAULT_PARSER", "auto")
    mode = (parser_mode or default_parser or "auto").strip().lower()
    if mode not in {"auto", "yolo", "docling"}:
        raise HTTPException(status_code=400, detail="parser_mode должен быть one of: auto|yolo|docling")
    return mode


def _run_docling_ingest(root: Path, source_id: str, profile: Any) -> None:
    extractor = DoclingExtractor()
    docling_pages = extractor.extract(profile)
    ing_dir = root / "data" / "ingested" / source_id
    pages_jsonl = ing_dir / "pages.jsonl"
    manifest_path = ing_dir / "manifest.json"
    docling_failed = (
        docling_pages < 1
        or (extractor.last_returncode is not None and extractor.last_returncode != 0)
        or not pages_jsonl.is_file()
        or pages_jsonl.stat().st_size == 0
    )
    if not docling_failed:
        return

    man: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            man = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            man = {}

    err = (man.get("error") or "").strip()
    if err:
        detail = f"Docling: {err}"
    elif extractor.last_returncode is not None and extractor.last_returncode != 0:
        detail = (
            f"Процесс Docling завершился с кодом {extractor.last_returncode}. "
            "Смотрите вывод сервера (uvicorn) — там обычно точнее (падение нативной части, нехватка памяти и т.д.). "
            "Попробуйте режим «Авто» / «YOLO»."
        )
    else:
        detail = (
            "Docling не дал пригодного текста для чанкования. "
            "Маленький размер файла на диске не означает мало памяти: загружены модели и растр страницы; "
            "в логах при сбое может быть std::bad_alloc даже для одной страницы. "
            "Попробуйте режим «Авто» / «YOLO»; точная причина — в выводе uvicorn и в "
            "data/ingested/<id>/manifest.json (поле error)."
        )
        if manifest_path.is_file() and "bad_alloc" in manifest_path.read_text(
            encoding="utf-8", errors="ignore"
        ).lower():
            detail = (
                "Docling (нативный preprocess): нехватка памяти (std::bad_alloc). "
                "Это не всегда «огромный PDF»: одна страница при нехватке RAM или фоновых процессах тоже возможна. "
                "Попробуйте режим «Авто» / «YOLO», закройте лишние приложения или увеличьте память."
            )
    raise HTTPException(status_code=503, detail=detail)


def _run_native_or_heavy_ingest(
    root: Path,
    source_id: str,
    decision: dict[str, str],
    profile: Any,
    file_path: Path,
    upload_dir: Path,
    get_yolo_extractor: Callable[[], Any],
) -> None:
    if profile.extension in (".xls", ".xlsx", ".csv"):
        src_dir = root / "data" / "ingested" / source_id
        src_dir.mkdir(parents=True, exist_ok=True)
        pages_jsonl = src_dir / "pages.jsonl"
        try:
            page_texts = excel_or_csv_workbook_to_text_pages(file_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Ошибка чтения таблицы: {e}")
        if not page_texts:
            raise HTTPException(status_code=500, detail="В книге нет листов с текстом или таблица пуста.")
        with pages_jsonl.open("w", encoding="utf-8") as f:
            for page_idx, text in enumerate(page_texts, start=1):
                rec = sanitize_utf8_json(
                    {"source_id": source_id, "page": page_idx, "text": text, "parser": "pandas"}
                )
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        decision["route"] = "native_table"
        return

    if profile.extension == ".txt":
        n_pages, _ = write_api_native_ingest_no_soffice(
            source_id, file_path, ".txt", root / "data" / "ingested"
        )
        if n_pages < 1:
            raise HTTPException(status_code=500, detail="Текстовый файл пуст или не прочитан.")
        decision["route"] = "native_txt_api"
        return

    if profile.extension in (".docx", ".doc", ".rtf"):
        soffice = resolve_soffice_path()
        if soffice:
            cmd = [
                str(soffice),
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(upload_dir),
                str(file_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            pdf_path = upload_dir / "document.pdf"
            if not pdf_path.exists():
                raise HTTPException(status_code=500, detail="Не удалось конвертировать документ в PDF.")
            file_path = pdf_path
        elif profile.extension == ".doc":
            raise HTTPException(
                status_code=500,
                detail=(
                    "Формат .doc требует LibreOffice (soffice) для конвертации в PDF. "
                    "Установите LibreOffice или задайте ALTIORA_SOFFICE. "
                    "Либо сохраните документ как .docx и загрузите снова."
                ),
            )
        else:
            n_pages, _ = write_api_native_ingest_no_soffice(
                source_id, file_path, profile.extension, root / "data" / "ingested"
            )
            if n_pages < 1:
                raise HTTPException(
                    status_code=500,
                    detail="Документ пуст после нативного разбора; для полного heavy-пайплайна установите LibreOffice.",
                )
            decision["route"] = "native_office_api"

    skip_yolo = decision.get("route") in ("native_table", "native_txt_api", "native_office_api")
    if not skip_yolo:
        extractor = get_yolo_extractor()
        extractor.extract(source_id, file_path)


def process_extract(root: Path, source_id: str, file_path: Path, upload_dir: Path, parser_mode: str | None, filename: str, get_yolo_extractor: Callable[[], Any]) -> dict[str, Any]:
    profile = detect_document_profile(source_id, file_path)
    mode = _validate_parser_mode(parser_mode)
    decision = decide_route(profile, APIArgs(True, mode))

    if decision["route"] == "light":
        extractor = LightExtractor()
        extractor.extract(profile)
    elif decision["route"] == "docling":
        _run_docling_ingest(root, source_id, profile)
    else:
        _run_native_or_heavy_ingest(
            root=root,
            source_id=source_id,
            decision=decision,
            profile=profile,
            file_path=file_path,
            upload_dir=upload_dir,
            get_yolo_extractor=get_yolo_extractor,
        )

    src_dir = root / "data" / "ingested" / source_id
    if not src_dir.exists():
        raise HTTPException(status_code=500, detail="Ошибка Ingest: текст не извлечен.")

    chunk_source_structural(
        src_dir,
        max_chars=app_settings.CHUNK_MAX_CHARS,
        overlap=app_settings.CHUNK_OVERLAP,
    )

    chunks_file = root / "data" / "chunks_structural" / f"{source_id}.jsonl"
    if chunks_file.exists():
        from scripts.enrich_chunks import process_file

        process_file(chunks_file)

    chunks = []
    if chunks_file.exists():
        with chunks_file.open("r", encoding="utf-8") as f:
            chunks = [json.loads(line) for line in f if line.strip()]

    return sanitize_utf8_json(
        {"source_id": source_id, "filename": filename, "route": decision["route"], "chunks": chunks}
    )
