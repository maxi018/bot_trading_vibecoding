# syntax=docker/dockerfile:1
# quantbot: bot + API + dashboard en una sola imagen.
#   docker compose up --build        (ver docker-compose.yml)

# ---- 1) build del dashboard (solo se usa para compilar; no llega a la imagen final)
FROM node:22-slim AS dashboard
WORKDIR /build/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# ---- 2) runtime: Python + el bot + el build estático del dashboard
FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --system --create-home --uid 10001 quantbot
WORKDIR /app/trading_bot
COPY trading_bot/pyproject.toml trading_bot/README.md ./
COPY trading_bot/quantbot ./quantbot
RUN pip install ".[dashboard,live]"
COPY trading_bot/config ./config
COPY --from=dashboard /build/dashboard/dist /app/dashboard/dist
RUN mkdir -p state data && chown -R quantbot:quantbot state data

USER quantbot
# Dentro del contenedor la API escucha en todas las interfaces; el compose la
# publica SOLO en 127.0.0.1 del host.
ENV QB_API_HOST=0.0.0.0 \
    QB_DASHBOARD_DIST=/app/dashboard/dist
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)" || exit 1
# SIGTERM -> safe shutdown del bot (cancela entradas, deja los stops nativos vivos)
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "quantbot"]
CMD ["demo", "--config", "config/demo.toml"]
