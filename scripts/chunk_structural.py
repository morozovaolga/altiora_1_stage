"""Чанкование текстов из ``data/ingested/<SRC>/pages.jsonl`` по структурным блокам (абзацам).

Идея: Вместо "слепого" скользящего окна по всей странице, мы разбиваем текст страницы на
логические блоки (абзацы) по двойному переводу строки (``\\n\\n``).
Слишком короткие абзацы склеиваются, чтобы избежать мусорных микро-чанков.
Слишком длинные абзацы (превышающие ``max_chars``) режутся окном как фоллбэк.

Поля в выходном JSONL (совместимо со схемой):
- ``chunk_id`` — уникальный внутри документа, здесь: ``"<doc_id>:p<page>:b<idx>"``
- ``doc_id`` — идентификатор документа
- ``page`` / ``page_end`` — страница
- ``section_title`` — текст активного заголовка секции
- ``text`` — текст чанка (структурного блока)
- ``source_ref`` — ``"<doc_id>:<page>:<chunk_id>"``
- ``block_type`` — тип блока (header, paragraph, table, list, other)
- ``block_level`` — уровень заголовка (число или null)
- ``parent_header_id`` — chunk_id родительского заголовка

Результат сохраняется в отдельную директорию: ``data/chunks_structural/<doc_id>.jsonl``,
чтобы не ломать основной пайплайн (``data/chunks/``).

Пример запуска::

    python scripts/chunk_structural.py --only SRC-0046,SRC-0005
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from scripts.text_sanitize import sanitize_utf8_json
except ImportError:  # запуск как scripts/chunk_structural.py
    from text_sanitize import sanitize_utf8_json

try:
    from scripts.md_table_postprocess import md_github_separator_line
except ImportError:
    from md_table_postprocess import md_github_separator_line

ROOT = Path(__file__).resolve().parents[1]
INGEST = ROOT / "data" / "ingested"
OUT_DIR = ROOT / "data" / "chunks_structural"

LIST_ITEM_RE = re.compile(r"^([*\-•]|\d+(?:\.\d+)*[.)]|[а-яА-ЯёЁa-zA-Z][.)])\s")
NUMBERED_POINT_RE = re.compile(r"^\d{1,3}[.)]\s+[А-ЯЁA-Zа-яёa-z]")
ROMAN_SECTION_RE = re.compile(r"^[IVXLCDM]+\.\s+[А-ЯЁA-Z]", re.IGNORECASE)
SECTION_KEYWORD_RE = re.compile(r"^(глава|раздел|приложение|часть)\s+[\dА-Яа-яЁёA-Za-z]+", re.IGNORECASE)


def normalize_ocr_text(text: str) -> str:
    """Локальная нормализация очевидных OCR-артефактов без изменения смысла."""
    text = re.sub(r"(?m)^(\d{1,3}),\s+([А-ЯЁA-Zа-яёa-z])", r"\1. \2", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    # Частый OCR-артефакт в юридических ссылках: "Собрание законодательства ... с. N 26, 3387"
    text = re.sub(r"(?i)(собрание законодательства российской федерации[^.\n]{0,220}),\s*с\.\s*N\s*(\d+),\s*(\d+)", r"\1, N \2, ст.\3", text)
    text = re.sub(r"(?i)(собрание законодательства российской федерации[^.\n]{0,220}),\s*N\s*(\d+),\s*(\d+)", r"\1, N \2, ст.\3", text)
    return text


def is_dangling_end(text: str) -> bool:
    """Эвристика: обрыв в конце блока (продолжение в следующем)."""
    t = text.strip()
    if not t:
        return False
    if t.endswith((".", ":", ";", "!", "?", ")")):
        return False
    if len(t) > 220:
        return False
    return bool(re.search(r"(в|и|с|по|для|о|об|от|при|на|к)$", t, re.IGNORECASE) or t[-1].islower())


def is_dangling_start(text: str) -> bool:
    """Эвристика: блок выглядит как продолжение предыдущего."""
    t = text.strip()
    if not t:
        return False
    if LIST_ITEM_RE.match(t):
        return False
    return bool(re.match(r"^[а-яёa-z(\")\],]", t))


def _split_numbered_lines_plain(text: str) -> str:
    """Нумерованные пункты в обычном тексте (не внутри markdown-таблицы)."""
    if not text:
        return text
    # "... . 5. Текст" -> "... .\n5. Текст"
    text = re.sub(r"(?<!\n)([.;:])\s+(\d{1,3}[.)]\s+[А-ЯЁA-Zа-яёa-z])", r"\1\n\2", text)
    # "... : а) Текст" -> "... :\nа) Текст" (не для «Шекспира: О. Генри» в ячейке таблицы)
    text = re.sub(r"(?<!\n)(:)\s+([а-яА-ЯёЁa-zA-Z][.)]\s+[А-ЯЁA-Zа-яёa-z])", r"\1\n\2", text)
    return text


def split_numbered_lines(text: str) -> str:
    """Разделить нумерованные пункты; тело [Таблица]|...| не трогаем."""
    if not text:
        return text
    if "[таблица]" not in text.lower():
        return _split_numbered_lines_plain(text)
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"\[Таблица\]\s*\n", text, flags=re.I):
        if m.start() > pos:
            out.append(_split_numbered_lines_plain(text[pos : m.start()]))
        tail = text[m.start() :]
        nxt = re.search(r"\n(?=#\s)", tail)
        if nxt:
            out.append(tail[: nxt.start()])
            pos = m.start() + nxt.start()
        else:
            out.append(tail)
            pos = len(text)
            break
    if pos < len(text):
        out.append(_split_numbered_lines_plain(text[pos:]))
    return "".join(out)


def repair_split_markdown_table_rows(text: str) -> str:
    """Склеить строки таблицы, ошибочно разорванные переносом (в т.ч. после split_numbered_lines)."""
    if not text or "|" not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if out:
            ps = out[-1].rstrip()
            if (
                ps.startswith("|")
                and not ps.endswith("|")
                and "|" in s
                and not s.startswith("#")
                and not re.match(r"^\[Таблица\]", s, re.I)
                and not s.startswith("|")
            ):
                out[-1] = ps + " " + s
                continue
        out.append(line)
    return "\n".join(out)


def _should_skip_pdf_line_heuristics(paragraph: str) -> bool:
    """Markdown / Excel-таблица: не применять PDF-эвристики построчного разбиения."""
    t = (paragraph or "").strip()
    if not t:
        return False
    if t.lower().startswith("[таблица]"):
        return True
    if _looks_like_markdown_table_block(t):
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 2:
        pipe_rows = sum(1 for ln in lines if _is_markdown_pipe_row_line(ln))
        if pipe_rows >= max(2, int(len(lines) * 0.55)):
            return True
    return False


def is_micro_fragment(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t == "[Таблица]":
        return True
    if t.startswith("#") or t.startswith("[Таблица]") or is_formula_block_text(t):
        return False
    words = re.findall(r"\S+", t)
    if len(words) <= 2:
        # Не считаем "микро", если это чисто номер формулы/пункта.
        if re.fullmatch(r"[\(\[]?\d+(?:\.\d+)*[\)\]]?", t):
            return False
        return True
    return False


def is_formula_block_text(text: str) -> bool:
    """Формула хранится как один блок: снимок из assets/formulas или служебный маркер."""
    t = (text or "").strip()
    return bool(
        "$$" in t
        or "assets/formulas/" in t
        or t.startswith("[Формула")
    )


def chunk_quality_flags(text: str, block_type: str, conf: float) -> dict:
    """Флаги качества чанка для downstream-пайплайнов."""
    t = text.strip()
    start_cont = is_dangling_start(t)
    end_cont = is_dangling_end(t)
    is_continuation = bool(start_cont and not LIST_ITEM_RE.match(t))
    is_truncated = bool(end_cont or re.search(r"[а-яА-ЯёЁa-zA-Z]-\s*$", t))
    suspicious_legal_ref = bool(
        re.search(r"(?i)собрание законодательства российской федерации", t)
        and re.search(r"(?i)\bс\.\s*N\b", t)
    )
    needs_review = bool(
        conf < 0.75 or is_truncated or suspicious_legal_ref or (block_type == "header" and NUMBERED_POINT_RE.match(t))
    )
    return {
        "is_continuation": is_continuation,
        "is_truncated": is_truncated,
        "needs_review": needs_review,
    }


def merge_continuation_into_previous(rows: list[dict], max_chars: int) -> list[dict]:
    """
    Вливает чанк с is_continuation в предыдущий, если тот же doc_id и длина ≤ max_chars.
    Текст: paragraph/list/other между собой; formula только с formula (не смешивать с абзацем).
    Заголовки (header) не склеиваются — продолжение после заголовка остаётся отдельным чанком.
    Два подряд table с тем же doc_id: склейка продолжения разрезанной markdown-таблицы
    (is_continuation или следующий фрагмент начинается с | без нового [Таблица] и без строки ---),
    либо следующий чанк снова с [Таблица], но строки совпадают с началом предыдущего (повтор шапки
    на новой странице Docling) — общий префикс отрезается. Лимит длины для склейки таблиц выше max_chars абзаца.
    """
    allowed_after = {"paragraph", "list", "other", "formula"}
    if len(rows) < 2:
        return rows
    out: list[dict] = [rows[0]]
    for curr in rows[1:]:
        prev = out[-1]
        if _table_chunks_mergeable(prev, curr, max_chars):
            prev_text = str(prev.get("text") or "")
            curr_text = str(curr.get("text") or "")
            tail = _split_duplicate_table_header_tail(prev_text, curr_text)
            if tail is not None:
                curr_norm = tail
            else:
                curr_norm = re.sub(r"^\s*\[Таблица\]\s*\n?", "", curr_text.strip(), flags=re.I)
            merged = f"{prev_text.rstrip()}\n{curr_norm}"
            conf = min(float(prev.get("structure_confidence", 0.9)), float(curr.get("structure_confidence", 0.9)))
            prev["text"] = merged
            prev["page_end"] = max(int(prev.get("page_end", prev["page"])), int(curr.get("page_end", curr["page"])))
            prev["structure_confidence"] = conf
            prev["block_type"] = "table"
            prev["block_level"] = None
            prev.update(chunk_quality_flags(merged, "table", conf))
            prev["entities"] = list(prev.get("entities") or []) + list(curr.get("entities") or [])
            continue

        prev_text = str(prev.get("text") or "")
        curr_text = str(curr.get("text") or "")
        same_doc = prev.get("doc_id") == curr.get("doc_id")
        pt = str(prev.get("block_type") or "")
        ct = str(curr.get("block_type") or "")
        if pt == "header" or ct == "header":
            types_ok = False
        elif pt == "formula" or ct == "formula":
            types_ok = pt == "formula" and ct == "formula"
        else:
            types_ok = pt in {"paragraph", "list", "other"} and ct in {"paragraph", "list", "other"}
        cont = bool(curr.get("is_continuation"))
        size_ok = len(prev_text) + len(curr_text) + 1 <= max_chars
        if cont and same_doc and types_ok and size_ok:
            merged = f"{prev_text.rstrip()} {curr_text.strip()}"
            meta = classify_block(merged)
            btype = meta["type"]
            if btype not in allowed_after:
                out.append(curr)
                continue
            conf = min(float(prev.get("structure_confidence", 0.9)), float(meta["conf"]), 0.85)
            prev["text"] = merged
            prev["page_end"] = max(int(prev.get("page_end", prev["page"])), int(curr.get("page_end", curr["page"])))
            prev["block_type"] = btype
            prev["block_level"] = meta["level"]
            prev["structure_confidence"] = conf
            prev.update(chunk_quality_flags(merged, btype, conf))
            prev["entities"] = list(prev.get("entities") or []) + list(curr.get("entities") or [])
            fv_prev = prev.get("formula_vars")
            fv_curr = curr.get("formula_vars")
            if fv_curr:
                prev["formula_vars"] = f"{fv_prev}\n{fv_curr}".strip() if fv_prev else str(fv_curr)
        else:
            out.append(curr)
    return out


def _is_markdown_pipe_row_line(line: str) -> bool:
    """Строка markdown-таблицы GFM: начинается с | и содержит ещё хотя бы один разделитель."""
    s = (line or "").strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_markdown_table_fragment(text: str) -> bool:
    """Фрагмент таблицы без шапки: одна или несколько pipe-строк (в т.ч. одна длинная строка Excel)."""
    t = re.sub(r"^\s*\[Таблица\]\s*\n?", "", (text or "").strip(), flags=re.I)
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return False
    return all(_is_markdown_pipe_row_line(ln) for ln in lines)


def _looks_like_markdown_table_block(text: str) -> bool:
    """Таблица в markdown-стиле: несколько строк | ... | (не по слову «таблица» в тексте)."""
    t = text.lstrip("\ufeff").strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    pipe_rows = sum(1 for ln in lines if _is_markdown_pipe_row_line(ln))
    return pipe_rows >= max(2, int(len(lines) * 0.55))


# Docling и др. часто повторяют один и тот же markdown-шапку на каждой странице;
# склейка «таблица + таблица» должна допускать больший суммарный размер, чем max_chars абзаца.
_TABLE_MERGE_CHAR_CAP = 500_000


def _md_table_lines_strip_label(text: str) -> list[str]:
    """Строки тела markdown-таблицы без префикса [Таблица] (для сравнения префиксов)."""
    t = re.sub(r"^\s*\[Таблица\]\s*\n?", "", (text or "").strip(), flags=re.I)
    return [ln.rstrip() for ln in t.splitlines()]


def _split_duplicate_table_header_tail(prev_text: str, curr_text: str) -> str | None:
    """
    Если curr повторяет начало prev (типично: та же шапка на следующей странице PDF),
    возвращает только «хвост» curr без повторяющегося префикса — его и нужно дописать к prev.
    Иначе None (склейка по старым правилам).
    """
    pl = _md_table_lines_strip_label(prev_text)
    cl = _md_table_lines_strip_label(curr_text)
    if len(pl) < 2 or len(cl) < 2:
        return None
    k = 0
    while k < len(pl) and k < len(cl) and pl[k] == cl[k]:
        k += 1
    if k < 2:
        return None
    head = cl[:k]
    if not any(md_github_separator_line(x) for x in head):
        return None
    if k >= len(cl):
        return ""
    return "\n".join(cl[k:])


def _table_chunks_mergeable(prev: dict, curr: dict, max_chars: int) -> bool:
    if str(prev.get("block_type") or "") != "table":
        return False
    curr_text = str(curr.get("text") or "")
    curr_bt = str(curr.get("block_type") or "")
    if curr_bt != "table" and not _is_markdown_table_fragment(curr_text):
        return False
    if prev.get("doc_id") != curr.get("doc_id"):
        return False
    prev_text = str(prev.get("text") or "")
    eff = max(int(max_chars), _TABLE_MERGE_CHAR_CAP)
    tail = _split_duplicate_table_header_tail(prev_text, curr_text)
    if tail is not None:
        return len(prev_text) + len(tail) + 2 <= eff
    if len(prev_text) + len(curr_text) + 2 > eff:
        return False
    if bool(curr.get("is_continuation")):
        return True
    ct = curr_text.strip()
    if ct.lower().startswith("[таблица]"):
        return False
    if not ct.startswith("|"):
        return False
    first_ln = ct.split("\n", 1)[0].strip()
    if md_github_separator_line(first_ln):
        return False
    return True


def _split_markdown_table_by_rows(text: str, max_chars: int) -> list[str]:
    """Режем длинную markdown-таблицу только по переводам строк между строками."""
    lines = text.splitlines()
    if not lines:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    cur = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and cur + add > max_chars:
            chunks.append("\n".join(buf))
            buf = [line]
            cur = len(line)
        else:
            buf.append(line)
            cur += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks if chunks else [text]


def split_text_fallback(text: str, max_chars: int, overlap: int) -> list[str]:
    """Фоллбэк: разрезать слишком длинный блок окном с перекрытием."""
    if len(text) <= max_chars:
        return [text]
    if _looks_like_markdown_table_block(text):
        return _split_markdown_table_by_rows(text, max_chars)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            min_break = max(start + int(max_chars * 0.6), start + 1)
            search_window = text[min_break:end]
            split_offsets = [
                search_window.rfind("\n\n"),
                search_window.rfind("\n"),
                search_window.rfind(". "),
                search_window.rfind("; "),
                search_window.rfind(", "),
                search_window.rfind(" "),
            ]
            best_offset = max(split_offsets)
            if best_offset > 0:
                end = min_break + best_offset + 1
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - overlap)
        while start < len(text) and text[start].isspace():
            start += 1
    return chunks

def is_header(p: str) -> bool:
    """Эвристика: похож ли абзац на заголовок?"""
    # Анти-регресс 5: Склеивание переносов OCR
    p_norm = re.sub(r'([а-яА-ЯёЁa-zA-Z]+)-\s*\n\s*([а-яА-ЯёЁa-zA-Z]+)', r'\1\2', p)
    p_strip = p_norm.strip()
    
    if not p_strip:
        return False
        
    if len(p_strip) > 250:
        return False
        
    lines = [l.strip() for l in p_strip.split('\n') if l.strip()]
    
    # Анти-регресс 6: Не плодить "header" пачками
    if len(lines) > 5:
        return False
        
    # Анти-регресс 1: Оглавление (TOC)
    toc_lines = sum(1 for l in lines if re.search(r'(?:\.{3,}|…|\s{4,})\s*\d+$', l) or re.search(r'\bСтр\.\s*\d+$', l, re.IGNORECASE))
    if toc_lines > 0:
        return False
        
    # Анти-регресс 2: "Таблица 1.1"
    if p_strip.lower().startswith("таблица"):
        return False
        
    # Анти-регресс 3: Служебный шум
    noise_pattern = r'^(методика|страница\s*\d+|консультантплюс|источник\s*публикации|\d+)$'
    if len(p_strip) < 40 and re.match(noise_pattern, p_strip, re.IGNORECASE):
        return False
        
    # Анти-регресс 4: Нумерация внутри абзаца
    if re.match(r'^[а-яёa-z]', p_strip):
        return False
        
    if is_formula_block_text(p_strip) or p_strip.lower().startswith("где ") or p_strip.lower().startswith("в котором "):
        return False
        
    # Эвристики распознавания заголовков
    if p_strip.isupper() and len(p_strip) < 150:
        return True
    if NUMBERED_POINT_RE.match(p_strip):
        return False
    if re.match(r'^(\d+(?:\.\d+){1,})\.?\s+[A-ZА-ЯЁ]', p_strip):
        return True
    if ROMAN_SECTION_RE.match(p_strip):
        return True
    if SECTION_KEYWORD_RE.match(p_strip):
        return True
    if 10 < len(p_strip) < 100 and not p_strip.endswith(('.', ':', ';', '!', '?')):
        return True
    return False

def get_header_level(text: str) -> int | None:
    """Определить уровень заголовка по нумерации (1. -> 1, 1.1. -> 2)."""
    text_strip = text.strip()
    m = re.match(r'^(\d+(?:\.\d+)*)\.?\s', text_strip)
    if m:
        return len(m.group(1).split('.'))
    if ROMAN_SECTION_RE.match(text_strip):
        return 1
    if text_strip.lower().startswith(("приложение", "глава", "раздел", "часть")):
        return 1
    return None

def classify_block(text: str) -> dict:
    """Эвристическая классификация структурного блока."""
    text_strip = text.strip()
    if not text_strip:
        return {"type": "other", "level": None, "conf": 0.0}
        
    if is_formula_block_text(text_strip):
        return {"type": "formula", "level": None, "conf": 0.95}
        
    lines = [l.strip() for l in text_strip.split('\n') if l.strip()]

    pipe_rows = sum(1 for l in lines if _is_markdown_pipe_row_line(l))
    # Продолжение Excel/markdown после split_text_fallback: одна очень длинная | row |
    if lines and pipe_rows == len(lines):
        return {"type": "table", "level": None, "conf": 0.8 if len(lines) > 1 else 0.76}

    if len(lines) >= 3 and pipe_rows >= max(3, int(len(lines) * 0.55)):
        return {"type": "table", "level": None, "conf": 0.82}
    if len(lines) >= 2 and pipe_rows >= 2 and pipe_rows >= int(len(lines) * 0.4):
        return {"type": "table", "level": None, "conf": 0.78}

    # Префикс [Таблица] из ingest — только если ниже реально есть pipe-разметка
    if text_strip.lower().startswith("[таблица]"):
        body = re.sub(r"^\s*\[таблица\]\s*", "", text_strip, flags=re.I).strip()
        blines = [l.strip() for l in body.split("\n") if l.strip()] if body else []
        bpipe = sum(1 for l in blines if _is_markdown_pipe_row_line(l))
        if blines and bpipe >= max(2, int(len(blines) * 0.45)):
            return {"type": "table", "level": None, "conf": 0.88}

    digit_ratio = sum(1 for c in text_strip if c.isdigit()) / max(len(text_strip), 1)
    short_lines_ratio = sum(1 for l in lines if len(l) < 40) / max(len(lines), 1)

    if len(lines) > 4 and short_lines_ratio > 0.6 and digit_ratio > 0.05 and pipe_rows >= 2:
        return {"type": "table", "level": None, "conf": 0.7}
        
    list_markers = sum(1 for l in lines if LIST_ITEM_RE.match(l))
    inline_list_markers = len(re.findall(r'\n\s*\d+\)\s+', text_strip))
    if len(lines) > 2 and list_markers / len(lines) >= 0.5:
        return {"type": "list", "level": None, "conf": 0.8}
    if inline_list_markers >= 2:
        return {"type": "list", "level": None, "conf": 0.75}
        
    if is_header(text_strip):
        return {"type": "header", "level": get_header_level(text_strip), "conf": 0.9}
    if NUMBERED_POINT_RE.match(text_strip):
        return {"type": "paragraph", "level": None, "conf": 0.8}
        
    if len(text_strip) > 60:
        conf = 0.9
        if is_dangling_start(text_strip) or is_dangling_end(text_strip):
            conf = 0.7
        return {"type": "paragraph", "level": None, "conf": conf}
        
    return {"type": "other", "level": None, "conf": 0.5}

def extract_structural_blocks(text: str, min_chars: int = 300, max_chars: int = 2500, overlap: int = 200) -> list[str]:
    """
    Разбиваем текст на логические блоки.
    1. Сплит по \n\n (и вариантам с пробелами).
    1.5. Для текстов с одинарными \n (из PDF) эвристически отщепляем заголовки.
    2. Эвристика заголовков: выделяем их в отдельные блоки.
    3. Склейка мелких абзацев до достижения min_chars.
    4. Разрезка гигантских абзацев (если больше max_chars) фоллбэком.
    """
    if not text.strip():
        return []

    text = repair_split_markdown_table_rows(text)
    
    # 1. Разбиваем по двойным (или более) переводам строк
    raw_paragraphs = re.split(r'\n\s*\n', text)
    raw_paragraphs = [p.strip() for p in raw_paragraphs if p.strip()]
    
    # 1.5 Эвристика для PDF без двойных переводов строк (как SRC-0021)
    split_paragraphs = []
    for p in raw_paragraphs:
        if _should_skip_pdf_line_heuristics(p):
            split_paragraphs.append(p)
            continue
        if '\n' in p and len(p) > 150:
            lines = p.split('\n')
            sub_blocks = []
            current_block_lines = []
            
            for line in lines:
                ls = line.strip()
                if not ls:
                    continue
                    
                is_numbered_header = bool(re.match(r'^(\d+(?:\.\d+){1,})\.?\s+[A-ZА-ЯЁ]', ls))
                is_upper_header = ls.isupper() and 5 < len(ls) < 150 and not re.fullmatch(r'(?i)(методика|страница\s*\d+|\d+|источник\s*публикации)', ls)
                is_word_header = bool(SECTION_KEYWORD_RE.match(ls))
                is_roman_header = bool(ROMAN_SECTION_RE.match(ls))
                    
                is_strong_header = is_numbered_header or is_upper_header or is_word_header or is_roman_header
                
                if is_strong_header:
                    if current_block_lines:
                        curr_text = '\n'.join(current_block_lines)
                        if not is_header(curr_text):
                            # Текущий блок — обычный текст, а мы нашли сильный заголовок -> отрезаем
                            sub_blocks.append(curr_text)
                            current_block_lines = [line]
                            continue
                        else:
                            # Текущий блок тоже заголовок
                            if is_numbered_header or is_word_header or is_roman_header:
                                # Это явно новый раздел (сменился номер или слово "Глава") -> отрезаем
                                sub_blocks.append(curr_text)
                                current_block_lines = [line]
                                continue
                else:
                    if current_block_lines:
                        curr_text = '\n'.join(current_block_lines)
                        if is_header(curr_text):
                            # Переход от заголовка к тексту: длинная строка с заглавной буквы или маркер списка
                            if (ls[0].isupper() and len(ls) > 50) or re.match(r'^([*\-•]|\d+(?:\.\d+)*[.)]|[а-яА-ЯёЁa-zA-Z][.)])\s', ls):
                                sub_blocks.append(curr_text)
                                current_block_lines = [line]
                                continue
                        
                current_block_lines.append(line)
                
            if current_block_lines:
                sub_blocks.append('\n'.join(current_block_lines))
                
            split_paragraphs.extend(sub_blocks)
        else:
            split_paragraphs.append(p)
            
    raw_paragraphs = [p.strip() for p in split_paragraphs if p.strip()]

    merged_blocks: list[str] = []
    current_block = ""
    
    # 2. Склеиваем мелкие абзацы и собираем элементы списков в единый блок
    for p in raw_paragraphs:
        if not current_block:
            current_block = p
        else:
            p_strip = p.strip()
            is_p_list = bool(LIST_ITEM_RE.match(p_strip))
            is_formula = is_formula_block_text(p_strip)
            prev_is_formula = is_formula_block_text(current_block)
            
            # Заголовки должны идти отдельным блоком
            if is_header(current_block):
                merged_blocks.append(current_block)
                current_block = p
            elif is_header(p):
                merged_blocks.append(current_block)
                current_block = p
            elif _should_skip_pdf_line_heuristics(current_block) or _should_skip_pdf_line_heuristics(p):
                merged_blocks.append(current_block)
                current_block = p
            # Склеиваем элементы списка в один блок (или если предыдущий абзац закончился на двоеточие)
            elif (is_p_list or current_block.strip().endswith(':') or is_formula or (prev_is_formula and p_strip.lower().startswith("где"))) and len(current_block) + len(p) + 1 <= max_chars:
                current_block += "\n" + p
            # Склеиваем "висячие" хвосты при переносах страниц/абзацев.
            elif is_dangling_end(current_block) and is_dangling_start(p_strip) and len(current_block) + len(p) + 1 <= max_chars:
                current_block += " " + p_strip
            # Иначе стандартная проверка по длине
            elif len(current_block) + len(p) + 2 <= min_chars:
                current_block += "\n\n" + p
            else:
                merged_blocks.append(current_block)
                current_block = p
                
    if current_block:
        merged_blocks.append(current_block)
        
    final_blocks: list[str] = []
    
    # 3. Обработка слишком длинных блоков
    for block in merged_blocks:
        if len(block) > max_chars:
            final_blocks.extend(split_text_fallback(block, max_chars, overlap))
        else:
            final_blocks.append(block)

    # 4. Склеиваем микро-фрагменты (1-2 слова) с соседним блоком.
    compact_blocks: list[str] = []
    for b in final_blocks:
        b_strip = b.strip()
        if is_micro_fragment(b_strip):
            if compact_blocks and len(compact_blocks[-1]) + len(b_strip) + 1 <= max_chars:
                compact_blocks[-1] = compact_blocks[-1].rstrip() + " " + b_strip
            else:
                compact_blocks.append(b_strip)
            continue
        compact_blocks.append(b)

    return [b for b in compact_blocks if b.strip()]


def chunk_source_structural(
    src_dir: Path,
    max_chars: int,
    overlap: int,
    *,
    out_dir: Path | None = None,
) -> int:
    pages_path = src_dir / "pages.jsonl"
    if not pages_path.is_file():
        return 0
    doc_id = src_dir.name  # SRC-xxxx

    doc_section_title = ""
    doc_parent_header_id = None

    rows_out: list[dict] = []
    with pages_path.open(encoding="utf-8", errors="replace") as fp:
        for line_no, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError as e:
                print(
                    f"WARN skip {doc_id} pages.jsonl line {line_no}: {e.msg}",
                    file=sys.stderr,
                )
                continue
            
            page = int(o["page"])
            text = repair_split_markdown_table_rows(
                split_numbered_lines(normalize_ocr_text(str(o.get("text") or "")))
            )
            
            blocks = extract_structural_blocks(text, min_chars=300, max_chars=max_chars, overlap=overlap)
            if not blocks:
                continue
            # Склейка continuation между страницами: последний чанк предыдущей страницы + первый текущей.
            if rows_out:
                prev = rows_out[-1]
                first_block = blocks[0].strip()
                first_meta = classify_block(first_block)
                prev_bt = str(prev.get("block_type") or "")
                prev_merge_ok = prev_bt in {"paragraph", "list", "other"} or (
                    prev_bt == "formula" and first_meta["type"] == "formula"
                )
                if (
                    prev.get("doc_id") == doc_id
                    and prev_merge_ok
                    and prev_bt != "header"
                    and is_dangling_end(str(prev.get("text") or ""))
                    and is_dangling_start(first_block)
                ):
                    prev["text"] = f"{str(prev.get('text') or '').rstrip()} {first_block}"
                    prev["page_end"] = page
                    prev["structure_confidence"] = min(float(prev.get("structure_confidence", 0.9)), 0.7)
                    blocks = blocks[1:]
                    if not blocks:
                        continue
                
            for idx, piece in enumerate(blocks):
                chunk_id = f"{doc_id}:p{page}:b{idx}"
                
                meta = classify_block(piece)
                current_parent_id = doc_parent_header_id
                
                if meta["type"] == "header":
                    # Очищаем переносы строк внутри заголовка
                    doc_section_title = re.sub(r'\s+', ' ', piece.strip())
                    assigned_parent = current_parent_id
                    doc_parent_header_id = chunk_id
                else:
                    assigned_parent = doc_parent_header_id

                rec = {
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "page": page,
                    "page_end": page,
                    "section_title": doc_section_title,
                    "text": piece,
                    "entities": [],
                    "source_ref": f"{doc_id}:{page}:{chunk_id}",
                    "block_type": meta["type"],
                    "block_level": meta["level"],
                    "parent_header_id": assigned_parent,
                    "structure_confidence": meta["conf"],
                }
                rec.update(chunk_quality_flags(piece, meta["type"], float(meta["conf"])))
                rows_out.append(rec)

    rows_out = merge_continuation_into_previous(rows_out, max_chars)

    target_out_dir = out_dir or OUT_DIR
    target_out_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_out_dir / f"{doc_id}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows_out:
            f.write(json.dumps(sanitize_utf8_json(r), ensure_ascii=False) + "\n")
            
    return len(rows_out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-chars", type=int, default=2500, help="max characters per chunk (fallback)")
    p.add_argument("--overlap", type=int, default=200, help="overlap between fallback chunks")
    p.add_argument(
        "--ingest-dir",
        type=Path,
        default=INGEST,
        help="input directory with ingested pages.jsonl folders (default: data/ingested)",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=OUT_DIR,
        help="output directory for structural chunks (default: data/chunks_structural)",
    )
    p.add_argument(
        "--only",
        type=str,
        default="",
        help="comma-separated folder names under ingested/, e.g. SRC-0046,SRC-0005. Leave empty to process all.",
    )
    args = p.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    total = 0
    processed_docs = 0
    ingest_dir = args.ingest_dir
    out_dir = args.out_dir
    if not ingest_dir.exists():
        raise SystemExit(f"ingest dir not found: {ingest_dir}")

    for src_dir in sorted(ingest_dir.iterdir(), key=lambda x: x.name):
        if not src_dir.is_dir():
            continue
        if src_dir.name.startswith("_"):
            continue
        if only and src_dir.name not in only:
            continue
            
        n = chunk_source_structural(src_dir, args.max_chars, args.overlap, out_dir=out_dir)
        if n:
            print(f"{src_dir.name}: {n} structural chunks -> {out_dir / (src_dir.name + '.jsonl')}")
            total += n
            processed_docs += 1
            
    print(f"Total structural chunks written: {total} across {processed_docs} documents.")
    print("Проверять на малом подмножестве SRC, не грузить весь корпус без запроса.")

if __name__ == "__main__":
    main()
