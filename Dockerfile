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
COPY alembic.ini .
COPY migrations ./migrations

EXPOSE 8000

# --proxy-headers: ALB 뒤에선 이게 없으면 request.client.host가 전부 ALB 사설 IP가 되어
# audit_logs의 클라이언트 추적이 무의미해진다 (RPA-222). forwarded-allow-ips="*"는
# X-Forwarded-For 위조를 아무에게나 허용한다는 뜻이지만, 인스턴스는 private subnet에서
# ALB SG의 8000 포트만 받으므로 헤더를 조작해 도달할 수 있는 주체가 없다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
