# API central do Canteiro. Contexto de build = raiz deste repositório (o domínio mora em dominio/).
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1
WORKDIR /srv
COPY dominio dominio
COPY pyproject.toml ./
COPY src src
RUN uv pip install --system ./dominio . \
 && useradd --create-home --uid 10001 canteiro
USER canteiro
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/saude', timeout=4)"
# --proxy-headers: atrás do balanceador do Render, o IP real vem no X-Forwarded-For dele (só ele alcança o contêiner).
CMD ["sh", "-c", "uvicorn canteiro_api.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
