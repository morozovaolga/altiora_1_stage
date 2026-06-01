"""Постобработка markdown-таблиц (pipe): заголовки PyMuPDF Col1…, разделители GitHub."""

from __future__ import annotations

import re

_COL_HEADER_PLACEHOLDER = re.compile(r"^Col\d+$", re.IGNORECASE)


def github_md_separator_cells(cells: list[str]) -> bool:
    if len(cells) < 2:
        return False
    for c in cells:
        t = c.strip().replace(" ", "")
        if not t or not re.fullmatch(r":?-{3,}:?", t):
            return False
    return True


def md_github_separator_line(line: str) -> bool:
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return False
    cells = [c.strip() for c in line[1:-1].split("|")]
    return github_md_separator_cells(cells)


def parse_pipe_markdown_table(md: str) -> list[list[str]] | None:
    raw_lines = [ln.rstrip() for ln in md.strip().splitlines() if ln.strip()]
    rows: list[list[str]] = []
    for ln in raw_lines:
        ln = ln.strip()
        if not (ln.startswith("|") and ln.endswith("|")):
            return None
        rows.append([c.strip() for c in ln[1:-1].split("|")])
    if len(rows) < 3:
        return None
    return rows


def rebuild_pipe_markdown_table(rows: list[list[str]]) -> str:
    return "\n".join("|" + "|".join(cells) + "|" for cells in rows)


def promote_col_placeholder_table_headers(md_table: str) -> str:
    """
    Ячейки ColN в шапке заменяются текстом из первой строки данных, строка удаляется.
    Если среди заменённых Col только один повторяющийся текст (например «Углеводороды» везде),
    подмену не делаем — реальные подписи колонок, скорее всего, ниже.
    """
    rows = parse_pipe_markdown_table(md_table)
    if not rows or len(rows) < 3:
        return md_table
    hdr, sep = rows[0], rows[1]
    rest = rows[2:]
    if not github_md_separator_cells(sep):
        return md_table
    if not rest:
        return md_table
    r1 = rest[0]
    if len(r1) != len(hdr):
        return md_table
    new_hdr = list(hdr)
    replaced_vals: list[str] = []
    replaced_any = False
    for i, h in enumerate(hdr):
        if _COL_HEADER_PLACEHOLDER.match(h.strip()):
            v = (r1[i] or "").strip() or h
            new_hdr[i] = v
            replaced_vals.append(v)
            replaced_any = True
    if not replaced_any:
        return md_table
    if len(replaced_vals) >= 2 and len(set(replaced_vals)) == 1:
        return md_table
    return rebuild_pipe_markdown_table([new_hdr, sep] + rest[1:])


def _normalize_table_row_to_ncols(row: list[str | None], ncols: int) -> list[str]:
    cells = [("" if c is None else str(c)) for c in row]
    while len(cells) < ncols:
        cells.append("")
    if ncols >= 2 and len(cells) > ncols:
        return [cells[0].strip(), " ".join(x.strip() for x in cells[1:])]
    return [c.strip() for c in cells[:ncols]]


def _is_noise_table_row(left: str, right: str) -> bool:
    """Очевидный мусор OCR в двухколоночной строке (например только «Ё»)."""
    if not left.strip() and not right.strip():
        return True
    if len(left) <= 12 and not right.strip() and re.fullmatch(r"[\sЁё:]+", left):
        return True
    return False


def merge_two_column_wrapped_rows(matrix: list[list[str | None]]) -> list[list[str]]:
    """
    PyMuPDF часто даёт отдельную «строку таблицы» на каждую горизонтальную линию сетки PDF,
    из-за чего перенос внутри ячейки превращается в лишние ряды с пустой левой или правой колонкой.
    Склеиваем такие хвосты в предыдущую логическую строку (типичный глоссарий «термин | определение»).

    Первую строку (шапку) не склеиваем с предыдущими: при len(out)==1 не вливаем в шапку.
    """
    if not matrix:
        return []
    try:
        ncols = max(len(r) for r in matrix)
    except ValueError:
        return []
    if ncols != 2:
        return [_normalize_table_row_to_ncols(list(r), ncols) for r in matrix]

    norm: list[list[str]] = []
    for raw in matrix:
        row = _normalize_table_row_to_ncols(list(raw), 2)
        if _is_noise_table_row(row[0], row[1]):
            continue
        norm.append(row)

    if not norm:
        return []
    out: list[list[str]] = [list(norm[0])]
    for i in range(1, len(norm)):
        left, right = norm[i][0], norm[i][1]
        pl, pr = out[-1][0], out[-1][1]
        if not left and right:
            substantial_prev = len(pr) > 22 or len(pl) > 35
            if len(out) >= 2 or substantial_prev:
                out[-1][1] = (pr + " " + right).strip() if pr else right
            else:
                out.append([left, right])
        elif not right and left:
            substantial_prev = len(pl) > 22 or len(pr) > 35
            if len(out) >= 2 or substantial_prev:
                out[-1][0] = (pl + " " + left).strip() if pl else left
            else:
                out.append([left, right])
        else:
            out.append([left, right])
    return out


def matrix_to_github_pipe_markdown(matrix: list[list[str]]) -> str:
    """Строит GFM pipe-таблицу: первая строка — заголовок, вторая — |---|---|."""
    if not matrix:
        return ""
    ncols = max(len(r) for r in matrix)
    lines: list[str] = []

    def esc_cell(s: str) -> str:
        return s.replace("\n", "<br>").replace("|", "\\|")

    for ri, raw in enumerate(matrix):
        row = list(raw)
        while len(row) < ncols:
            row.append("")
        body = "| " + " | ".join(esc_cell(str(c)) for c in row[:ncols]) + " |"
        lines.append(body)
        if ri == 0:
            lines.append("| " + " | ".join("---" for _ in range(ncols)) + " |")
    return "\n".join(lines)


def pymupdf_table_to_github_markdown(table: object) -> str | None:
    """
    Предпочтительный путь вместо Table.to_markdown():
    - extract(layout=True) сохраняет переносы внутри ячейки;
    - без fill_empty — нет «размазывания» текста по соседним None-ячейкам;
    - для 2 колонок — склейка PDF-строк с пустой левой/правой ячейкой.
    """
    try:
        try:
            matrix = table.extract(layout=True)  # type: ignore[attr-defined]
        except Exception:
            matrix = table.extract()  # type: ignore[attr-defined]
    except Exception:
        return None
    if not matrix:
        return None
    matrix = [[("" if c is None else str(c)) for c in row] for row in matrix]
    matrix = [row for row in matrix if any((c or "").strip() for c in row)]
    if not matrix:
        return None
    try:
        ncol = int(table.col_count)  # type: ignore[attr-defined]
    except Exception:
        ncol = max(len(r) for r in matrix)
    if ncol == 2 and len(matrix) > 1:
        matrix = merge_two_column_wrapped_rows(matrix)
    else:
        matrix = [_normalize_table_row_to_ncols(r, ncol) for r in matrix]
    return matrix_to_github_pipe_markdown(matrix)
