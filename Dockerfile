# syntax=docker/dockerfile:1
# API central do Canteiro. Contexto de build = raiz deste repositório (o domínio mora em dominio/).
# Imagens travadas pelo digest do índice multi-arquitetura: a mesma tag pode apontar para outra imagem amanhã, e o que
# o CI testou tem de ser o que o Render publica. O Dependabot (ecossistema docker) sobe tag e digest juntos; o uv é um
# estágio com nome, e não um --mount=from=<imagem>, porque o Dependabot só lê as linhas FROM.
FROM ghcr.io/astral-sh/uv:0.13@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH=/srv/.venv/bin:$PATH
WORKDIR /srv
# Versões vêm do uv.lock (--frozen): o Render instala exatamente o que o CI testou. O uv entra só durante o build
# (--mount) e não fica na imagem. Dependências antes do código, para mudança no src não reinstalar tudo.
COPY pyproject.toml uv.lock ./
COPY dominio dominio
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-editable \
 && useradd --create-home --uid 10001 canteiro
USER canteiro
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/saude', timeout=4)"
# Sem --proxy-headers: com --forwarded-allow-ips='*' o uvicorn usava o endereço mais à esquerda do X-Forwarded-For,
# que o próprio cliente escreve. O IP do visitante sai de IP_CABECALHO (a borda sobrescreve) ou do segredo do Next.
CMD ["sh", "-c", "uvicorn canteiro_api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
