# Deploy gratuito do MVP-0 (Vercel + Render + MongoDB Atlas M0)

## O que está no ar (criado em 30/10/2026)

> Até 02/11/2026 tudo saía do monorepo `Rizzi26/obras-agentic` (arquivado). Os serviços abaixo ainda apontam para ele
> até serem religados aos repositórios novos: Render → Settings → Repository; Vercel → Settings → Git.

| Recurso | Onde | Endereço / identificação |
|---|---|---|
| Web | Vercel, projeto `canteiro-web` (time `rizzi26s-projects`, repo `canteiro-web`, raiz do repo) | https://canteiro-web-nu.vercel.app (`canteiro-web.vercel.app` já era de outra conta) |
| Landing (Plumo, lista de espera) | Vercel, projeto `plumo-site` (repo `canteiro-web`, raiz `site`, publica só `public/`, estático, sem build) | https://plumo-site.vercel.app (a 1ª versão saiu como Aprumo em `aprumo-site-nine.vercel.app`, que segue apontando para o mesmo projeto; `aprumo-site.vercel.app` é de outra conta) |
| API | Render `canteiro-api` (`srv-dbig4pcs728c73f606m0`), Virginia, free | https://canteiro-api.onrender.com |
| Agente | Render `canteiro-agente` (`srv-dbig4rvlk1mc73emi7t0`), Virginia, free, `MODELO_ROTEIRO=1` | https://canteiro-agente.onrender.com |
| Banco | Atlas, projeto `canteiro`, cluster M0 `canteiro` (AWS us-east-1), usuário `canteiro-api` com `readWrite@canteiro` | `canteiro.lyhqjtc.mongodb.net` |

- **Segredos de produção:** só em `~/.config/canteiro/producao.env` (fora do repo, `chmod 600`) e nos painéis.
- **Rede do Atlas:** `0.0.0.0/0`, por decisão do usuário (o Render gratuito não tem IP fixo e as faixas compartilhadas
  rotacionam). A proteção fica no usuário restrito, na senha de 32 caracteres e no TLS.
- **Os serviços do Render foram criados pela API** (a CLI do Render só valida Blueprints), com a mesma configuração do
  `render.yaml`. Mudou o `render.yaml`? Atualize o serviço pelo painel ou pela API (ou adote o arquivo como Blueprint).
- **CI/CD:** cada repositório tem o seu GitHub Actions (`.github/workflows/ci.yml`), a cada push e PR. O Render publica
  a API e o agente só quando os checks do repo deles passam (`autoDeployTrigger: checksPass`). A Vercel publica a web a cada push na `main`.


| Peça | Onde | Plano |
|---|---|---|
| Web (Next.js, repo `canteiro-web`) | Vercel | Hobby (uso não comercial; para cobrar clientes, Pro) |
| API (`canteiro-api`) e agente (`canteiro-agente`) | Render, pelo `render.yaml` de cada repo | Free (dorme após 15 min; acorda em ~30–60 s) |
| Banco | MongoDB Atlas | M0 (gratuito, 512 MB, replica set → transações funcionam) |

Os repositórios são **públicos**: todo segredo vai só no painel de cada serviço.

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

1. New → **Blueprint** → o repositório `canteiro-api`, e de novo com o `canteiro-agente`. Cada um tem o seu `render.yaml`.
2. Preencha as variáveis marcadas como `sync: false`:
   - `canteiro-api`: `MONGO_URL` (Atlas), `TOKEN_SERVICO_AGENTE`, `CHAVE_CIFRA`, `PROXY_SEGREDO`, `SEED_ADMIN_EMAIL`.
     (`JWT_SEGREDO` o Render gera sozinho.)
   - `canteiro-agente`: `API_URL` = `https://canteiro-api.onrender.com` (a URL que o Render mostrar), o mesmo
     `TOKEN_SERVICO_AGENTE`. `MODELO_ROTEIRO=1` já vem ligado (demo sem chave, 0 tokens).
3. Depois do deploy: `https://canteiro-api.onrender.com/saude` e `https://canteiro-agente.onrender.com/saude` devem responder `{"ok":true}`.

### Fotos dos comprovantes

O disco do Render gratuito some a cada deploy e quando o serviço dorme: as fotos dos comprovantes ficam no próprio
Atlas (`ARQUIVOS=banco`, já fixado no `render.yaml`), em pedaços de 512 KiB nas coleções `arquivos` e
`arquivos_pedacos`. A web reduz cada foto para ~1 MB; o M0 tem 512 MB para tudo, o que dá algumas centenas de
comprovantes. Quando apertar: bucket privado (R2 ou GCS) atrás da mesma interface `Arquivos`, ou o Atlas pago.
`ARQUIVOS=disco` só serve na máquina de quem desenvolve.

## 4. Dados de exemplo (seed) no Atlas

O plano gratuito do Render não tem terminal, então rode o seed da sua máquina, com as variáveis só no terminal:

```bash
cd canteiro-api
MONGO_URL='mongodb+srv://…' MONGO_DB=canteiro SEED_ADMIN_EMAIL='voce@…' SEED_ADMIN_SENHA='…(12+ caracteres)' \
  .venv/bin/python -m canteiro_api.seed
```

## 5. Vercel (web)

1. Add New → Project → o repositório `canteiro-web`, raiz do repo (framework Next.js, detectado sozinho).
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

## 8. Índices e vencimento (TTL) dos documentos

**Mongo/Atlas:** nada a fazer à mão. A API cria os índices e os índices TTL ao subir (`garantir_indices` em
`src/canteiro_api/armazenamento/mongo.py`, listas `INDICES` e `TTL`).

- **TTL:** `renovacoes`, `tentativas` e `outbox` guardam em `expira_em` o instante (data BSON, nunca texto) em que o
  documento vence, e o índice usa `expireAfterSeconds: 0`. Renovação vence com o próprio token; tentativa, no fim da
  janela do limite de login; evento da outbox, **7 dias depois de criado** (`RETENCAO_OUTBOX` em `modelos.py`): ainda
  não há relay em produção (`publicar_pendentes` só roda nos testes), e sem prazo a outbox cresceria a cada transação.
  Documentos gravados antes de 04/11/2026 não têm `expira_em` e não vencem sozinhos; para limpá-los uma vez:
  `db.tentativas.deleteMany({expira_em: {$exists: false}})` e o mesmo em `outbox` (renovações já tinham o campo).
- **Índices trocados em 04/11/2026:** todo índice composto passou a terminar em `_id` (a API sempre desempata a
  ordenação por ele; sem isso o Mongo ordena em memória). Os antigos, sem `_id` no fim, continuam no Atlas e só
  ocupam espaço: liste com `db.<colecao>.getIndexes()` e apague com `dropIndex` os que não estão em `INDICES`.

**Firestore (quando migrar):** TTL lá não é índice, é política por grupo de coleção, e tem de ser ligada uma vez em
cada projeto. O formato já é o mesmo (o campo guarda o instante do vencimento, como `Timestamp`):

```bash
for c in renovacoes tentativas outbox; do
  gcloud firestore fields ttls update expira_em --collection-group=$c --enable-ttl --project=<projeto>
done
```

O Firestore apaga em até ~24 h depois do vencimento (o Mongo, em minutos): o código continua conferindo a validade
ao ler, então o atraso não muda comportamento. Os índices compostos de `INDICES` viram declarações em
`firestore.indexes.json` (o `_id` do fim corresponde ao `__name__`, que o Firestore já acrescenta sozinho).
