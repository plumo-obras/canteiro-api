# canteiro-api

API central do Canteiro (agente de IA para gestão de obras): dona dos dados e das regras. FastAPI + a porta de
armazenamento portável. Também guarda o pacote `dominio/` (regras puras, modelos e a porta), que o
[canteiro-agente](https://github.com/plumo-obras/canteiro-agente) instala por tag (`dominio-vX.Y.Z`).

Os outros repositórios: [canteiro-web](https://github.com/plumo-obras/canteiro-web) (Next.js) e
[canteiro-agente](https://github.com/plumo-obras/canteiro-agente) (Claude + Tool Runner).

Leia antes de codar: `docs/arquitetura.md`, `docs/contrato.md` (o contrato entre API, agente e web; ele mora aqui) e
`docs/deploy.md`.

## Regras da casa

**O repositório é PÚBLICO.** Nada de segredo em arquivo, teste ou log: `.env` fica fora do git (há `.env.exemplo`);
chave da Anthropic da plataforma só em variável de ambiente; chave própria de cliente cifrada no banco. Pesquisa de
mercado e estratégia ficam em `pesquisa/` (ignorada pelo git).

**Banco portável: MongoDB hoje, Firestore (Firebase) depois.** Toda leitura e escrita passa pela porta
`canteiro_dominio.armazenamento`; o resto do código nunca importa `pymongo`. A porta só aceita o que o Firestore aceita:
igualdade e `in` (≤ 30), desigualdade em um campo só, ordenação começando por esse campo, limite. Sem joins, sem regex,
sem aggregation pipeline (relatório = projeção pré-calculada ou soma feita na API). Unicidade e idempotência pelo **ID do
documento**. Transação: leia tudo antes de escrever, ≤ 500 escritas. **Todo documento que a transação lê para decidir, ela também grava** (ao menos uma versão/`atualizado_em`): no Firestore a leitura entra no controle de conflito, no Mongo não, e é essa regra que deixa os dois com o mesmo comportamento. Nada de lista que cresce sem limite dentro de um
documento. Quantidade em milésimos e dinheiro em centavos (inteiros); IDs UUIDv7 gerados por nós. Um mesmo conjunto de
testes de contrato roda contra o adaptador em memória e o do Mongo (e, depois, o do Firestore).

**Toda conta no domínio, nunca no modelo de IA.** Falta, saldo, transições: `canteiro_dominio.regras`. O agente cita a
`Conta` devolvida pela API.

**O agente só propõe.** Escrita do agente = `POST /propostas`. Só a aprovação de uma pessoa executa, e toda execução gera
`EntradaRegistro` com `desfazer`. Não crie caminho de escrita do agente que pule isso.

**Multi-tenant e papel no servidor.** `tenant_id` em todo documento e em toda consulta (a porta exige). O papel vem do
banco a cada requisição, nunca do token. O agente age com o token do usuário.

**Consumo de IA medido.** Toda chamada ao modelo grava `UsoIA`; o saldo de créditos é checado antes da chamada.

**Comentário explica por quê, não o quê.** Português do Brasil no código de domínio e nas mensagens.

**Mudou o domínio? Publique a tag.** Mudança em `dominio/` que o agente precisa usar: suba a versão em
`dominio/pyproject.toml`, crie a tag `dominio-vX.Y.Z` e atualize o `tag` em `pyproject.toml` do canteiro-agente. Mudou o
contrato (`docs/contrato.md`)? Abra o ajuste no canteiro-web e no canteiro-agente junto.

## Comandos

```bash
docker compose up -d mongo                                # Mongo (replica set de 1 nó)
uv venv -p 3.14 .venv && uv pip install -p .venv -e ./dominio -e ".[dev]"
(cd dominio && uv venv -p 3.14 .venv && uv pip install -p .venv -e ".[dev]" && .venv/bin/pytest)   # regras
.venv/bin/pytest                                          # API (memória + o Mongo do compose)
.venv/bin/ruff check src tests
.venv/bin/uvicorn canteiro_api.main:app --reload
docker compose up -d --build                              # Mongo + API em contêiner (lê o .env)
docker compose -f docker-compose.yml -f docker-compose.completo.yml up -d --build   # + agente e web (repos lado a lado)
```
