FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDFBOX_JAR_PATH=/opt/pdfbox/pdfbox-app.jar \
    DEBIAN_FRONTEND=noninteractive

RUN mkdir -p /usr/share/man/man1 \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl openjdk-17-jre-headless \
    && mkdir -p /opt/pdfbox \
    && curl -fsSL --retry 3 --retry-delay 2 https://archive.apache.org/dist/pdfbox/3.0.3/pdfbox-app-3.0.3.jar -o /opt/pdfbox/pdfbox-app.jar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
