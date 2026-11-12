---
name: rota-e-contrato
description: Use ao criar ou mudar um endpoint da canteiro-api (arquivo em `src/canteiro_api/rotas/`, esquema em `esquemas.py`, serviço em `servicos/`), ao mudar formato de corpo/resposta, código de status ou papel exigido, ou quando surgirem "endpoint", "rota", "contrato", "viewer/admin/master", "403", "404", "a web/o agente precisa de". Cobre rota → serviço → esquema, isolamento por tenant/obra, papéis, docs/contrato.md e a ordem de deploy entre os três repos.
---

# Rota e contrato

## Formato

- **Rota** (`rotas/<area>.py`, `r = APIRouter(...)`, incluída em `main.create_app`): só recebe o corpo validado
  (`esquemas.py`), resolve o usuário e o papel, chama o serviço e devolve `response_model`. Sem regra, sem banco.
  O armazenamento vem de `request.app.state.arm` (padrão `_arm` nas rotas). Modelo enxuto: `rotas/fornecedores.py`.
- **Serviço** (`servicos/<area>.py`): a regra de aplicação; recebe `arm` e `UsuarioAtual`; conta pura vai para
  `canteiro_dominio.regras`. Escrita segue a skill `escrita-com-registro`; banco, a `armazenamento-portavel`.
- **Erros:** levante `NaoEncontrado` (404), `Conflito` (409), `ErroNegocio` (422), `GrandeDemais` (413) de
  `nucleo.py`, ou `RegraViolada` do domínio (422). Resposta sempre `{"detail": "mensagem em português"}`.
- **Números no JSON:** centavos e milésimos inteiros; a formatação é da web e do agente.

## Isolamento (em TODA leitura e escrita)

- O tenant sai **só** de `u.tenant_id` (do token conferido no banco), nunca do corpo, da query ou de um campo que o
  agente informe.
- Recurso com `obra_id` na rota: confira com `obra_do_tenant(arm, u.tenant_id, obra_id)`; insumo com
  `insumo_da_obra`. Documento filho (pedido, gasto, comprovante): confira que o `obra_id` dele é o da rota.
- Recurso de outro tenant ou de outra obra → **404**, nunca 403 (não revela que existe).

## Papéis

- `Papel` em `modelos.py`: `viewer` < `admin` < `master` (`NIVEL`). Leitura: `Depends(usuario_atual)`. Ação de
  operação: `admin = exigir(Papel.ADMIN)`. Chave da Anthropic: `exigir(Papel.ADMIN)`. Propor é qualquer papel;
  aprovar é admin+.
- O papel é lido do banco a cada requisição (`auth.resolver_usuario`); rebaixar vale na hora. A web só esconde botão.
- Rotas do agente: `auth.servico_agente` / `auth.usuario_pelo_agente()` (token de serviço + `X-Canteiro-Usuario`).

## Contrato entre repositórios

1. Atualize `docs/contrato.md` no mesmo PR (rota, corpo, resposta, status, papel). Ele é a fonte da verdade.
2. Avise no relatório/PR o que muda para o **canteiro-web** (telas, proxy do Next) e o **canteiro-agente**
   (ferramentas em `docs/contrato.md` § Agente) e abra os ajustes lá.
3. Ordem de deploy: **API primeiro, compatível** (campo novo opcional, rota nova ao lado da velha); depois agente e
   web; só então remova o antigo. O Render publica a API quando o CI passa (`autoDeployTrigger: checksPass`).
4. Mudou algo em `dominio/` que o agente usa? Skill `dominio-versao`.
5. Rota com upload ou corpo grande: registre o limite em `limite_corpo._ROTAS` (skill `seguranca-auth`).
6. Consulta nova: índice em `INDICES` (skill `armazenamento-portavel`).

## Checklist

- [ ] Rota fina → serviço → esquema; `response_model` declarado.
- [ ] Tenant só de `u.tenant_id`; `obra_do_tenant`/`insumo_da_obra`; outro tenant/obra → 404.
- [ ] Papel mínimo certo (`usuario_atual` / `exigir(Papel.ADMIN|MASTER)`), com teste de 403 para o papel abaixo.
- [ ] Erros com `ErroNegocio` & cia., mensagem em português.
- [ ] `docs/contrato.md` atualizado; web e agente avisados; mudança compatível para o deploy em ordem.
- [ ] Testes: caminho feliz, 404 de outro tenant, 403 de papel, `ruff check src tests dominio`.
