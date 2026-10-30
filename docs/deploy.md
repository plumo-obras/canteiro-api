# Deploy gratuito do MVP-0 (Vercel + Render + MongoDB Atlas M0)

## O que está no ar (criado em 30/10/2026)

| Recurso | Onde | Endereço / identificação |
|---|---|---|
| Web | Vercel, projeto `canteiro-web` (time `rizzi26s-projects`, raiz `apps/web`) | https://canteiro-web-nu.vercel.app (`canteiro-web.vercel.app` já era de outra conta) |
| Landing (Plumo, lista de espera) | Vercel, projeto `plumo-site` (raiz `apps/site`, estático, sem build) | https://plumo-site.vercel.app (a 1ª versão saiu como Aprumo em `aprumo-site-nine.vercel.app`, que segue apontando para o mesmo projeto; `aprumo-site.vercel.app` é de outra conta) |
| API | Render `canteiro-api` (`srv-dbig4pcs728c73f606m0`), Virginia, free | https://canteiro-api.onrender.com |
| Agente | Render `canteiro-agente` (`srv-dbig4rvlk1mc73emi7t0`), Virginia, free, `MODELO_ROTEIRO=1` | https://canteiro-agente.onrender.com |
| Banco | Atlas, projeto `canteiro`, cluster M0 `canteiro` (AWS us-east-1), usuário `canteiro-api` com `readWrite@canteiro` | `canteiro.lyhqjtc.mongodb.net` |

- **Segredos de produção:** só em `~/.config/canteiro/producao.env` (fora do repo, `chmod 600`) e nos painéis.
- **Rede do Atlas:** `0.0.0.0/0`, por decisão do usuário (o Render gratuito não tem IP fixo e as faixas compartilhadas
  rotacionam). A proteção fica no usuário restrito, na senha de 32 caracteres e no TLS.
- **Os serviços do Render foram criados pela API** (a CLI do Render só valida Blueprints), com a mesma configuração do
  `render.yaml`. Mudou o `render.yaml`? Atualize o serviço pelo painel ou pela API (ou adote o arquivo como Blueprint).
- **CI/CD:** o GitHub Actions (`.github/workflows/ci.yml`) roda a cada push e PR. O Render publica a API e o agente só
  quando os checks passam (`autoDeployTrigger: checksPass`). A Vercel publica a web a cada push na `main`.


| Peça | Onde | Plano |
|---|---|---|
| Web (Next.js, `apps/web`) | Vercel, **2º projeto** (o 1º é o protótipo na raiz) | Hobby (uso não comercial; para cobrar clientes, Pro) |
| API (`apps/api`) e agente (`apps/agente`) | Render, pelo `render.yaml` da raiz | Free (dorme após 15 min; acorda em ~30–60 s) |
| Banco | MongoDB Atlas | M0 (gratuito, 512 MB, replica set → transações funcionam) |

O repositório é **público**: todo segredo vai só no painel de cada serviço.

## 1. Gere os segredos (no seu terminal, não salve em arquivo do repo)

```bash
python3 -c "import secrets; print('TOKEN_SERVICO_AGENTE', secrets.token_urlsafe(32))"
python3 -c "import secrets; print('PROXY_SEGREDO', secrets.token_urlsafe(32))"
python3 -c "import base64, os; print('CHAVE_CIFRA', base64.b64encode(os.urandom(32)).decode())"
```

## 2. MongoDB Atlas (M0)

1. Crie o cluster **M0** na **AWS us-east-1 (N. Virginia)**, a mesma região dos serviços do Render no `render.yaml`.
2. Database Access: um usuário só para o Canteiro, com senha forte e papel `readWrite` no banco `canteiro`.
3. Network Access: o Render gratuito não tem IP fixo, então libere `0.0.0.0/0`. A proteção fica no usuário e na senha
   (e no TLS do `mongodb+srv`).
4. Copie a string `mongodb+srv://…` (com o usuário e a senha) para usar no passo 3 e no seed.

## 3. Render (API e agente)

1. New → **Blueprint** → escolha o repositório `obras-agentic`. O Render lê o `render.yaml` e cria `canteiro-api` e `canteiro-agente`.
2. Preencha as variáveis marcadas como `sync: false`:
   - `canteiro-api`: `MONGO_URL` (Atlas), `TOKEN_SERVICO_AGENTE`, `CHAVE_CIFRA`, `PROXY_SEGREDO`, `SEED_ADMIN_EMAIL`.
     (`JWT_SEGREDO` o Render gera sozinho.)
   - `canteiro-agente`: `API_URL` = `https://canteiro-api.onrender.com` (a URL que o Render mostrar), o mesmo
     `TOKEN_SERVICO_AGENTE`. `MODELO_ROTEIRO=1` já vem ligado (demo sem chave, 0 tokens).
3. Depois do deploy: `https://canteiro-api.onrender.com/saude` e `https://canteiro-agente.onrender.com/saude` devem responder `{"ok":true}`.

## 4. Dados de exemplo (seed) no Atlas

O plano gratuito do Render não tem terminal, então rode o seed da sua máquina, com as variáveis só no terminal:

```bash
cd apps/api
MONGO_URL='mongodb+srv://…' MONGO_DB=canteiro SEED_ADMIN_EMAIL='voce@…' SEED_ADMIN_SENHA='…(12+ caracteres)' \
  .venv/bin/python -m canteiro_api.seed
```

## 5. Vercel (web)

1. Add New → Project → o mesmo repositório → **Root Directory: `apps/web`** (framework Next.js, detectado sozinho).
2. Variáveis de ambiente: `API_URL=https://canteiro-api.onrender.com`, `AGENTE_URL=https://canteiro-agente.onrender.com`,
   `PROXY_SEGREDO` (o mesmo da API), e `CANTEIRO_DEMO` **vazio** (com `1`, a web usa os dados de demonstração e ignora a API).
3. A conversa em streaming passa por uma função do Next com `maxDuration = 300` s (limite do plano Hobby com Fluid Compute).

## 6. Antes de uma demo

Os serviços gratuitos dormem. Acorde os dois uns minutos antes:

```bash
curl -s https://canteiro-api.onrender.com/saude; curl -s https://canteiro-agente.onrender.com/saude
```

## 7. Ligar o Claude de verdade

No `canteiro-agente`: `MODELO_ROTEIRO=0` e `ANTHROPIC_API_KEY` com a chave de uma organização do Console da Anthropic
(a assinatura Pro/Max não serve para a API). Cada conversa passa a gastar tokens, medidos em `/uso` e descontados dos créditos.
