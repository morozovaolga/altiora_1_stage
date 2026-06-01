FROM python:3.10-slim

# Отключаем буферизацию вывода Python (полезно для логов)
ENV PYTHONUNBUFFERED=1

# Системные зависимости: CV/OCR + libreoffice (headless Office→PDF для FastAPI)
RUN mkdir -p /usr/share/man/man1 && \
    apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-rus \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Установка Python-библиотек
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода проекта (каталог data/ не входит в контекст — см. .dockerignore)
COPY . .

# Пустые рабочие каталоги для рантайма API (ingest → чанки); без старых SRC/API-артефактов.
RUN mkdir -p data/ingested data/chunks_structural data/api_uploads data/chunks

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]