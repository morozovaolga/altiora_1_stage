"""Нормализация текста для UTF-8 / JSON (суррогаты из OCR и т.п.)."""


def sanitize_utf8_json(obj):
    """
    Рекурсивно заменяет в строках символы, которые нельзя закодировать в UTF-8
    (одиночные суррогаты U+D800–U+DFFF и т.д.), на U+FFFD.
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = sanitize_utf8_json(k) if isinstance(k, str) else k
            out[nk] = sanitize_utf8_json(v)
        return out
    if isinstance(obj, list):
        return [sanitize_utf8_json(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_utf8_json(x) for x in obj)
    return obj
