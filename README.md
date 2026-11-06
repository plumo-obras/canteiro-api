# canteiro-api

API central da **Plumo** (o produto; o código ainda se chama Canteiro), um agente de IA para gestão de obras. Esta API
é **dona dos dados e das regras** de uma obra: orçamento em versões, cronograma e medições, compras, entregas e
estoque, gastos com comprovante, propostas do agente e o registro com desfazer. FastAPI, MongoDB hoje e Firestore
depois, pela mesma porta de armazenamento.

## Papel no sistema

| Repositório | O que é |
|---|---|
| **canteiro-api** (este) | API central + o pacote `dominio/` (regras puras, modelos e a porta de armazenamento) + `docs/` (contrato, arquitetura, deploy) |
| [canteiro-agente](https://github.com/plumo-obras/canteiro-agente) | serviço do agente (Claude + Tool Runner): conversa por SSE, usa a API com o token do usuário e **só propõe** escrita |
| [canteiro-web](https://github.com/plumo-obras/canteiro-web) | a aplicação web (Next.js) e a landing da Plumo |

```
web (Next.js) ──REST──► canteiro-api ◄──REST (token do usuário)── canteiro-agente (Claude)
     │                       │                                             ▲
     └──────── SSE (conversa) ┼─────────────────────────────────────────────┘
                              ▼
                  porta de armazenamento ──► MongoDB (Atlas) · Firestore depois
```

- A web fala com a API **pelo servidor do Next**; o navegador só chama direto a `POST /lista-espera` da landing (é a
  única rota com CORS).
- O agente nunca grava: chama `POST /propostas`, e só a aprovação de uma pessoa executa. Rotas internas
  (`POST /uso`, `GET /interno/chave-anthropic`) exigem o `TOKEN_SERVICO_AGENTE` **e** o token do usuário em
  `X-Canteiro-Usuario`.
- O contrato entre os três mora aqui: [`docs/contrato.md`](docs/contrato.md).

## Stack

Python 3.14 · FastAPI · Pydantic 2 / pydantic-settings · PyMongo assíncrono (sem Motor) · argon2 + PyJWT ·
`cryptography` (AES-GCM para a chave própria do cliente) · openpyxl (planilhas de orçamento) · uv · pytest +
pytest-asyncio · ruff. MongoDB 8 em replica set (transações) local; Atlas M0 em produção.

## Arquitetura

```
rotas/ (FastAPI: valida o corpo com esquemas.py, resolve usuário e papel)
  └─► servicos/ (regra de aplicação; lê e grava SÓ pela porta)
        ├─► canteiro_dominio.regras / etapas / gastos (contas puras, sem banco)
        └─► canteiro_dominio.armazenamento (a porta)
              ├─ armazenamento/memoria.py   (testes)
              └─ armazenamento/mongo.py     (único arquivo que importa pymongo)
```

- **Rota → serviço → porta.** A rota não fala com o banco; o serviço não importa `pymongo`. Erro de negócio sobe como
  `ErroNegocio` / `NaoEncontrado` / `Conflito` / `GrandeDemais` (`nucleo.py`) e vira `{"detail": "..."}`.
- **Porta portável.** `canteiro_dominio.armazenamento` só aceita o que o Firestore aceita: igualdade e `in` (≤ 30),
  desigualdade em um campo só, ordenação começando por esse campo, limite; sem join, regex ou agregação. Transação lê
  tudo antes da primeira escrita (≤ 500 escritas). Unicidade e idempotência pelo **ID do documento**. Dinheiro em
  centavos e quantidade em milésimos (inteiros); IDs UUIDv7 (`novo_id`) ou derivados (`id_derivado`). A migração para o
  Firestore vira "escrever o adaptador e passar na mesma suíte de contrato" (`tests/test_contrato_armazenamento.py`).
- **Toda escrita gera registro.** Uma operação é `preparar_*` (só lê, devolve `Operacao`) + `aplicar()` (só grava);
  `executar_e_registrar` grava a `EntradaRegistro` com a receita de **desfazer** na mesma transação, e o evento vai
  para a outbox junto (`nucleo.evento`).
- **Multi-tenant e papel no servidor.** `tenant_id` em todo documento e em toda consulta (a porta exige). O JWT só diz
  quem é e de qual cliente; o papel (`viewer` < `admin` < `master`) é lido do banco a cada requisição
  (`auth.resolver_usuario`, `auth.exigir`). Recurso de outro cliente responde **404**.
- **Pacote `dominio/`** (`canteiro-dominio`): modelos, regras e a porta, sem banco. A API o instala em modo editável; o
  agente instala uma versão fixa por **tag `dominio-vX.Y.Z`**:

  ```toml
  canteiro-dominio = { git = "https://github.com/plumo-obras/canteiro-api", tag = "dominio-vX.Y.Z", subdirectory = "dominio" }
  ```

  O CI (job `versao-dominio`) falha se `dominio/` mudar sem subir a versão em `dominio/pyproject.toml`.

Decisões e o porquê: [`docs/arquitetura.md`](docs/arquitetura.md).

## Rodar local

Com Docker (Mongo em replica set + API em http://localhost:8000):

```bash
cp .env.exemplo .env              # preencha JWT_SEGREDO, TOKEN_SERVICO_AGENTE e CHAVE_CIFRA (geradores no arquivo)
docker compose up -d --build
curl localhost:8000/saude         # {"ok":true}
```

Sem Docker para a API (recarga a quente):

```bash
docker compose up -d mongo
uv venv -p 3.14 .venv && uv pip install -p .venv -e ./dominio -e ".[dev]"
.venv/bin/uvicorn canteiro_api.main:app --reload
```

Dados de exemplo (a obra Casa Vila Nova do protótipo e um admin; a senha só no terminal ou no `.env`):

```bash
SEED_ADMIN_SENHA='…(12+ caracteres)' .venv/bin/python -m canteiro_api.seed
```

O produto inteiro, com os três repositórios clonados lado a lado:

```bash
git clone https://github.com/plumo-obras/canteiro-api
git clone https://github.com/plumo-obras/canteiro-agente
git clone https://github.com/plumo-obras/canteiro-web
cd canteiro-api && docker compose -f docker-compose.yml -f docker-compose.completo.yml up -d --build
# web em http://localhost:3000 · API :8000 · agente :8100 (modelo de roteiro, 0 tokens, sem ANTHROPIC_API_KEY)
```

## Testes e lint

```bash
.venv/bin/pytest                                   # API: cada teste roda contra a memória E contra o Mongo do compose
.venv/bin/ruff check src tests dominio
(cd dominio && uv venv -p 3.14 .venv && uv pip install -p .venv -e ".[dev]" && .venv/bin/pytest)   # regras puras
```

Sem o Mongo no ar, a metade "mongo" dos testes é pulada; com `EXIGIR_MONGO=1` (como no CI) ela falha. O CI
(`.github/workflows/ci.yml`) roda os testes do domínio, a conferência de versão do domínio, ruff + pytest com Mongo e o
build da imagem com `/saude`.

## Variáveis de ambiente

Lidas por `src/canteiro_api/config.py` (do ambiente ou do `.env`, que nunca vai para o git). Nenhum segredo tem valor
padrão. Modelo comentado em [`.env.exemplo`](.env.exemplo).

| Variável | Para quê |
|---|---|
| `AMBIENTE` | `desenvolvimento` (padrão) ou `producao`. Em produção a API **recusa subir** sem `ARQUIVOS=banco` e `ARMAZENAMENTO=mongo` (`Config.conferir`) |
| `ARMAZENAMENTO` | `mongo` (padrão) ou `memoria` |
| `MONGO_URL`, `MONGO_DB` | conexão do Mongo (precisa de replica set: transações) e nome do banco |
| `JWT_SEGREDO` | assina o JWT de acesso; obrigatório, ≥ 32 caracteres |
| `ACESSO_MINUTOS`, `RENOVACAO_DIAS` | validade do token de acesso e do de renovação |
| `JANELA_REUSO_RENOVACAO_SEGUNDOS` | renovação já usada que reaparece dentro dela recebe 409 em vez de derrubar a sessão |
| `TOKEN_SERVICO_AGENTE` | segredo com que o canteiro-agente chama as rotas internas (o mesmo valor lá) |
| `CHAVE_CIFRA` | 32 bytes em base64; cifra a chave própria da Anthropic de cada cliente no banco |
| `PROXY_SEGREDO` | segredo que o servidor do Next manda em `X-Canteiro-Proxy` para a API aceitar o IP do visitante |
| `IP_CABECALHO` | cabeçalho com o IP real que a borda sobrescreve (`cf-connecting-ip` no Render) |
| `TRUSTED_PROXIES` | redes de proxies de confiança para ler `X-Forwarded-For`; vazio = nunca lê |
| `TENTATIVAS_POR_EMAIL`, `TENTATIVAS_POR_IP`, `JANELA_TENTATIVAS_MINUTOS` | limite de tentativas de login (e de troca de senha) |
| `LISTA_ESPERA_POR_IP` | cadastros na lista de espera por IP na mesma janela |
| `CORS_ORIGENS` | origens que podem chamar `POST /lista-espera` do navegador; `*` é ignorado |
| `ARQUIVOS`, `ARQUIVOS_DIR` | onde ficam os comprovantes: `banco` (padrão, em pedaços no próprio Mongo), `disco` (pasta local, só desenvolvimento) ou `memoria` |
| `FRANQUIA_PADRAO_MICRODOLARES` | créditos de IA de um cliente novo (1 US$ = 1.000.000) |
| `SEED_ADMIN_EMAIL`, `SEED_ADMIN_SENHA` | admin criado por `python -m canteiro_api.seed`; a senha só no `.env` local |

Só dos testes: `MONGO_URL` (onde está o Mongo do compose) e `EXIGIR_MONGO=1` (falhar em vez de pular sem Mongo).

## Deploy

- **Render** (plano gratuito, Docker, `render.yaml`): publica sozinho **depois que o CI do GitHub passa**
  (`autoDeployTrigger: checksPass`). Saúde em `/saude`. O serviço gratuito dorme após 15 min sem uso.
- **Banco:** MongoDB Atlas M0. A API cria índices e TTL ao subir (`garantir_indices`, listas `INDICES` e `TTL` em
  `armazenamento/mongo.py`).
- **Guarda de produção:** o `render.yaml` declara `AMBIENTE=producao` e `ARQUIVOS=banco`; com `AMBIENTE=producao`, a
  API não sobe se `ARQUIVOS` não for `banco` ou `ARMAZENAMENTO` não for `mongo` (disco e memória perderiam os
  comprovantes a cada deploy).
- **Segredos** (`sync: false` no `render.yaml`) só no painel do Render. `JWT_SEGREDO` o Render gera.
- **Ordem entre repositórios:** mudança de contrato sai primeiro aqui (de forma compatível), depois agente e web.
  Mudança em `dominio/` que o agente usa: suba a versão, publique a tag `dominio-vX.Y.Z` e atualize o `tag` no
  `pyproject.toml` do canteiro-agente.

Passo a passo, TTL, arquivos no banco e o que está no ar: [`docs/deploy.md`](docs/deploy.md).

## Estrutura

```
src/canteiro_api/
  main.py            create_app(config, arm): app, tratadores de erro, CORS só na lista de espera, LimiteCorpo
  config.py          variáveis de ambiente + Config.conferir (guarda de produção)
  auth.py            login (argon2), JWT de acesso, renovação com rotação, troca de senha, exigir(papel)
  nucleo.py          ErroNegocio & cia., Operacao, registrar / executar_e_registrar, outbox, listar_pagina
  esquemas.py        corpos e respostas das rotas (Pydantic)
  modelos.py         Papel, Tenant, Usuario, Renovacao, EventoOutbox, doc()
  limite_corpo.py    limite do corpo por rota, antes de ler (413)
  arquivos.py        comprovantes: banco (pedaços de 512 KiB), disco, memória; isolamento por tenant
  cofre.py           AES-GCM da chave própria do cliente
  eventos.py         publicar_pendentes (outbox → publicador)
  seed.py            Casa Vila Nova de exemplo
  armazenamento/     base.py (normalizar, validar, TransacaoComBuffer), memoria.py, mongo.py
  rotas/             auth, obras, fornecedores, propostas (+ registro, uso), gastos, sessoes (+ conta), lista_espera
  servicos/          obras, orcamento, versoes, cronograma, estoque, pedidos, fornecedores, propostas, gastos,
                     importacao, resumo, sessoes, uso, tipo_arquivo
dominio/             pacote canteiro-dominio: modelos, regras, etapas, gastos, leitura, tipos, armazenamento (a porta)
tests/               conftest.py (fixtures arm/http/vila) + um arquivo por área
docs/                contrato.md, arquitetura.md, deploy.md
.claude/skills/      roteiros do Claude Code para este repositório (escrita, porta, rotas, segurança, testes, domínio)
```

## Leia também

- [`CLAUDE.md`](CLAUDE.md) — as regras da casa (repositório público, banco portável, o agente só propõe).
- [`docs/contrato.md`](docs/contrato.md) — o contrato entre API, agente e web. Mudou aqui, avise os outros dois.
- [`docs/arquitetura.md`](docs/arquitetura.md) e [`docs/deploy.md`](docs/deploy.md).
- [canteiro-agente](https://github.com/plumo-obras/canteiro-agente) · [canteiro-web](https://github.com/plumo-obras/canteiro-web)

O repositório é **público**: nenhum segredo, e-mail real, senha ou string de conexão em arquivo, teste ou log.
