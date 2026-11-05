FROM python:3.11-slim-bookworm

# WeasyPrint 시스템 의존성 + 한글 폰트 (Noto Sans CJK KR)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf2.0-0 \
        libffi-dev \
        shared-mime-info \
        fonts-noto-cjk \
        fonts-noto-cjk-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

RUN pip install --no-cache-dir \
        fastapi \
        "uvicorn[standard]" \
        weasyprint \
        markdown \
        pygments \
        pydantic

COPY app /srv/app

ENV PORT_PROJECT_INTERNAL_TOKEN="" \
    PDF_SERVICE_DATA_DIR=/data \
    PDF_SERVICE_PUBLIC_BASE_URL=http://127.0.0.1:8102 \
    PORT_PROJECT_DOWNLOAD_TOKEN_TTL_SECONDS=3600

VOLUME ["/data"]

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
