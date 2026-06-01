"""
Альтернативный Ingest документов с использованием DocLayout-YOLO и EasyOCR.
docTR заменен на EasyOCR из-за отсутствия встроенной качественной поддержки кириллицы.
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd
import numpy as np
import cv2
import fitz  # PyMuPDF

# Отключаем лишние потоки для экономии ресурсов
os.environ["OMP_NUM_THREADS"] = "2"

try:
    from doclayout_yolo import YOLOv10
    from huggingface_hub import hf_hub_download
    import easyocr
except ImportError:
    print("Ошибка: Не установлены зависимости. Выполните: pip install doclayout-yolo easyocr", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WB = ROOT / "inventory_2026-04-17.xlsx"
INGEST_DIR = ROOT / "data" / "ingested"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def normalize_ocr_garbage(text: str) -> str:
    """Удаление мусорных символов OCR и лишних пробелов."""
    if not text:
        return text
    # Замена "квадратиков" и прочих replacement-символов.
    text = text.replace("\ufffd", " ")
    # Удаляем управляющие символы, кроме переносов строк и табов.
    text = re.sub(r"[\x00-\x08\x0B-\x1F\x7F]", " ", text)
    # Нормализация повторяющихся пробелов.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def decode_bytes_with_chardet(raw: bytes) -> str:
    """Декодирование сырых байт (например sidecar .txt). Для PDF PyMuPDF уже отдаёт str — см. fix_word_encoding."""
    if not raw:
        return ""
    try:
        import chardet
    except ImportError:
        return raw.decode("utf-8", errors="replace")
    det = chardet.detect(raw)
    enc = (det.get("encoding") or "").strip() or "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def looks_like_mojibake_latin1251(word: str) -> bool:
    """Токен без кириллицы, но с символами Latin-1 Supplement — часто cp1251, прочитанный как Latin-1."""
    if not word or any("\u0400" <= c <= "\u04ff" for c in word):
        return False
    return any(192 <= ord(c) <= 255 for c in word)


def fix_word_encoding(text: str) -> str:
    """Восстанавливает кириллицу: байты cp1251 были интерпретированы как Latin-1 (типичный mojibake в PDF)."""
    if not text or not looks_like_mojibake_latin1251(text):
        return text
    try:
        fixed = text.encode("latin-1", errors="strict").decode("windows-1251", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if any("\u0400" <= c <= "\u04ff" for c in fixed):
        return fixed
    return text


try:
    from scripts.md_table_postprocess import (
        promote_col_placeholder_table_headers,
        pymupdf_table_to_github_markdown,
    )
except ImportError:
    from md_table_postprocess import (
        promote_col_placeholder_table_headers,
        pymupdf_table_to_github_markdown,
    )


def text_readability_metrics(text: str) -> tuple[float, float]:
    """Оценка читаемости: доля кириллицы и доля "странных" токенов."""
    t = text or ""
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", t)
    if not letters:
        return 0.0, 1.0
    cyr = re.findall(r"[А-Яа-яЁё]", t)
    cyr_ratio = len(cyr) / len(letters)
    tokens = re.findall(r"\S+", t)
    weird = 0
    for tok in tokens:
        core = re.sub(r"[^\wА-Яа-яЁё]", "", tok)
        if len(core) >= 4:
            vow = re.search(r"[аеёиоуыэюяAEIOUYaeiouy]", core)
            if not vow:
                weird += 1
    weird_ratio = weird / max(len(tokens), 1)
    return cyr_ratio, weird_ratio

def remove_table_lines(image_cv):
    """Удаляет вертикальные и горизонтальные линии таблицы с изображения."""
    if len(image_cv.shape) == 3:
        gray = cv2.cvtColor(image_cv, cv2.COLOR_BGR2GRAY)
    else:
        gray = image_cv
        
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, -2)
    
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    detect_horizontal = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horizontal_kernel, iterations=2)
    cnts, _ = cv2.findContours(detect_horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        cv2.drawContours(image_cv, [c], -1, (255, 255, 255), 3)
        
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    detect_vertical = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, vertical_kernel, iterations=2)
    cnts, _ = cv2.findContours(detect_vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        cv2.drawContours(image_cv, [c], -1, (255, 255, 255), 3)
        
    return image_cv

# Классы YOLO, которые нужно парсить (содержат текст)
TEXT_CLASSES = {"title", "plain text", "table", "table_caption", "table_footnote", "isolate_formula", "formula_caption", "figure_caption", "list"}


def upscale_formula_crop(crop_rgb: np.ndarray) -> np.ndarray:
    """Увеличивает вырезку формулы для архива/распознавания (LANCZOS), пока короткая сторона < порога.

    ``ALTIORA_FORMULA_MIN_EDGE`` — целевая минимальная сторона в пикселях (0 = не масштабировать).
    ``ALTIORA_FORMULA_MAX_SCALE`` — максимум увеличения (по умолчанию 4).
    """
    if crop_rgb is None or crop_rgb.size == 0:
        return crop_rgb
    h, w = crop_rgb.shape[:2]
    min_e = min(h, w)
    if min_e < 1:
        return crop_rgb
    try:
        target = int(os.getenv("ALTIORA_FORMULA_MIN_EDGE", "512"))
    except ValueError:
        target = 512
    if target <= 0:
        return crop_rgb
    try:
        max_scale = float(os.getenv("ALTIORA_FORMULA_MAX_SCALE", "4"))
    except ValueError:
        max_scale = 4.0
    max_scale = max(1.0, max_scale)
    scale = min(max_scale, max(1.0, float(target) / float(min_e)))
    if scale <= 1.001:
        return crop_rgb
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    return cv2.resize(crop_rgb, (nw, nh), interpolation=cv2.INTER_LANCZOS4)


def write_formula_svg_wrapper(svg_path: Path, png_basename: str, width: int, height: int) -> None:
    """SVG-обёртка над PNG: не векторизует скан, но даёт предсказуемые размеры и один файл для UI."""
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'  <image width="{width}" height="{height}" xlink:href="{png_basename}" '
        'preserveAspectRatio="xMidYMid meet"/>\n'
        "</svg>\n"
    )
    svg_path.write_text(svg, encoding="utf-8")


def box_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """IoU для подавления повторных детекций одной и той же формулы."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / max(1, area_a + area_b - inter)


