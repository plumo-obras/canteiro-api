---
name: seguranca-auth
description: Use ao mexer em login, sessão ou segredo na canteiro-api — `auth.py`, `rotas/auth.py`, JWT, renovação, troca de senha, limite de tentativas, `limite_corpo.py`, upload de arquivo (comprovante, planilha), `tipo_arquivo.py`, `arquivos.py`, `cofre.py`, IP do cliente (`IP_CABECALHO`, `PROXY_SEGREDO`, `TRUSTED_PROXIES`), CORS, logs, ou ao adicionar variável secreta. Também ao escrever qualquer arquivo, teste ou log: o repositório é PÚBLICO.
---

# Segurança e autenticação

## Repositório público

Nada de segredo, e-mail real, senha, string de conexão ou ID de serviço em arquivo, teste, log ou mensagem de
commit. Segredo novo: campo em `config.py` **sem valor padrão**, linha comentada em `.env.exemplo` (vazia, com o
gerador), `sync: false` no `render.yaml`. Testes geram segredo na hora (`secrets.token_urlsafe`, ver
`tests/conftest.py`) e usam e-mails `@teste.exemplo`.

## Sessão (`src/canteiro_api/auth.py`)

- Senha: argon2 (`hash_senha`, `senha_confere`) rodando em `asyncio.to_thread`; e-mail inexistente gasta o mesmo
  tempo (`_HASH_FANTASMA`) e a mensagem é a mesma.
- Acesso: JWT HS256 curto (`ACESSO_MINUTOS`), claims `sub`, `tid`, `typ="acesso"`, `ems`. **Papel nunca no token**:
  `resolver_usuario` lê usuário, papel e `ativo` do banco a cada requisição.
- Renovação: token opaco, guardado só o hash (`_sha`), **troca a cada uso** (`renovar`), família revogável.
  Reuso fora de `JANELA_REUSO_RENOVACAO_SEGUNDOS` = vazamento → `_revogar_familia`; dentro dela → 409 sem emitir nada.
- Troca de senha (`trocar_senha`, `POST /auth/senha`): confere a atual, conta no mesmo limite do login, grava
  `sessoes_validas_desde` (derruba acessos e renovações anteriores), devolve sessão nova e registra **sem desfazer e
  sem nada da senha**.
- Limite de tentativas por e-mail e por IP (`_reservar_tentativa`, coleção `tentativas` com TTL).
- Comparação de segredo sempre `hmac.compare_digest` em **bytes** (`servico_agente`, `_ip`).

## IP do visitante (`rotas/auth.py::_ip`)

Ordem: `PROXY_SEGREDO` válido em `X-Canteiro-Proxy` → `X-Canteiro-Ip-Cliente`; senão `IP_CABECALHO`
(`cf-connecting-ip` no Render, a borda sobrescreve); senão `X-Forwarded-For` só vindo de `TRUSTED_PROXIES`, lido da
direita. Nunca `--proxy-headers` no uvicorn (ver `Dockerfile`).

## Corpo e upload

- `limite_corpo.LimiteCorpo` recusa antes de ler (Content-Length) e conta o fluxo chunked: 1 MiB padrão; rotas
  maiores em `_ROTAS` (`LIMITE_UPLOAD`, `LIMITE_COMPROVANTE`, `LIMITE_MENSAGENS`). Rota nova com upload = entrada lá.
- Tipo pelo conteúdo, não pelo `Content-Type`: `servicos/tipo_arquivo.detectar` (magic bytes JPG/PNG/WEBP/PDF e
  lado ≤ `LADO_MAX`). Tamanhos em `servicos/gastos.py` (`MAX_ARQUIVO`, `MAX_IMAGEM`, `MAX_LEGENDA`) e
  `arquivos.MAX_TAMANHO`; leia no máximo `limite + 1` bytes.
- Caminho de arquivo: `arquivos.conferir_caminho` (`<obra_id>/<nome>`, sem `..`, barra invertida, subpasta, oculto);
  o `tenant_id` entra no hash do ID; outro tenant lê `None`.
- CORS só em `POST /lista-espera` (`_CorsSoNaListaDeEspera` em `main.py`); nunca `*`.

## Segredos guardados e logs

- Chave própria da Anthropic: conferida na Anthropic antes de gravar (`servicos/chave_anthropic.py`), cifrada com
  AES-GCM (`cofre.py`, `CHAVE_CIFRA`), só sai por `GET /interno/chave-anthropic` para o serviço do agente. Admin ou
  master conecta e remove.
- Log nunca leva token, senha, hash, chave, e-mail ou corpo de documento. `pymongo.command` fica travado em INFO
  (`armazenamento/mongo.py`). Mensagem de erro para o cliente não ecoa o segredo recebido.

## Checklist

- [ ] Nenhum segredo/PII em arquivo, teste, log ou exceção; e-mails de teste `@teste.exemplo`.
- [ ] Segredo novo: `config.py` sem padrão + `.env.exemplo` + `render.yaml` `sync: false` + README.
- [ ] Papel e tenant lidos do banco; nada de autorização por claim.
- [ ] Upload: limite em `limite_corpo._ROTAS`, tipo por `detectar`, tamanho conferido, caminho por `conferir_caminho`.
- [ ] `compare_digest` em bytes para segredo; IP só por `_ip`.
- [ ] Testes do caso negado (401/403/413/429) e de que o segredo não aparece na resposta nem no registro
  (`tests/test_auth.py`, `test_trocar_senha.py`, `test_limites.py`, `test_arquivos.py`).
