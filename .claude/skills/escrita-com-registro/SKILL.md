---
name: escrita-com-registro
description: Use ao criar ou mudar QUALQUER operação que grava dado de obra/tenant na canteiro-api — rota POST/PATCH/PUT/DELETE nova, serviço com `em_transacao`, novo tipo de proposta do agente (TipoProposta), nova receita de desfazer (`_DESFAZER`), ou quando aparecer "desfazer", "registro", "auditoria", "proposta", "aprovar", "idempotência", "chave_idempotencia". Garante EntradaRegistro com desfazer, outbox e o agente só propondo.
---

# Escrita com registro e desfazer

Toda escrita de negócio gera uma `EntradaRegistro` (coleção `registro`) **na mesma transação**, com a receita
para desfazer. O agente nunca grava: ele cria proposta, e só a aprovação de uma pessoa executa.

## As peças (leia antes)

- `src/canteiro_api/nucleo.py`: `Operacao(obra_id, aplicar, texto, desfazer)`, `executar_e_registrar`, `registrar`,
  `evento` (outbox), `receita_inversa` / `verbo_desfazer` (refazer = desfazer o desfazer).
- `src/canteiro_api/servicos/propostas.py`: `criar_proposta` (valida e guarda, não executa), `_preparar_proposta`
  (o `match` por `TipoProposta`), `aprovar`, `recusar`, o mapa `_DESFAZER` e `desfazer`.
- Exemplo pequeno e completo: `servicos/fornecedores.py` (`editar` + `preparar_desfazer_edicao`).
- Modelos: `EntradaRegistro`, `Proposta`, `TipoProposta` em `dominio/src/canteiro_dominio/modelos.py`.

## Formato de uma operação

1. Fora da transação: valide o corpo, faça as **consultas** (a transação só lê por ID) e confira a obra com
   `obra_do_tenant` / `insumo_da_obra`.
2. Dentro de `arm.em_transacao(corpo_tx)`: `preparar_*` **só lê** (`tx.obter`) e devolve `Operacao`; `aplicar()` **só
   grava**. Nada de ler depois de escrever (`EscritaAntesDaLeitura`).
3. `executar_e_registrar(tx, u, op, origem=..., proposta_id=..., registro_id=...)` grava o resultado e o registro.
   Responda com `registro_id` para a tela oferecer Desfazer.
4. `evento(tx, u.tenant_id, "<area>.<fato>", obra_id, {...})` quando outro serviço (agente) precisa saber.

## Desfazer

- A receita é um `dict` com `"op"` + o mínimo para a inversa (IDs e `antes`/`depois` — nunca segredo ou hash).
- Escreva `preparar_<inversa>(tx, u, receita) -> Operacao` e registre em `_DESFAZER` (`servicos/propostas.py`).
- A inversa **confere que nada mudou depois** (ex.: `fornecedores.preparar_desfazer_edicao` compara `depois` e dá
  `Conflito`) e devolve, ela também, uma receita (`receita_inversa`) — assim Refazer funciona.
- Estoque e dinheiro são livro-razão: desfazer é **estorno** (movimento/gasto inverso), não apagar documento.
- Sem desfazer só com motivo escrito no código (ex.: troca de senha e chave da Anthropic: `desfazer=None`, comentado).

## Idempotência

- Escrita repetível recebe `chave_idempotencia` e vira **ID derivado** (`id_derivado(tenant_id, "<tipo>", chave)` em
  `servicos/estoque.py`); repetir cai em `ConflitoDeId` ou no documento existente, e devolve o mesmo resultado.
- Propostas: mesma chave = mesma proposta (200, `ja_existia: true`); chave igual com dados diferentes = 409; proposta
  igual pendente travada por `propostas_pendentes/{id_trava(...)}`. O agente manda `"{sessao_id}:{tool_use_id}"`.

## Novo tipo de proposta

1. `TipoProposta` + o modelo dos `dados` (domínio → bump de versão, ver skill `dominio-versao`).
2. `_dados` e `EFEITO_POR_TIPO` (`servicos/propostas.py`), `AJUSTAVEIS` (`esquemas.py`) e a validação em `criar_proposta` (confira referências **antes** de
   guardar: cartão que nunca poderia ser aprovado é bug).
3. Ramo em `_preparar_proposta` que reaproveita o mesmo `preparar_*` da tela (uma regra só), com `origem="agente"`.
4. `docs/contrato.md` (seção Propostas) e a ferramenta `propor_*` no canteiro-agente.

## Checklist

- [ ] Leitura toda antes da primeira escrita; consultas fora da transação.
- [ ] `executar_e_registrar` (ou `registrar`) na mesma transação; `registro_id` na resposta.
- [ ] Receita de desfazer + `preparar_*` em `_DESFAZER`, que recusa se o dado mudou depois.
- [ ] Chave de idempotência → ID derivado, quando a escrita pode ser repetida (rede, agente).
- [ ] Nenhum caminho em que o agente (token de serviço) grave sem proposta aprovada.
- [ ] Testes: executa, registra, desfaz, refaz, repetição idempotente e outro tenant → 404 (skill `testes`).
