---
name: testes
description: Use ao escrever, corrigir ou rodar testes da canteiro-api ou do pacote dominio — arquivo em `tests/` ou `dominio/tests/`, fixtures `arm`/`http`/`vila`/`tenant`/`config`, teste "pulado" por falta de Mongo, falha só no adaptador mongo, ou antes de abrir PR ("rodar os testes", "pytest", "ruff", "CI falhou").
---

# Testes

## Como são montados (`tests/conftest.py`)

- `arm` é parametrizada: **todo teste que usa `arm` (ou `http`/`vila`) roda duas vezes**, contra
  `ArmazenamentoMemoria` e contra o Mongo do compose (`_mongo_sessao`: um banco descartável
  `canteiro_teste_<hex>` por execução, apagado no fim). Sem limpeza entre testes: cada um usa tenant e IDs novos.
- Sem Mongo no ar, a metade "mongo" é **pulada**; com `EXIGIR_MONGO=1` (CI) ela falha.
- `config`: `Config` com segredos gerados na hora, `armazenamento="memoria"`, `arquivos="banco"`, `_env_file=None`.
- `http`: `httpx.AsyncClient` com `ASGITransport` sobre `create_app(config, arm)` e um IP aleatório por teste (o
  limite de tentativas não vaza entre testes).
- `vila` / `nova_vila(http, arm, config)`: semeia a Casa Vila Nova num tenant novo e loga um admin. Atalhos:
  `vila.h` (cabeçalho), `vila.obra`, `vila.insumo["bloco"|"aço"|"cimento"|"areia"]`, `vila.pedido["PED-0157"]`,
  `await vila.conta("aço")`, `h, u = await vila.usuario("viewer")` (outro usuário já logado: cabeçalho e documento; `tenant_id=` opcional).
- `tenant`: um `tenant_id` novo para testes direto na porta.
- Quantidade/dinheiro nos asserts com `qtd(...)` / `reais(...)` de `canteiro_dominio.tipos`.
- pytest-asyncio em modo `auto` com loop de sessão (`pyproject.toml`): `async def test_...` sem decorador.

## O que todo recurso novo testa

- **Isolamento por tenant (obrigatório):** um `intruso = await nova_vila(http, arm, config)` tenta ler e gravar no
  recurso da `vila` → 404, e o dado da vila não mudou. Modelo: `test_isolamento_entre_tenants` em
  `tests/test_estoque.py`.
- **Papel:** o papel abaixo do mínimo recebe 403 (`h, _ = await vila.usuario("viewer")`).
- **Escrita:** resposta traz `registro_id`; `POST /registro/{id}/desfazer` volta o estado; desfazer de novo → 409;
  repetir com a mesma `chave_idempotencia` devolve o mesmo resultado (ver `tests/test_pedidos_propostas.py`).
- **Consulta nova de rota:** `tests/test_indices.py` pega sozinho se faltar índice (só com Mongo).
- **Porta/adaptador:** caso em `tests/test_contrato_armazenamento.py` (roda nos dois adaptadores).
- **Regra pura:** em `dominio/tests/` (sem banco, sem fixtures da API).
- Nada de segredo ou e-mail real; senhas com `secrets.token_urlsafe`, e-mails `@teste.exemplo`.

## Rodar

```bash
docker compose up -d mongo                         # para a metade mongo não ser pulada
.venv/bin/pytest                                   # ou: .venv/bin/pytest tests/test_estoque.py -k isolamento
.venv/bin/ruff check src tests dominio
(cd dominio && .venv/bin/pytest)                   # venv próprio: uv venv -p 3.14 .venv && uv pip install -p .venv -e ".[dev]"
```

O CI (`.github/workflows/ci.yml`) roda domínio, conferência de versão do domínio, ruff + pytest com Mongo
(`EXIGIR_MONGO=1`) e o build da imagem; o Render só publica com tudo verde.

## Checklist

- [ ] Teste usa `arm`/`http`/`vila` (roda em memória e Mongo), não um adaptador fixo, salvo motivo.
- [ ] Teste de isolamento entre tenants (404) e de papel (403) para rota nova.
- [ ] Escrita: registro, desfazer e idempotência cobertos.
- [ ] Rodou com o Mongo do compose no ar (nada "skipped" na metade mongo) e `ruff check` limpo.
