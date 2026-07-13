FROM docker-hub.common.repositories.cloud.sap/chainguard/wolfi-base AS build

ENV PIP_INDEX_URL=https://int.repositories.cloud.sap/artifactory/api/pypi/proxy-3rd-party-pypi/simple \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apk add --no-cache python-3.13 && \
    python3.13 -m ensurepip && \
    python3.13 -m venv /opt/venv && \
    pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y wheel pip setuptools

RUN find /opt/venv -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) -exec rm -rf {} + 2>/dev/null; \
    find /opt/venv -type f -name "*.pyc" -delete; \
    true

FROM docker-hub.common.repositories.cloud.sap/chainguard/wolfi-base

RUN apk add --no-cache python-3.13

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY app/ ./app/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/.well-known/agent-card.json')" || exit 1

CMD ["python", "app/main.py", "--host", "0.0.0.0", "--port", "9000"]