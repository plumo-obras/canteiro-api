---
name: armazenamento-portavel
description: Use ao ler ou gravar no banco na canteiro-api — nova coleção, nova `Consulta`/`Filtro`, transação (`em_transacao`), índice em `INDICES`/`TTL`, campo de dinheiro ou quantidade, ID de documento, mexer em `armazenamento/` (memoria.py, mongo.py, base.py) ou em `canteiro_dominio.armazenamento`, ou quando surgir "Firestore", "Mongo", "pymongo", "agregação", "join", "índice", "COLLSCAN". Mantém tudo compatível com Firestore.
---

# Armazenamento portável (Mongo hoje, Firestore depois)

O código fala só com a porta `canteiro_dominio.armazenamento` (`dominio/src/canteiro_dominio/armazenamento.py`).
Ela é **menor que o Mongo de propósito**: só o que o Firestore também faz. Migrar = escrever o adaptador Firestore e
passar na mesma suíte de contrato.

## Onde fica cada coisa

- Porta: `Consulta`, `Filtro`, `Transacao`, `Armazenamento`, `ConflitoDeId`, `LIMITE_IN = 30`,
  `LIMITE_ESCRITAS_TRANSACAO = 500`.
- Comum aos adaptadores: `src/canteiro_api/armazenamento/base.py` (`normalizar`, `validar_doc`, `validar_campos`,
  `validar_consulta`, `TransacaoComBuffer`, `EscritaAntesDaLeitura`, `DocumentoAusente`).
- `armazenamento/memoria.py` (testes) e `armazenamento/mongo.py` — **o único arquivo que importa `pymongo`**, com
  `INDICES`, `TTL` e `garantir_indices`.

## Regras

- **Consulta:** igualdade e `in` (≤ 30); desigualdade (`< <= > >=`) em **um campo só**, e a 1ª ordenação nesse
  campo; limite. Sem join, regex, `$or`, aggregation pipeline. Relatório = soma em Python linha a linha
  (`servicos/uso.py`) ou projeção gravada (ex.: `saldos`).
- **`tenant_id` sempre:** todo documento tem `id` (str, sem `/`) e `tenant_id`; toda `obter`/`consultar`/`atualizar`
  recebe o tenant. Dado global usa `TENANT_SISTEMA` (`modelos.py`). `id`, `_id` e `tenant_id` não se atualizam.
- **Transação:** leia tudo (`tx.obter`, só por ID) antes da primeira escrita; nada de `consultar` dentro dela — faça
  a consulta antes e releia por ID lá dentro. ≤ 500 escritas. O corpo pode rodar mais de uma vez (conflito → repete):
  sem efeito colateral fora do `tx`.
- **O que decide, grava:** todo documento que a transação lê para decidir, ela também atualiza (ao menos
  `atualizado_em`). No Firestore a leitura entra no controle de conflito; no Mongo não — é essa regra que deixa os
  dois iguais (ver a trava `propostas_pendentes` em `servicos/propostas.py`).
- **Unicidade e idempotência pelo ID:** não existe índice único. Use `id_derivado(tenant_id, ...)`
  (`servicos/estoque.py`) e trate `ConflitoDeId`. IDs novos: `novo_id()` (UUIDv7, `canteiro_dominio.tipos`).
- **Números:** dinheiro em **centavos**, quantidade em **milésimos**, inteiros (`Centavos`, `Milesimos`, `qtd`,
  `reais`). Nunca float nem Decimal no banco. Créditos de IA em microdólares.
- **Datas:** sempre com fuso, UTC; `normalizar` trunca no milissegundo. Data sem fuso é erro.
- **Documento limitado:** nenhuma lista que cresce sem limite dentro de um documento (vira coleção, como
  `mensagens`, `movimentos`, `arquivos_pedacos`). Máx. ~1 MiB por documento no Firestore.
- **Listagem paginada:** `nucleo.listar_pagina` (cursor exclusivo, completa o grupo do empate).

## Índice novo

Toda consulta nova de rota precisa do índice em `INDICES` (`armazenamento/mongo.py`): igualdades, depois o campo da
desigualdade/ordem, **terminando em `_id`**, com `tenant_id` na frente. `tests/test_indices.py` espiona as consultas
das rotas e falha em `COLLSCAN` ou `SORT` em memória. Coleção que vence sozinha: campo `expira_em` (data) + `TTL`.
Ao migrar, cada entrada vira `firestore.indexes.json` (ver `docs/deploy.md` §8).

## Checklist

- [ ] Nada de `pymongo` fora de `armazenamento/mongo.py`.
- [ ] Consulta passa em `Consulta.validar()` e tem índice em `INDICES` terminando em `_id`.
- [ ] Transação: leituras por ID primeiro, sem `consultar`, ≤ 500 escritas, quem decide é gravado.
- [ ] Dinheiro/quantidade inteiros; datas UTC com fuso; IDs `novo_id`/`id_derivado`, sem `/`.
- [ ] Mudou a porta ou um adaptador? Teste em `tests/test_contrato_armazenamento.py` (roda em memória e Mongo).
- [ ] `.venv/bin/pytest` com o Mongo do compose no ar (`docker compose up -d mongo`).
