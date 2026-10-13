FROM python:3.13-slim
# OCR בעברית וקריאת PDF סרוק
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-heb poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir gunicorn || true
COPY cpa_ai ./cpa_ai
ENV CPA_AI_HOST=0.0.0.0 CPA_AI_PORT=8000 \
    CPA_AI_DATABASE_URL=sqlite:////data/cpa_ai.db CPA_AI_DATA_DIR=/data/files
VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v1/health')"
CMD ["python", "-m", "cpa_ai"]
