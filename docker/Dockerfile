# собирается из корня репозитория:
#   docker build -f docker/Dockerfile -t botmax .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --uid 10001 --create-home bot

COPY requirements/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot docker/*.py ./
COPY --chown=bot:bot docker/certs/ ./certs/
COPY --chown=bot:bot docker/assets/ ./assets/
COPY --chown=bot:bot docker/content/ ./content/
RUN mkdir -p /app/data && chown bot:bot /app/data

USER bot
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1
CMD ["python", "main.py"]
