FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY pyproject.toml /app/
COPY shared /app/shared
COPY trusted /app/trusted

RUN useradd --create-home --shell /bin/bash browseruser \
    && pip install --no-cache-dir ".[browser]" \
    && python -m playwright install --with-deps chromium \
    && mkdir -p /ms-playwright /home/browseruser/.cache \
    && chown -R browseruser:browseruser /ms-playwright /home/browseruser

USER browseruser
ENV HOME=/home/browseruser

EXPOSE 8083
