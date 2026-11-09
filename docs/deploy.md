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
   - Já vêm no `render.yaml` (sem segredo): `AMBIENTE=producao` e `ARQUIVOS=banco` (veja a seção 9). O serviço atual
     foi criado pela API, não como Blueprint: **antes de publicar a leitura de comprovantes**, acrescente as duas no
     painel (Environment), senão a API sobe com o padrão `ARQUIVOS=banco` mas sem a trava de produção.
3. Depois do deploy: `https://canteiro-api.onrender.com/saude` e `https://canteiro-agente.onrender.com/saude` devem responder `{"ok":true}`.

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

## 9. Arquivos dos comprovantes (no próprio Atlas, em pedaços)

Os comprovantes de gasto (foto até 3,5 MB, PDF até 4 MiB) ficam **no mesmo banco** do Atlas, pela mesma porta de
armazenamento do resto (abordagem do João Sotto): cada arquivo vira pedaços de 512 KiB, um documento cada, na coleção
`arquivos_pedacos`, e um documento-índice em `arquivos` (quantos pedaços, tamanho, sha256), gravado **por último na
mesma transação**: sem índice o arquivo não existe, nunca pela metade. A leitura é por ID (sem consulta nem índice
novo) e confere o sha256. O disco do Render não serve: ele é recriado a cada deploy e quando o serviço gratuito dorme,
e o comprovante enviado de manhã sumiria antes da revisão.

- **Configuração:** `ARQUIVOS=banco` (padrão) usa a mesma `MONGO_URL`; não há serviço nem segredo novo.
  `ARQUIVOS=supabase` põe os arquivos num bucket privado (veja a alternativa no fim desta seção).
  `ARQUIVOS=disco` (pasta `.arquivos/`, fora do git) e `memoria` são só para desenvolvimento e testes.
- **Trava de produção:** com `AMBIENTE=producao`, a API **recusa subir** se `ARQUIVOS` não for `banco`/`supabase` ou
  `ARMAZENAMENTO` não for `mongo` (`Config.conferir`). Regra escolhida por ser explícita: produção se declara, e
  nenhuma combinação de variáveis esquecida faz o envio "funcionar" gravando num lugar que some.
- **Portável para o Firestore:** nada aqui é exclusivo do Mongo (por isso não é GridFS). O pedaço de 512 KiB fica
  abaixo do 1 MiB por documento do Firestore, e o ID (sha256 de `tenant|caminho`, sem `/`) é válido nos dois.
  O maior arquivo (4 MiB) = 8 pedaços + o índice = **9 escritas e ~4 MiB numa transação**: no Mongo cabe com folga
  (16 MB por documento; transação sem teto de tamanho desde o 4.2, só o tempo de 60 s); no Firestore, o commit aceita
  até 500 escritas (o teto que a porta já impõe) e 10 MiB por requisição (mesmo em base64, ~5,4 MiB). A porta recusa arquivo acima de 4 MiB (`MAX_TAMANHO` em `src/canteiro_api/arquivos.py`)
  para essa conta nunca deixar de fechar.
- **Isolamento:** o `tenant_id` vai em todo documento e em toda leitura, e entra no hash do ID (o `_id` é único na
  coleção inteira, não por cliente). O caminho é `<obra_id>/<nome>` e a porta recusa qualquer outra forma (`..`,
  barra invertida, caminho absoluto, subpasta, arquivo oculto). Arquivo de outro tenant volta como inexistente. O
  arquivo só sai pela rota `GET /comprovantes/{id}/arquivo`, com login.
- **Espaço (M0 = 512 MB para dados + índices + arquivos):** a web reduz cada foto para ~1 MB; 200 comprovantes ≈
  100–400 MB. Acompanhe no Atlas (Cluster → Metrics → *Logical Data Size*, ou Collections → banco `canteiro` →
  `arquivos_pedacos`) ou no `mongosh`: `db.stats().dataSize` e `db.arquivos_pedacos.stats().storageSize`. Passou de
  ~350 MB: hora de subir o plano (M10/Flex) ou mover os arquivos para um bucket privado (a porta `Arquivos` já isola a
  troca; o GCS vem junto com o Firestore). Arquivo duplicado não ocupa espaço de novo (o nome sai do sha256 do
  conteúdo), e como pedaços e índice vão na mesma transação, envio interrompido não deixa sobra.