def box_contains_mostly(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int], threshold: float = 0.82) -> bool:
    """True, если inner почти целиком лежит внутри outer."""
    ox1, oy1, ox2, oy2 = outer
    ix1, iy1, ix2, iy2 = inner
    jx1, jy1 = max(ox1, ix1), max(oy1, iy1)
    jx2, jy2 = min(ox2, ix2), min(oy2, iy2)
    inter = max(0, jx2 - jx1) * max(0, jy2 - jy1)
    inner_area = max(1, (ix2 - ix1) * (iy2 - iy1))
    return (inter / inner_area) >= threshold


def dedupe_formula_boxes(boxes: list[dict]) -> list[dict]:
    """Оставляет одну детекцию на одну визуальную формулу, чтобы не дублировать её в выдаче."""
    formulas = [b for b in boxes if b.get("label") == "isolate_formula"]
    if len(formulas) <= 1:
        return boxes

    selected: list[dict] = []
    for b in sorted(formulas, key=lambda x: float(x.get("conf", 0.0)), reverse=True):
        bb = b["box"]
        duplicate = False
        for kept in selected:
            kb = kept["box"]
            if box_iou(bb, kb) >= 0.45 or box_contains_mostly(kb, bb) or box_contains_mostly(bb, kb):
                duplicate = True
                break
        if not duplicate:
            selected.append(b)

    selected_ids = {id(b) for b in selected}
    return [b for b in boxes if b.get("label") != "isolate_formula" or id(b) in selected_ids]


def point_in_any_formula_box(cx: float, cy: float, formula_boxes: list[tuple[int, int, int, int]]) -> bool:
    """Проверяет, попадает ли слово внутрь области формулы."""
    for x1, y1, x2, y2 in formula_boxes:
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return True
    return False


def sort_layout_boxes(boxes: list, page_w: int, page_h: int) -> list:
    """Порядок чтения страницы: при двух колонках — левая сверху вниз, затем правая; иначе сверху вниз, слева направо по (y1, x1).

    Раньше для одной колонки использовались «полосы» по центру Y и квантование bucket — из‑за этого
    блоки титула/шапки могли оказываться не в визуальном порядке. Якорь по верхнему краю y1 стабильнее.
    """
    if len(boxes) <= 1:
        return boxes
    centers_x = [((b["box"][0] + b["box"][2]) / 2) for b in boxes]
    left_n = sum(1 for cx in centers_x if cx < page_w * 0.42)
    right_n = sum(1 for cx in centers_x if cx > page_w * 0.58)
    n = len(boxes)
    two_col = n >= 6 and left_n >= max(3, int(n * 0.22)) and right_n >= max(3, int(n * 0.22))
    if two_col:
        gutter = page_w * 0.5

        def key_two(b: dict) -> tuple:
            x1, y1, x2, y2 = b["box"]
            cx = (x1 + x2) / 2
            col = 0 if cx < gutter else 1
            return (col, y1, x1)

        return sorted(boxes, key=key_two)

    def key_reading_order(b: dict) -> tuple:
        x1, y1, x2, y2 = b["box"]
        return (y1, x1)

    return sorted(boxes, key=key_reading_order)


