"""Анализ источников в ``source_registry``: политика ingest по формату файла.

Читает Excel (по умолчанию ``inventory_2026-04-17.xlsx``), для каждой строки выводит рекомендацию:
что делать с файлом **до** или **вместо** текущего ``ingest_extract_pages.py`` (нативный путь, PDF‑канон,
RTF/TXT/XLSX и т.д.).

Пишет машиночитаемый отчёт: ``data/source_format_policy.json`` (UTF‑8).

Политика (кратко):

- **pdf** — оставить как есть; страницы = движок PyMuPDF.
- **docx** — PyMuPDF открывает DOCX, но **номер страницы может не совпадать с Word**; для эталонов «как в Word»
  рекомендуется **PDF, сохранённый из Word**, и обновление ``file_path`` / отдельного поля пути в реестре.
  Альтернатива: оставить DOCX и опираться на **chunk_id + цитату** (без привязки к «листу Word»).
- **doc** (в т.ч. **Word 95** и прочие старые бинарники) — **не** ingest; копирование текста в новый DOCX **не** считается рабочей процедурой
  (теряются колонтитулы, поля, нумерация, объекты). Нужна **официальная конвертация**: «Сохранить как» в Word/LibreOffice в **DOCX или PDF**,
  проверка вёрстки, обновление ``file_path`` в реестре.
- **rtf** — в ingest часто **одна псевдо‑страница** (без ``\\f``) → **несовпадение** эталонных страниц, BM25 и **chunk_id**;
  для рабочего контура **рекомендуется PDF из Word** (или DOCX→PDF) и запись **PDF** в реестр. Запасной вариант: **page=1 + chunk_id** только как временная мера.
- **txt**, лог‑подобные — **не PDF**; нарезка чанками, локаторы **строка/смещение** (страница не используется).
- **xlsx** / **xls** — не «страницы», а **лист + диапазон**; PDF печати таблицы обычно **не** рекомендуется
  как основной путь извлечения (потеря структуры); отдельный ingest таблиц позже.
- **изображения** (jpg/png/heic и т.д.) — в текущем ingest **не разобраны**; целевой контур **OCR → текст/страница**.

Опционально: ``--try-libreoffice`` — для форматов с ``optional_pdf_conversion`` (DOCX, RTF, **DOC**) попытаться
вызвать ``soffice --headless --convert-to pdf`` (если в PATH или стандартном пути есть LibreOffice). Иначе только запись политики в JSON.

Пример::

    python scripts/analyze_source_formats.py
    python scripts/analyze_source_formats.py --workbook inventory.xlsx
    python scripts/analyze_source_formats.py --try-libreoffice --dry-run --only SRC-0049
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WB = ROOT / "inventory_2026-04-17.xlsx"
OUT_JSON = ROOT / "data" / "source_format_policy.json"


def ext_of(fp: str) -> str:
    p = (fp or "").strip().lower()
    if "." not in p:
        return ""
    return p.rsplit(".", 1)[-1]


def normalize_format(fmt: str, fp: str) -> str:
    f = (fmt or "").strip().lower()
    if f and f != "nan":
        return f
    return ext_of(fp)


def policy_for(fmt: str, fp: str) -> dict:
    """Единая таблица политики по формату (и расширению файла)."""
    fmt = normalize_format(fmt, fp)
    e = ext_of(fp)

    base = {
        "format_normalized": fmt or e or "unknown",
        "extension": e,
        "ingest_as_in_registry": True,
        "notes_for_operators": [],
        "recommended_preprocessing": None,
        "page_semantics": None,
        "alternatives": [],
        "optional_pdf_conversion": False,
    }

    if fmt == "pdf" or e == "pdf":
        base["page_semantics"] = "pdf_print_pages_match_pymupdf_index_usually"
        base["recommended_preprocessing"] = "none"
        return base

    if fmt in ("docx",) or e == "docx":
        base["page_semantics"] = "pymupdf_docx_page_index_may_differ_from_word_status_bar"
        base["recommended_preprocessing"] = "export_pdf_from_word_set_registry_to_pdf_for_page_critical_qa"
        base["alternatives"] = [
            "keep_docx_use_chunk_id_and_citation_only",
            "run_link_qa_to_chunks_with_strategy_page_then_global",
        ]
        base["optional_pdf_conversion"] = True
        base["notes_for_operators"].append(
            "Если в golden QA важны «страницы как в Word», заведите PDF из того же документа и укажите его в source_registry."
        )
        return base

    if fmt == "doc" or e == "doc":
        base["ingest_as_in_registry"] = False
        base["recommended_preprocessing"] = "convert_binary_doc_to_docx_or_pdf_via_word_or_libreoffice_then_update_registry"
        base["page_semantics"] = "n_a_until_converted"
        base["optional_pdf_conversion"] = True
        base["alternatives"] = ["libreoffice_headless_convert_if_installed"]
        base["notes_for_operators"].extend(
            [
                "Старый .doc (в т.ч. Word 95): PyMuPDF не открывает; вставка текста в новый док — только черновик, не продакшен.",
                "Целевой файл: DOCX (сохранить как в современном Word) или PDF для стабильных страниц и трассировки.",
            ]
        )
        return base

    if fmt == "rtf" or e == "rtf":
        base["page_semantics"] = "single_pseudo_page_if_no_form_feed_split_breaks_search_and_golden_pages"
        base["recommended_preprocessing"] = "export_pdf_from_word_set_registry_to_pdf_same_as_docx_policy"
        base["alternatives"] = [
            "keep_rtf_temporary_only_use_page_1_and_chunk_id_and_page_then_global_linking",
        ]
        base["optional_pdf_conversion"] = True
        base["notes_for_operators"].extend(
            [
                "RTF без разрывов \\f даёт одну «страницу» и много p1:c* — эталонные номера страниц из печатного вида не совпадают с чанками и ухудшают retrieval/eval.",
                "Для пилота и продакшена: тот же маршрут, что для DOCX — **PDF (или DOCX) из Word** как канон для ingest.",
            ]
        )
        return base

    if fmt == "txt" or e in ("txt", "log", "md", "csv"):
        base["page_semantics"] = "no_print_pages_use_chunk_or_line_offset"
        base["recommended_preprocessing"] = "none_plain_text_chunking"
        base["notes_for_operators"].append("Не конвертировать в PDF для классификации текста; использовать chunk_id / смещение.")
        return base

    if fmt in ("xlsx", "xls") or e in ("xlsx", "xls"):
        base["page_semantics"] = "sheet_row_range_not_print_page"
        base["recommended_preprocessing"] = "dedicated_table_ingest_future_not_pdf_print"
        base["alternatives"] = ["export_csv_per_sheet_for_prototype"]
        base["notes_for_operators"].append(
            "Печать листа в PDF обычно хуже для структуры; лучше отдельный парсер XLSX."
        )
        return base

    if e in ("jpg", "jpeg", "png", "webp", "heic", "tif", "tiff", "bmp", "gif"):
        base["ingest_as_in_registry"] = False
        base["recommended_preprocessing"] = "ocr_pipeline_not_in_current_ingest"
        base["page_semantics"] = "image_one_file_or_multipage_tiff"
        return base

    base["recommended_preprocessing"] = "review_manually"
    base["page_semantics"] = "unknown"
    base["notes_for_operators"].append(f"Формат «{fmt or e}» — проверить поддержку в ingest_extract_pages.py.")
    return base


def find_soffice() -> Path | None:
    for name in ("soffice", "soffice.exe"):
        p = shutil.which(name)
        if p:
            return Path(p)
    win = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
    if win.is_file():
        return win
    return None


def try_libreoffice_pdf(src: Path, out_dir: Path, dry: bool) -> tuple[bool, str]:
    soffice = find_soffice()
    if not soffice:
        return False, "LibreOffice (soffice) не найден в PATH и стандартном пути"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(soffice),
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir.resolve()),
        str(src.resolve()),
    ]
    if dry:
        return True, "dry-run: " + " ".join(cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "exit " + str(r.returncode))[:500]
    return True, f"ok -> {out_dir}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workbook", type=Path, default=DEFAULT_WB)
    p.add_argument("--output", type=Path, default=OUT_JSON)
    p.add_argument("--only", type=str, default="", help="SRC-0049,SRC-0050")
    p.add_argument("--try-libreoffice", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    wb = args.workbook
    if not wb.is_file():
        raise SystemExit(f"нет файла: {wb}")

    df = pd.read_excel(wb, sheet_name="source_registry")
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    rows_out: list[dict] = []
    for _, row in df.iterrows():
        src = str(row.get("source_id", "")).strip()
        fp = str(row.get("file_path", "")).strip()
        fmt = str(row.get("format", "")).strip()
        if not src or src.lower() == "nan":
            continue
        if only and src not in only:
            continue

        pol = policy_for(fmt, fp)
        rec = {
            "source_id": src,
            "file_path": fp,
            "format_column": fmt,
            **pol,
        }
        abs_path = (ROOT / fp).resolve() if fp and not fp.lower().startswith("http") else None

        if args.try_libreoffice and pol.get("optional_pdf_conversion") and abs_path and abs_path.is_file():
            out_pdf_dir = ROOT / "data" / "derived_pdf" / src
            ok, msg = try_libreoffice_pdf(abs_path, out_pdf_dir, args.dry_run)
            rec["libreoffice_conversion"] = {"attempted": True, "ok": ok, "detail": msg}
        elif args.try_libreoffice:
            rec["libreoffice_conversion"] = {"attempted": False, "detail": "skipped_not_applicable_or_missing_file"}

        rows_out.append(rec)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"workbook": str(wb), "generated_policy_rows": rows_out}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(rows_out)} rows -> {args.output}")

    # Краткая сводка в stdout
    by_prep: dict[str, int] = {}
    for r in rows_out:
        k = str(r.get("recommended_preprocessing") or "?")
        by_prep[k] = by_prep.get(k, 0) + 1
    print("--- recommended_preprocessing counts ---")
    for k, v in sorted(by_prep.items(), key=lambda x: -x[1]):
        print(f"  {v}\t{k}")


if __name__ == "__main__":
    main()
