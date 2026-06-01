"""
Постобработка структурных чанков (Enrichment).
- Объединяет формулы с их расшифровками ("где M - ...").
- Извлекает сущности (entities) с помощью Regex.
"""
import json
import re
import logging
from pathlib import Path

try:
    from scripts.text_sanitize import sanitize_utf8_json
except ImportError:
    from text_sanitize import sanitize_utf8_json

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

ROOT = Path(__file__).resolve().parents[1]
CHUNKS_DIR = ROOT / "data" / "chunks_structural"

def extract_entities(text: str) -> list:
    entities = []
    
    # 1. Ссылки на таблицы, рисунки и приложения
    for match in re.finditer(r'(?i)\b(таблиц[еуа]\s+\d+(?:\.\d+)*)\b', text):
        entities.append({"type": "table_ref", "value": match.group(1).lower()})
    for match in re.finditer(r'(?i)\b(рис\.|рисунок)\s+\d+(?:\.\d+)*\b', text):
        entities.append({"type": "figure_ref", "value": match.group(0).lower()})
    for match in re.finditer(r'(?i)(приложени[еяю])\s+[\d\.]+', text):
        entities.append({"type": "reference", "value": match.group(0).lower()})
        
    # 2. Единицы измерения
    for match in re.finditer(r'\b\d+(?:[\.,]\d+)?\s*(?:г/с|мг/м.{0,2}|т/год|ПДК|°C|кг|м³|г/м³|т/г)\b', text):
        entities.append({"type": "measurement", "value": match.group(0).replace(',', '.')})
        
    # 3. Нормативные документы (ГОСТ, СанПиН, ФЗ)
    for match in re.finditer(r'(?i)(ГОСТ\s+Р?\s*\d+-\d+|СанПиН\s+\d\.\d\.\d\.\d+-\d+|ФЗ[ -]\d+)', text):
        entities.append({"type": "normative_ref", "value": match.group(1).upper()})
        
    return entities

def process_file(filepath: Path) -> tuple[int, int]:
    with open(filepath, 'r', encoding='utf-8') as f:
        chunks = [json.loads(line) for line in f if line.strip()]

    enriched = []
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        
        # Заполняем пустые сущности
        if not chunk.get('entities'):
            chunk['entities'] = extract_entities(chunk['text'])

        # Если это формула, проверяем следующий блок на наличие "где..."
        if chunk.get('block_type') == 'formula' and i + 1 < len(chunks):
            next_chunk = chunks[i+1]
            text_lower = next_chunk.get('text', '').strip().lower()
            
            if next_chunk.get('page') == chunk.get('page') and re.match(r'^где[\s:]', text_lower):
                # Добавляем расшифровку в отдельное мета-поле, чтобы не ломать Markdown формулы
                chunk['formula_vars'] = next_chunk['text']
                
                # Извлекаем сущности из расшифровки и объединяем
                if not next_chunk.get('entities'):
                    next_chunk['entities'] = extract_entities(next_chunk['text'])
                    
                chunk['entities'].extend(next_chunk['entities'])
                
                # Сохраняем обогащенный чанк и пропускаем следующий (мы его поглотили)
                enriched.append(chunk)
                i += 2
                continue
                
        # Склеиваем разорванные списки на одной странице
        if (enriched and enriched[-1].get('block_type') == 'list' 
                and chunk.get('block_type') == 'list'
                and chunk.get('page') == enriched[-1].get('page')):
            enriched[-1]['text'] += '\n' + chunk['text']
            enriched[-1]['entities'].extend(chunk['entities'])
            i += 1
            continue
        
        enriched.append(chunk)
        i += 1

    # Перезаписываем файл
    with open(filepath, 'w', encoding='utf-8') as f:
        for c in enriched:
            f.write(json.dumps(sanitize_utf8_json(c), ensure_ascii=False) + '\n')
            
    return len(chunks), len(enriched)

if __name__ == "__main__":
    for file_path in CHUNKS_DIR.glob("*.jsonl"):
        orig_len, new_len = process_file(file_path)
        if orig_len - new_len > 0:
            logging.info(f"[{file_path.name}] Объединено {orig_len - new_len} расшифровок формул.")