- **Bucket privado no Supabase Storage (`ARQUIVOS=supabase`) — o modo de PRODUÇÃO desde 09/11/2026** (projeto em
  us-east-1, perto do Render em Virginia; os 9 comprovantes da época migrados e apagados do Mongo). Tira os bytes do M0 (1 GB grátis
  no Storage, à parte dos 512 MB do Atlas); o Mongo continua com o documento do comprovante (tamanho, sha256,
  caminho). Implementado em `ArquivosSupabase` (`src/canteiro_api/arquivos.py`), pela API REST do Storage:
  - **Montar:** projeto no Supabase (região São Paulo, por LGPD) → Storage → bucket **privado** `comprovantes`
    (opcional: limite de 5 MB por arquivo e tipos `image/jpeg,image/png,image/webp,application/pdf`; a API envia o
    tipo real, detectado pelos bytes). Nenhuma policy
    de RLS: só a API acessa, com a chave secreta.
  - **Variáveis no Render** (nunca no git): `ARQUIVOS=supabase`, `SUPABASE_URL` (`https://<projeto>.supabase.co`),
    `SUPABASE_CHAVE_SERVICO` (a `sb_secret_...`, ou a `service_role` antiga) e, se não for `comprovantes`,
    `SUPABASE_BUCKET`. Faltando URL https ou chave, a API recusa subir.
  - **Como grava:** objeto `<tenant>/<obra_id>/<nome>`, sem sobrescrever (`x-upsert: false`; "já existe" é o mesmo
    arquivo, porque o nome sai do sha256). Storage fora do ar vira 503 "tente de novo", e o erro nunca leva a chave.
  - **Cuidados do plano gratuito:** projeto parado 7 dias **pausa** (os arquivos ficam inacessíveis até reativar no
    painel); limite de projetos ativos por conta; cota mensal de tráfego de saída.
  - **Ping contra a pausa:** `GET /saude/arquivos` (pública, sem dado, resultado guardado 10 min) confere o bucket;
    o workflow `.github/workflows/manter-storage.yml` chama todo dia às 08:23. Run vermelho = a API não abriu o bucket.
    Em repositório público o GitHub desliga agendamento após 60 dias sem commit (avisa por e-mail; reative em Actions).
  - **Trocar sem perder arquivo** (`python -m canteiro_api.migrar_arquivos`, com `MONGO_URL`, `MONGO_DB` e as
    `SUPABASE_*` no ambiente; nada gravado em arquivo):
    1. Rodar o script: copia tudo, lê de volta do bucket e confere o sha256. Não apaga nada.
    2. No Render: `ARQUIVOS=supabase` (com as duas `SUPABASE_*` já lá) e esperar o deploy.
    3. Rodar o script de novo: pega o que chegou no banco entre o passo 1 e o 2.
    4. Abrir alguns comprovantes no app. Então `--apagar-do-banco`: tira do Mongo só o que está conferido no bucket.
    Arquivo que não confere fica no banco e aparece no relatório (saída com código 1). Rodar de novo é seguro, e
    termina um `--apagar-do-banco` que parou no meio. **Depois do passo 4 não há volta para `ARQUIVOS=banco`** sem
    copiar no sentido contrário: os comprovantes migrados só existem no bucket.
- **Retenção — decisão pendente (LGPD):** hoje nenhum arquivo é apagado. Estornar/desfazer um gasto ou descartar um
  comprovante mantém o arquivo de propósito (é a prova do pagamento e o Desfazer precisa dele). Falta decidir o prazo
  (ex.: descartados apagados após N dias; confirmados enquanto a obra existir + prazo fiscal) e o pedido de exclusão
  do titular.
