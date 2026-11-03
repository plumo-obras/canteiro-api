# canteiro-api

API central do Canteiro: dona dos dados e das regras de uma obra (orçamento, cronograma e medições, compras e
estoque, propostas do agente, registro com desfazer). FastAPI, MongoDB hoje e Firestore depois, pela mesma porta de
armazenamento.

| Repositório | O que é |
|---|---|
| **canteiro-api** (este) | API + o pacote `dominio/` (regras puras e porta de armazenamento) + `docs/` (contrato, arquitetura, deploy) |
| [canteiro-agente](https://github.com/plumo-obras/canteiro-agente) | serviço do agente (Claude), que só propõe escrita |
| [canteiro-web](https://github.com/plumo-obras/canteiro-web) | a aplicação web (Next.js) e a landing |

## Rodar

```bash
cp .env.exemplo .env                       # preencha JWT_SEGREDO, CHAVE_CIFRA, TOKEN_SERVICO_AGENTE (geradores no arquivo)
docker compose up -d --build               # Mongo em replica set + API em http://localhost:8000
curl localhost:8000/saude
```

Sem Docker para a API (recarga a quente):

```bash
docker compose up -d mongo
uv venv -p 3.14 .venv && uv pip install -p .venv -e ./dominio -e ".[dev]"
.venv/bin/uvicorn canteiro_api.main:app --reload
.venv/bin/pytest
```

O produto inteiro, com os três repositórios clonados lado a lado:

```bash
git clone https://github.com/plumo-obras/canteiro-api
git clone https://github.com/plumo-obras/canteiro-agente
git clone https://github.com/plumo-obras/canteiro-web
cd canteiro-api && docker compose -f docker-compose.yml -f docker-compose.completo.yml up -d --build
# web em http://localhost:3000 · API :8000 · agente :8100 (modelo de roteiro, 0 tokens, sem ANTHROPIC_API_KEY)
```

## Domínio

`dominio/` é o pacote `canteiro-dominio`: regras sem banco (conta da falta, transições de pedido, propostas) e a porta
`canteiro_dominio.armazenamento`. O agente instala uma versão fixa por tag:

```toml
canteiro-dominio = { git = "https://github.com/plumo-obras/canteiro-api", tag = "dominio-v0.1.0", subdirectory = "dominio" }
```

O repositório é público: segredo nenhum em arquivo, teste ou log. Veja `CLAUDE.md` e `docs/deploy.md`.
