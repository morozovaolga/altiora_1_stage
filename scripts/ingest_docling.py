"""
Ingest документов через Docling с выводом в pages.jsonl.

Поддерживает:
- PDF напрямую
- DOC/DOCX/RTF/TXT через конвертацию в PDF (LibreOffice)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import fitz
import pandas as pd

try:
    from scripts.md_table_postprocess import promote_col_placeholder_table_headers
except ImportError:
    from md_table_postprocess import promote_col_placeholder_table_headers

try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
except ImportError:
    print("Ошибка: Не установлена библиотека docling. Выполните: pip install docling", file=sys.stderr)
    sys.exit(1)


ROOT = Path(__file__).resolve().parents[1]
# Запуск как `python scripts/ingest_docling.py` кладёт в sys.path каталог `scripts/`,
# из-за чего пакет `scripts.*` не находится — добавляем корень репозитория.
_repo_root = str(ROOT)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
DEFAULT_WB = ROOT / "inventory_2026-04-17.xlsx"
DEFAULT_OUT = ROOT / "data" / "ingested"
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\ufffd", " ")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = "\n\n".join(part for part in text.split("\n\n") if part.strip())
    return text.strip()


def convert_office_to_pdf(input_path: Path, tmp_dir: Path) -> Path:
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if not soffice and os.path.exists(r"C:\Program Files\LibreOffice\program\soffice.exe"):
        soffice = r"C:\Program Files\LibreOffice\program\soffice.exe"
    elif not soffice and os.path.exists(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        soffice = r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"
    if not soffice:
        raise RuntimeError("Не найден LibreOffice (soffice) для конвертации office -> pdf.")
    cmd = [str(soffice), "--headless", "--convert-to", "pdf", "--outdir", str(tmp_dir), str(input_path)]
    subprocess.run(cmd, check=True, capture_output=True)
    out = tmp_dir / f"{input_path.stem}.pdf"
    if not out.exists():
        raise RuntimeError("Конвертация в PDF не создала ожидаемый файл.")
    return out


def convert_docling_to_pages(pdf_path: Path, converter: DocumentConverter) -> dict[int, str]:
    conv_res = converter.convert(pdf_path)
    docling_doc = conv_res.document
    pages_content: dict[int, list[str]] = {}

    for item, _ in docling_doc.iterate_items():
        label = item.label.name.lower() if hasattr(item.label, "name") else str(item.label).lower()
        if label in {"page_header", "page_footer"}:
            continue

        text_chunk = ""
        if label == "table":
            try:
                text_chunk = item.export_to_markdown(doc=docling_doc)
            except Exception:
                try:
                    text_chunk = item.export_to_markdown()
                except Exception:
                    text_chunk = ""
            if text_chunk:
                text_chunk = promote_col_placeholder_table_headers(text_chunk.strip())
                text_chunk = "[Таблица]\n" + text_chunk
        elif hasattr(item, "text") and item.text:
            text_chunk = item.text

        text_chunk = normalize_text(text_chunk)
        if not text_chunk:
            continue

        page_no = 1
        if hasattr(item, "prov") and item.prov:
            try:
                page_no = int(item.prov[0].page_no)
            except Exception:
                page_no = 1
        pages_content.setdefault(page_no, []).append(text_chunk)

    out: dict[int, str] = {}
    for p in sorted(pages_content):
        out[p] = "\n\n".join(pages_content[p])
    return out


def process_source(source_id: str, file_path: Path, converter: DocumentConverter, output_root: Path) -> int:
    out_dir = output_root / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    error = None
    pages_written = 0
    parser_name = "docling"
    working_pdf: Path | None = None
    tmp_dir_obj: tempfile.TemporaryDirectory[str] | None = None

    try:
        ext = file_path.suffix.lower()
        if ext in (".xlsx", ".xls", ".csv"):
            # Excel/CSV: все листы через pandas (как native_table в API). Конвертация в PDF даёт LibreOffice часто только активный лист.
            from scripts.ingest_router import excel_or_csv_workbook_to_text_pages

            pages_list = excel_or_csv_workbook_to_text_pages(file_path)
            pages_jsonl = out_dir / "pages.jsonl"
            with pages_jsonl.open("w", encoding="utf-8") as f:
                for i, text in enumerate(pages_list, start=1):
                    rec = {"source_id": source_id, "page": i, "text": text, "parser": "pandas"}
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    pages_written += 1
            parser_name = "pandas"
        else:
            tmp_dir_obj = tempfile.TemporaryDirectory()
            tmp_dir = Path(tmp_dir_obj.name)
            if ext == ".pdf":
                # Docling иногда падает на не-ASCII путях, копируем в temp ASCII path.
                working_pdf = tmp_dir / "input.pdf"
                shutil.copy2(file_path, working_pdf)
            else:
                working_pdf = convert_office_to_pdf(file_path, tmp_dir)

            pages_map = convert_docling_to_pages(working_pdf, converter)
            from scripts.ingest_router import strip_running_headers_footers

            keys = sorted(pages_map.keys())
            texts = [pages_map[k] for k in keys]
            if len(texts) >= 3:
                stripped = strip_running_headers_footers(texts)
                for k, t in zip(keys, stripped):
                    pages_map[k] = t if t.strip() else pages_map[k]

            pages_jsonl = out_dir / "pages.jsonl"
            with pages_jsonl.open("w", encoding="utf-8") as f:
                for pnum in sorted(pages_map):
                    rec = {"source_id": source_id, "page": pnum, "text": pages_map[pnum], "parser": parser_name}
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    pages_written += 1
    except Exception as e:
        error = str(e)
        logging.error(f"[{source_id}] Docling ingest error: {e}")
    finally:
        if tmp_dir_obj:
            tmp_dir_obj.cleanup()

    excel_native = parser_name == "pandas"
    if pages_written == 0 and error is None:
        if excel_native:
            error = "Не удалось прочитать листы таблицы (пусто или без текста)."
        else:
            error = (
                "Docling не вернул ни одного текстового блока (пустой результат на выходе). "
                "Это не обязательно «большой файл»: одна страница с растром/сканом может дать пустой разбор при сбое OCR или фильтрации блоков."
            )
    manifest = {
        "source_id": source_id,
        "file_path": str(file_path.relative_to(ROOT)).replace("\\", "/") if file_path.is_absolute() else str(file_path),
        "format": file_path.suffix.lstrip(".").lower(),
        "status": "ok" if pages_written > 0 else "error",
        "page_count": pages_written,
        "error": error,
        "notes": (
            ["Табличный файл: pandas, все непустые листы (при docling для Excel не используется конвертация в PDF)."]
            if excel_native
            else ["Parsed via Docling (layout+OCR)"]
        ),
        "route": "native_table" if excel_native else "docling",
        "ocr_mode": "off" if excel_native else "on",
        "parser": parser_name,
        "process_time_sec": round(time.perf_counter() - started, 3),
    }
    with (out_dir / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return pages_written


def _build_docling_converter() -> DocumentConverter:
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.ocr_options = EasyOcrOptions(lang=["ru", "en"])
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workbook", type=Path, default=DEFAULT_WB)
    ap.add_argument(
        "--direct-file",
        type=Path,
        default=None,
        help="Один файл без Excel-реестра; требуется ровно один source_id в --only (режим API / одиночный прогон).",
    )
    ap.add_argument("--only", type=str, default="")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    args.output_root.mkdir(parents=True, exist_ok=True)

    if args.direct_file is not None:
        only_ids = {s.strip() for s in args.only.split(",") if s.strip()}
        if len(only_ids) != 1:
            raise SystemExit(
                "С --direct-file укажите ровно один идентификатор в --only (например --only API-f0f2559a)."
            )
        source_id = only_ids.pop()
        direct = args.direct_file.resolve()
        if not direct.is_file():
            raise SystemExit(f"File not found: {direct}")
        if args.resume:
            mp = args.output_root / source_id / "manifest.json"
            if mp.is_file():
                try:
                    man = json.loads(mp.read_text(encoding="utf-8"))
                    if man.get("status") in {"ok", "empty"}:
                        logging.info(f"[{source_id}] Уже обработан (--resume), пропуск.")
                        return
                except Exception:
                    pass
        converter = _build_docling_converter()
        pages = process_source(source_id, direct, converter, args.output_root)
        logging.info(f"Docling (--direct-file) done. source_id={source_id} pages={pages}")
        return

    if not args.workbook.is_file():
        raise SystemExit(f"Workbook not found: {args.workbook}")

    df = pd.read_excel(args.workbook, sheet_name="source_registry")
    only_ids = {s.strip() for s in args.only.split(",") if s.strip()}

    converter = _build_docling_converter()

    processed = 0
    for _, row in df.iterrows():
        src = str(row.get("source_id") or "").strip()
        fp = str(row.get("file_path") or "").strip()
        if not src or not fp or src.lower() == "nan":
            continue
        if only_ids and src not in only_ids:
            continue
        if args.resume:
            mp = args.output_root / src / "manifest.json"
            if mp.is_file():
                try:
                    with mp.open(encoding="utf-8") as f:
                        man = json.load(f)
                    if man.get("status") in {"ok", "empty"}:
                        continue
                except Exception:
                    pass

        abs_path = (ROOT / fp).resolve()
        if not abs_path.is_file():
            logging.warning(f"[{src}] file not found: {abs_path}")
            continue
        pages = process_source(src, abs_path, converter, args.output_root)
        if pages > 0:
            processed += 1
            logging.info(f"[{src}] pages: {pages}")
    logging.info(f"Docling ingest done. processed={processed}")


if __name__ == "__main__":
    main()

