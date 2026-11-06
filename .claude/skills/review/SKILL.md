---
name: review
description: Use para revisar um PR, branch ou atividade do canteiro-api antes do merge — sempre que pedirem "revisa o PR #N", "faz o review", "pode mergear?", "passa a régua", "review da feature/atividade X", ou ao terminar uma tarefa grande e quiser se auto-revisar. Dá veredito (aprova / aprova com ajustes / reprova) com nota pela régua da casa.
---

# Review de PR no canteiro-api

A mesma régua para todo PR do Plumo (nos 3 repos), para que "aprova" signifique a mesma coisa sempre. O review
**confere rodando**, não só lendo o diff: um PR verde no branch dele pode quebrar em cima da `main` de hoje.

## 1. Juntar o contexto

```bash
gh pr view N --json title,author,body,headRefName,baseRefName,files,commits,statusCheckRollup,mergeable,isDraft
gh pr diff N > /tmp/prN.diff
```
- Leia a descrição e os commits: o que o PR diz que faz, e o que ficou de fora.
- `mergeable: CONFLICTING` é **bloqueante**: alguém mudou o mesmo arquivo na `main` depois do PR abrir. Descubra o
  que a `main` trouxe (`git log origin/main -- <arquivo>`) — o conflito costuma esconder uma regra nova que o PR
  precisa respeitar.
- Feature que atravessa os 3 repos: revise os PRs juntos e diga a **ordem de merge/deploy** (API compatível primeiro;
  domínio novo → tag `dominio-vX.Y.Z` → agente aponta a tag → web).

## 2. Conferir rodando, sobre a `main` atual

Num worktree próprio (não suje a pasta de quem está trabalhando):
```bash
git fetch -q origin && git fetch -q origin pull/N/head:prN
git worktree add /tmp/wt-prN origin/main && cd /tmp/wt-prN && git merge --no-commit prN
```
```bash
uv sync -q --all-extras && uv run -q pytest -q && uv run -q ruff check .
```
(os testes rodam com armazenamento em memória e, havendo Mongo, também nele — `EXIGIR_MONGO=1` obriga.)
Se um teste do PR falhar só por causa do conflito, diga isso — não é defeito do PR, é o rebase que falta.
Remova o worktree no fim (`git worktree remove --force /tmp/wt-prN`).

## 3. Passar a régua

**Vetos** — qualquer um reprova, independente da nota:
- **V1** escrita fora do fluxo da casa: toda escrita gera `EntradaRegistro` com desfazer; agente/IA só **propõe**
  (nada gravado sem aprovação humana).
- **V2** isolamento entre clientes: toda leitura e escrita escopada por tenant (e obra); recurso de outro tenant é 404.
- **V3** segredo ou dado real no repo **público**: chave, token, senha, id de serviço, string de conexão, comprovante,
  CNPJ/CPF ou nome de pessoa real em fixture ou log.
- **V4** porta de armazenamento incompatível com Firestore, dinheiro fora de centavos, ou conta de dinheiro fora do
  domínio/API.
- **V5** CI vermelho ou teste que não roda.

**Critérios** (nota 0–10 × peso):
| | Critério | Peso |
|---|---|---|
| C1 | Correção: bugs, casos de borda, concorrência, idempotência | 3 |
| C2 | Segurança: isolamento por tenant, papel conferido no servidor, limites de corpo e de upload (magic bytes, tamanho), nada de segredo ou PII em log, autenticação e renovação intactas, custo de IA registrado por cliente | 3 |
| C3 | Testes: cobrem a regra nova, falham sem ela, isolamento por tenant testado | 2 |
| C4 | Contrato entre os 3 repos: `docs/contrato.md`, tipos e chamadas consistentes, ordem de deploy, dado antigo migrado | 2 |
| C5 | Aderência ao projeto: CLAUDE.md, português, comentário explica o porquê, padrões existentes, lint/tipos | 1 |
| C6 | Operação: índice para toda consulta nova (`INDICES`), deploy sem passo manual esquecido, dado já gravado migrado ou lido com padrão, mensagens de erro em português que a web mostra direto | 1 |

Nota = Σ(nota × peso) / 12. **Veredito:** algum veto → REPROVA · ≥ 8,0 → APROVA · 6,0–7,9 → APROVA COM AJUSTES
(liste os bloqueantes) · < 6,0 → REPROVA. Conflito com a `main` não muda a nota, mas impede o merge até resolver.

## 4. O que olhar neste repo

- **Escrita**: `preparar_*` só lê, `aplicar()` só grava, `executar_e_registrar` na mesma transação, receita de
  desfazer registrada; proposta idempotente. Skill `escrita-com-registro`.
- **Porta**: nada de `consultar` dentro de transação, ler antes de escrever, igualdade/`in` ≤ 30, desigualdade em um
  campo só, ≤ 500 escritas; `pymongo` só em `mongo.py`. Skill `armazenamento-portavel`.
- **Rota**: fina, erro vindo de `nucleo.py`, tenant de `u.tenant_id` + `obra_do_tenant`, papel com `exigir(...)`.
  Teste de 404 para outro tenant e 403 para papel. Skills `rota-e-contrato` e `testes`.
- **Contrato**: `docs/contrato.md` atualizado no mesmo PR; diga o que muda para web e agente e a ordem de deploy.
- **Domínio**: mudou `dominio/`? `version` subiu (o CI cobra), e depois do merge sai a tag nova e o agente aponta
  para ela. Skill `dominio-versao`.
- **Segurança**: limites (`LimiteCorpo`, `detectar`), IP pelo `_ip`, segredo novo só por variável. Skill
  `seguranca-auth`.
- **Dinheiro**: centavos inteiros; conta no domínio, não na rota.

## 5. Escrever o review

Formato (é o que vai como comentário no PR — só publique quando o humano pedir):
```markdown
# Review — <título curto>

**Veredito: APROVA COM AJUSTES** · nota **7,1**.
C1 7 · C2 7,5 · C3 7 · C4 7 · C5 8 · C6 5,5. Nenhum veto: <por quê, em uma linha>. <O que foi rodado e o resultado.>

### Bloqueantes
1. **<problema>** (`arquivo:linha`): <cenário concreto que quebra> → <o que fazer>.

### Não bloqueantes
- `arquivo:linha`: <problema> → <sugestão>.

### O que está bom
<o que o PR acertou e deve continuar assim — específico, não elogio genérico>
```
Regras: cada bloqueante tem **cenário concreto** (entrada → resultado errado), não "pode dar problema"; cite
`arquivo:linha`; separe o que é do PR do que já estava na `main`; não invente achado para encher.

## 6. Se pedirem para ajustar e mergear

Ajuste no branch do PR (merge da `main` + correções num commit que lista o que mudou e por quê), rode tudo de novo,
espere o CI, `gh pr ready N` se for rascunho, `gh pr merge N --merge`, e acompanhe o deploy. Push e merge só com o
pedido explícito do humano.

## Checklist
- [ ] Li descrição, commits e diff inteiro
- [ ] Mesclei sobre a `main` atual e rodei pytest e ruff
- [ ] Passei pelos 5 vetos explicitamente
- [ ] Nota por critério + veredito pela régua
- [ ] Bloqueantes com cenário concreto e `arquivo:linha`
- [ ] Disse a ordem de merge/deploy se a mudança atravessa repos