def pick_box_for_word_center(boxes: List[Any], cx: float, cy: float) -> Optional[dict]:
    """Блок, в который попадает центр слова: при пересечении регионов YOLO — самый маленький по площади
    (обычно строка/абзац, а не весь лист). Сначала без isolate_formula, чтобы тело не «съедало» вырезка формулы."""
    def candidates(skip_formula: bool) -> List[tuple]:
        out: List[tuple] = []
        for b in boxes:
            if skip_formula and b.get("label") == "isolate_formula":
                continue
            bx1, by1, bx2, by2 = b["box"]
            if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                area = max(1, (bx2 - bx1) * (by2 - by1))
                out.append((area, b))
        return out

    cand = candidates(skip_formula=True)
    if not cand:
        cand = candidates(skip_formula=False)
    if not cand:
        return None
    return min(cand, key=lambda t: t[0])[1]


class YoloDoctrExtractor:
    def __init__(self):
        logging.info("Инициализация DocLayout-YOLO...")
        
        # Сначала пытаемся загрузить модель из локального кэша (полностью автономный режим без интернета)
        try:
            model_path = hf_hub_download(
                repo_id="juliozhao/DocLayout-YOLO-DocStructBench", 
                filename="doclayout_yolo_docstructbench_imgsz1024.pt",
                local_files_only=True
            )
        except Exception:
            logging.info("Модель не найдена локально. Скачивание с HuggingFace (потребуется интернет)...")
            model_path = hf_hub_download(
                repo_id="juliozhao/DocLayout-YOLO-DocStructBench", 
                filename="doclayout_yolo_docstructbench_imgsz1024.pt"
            )
        self.yolo_model = YOLOv10(model_path)
        
        logging.info("Инициализация EasyOCR (поддержка кириллицы)...")
        self.ocr_reader = easyocr.Reader(['ru', 'en'])
        
    def process_image(
        self,
        page: fitz.Page,
        img_np: np.ndarray,
        native_words: list = None,
        zoom: float = 1.0,
        formula_assets_dir: Path | None = None,
        page_one_based: int = 1,
        formula_counter: list | None = None,
    ) -> str:
        h, w = img_np.shape[:2]
        
        try:
            _min_native = int(os.getenv("ALTIORA_NATIVE_WORD_MIN", "48"))
        except ValueError:
            _min_native = 48
        _min_native = max(8, _min_native)
        # Слишком низкий порог объявлял страницу «сканом» при живом текстовом слое → лишний OCR и перестановки слов.
        is_scan = not native_words or len(native_words) < _min_native
        rotated_angle = 0
        
        if is_scan:
            # Автовыравнивание (OSD): гибридный подход YOLO (формат) + EasyOCR (чтение)
            
            # 1. Определяем ориентацию страницы (книжная vs альбомная) через YOLO
            res_0 = self.yolo_model.predict(img_np, imgsz=640, conf=0.25, verbose=False)[0]
            boxes_0 = len([b for b in res_0.boxes if self.yolo_model.names[int(b.cls[0])] in TEXT_CLASSES])
            
            img_90 = cv2.rotate(img_np, cv2.ROTATE_90_CLOCKWISE)
            res_90 = self.yolo_model.predict(img_90, imgsz=640, conf=0.25, verbose=False)[0]
            boxes_90 = len([b for b in res_90.boxes if self.yolo_model.names[int(b.cls[0])] in TEXT_CLASSES])
            
            # Если 0 градусов дает больше блоков, проверяем 0 и 180. Иначе 90 и 270.
            candidates = [0, 180] if boxes_0 >= boxes_90 * 0.9 else [90, 270]
            
            # 2. Определяем точный угол: где текст читается увереннее (по confidence)
            best_angle = candidates[0]
            max_score = -1
            
            # Для ускорения OCR ужимаем картинку, но оставляем читаемое разрешение (1500px)
            scale = 1500.0 / max(h, w) if max(h, w) > 1500 else 1.0
            small_img = cv2.resize(img_np, (0, 0), fx=scale, fy=scale)
            
            for angle in candidates:
                test_img = small_img
                if angle == 90:
                    test_img = cv2.rotate(small_img, cv2.ROTATE_90_CLOCKWISE)
                elif angle == 180:
                    test_img = cv2.rotate(small_img, cv2.ROTATE_180)
                elif angle == 270:
                    test_img = cv2.rotate(small_img, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    
                # Читаем сжатую картинку с деталями, чтобы получить уверенность распознавания
                text_res = self.ocr_reader.readtext(test_img)
                
                # Считаем взвешенную сумму уверенности (игнорируем мусор с conf < 0.4)
                score = sum(len(text) * conf for bbox, text, conf in text_res if conf > 0.4)
                
                # Даем небольшой приоритет 0 и 90, чтобы не крутить при одинаковом результате
                if angle in (180, 270):
                    score = score * 0.95
                    
                if score > max_score:
                    max_score = score
                    best_angle = angle
                    
            if best_angle == 90:
                img_np = cv2.rotate(img_np, cv2.ROTATE_90_CLOCKWISE)
                rotated_angle = 90
                logging.info("    -> Автовыравнивание: поворот на 90° (по часовой)")
            elif best_angle == 180:
                img_np = cv2.rotate(img_np, cv2.ROTATE_180)
                rotated_angle = 180
                logging.info("    -> Автовыравнивание: поворот на 180° (вверх ногами)")
            elif best_angle == 270:
                img_np = cv2.rotate(img_np, cv2.ROTATE_90_COUNTERCLOCKWISE)
                rotated_angle = 270
                logging.info("    -> Автовыравнивание: поворот на 90° (против часовой)")
                
        # Микро-выравнивание (Deskew: устранение перекоса сканера от -15 до 15 градусов)
        try:
            gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
            thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
            
            # Горизонтальное размытие для склеивания букв в сплошные текстовые строки
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 5))
            dilate = cv2.dilate(thresh, kernel, iterations=3)
            
            contours, _ = cv2.findContours(dilate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            angles = []
            for c in contours:
                if cv2.contourArea(c) < 100:
                    continue
                    
                rect = cv2.minAreaRect(c)
                angle = rect[-1]
                
                # Универсальная нормализация угла (для разных версий OpenCV)
                if angle < -45:
                    angle += 90
                elif angle > 45:
                    angle -= 90
                    
                if -15 < angle < 15 and angle != 0:
                    angles.append(angle)
                    
            if angles:
                skew_angle = float(np.median(angles))
                if abs(skew_angle) > 0.5:
                    logging.info(f"    -> Автовыравнивание: устранение перекоса сканера на {skew_angle:.2f}°")
                    h_img, w_img = img_np.shape[:2]
                    center = (w_img // 2, h_img // 2)
                    M = cv2.getRotationMatrix2D(center, skew_angle, 1.0)
                    # BORDER_REPLICATE предотвращает черные края, которые ломают поиск таблиц
                    img_np = cv2.warpAffine(img_np, M, (w_img, h_img), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                    rotated_angle += skew_angle
        except Exception as e:
            logging.warning(f"    -> Ошибка при микро-выравнивании: {e}")

        h, w = img_np.shape[:2]

        # Получаем структуру через YOLO (быстро)
        results = self.yolo_model.predict(img_np, imgsz=1024, conf=0.2, verbose=False)[0]
        
        boxes = []
        for box in results.boxes:
            cls_id = int(box.cls[0])
            label = self.yolo_model.names[cls_id]
            if label not in TEXT_CLASSES:
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            boxes.append({"label": label, "box": (x1, y1, x2, y2), "words": [], "conf": conf})

        boxes = sort_layout_boxes(dedupe_formula_boxes(boxes), w, h)
        formula_regions = [b["box"] for b in boxes if b.get("label") == "isolate_formula"]
        
        if not is_scan and rotated_angle == 0:
            # Идеальный вариант: используем 100% точный родной текстовый слой PDF
            for w in native_words:
                x0, y0, x1, y1, text, block_no, line_no, word_no = w
                text = fix_word_encoding(str(text))
                # Переводим координаты из поинтов (72 DPI) в пиксели изображения
                cx = (x0 + x1) / 2 * zoom
                cy = (y0 + y1) / 2 * zoom
                x_min = x0 * zoom
                x_max = x1 * zoom
                if point_in_any_formula_box(cx, cy, formula_regions):
                    continue
                    
                chosen = pick_box_for_word_center(boxes, cx, cy)
                if chosen is not None:
                    chosen["words"].append((cx, cy, text, x_min, x_max, block_no, line_no, word_no))
        else:
            # Улучшение изображения (реставрация) для "слепых" машинописных сканов (как SRC-0004)
            try:
                gray_ocr = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
                
                # 1. Автоконтраст: растягиваем гистограмму (самый светлый -> белый, самый темный -> черный)
                gray_ocr = cv2.normalize(gray_ocr, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
                
                # 2. CLAHE: локально вытягивает бледные буквы и выравнивает фон
                clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
                enhanced = clahe.apply(gray_ocr)
                
                # 3. Unsharp Masking: повышает резкость размытых краев машинописных букв
                blur = cv2.GaussianBlur(enhanced, (0, 0), 3)
                enhanced = cv2.addWeighted(enhanced, 1.5, blur, -0.5, 0)
                
                # 4. Утолщение истонченных бледных линий (эрозия светлого фона = жирный шрифт)
                # Ядро 2x2 делает тонкие буквы плотнее, предотвращая их распад на точки при OCR
                kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
                enhanced = cv2.erode(enhanced, kernel, iterations=1)
                
                ocr_img = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)
            except Exception as e:
                logging.warning(f"    -> Ошибка реставрации картинки: {e}")
                ocr_img = img_np
                
            # Убираем линии таблиц, чтобы OCR не читал их как буквы (ПО, ОИ и т.д.)
            ocr_img = remove_table_lines(ocr_img.copy())

            ocr_confidences = []
            
            try:
                import pytesseract
                
                # Если на Windows, пытаемся подхватить tesseract, если его нет в PATH
                if os.name == 'nt':
                    tess_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
                    if os.path.exists(tess_path):
                        pytesseract.pytesseract.tesseract_cmd = tess_path
                
                # Tesseract OCR: строго русский язык, чтобы избежать латинских галлюцинаций
                ocr_data = pytesseract.image_to_data(ocr_img, lang='rus', config='--psm 3', output_type='dict')
                
                for i in range(len(ocr_data['text'])):
                    conf = float(ocr_data['conf'][i])
                    text = str(ocr_data['text'][i]).strip()
                    
                    if text:
                        ocr_confidences.append(conf / 100.0)
                        
                    if conf > 40 and text:
                        x, y = ocr_data['left'][i], ocr_data['top'][i]
                        w, h = ocr_data['width'][i], ocr_data['height'][i]
                        
                        cx = x + w / 2.0
                        cy = y + h / 2.0
                                
                        chosen = pick_box_for_word_center(boxes, cx, cy)
                        if chosen is not None:
                            chosen["words"].append((cx, cy, text, x, x + w, -1, -1, -1))
            except Exception as e:
                logging.warning(f"    -> Tesseract недоступен ({e}). Проверьте наличие rus.traineddata! Используем EasyOCR")
                results = self.ocr_reader.readtext(ocr_img)
                for bbox, text, conf in results:
                    text_str = str(text).strip()
                    if text_str:
                        ocr_confidences.append(conf)
                        
                    if conf < 0.4 or not text_str:
                        continue
                        
                    x_coords = [p[0] for p in bbox]
                    y_coords = [p[1] for p in bbox]
                    cx, cy = sum(x_coords) / 4.0, sum(y_coords) / 4.0
                    if point_in_any_formula_box(cx, cy, formula_regions):
                        continue
                            
                    chosen = pick_box_for_word_center(boxes, cx, cy)
                    if chosen is not None:
                        chosen["words"].append((cx, cy, text, min(x_coords), max(x_coords), -1, -1, -1))
                            
            # Автоматическая отбраковка мусорных сканов
            if ocr_confidences:
                good_words = sum(1 for c in ocr_confidences if c >= 0.4)
                total_words = len(ocr_confidences)
                # Если нейросеть нашла больше 15 токенов, но меньше 40% из них читаются уверенно
                if total_words > 15 and (good_words / total_words) < 0.4:
                    logging.warning(f"    -> Страница отбракована как нечитаемая (уверенность OCR: {good_words/total_words:.1%})")
                    return "[ВНИМАНИЕ: СТРАНИЦА НЕЧИТАЕМА ИЗ-ЗА ПЛОХОГО КАЧЕСТВА СКАНА. ПОЖАЛУЙСТА, ОБРАТИТЕСЬ К ОРИГИНАЛУ]"

        page_text_blocks = []
        for b in boxes:
            label = b["label"]
            conf = b.get("conf", 1.0)
            
            if conf < 0.25 and label not in ("table", "isolate_formula"):
                continue
            
            if label == "isolate_formula":
                try:
                    curr_h, curr_w = img_np.shape[:2]
                    x1, y1, x2, y2 = map(int, b["box"])
                    
                    # Ограничиваем координаты краями изображения на случай сдвигов
                    x1, y1 = max(0, x1), max(0, y1)
                    x2, y2 = min(curr_w, x2), min(curr_h, y2)
                    
                    if x2 > x1 and y2 > y1:
                        crop_img = img_np[y1:y2, x1:x2]
                        crop_hi = upscale_formula_crop(crop_img)
                        img_md = ""
                        snap_ok = (
                            formula_assets_dir is not None
                            and formula_counter is not None
                            and os.getenv("ALTIORA_FORMULA_SNAPSHOTS", "1").strip().lower()
                            not in ("0", "false", "no", "")
                        )
                        use_svg_link = (
                            os.getenv("ALTIORA_FORMULA_USE_SVG", "0").strip().lower()
                            in ("1", "true", "yes")
                        )
                        if snap_ok:
                            try:
                                formula_assets_dir.mkdir(parents=True, exist_ok=True)
                                formula_counter[0] += 1
                                fname = f"p{page_one_based}_f{formula_counter[0]}.png"
                                out_png = formula_assets_dir / fname
                                bgr = cv2.cvtColor(crop_hi, cv2.COLOR_RGB2BGR)
                                cv2.imwrite(
                                    str(out_png),
                                    bgr,
                                    [int(cv2.IMWRITE_PNG_COMPRESSION), 1],
                                )
                                if use_svg_link:
                                    hh, ww = crop_hi.shape[:2]
                                    svg_name = f"{Path(fname).stem}.svg"
                                    write_formula_svg_wrapper(
                                        formula_assets_dir / svg_name, fname, ww, hh
                                    )
                                    img_md = f"![](assets/formulas/{svg_name})\n\n"
                                else:
                                    img_md = f"![](assets/formulas/{fname})\n\n"
                            except Exception as ex:
                                logging.warning(f"    -> Не удалось сохранить снимок формулы: {ex}")

                        if img_md:
                            page_text_blocks.append(img_md.strip())
                        else:
                            page_text_blocks.append("[Формула: изображение недоступно]")
                    else:
                        page_text_blocks.append("[Формула: неверные координаты]")
                except Exception as e:
                    logging.warning(f"    -> Ошибка распознавания формулы: {e}")
                    page_text_blocks.append("[Формула: ошибка распознавания]")
                continue

            if label == "table":
                try:
                    if rotated_angle != 0:
                        raise ValueError("Изображение перевернуто, пропускаем PyMuPDF")
                        
                    x1, y1, x2, y2 = b["box"]
                    # Конвертируем пиксели в PDF-поинты для PyMuPDF
                    clip_rect = fitz.Rect(x1 / zoom, y1 / zoom, x2 / zoom, y2 / zoom)
                    
                    # Защита от выхода координат YOLO за пределы страницы
                    clip_rect = clip_rect.intersect(page.rect)
                    
                    if clip_rect.is_empty or not clip_rect.is_valid:
                        raise ValueError("Invalid clip rect")
                        
                    # Ищем таблицы только в области, найденной YOLO
                    tables = page.find_tables(clip=clip_rect)
                    
                    # Если PyMuPDF нашел таблицу, конвертируем ее в Markdown
                    if tables and tables[0].row_count > 1:
                        tbl0 = tables[0]
                        # to_markdown(fill_empty=True) размазывает текст по None-ячейкам; для глоссариев
                        # с переносами внутри ячеек — extract(layout=True) + склейка двухколоночных хвостов.
                        md_table = pymupdf_table_to_github_markdown(tbl0)
                        if not md_table or len(md_table.strip()) < 10:
                            md_table = tbl0.to_markdown(fill_empty=False)
                        # PyMuPDF помечает жирный шрифт в PDF как **…**; мы ренерим ячейки как HTML без MD.
                        md_table = re.sub(r"\*\*([^*]*)\*\*", r"\1", md_table)
                        md_table = promote_col_placeholder_table_headers(md_table)
                        if md_table and len(md_table.strip()) > 10:
                            page_text_blocks.append(f"[Таблица]\n{md_table}")
                        else:
                            raise ValueError("Empty markdown table")
                    else:
                        raise ValueError("PyMuPDF found no structured table")
                except Exception:
                    # Фоллбэк: эвристическая сборка Markdown-таблицы из OCR-слов
                    if not b["words"]: continue
                    
                    # 1. Группируем слова по строкам (допуск по Y = 15px)
                    b["words"].sort(key=lambda w: w[1])
                    lines = []
                    current_line = []
                    if b["words"]:
                        current_y = b["words"][0][1]
                        for w in b["words"]:
                            if abs(w[1] - current_y) <= 15:
                                current_line.append(w)
                            else:
                                lines.append(sorted(current_line, key=lambda x: x[0]))
                                current_line = [w]
                                current_y = w[1]
                        if current_line:
                            lines.append(sorted(current_line, key=lambda x: x[0]))
                    
                    # 2. Формируем строки, вставляя разделитель "|" при разрыве > 30px
                    md_lines = []
                    max_pipes = 0
                    for line in lines:
                        row_str = ""
                        last_x_max = -1
                        for w in line:
                            cx, cy, w_text, x_min, x_max = w[0], w[1], w[2], w[3], w[4]
                            if last_x_max != -1 and (x_min - last_x_max) > 30:
                                row_str += " | "
                            elif last_x_max != -1:
                                row_str += " "
                            row_str += fix_word_encoding(str(w_text)).replace('\n', ' ').replace('|', r'\|')
                            last_x_max = x_max
                        md_lines.append(row_str)
                        max_pipes = max(max_pipes, row_str.count("|"))
                        
                    # 3. Выравниваем колонки для валидного Markdown
                    final_md = []
                    for i, line_str in enumerate(md_lines):
                        pipes_to_add = max_pipes - line_str.count("|")
                        padded_line = f"| {line_str} " + ("| " * pipes_to_add) + "|"
                        padded_line = " ".join(padded_line.split()) # Убираем лишние пробелы
                        final_md.append(padded_line)
                        if i == 0:
                            final_md.append("|" + ("---| " * (max_pipes + 1)).strip())
                            
                    text = "\n".join(final_md)
                    nonempty_cells = len(re.findall(r"[А-Яа-яЁёA-Za-z0-9]{2,}", text))
                    if max_pipes >= 4 and nonempty_cells < len(md_lines) * 2:
                        text = "\n".join(" ".join(row.split()) for row in md_lines if row.strip())
                    else:
                        text = promote_col_placeholder_table_headers(text)
                    page_text_blocks.append(f"[Таблица]\n{text}")
                continue
                
            if not b["words"]:
                continue
                
            # Сборка текста без перемешивания слов:
            # - для нативного PDF слоя: используем block/line/word порядок;
            # - для OCR: аккуратно группируем в строки по Y и сортируем по X.
            has_native_order = any(len(w) >= 8 and w[5] >= 0 and w[6] >= 0 and w[7] >= 0 for w in b["words"])
            if has_native_order:
                ordered_words = sorted(
                    b["words"],
                    key=lambda w: (w[5], w[6], w[7], w[0])
                )
                text = " ".join(fix_word_encoding(str(w[2])) for w in ordered_words)
            else:
                words_sorted = sorted(b["words"], key=lambda w: (w[1], w[0]))
                ys_unique = sorted({round(w[1], 1) for w in words_sorted})
                line_gap = 10
                if len(ys_unique) >= 2:
                    gaps = [
                        ys_unique[i + 1] - ys_unique[i]
                        for i in range(len(ys_unique) - 1)
                        if ys_unique[i + 1] - ys_unique[i] > 0.5
                    ]
                    if gaps:
                        gaps.sort()
                        q = gaps[max(0, len(gaps) // 4)]
                        line_gap = max(7, min(26, int(q * 0.55)))
                lines = []
                current_line = []
                current_y = None
                for w in words_sorted:
                    y = w[1]
                    if current_y is None or abs(y - current_y) <= line_gap:
                        current_line.append(w)
                        if current_y is None:
                            current_y = y
                        else:
                            current_y = (current_y * (len(current_line) - 1) + y) / len(current_line)
                    else:
                        lines.append(sorted(current_line, key=lambda x: x[0]))
                        current_line = [w]
                        current_y = y
                if current_line:
                    lines.append(sorted(current_line, key=lambda x: x[0]))
                text = " ".join(" ".join(w[2] for w in line) for line in lines)
            
            # --- ИСПРАВЛЕНИЯ OCR И МУСОРА ---
            # 1. Очистка Unicode-артефактов (маркеры списков)
            text = text.replace('\uf0a7', '•').replace('\uf0b7', '•')
            text = normalize_ocr_garbage(text)
            
            # 2. Удаление изолированных номеров страниц и ложных заголовков-цифр
            if re.fullmatch(r'\d{1,3}', text.strip()) or re.fullmatch(r'#\s*\d{1,3}', text.strip()):
                continue
                
            # 3. Удаление оглавления (строки с длинными точечными линиями)
            if re.search(r'\.{5,}', text):
                continue
                
            # 4. Присоединение оторванных номеров формул к предыдущей формуле
            if re.fullmatch(r'\(\s*\d+\s*\)', text.strip()) and page_text_blocks:
                last_block = page_text_blocks[-1]
                if "$$" in last_block or "[Формула" in last_block or "assets/formulas/" in last_block:
                    page_text_blocks[-1] = f"{last_block} {text.strip()}"
                    continue
            # --------------------------------
            
            if label == "title":
                # Нумерованный пункт — это не заголовок раздела
                if re.match(r'^\d+[\.\)]\s', text.strip()):
                    page_text_blocks.append(text)
                else:
                    page_text_blocks.append(f"# {text}")
            # Старая логика для таблиц теперь не нужна, так как обрабатывается выше
            # elif label == "table":
            #     # Фоллбэк на старый метод, если PyMuPDF не нашел таблицу
            #     if not b["words"]: continue
            #     b["words"].sort(key=lambda w: (w[1] // 15, w[0]))
            #     text = " ".join([w[2] for w in b["words"]])
            #     page_text_blocks.append(f"[Таблица]\n{text}")
            else:
                page_text_blocks.append(text)
                
        full_text = "\n\n".join(page_text_blocks)
        
        # Склеиваем слова, разорванные переносом (например, "Темпе- ратура" или "Темпе-\nратура" -> "Температура")
        full_text = re.sub(r'(?<=[а-яА-ЯёЁa-zA-Z])-\s+(?=[а-яА-ЯёЁa-zA-Z])', '', full_text)
        full_text = normalize_ocr_garbage(full_text)

        # Дополнительный guardrail: если OCR дал явную "кашу", а native текст доступен, используем native.
        cyr_ratio, weird_ratio = text_readability_metrics(full_text)
        if is_scan and native_words and len(native_words) > 30 and (cyr_ratio < 0.35 or weird_ratio > 0.55):
            try:
                native_text = normalize_ocr_garbage(page.get_text("text"))
                native_text = re.sub(r"\S+", lambda m: fix_word_encoding(m.group(0)), native_text)
                if len(native_text) > 200:
                    n_cyr, n_weird = text_readability_metrics(native_text)
                    if n_cyr >= cyr_ratio and n_weird <= weird_ratio:
                        logging.info("    -> OCR текст низкого качества, fallback на native text layer")
                        full_text = native_text
            except Exception:
                pass

        return full_text

    def extract(self, source_id: str, file_path: Path, output_root: Path | None = None) -> int:
        root = output_root if output_root is not None else INGEST_DIR
        out_dir = root / source_id
        out_dir.mkdir(parents=True, exist_ok=True)
        pages_jsonl = out_dir / "pages.jsonl"

        page_buffer: list[tuple[int, str]] = []
        written_pages = 0
        try:
            doc = fitz.open(file_path)
            total_pages = len(doc)
            for i, page in enumerate(doc):
                logging.info(f"[{source_id}] Обработка страницы {i + 1}/{total_pages}...")
                
                # Растеризуем страницу
                dpi = 300 # 300 DPI дает радикально лучшее качество для EasyOCR на сканах
                zoom = dpi / 72.0 # Масштаб между PDF-поинтами и пикселями картинки
                pix = page.get_pixmap(dpi=dpi)
                img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    img_np = cv2.cvtColor(img_np, cv2.COLOR_RGBA2RGB)
                elif pix.n == 1:
                    img_np = cv2.cvtColor(img_np, cv2.COLOR_GRAY2RGB)
                    
                # Извлекаем нативный текстовый слой из PyMuPDF (координаты и слова)
                native_words = page.get_text("words")
                    
                formula_dir = out_dir / "assets" / "formulas"
                formula_ctr: list[int] = [0]
                text = self.process_image(
                    page,
                    img_np,
                    native_words,
                    zoom,
                    formula_dir,
                    i + 1,
                    formula_ctr,
                )
                
                if text.strip():
                    page_buffer.append((i + 1, text))
            doc.close()

            from scripts.ingest_router import strip_running_headers_footers

            texts = [t for _, t in page_buffer]
            stripped = strip_running_headers_footers(texts) if len(texts) >= 3 else texts
            for j, (pnum, orig) in enumerate(page_buffer):
                t = stripped[j] if j < len(stripped) else orig
                if not t.strip() and orig.strip():
                    t = orig
                page_buffer[j] = (pnum, t)

            with pages_jsonl.open("w", encoding="utf-8") as f:
                for pnum, text in page_buffer:
                    record = {
                        "source_id": source_id,
                        "page": pnum,
                        "text": text,
                        "parser": "yolo_doctr"
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written_pages += 1
            
            manifest = {
                "source_id": source_id,
                "file_path": str(file_path.relative_to(ROOT)).replace("\\", "/"),
                "format": "pdf",
                "status": "ok" if written_pages > 0 else "empty",
                "page_count": written_pages,
                "error": None,
                "notes": ["Parsed via DocLayout-YOLO + Native Text/EasyOCR (Hybrid)"],
                "route": "heavy",
                "ocr_mode": "on",
                "parser": "yolo_doctr"
            }
            with (out_dir / "manifest.json").open("w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logging.error(f"[{source_id}] Ошибка: {e}")
            return 0
            
        return written_pages

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WB)
    parser.add_argument("--only", type=str, default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=INGEST_DIR,
        help="Каталог вида data/ingested (подпапки SRC-xxxx с pages.jsonl и manifest.json)",
    )
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    if not args.workbook.is_file():
        logging.error(f"Файл реестра не найден: {args.workbook}")
        sys.exit(1)
        
    df = pd.read_excel(args.workbook, sheet_name="source_registry")
    only_ids = {s.strip() for s in args.only.split(",") if s.strip()}
    
    extractor = YoloDoctrExtractor()
    processed = 0
    
    for _, row in df.iterrows():
        src = str(row.get("source_id", "")).strip()
        fp = str(row.get("file_path", "")).strip()
        fmt = str(row.get("format", "")).strip().lower()
        
        if not src or src == "nan" or not fp:
            continue
        if only_ids and src not in only_ids:
            continue
            
        if fmt != "pdf":
            logging.warning(f"[{src}] Формат {fmt} не поддерживается в yolo_doctr, только pdf.")
            continue
            
        abs_path = (ROOT / fp).resolve()
        if not abs_path.is_file():
            logging.warning(f"[{src}] Файл не найден: {abs_path}")
            continue
            
        pages = extractor.extract(src, abs_path, output_root=output_root)
        if pages > 0:
            logging.info(f"[{src}] Успешно обработано страниц: {pages}")
            processed += 1
            
    logging.info(f"Завершено. Обработано: {processed}")

if __name__ == "__main__":
    main()
