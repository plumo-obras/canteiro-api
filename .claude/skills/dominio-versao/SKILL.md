---
name: dominio-versao
description: Use ao alterar qualquer arquivo em `dominio/src/canteiro_dominio/` (modelos, regras, etapas, gastos, leitura, tipos, armazenamento), quando o job `versao-dominio` do CI falhar ("dominio/ mudou mas a versão não"), ao publicar a tag `dominio-vX.Y.Z`, ou quando o canteiro-agente precisar de um modelo/regra novo do domínio.
---

# Versão do pacote de domínio

`dominio/` é o pacote `canteiro-dominio`: modelos, regras puras e a porta de armazenamento, sem banco. A API o usa em
modo editável (`[tool.uv.sources]` no `pyproject.toml` da raiz). O **canteiro-agente** instala uma versão fixa pela
tag git:

```toml
canteiro-dominio = { git = "https://github.com/plumo-obras/canteiro-api", tag = "dominio-vX.Y.Z", subdirectory = "dominio" }
```

## O que pode ir no domínio

Conta e regra determinística (falta, transições de pedido, etapas, gastos), modelos de documento e a porta. **Nada**
de FastAPI, pymongo, rede ou configuração. Toda conta mora aqui, nunca no modelo de IA: o agente cita a `Conta` que a
API devolve.

## Passo a passo

1. Mude `dominio/src/canteiro_dominio/...` e teste em `dominio/tests/`
   (`cd dominio && .venv/bin/pytest`). Rode também a suíte da API, que usa o domínio editável.
2. Suba `version` em `dominio/pyproject.toml` no mesmo PR (semver: correção = patch; campo/regra nova compatível =
   minor; mudança que quebra quem já usa = major, e combine com o agente antes). Só `dominio/tests/` mudou? Não
   precisa subir. O job `versao-dominio` do CI confere isso.
3. Mudou modelo que trafega no JSON? Atualize `docs/contrato.md` (skill `rota-e-contrato`).
4. Depois do merge na `main`, publique a tag no commit do merge:

   ```bash
   git fetch origin && git tag dominio-vX.Y.Z origin/main && git push origin dominio-vX.Y.Z
   ```

   A tag é imutável na prática: não mova uma tag publicada; corrigiu algo, suba outra versão.
5. No canteiro-agente, troque o `tag` no `pyproject.toml`, atualize o lock (`uv lock`), rode os testes de lá e abra o
   PR. Mantenha a API compatível com a versão antiga enquanto o agente não sobe.

## Checklist

- [ ] Domínio sem dependência de banco, web ou config.
- [ ] Testes em `dominio/tests/` + suíte da API verdes.
- [ ] `version` subiu em `dominio/pyproject.toml` (CI `versao-dominio`).
- [ ] `docs/contrato.md` atualizado se o formato mudou.
- [ ] Tag `dominio-vX.Y.Z` publicada após o merge, sem mover tag antiga.
- [ ] PR no canteiro-agente apontando a tag nova.
