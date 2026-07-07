FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDFBOX_JAR_PATH=/opt/pdfbox/pdfbox-app.jar \
    DEBIAN_FRONTEND=noninteractive

# openjdk: PDFBox(스캔 PDF 폴백) · libreoffice-impress: 레거시 .ppt → .pptx 변환(soffice, RPA-44)
# fonts-noto-cjk: LibreOffice 변환 시 한국어 글리프 (없으면 일부 텍스트가 깨질 수 있음)
RUN mkdir -p /usr/share/man/man1 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        curl openjdk-17-jre-headless libreoffice-impress fonts-noto-cjk \
    && mkdir -p /opt/pdfbox \
    && curl -fsSL --retry 3 --retry-delay 2 https://archive.apache.org/dist/pdfbox/3.0.3/pdfbox-app-3.0.3.jar -o /opt/pdfbox/pdfbox-app.jar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
