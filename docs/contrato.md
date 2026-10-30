# Contrato do MVP-0 (API central × agente × web)

Fonte da verdade entre as três frentes. Mudou aqui, mudou para todos: quem altera avisa no relatório.
Modelos de documento: `packages/dominio/src/canteiro_dominio/modelos.py`. Regras: `regras.py`.
Quantidade em **milésimos** e dinheiro em **centavos** (inteiros) em todo JSON; a formatação ("2.760", "R$ 472,00")
é do front e das mensagens do agente.

## Autenticação (login próprio)

- `POST /auth/entrar` `{email, senha}` → `{acesso, renovacao, usuario:{id, nome, tenant_id, papel}}`.
  Acesso: JWT curto (15 min). Renovação: opaco, guardado no banco (hash), revogável, troca a cada uso.
- `POST /auth/renovar` `{renovacao}` → novo par **e** `usuario` (mesmo corpo do `/auth/entrar`). `POST /auth/sair` revoga.
- Senha com argon2. Limite de tentativas por e-mail e por IP. O IP só vem do `X-Forwarded-For` quando quem chama está
  em `TRUSTED_PROXIES` (IPs/redes separados por vírgula no `.env`, vazio por padrão); vale o último endereço da cadeia
  que não é proxy de confiança. Papéis no banco: `viewer` (lê), `admin` (aprova e
  opera), `master` (+usuários). O papel **nunca** vem do token: a API resolve no banco a cada requisição.
- Todas as rotas abaixo exigem `Authorization: Bearer <acesso>` e só enxergam o `tenant_id` do usuário.
- Erros: `{detail: "mensagem pronta para a pessoa"}`. 401 sem sessão · 403 papel insuficiente · 404 não existe (ou é
  de outro cliente) · 409 conflito (já decidido, chave reutilizada, fora de ordem) · 422 regra de negócio · 429 tentativas.
- OpenAPI em `GET /openapi.json` (e `/docs`).

## Lista de espera (landing pública, sem login)

Única rota que o navegador chama direto (a landing da Plumo, `apps/site`, em outro domínio). Não exige token.

- `POST /lista-espera` `{email, nome?, empresa?, papel?, obras_ativas?, origem?, site?}` → `{ok: true, ja_estava}`.
  `email` obrigatório (até 254, conferido na forma); `nome`/`empresa` até 120; `origem` até 80 (ex. utm);
  `papel` ∈ `engenheiro | mestre | dono | comprador | outro`; `obras_ativas` inteiro 0..999. Campo desconhecido → 422.
- Guarda em `lista_espera` no tenant do sistema, ID = hash do e-mail normalizado (minúsculo, sem espaço nas pontas).
  1ª vez → 201 `{ok:true, ja_estava:false}`; repetida → 200 `{ok:true, ja_estava:true}`, **sem sobrescrever** o que já
  estava. Nunca devolve posição na fila nem dado guardado.
- `site` é isca para robô (campo escondido na página): preenchido → 201 como se tivesse dado certo, e nada é gravado.
- Limite por IP (`LISTA_ESPERA_POR_IP`, padrão 10, na mesma janela do login); toda chamada conta, inclusive a da isca.
  Estourou → 429.
- CORS só nesta rota, para as origens de `CORS_ORIGENS` (vírgula; `*` é ignorado), só `POST` com `Content-Type`,
  sem credenciais. Origem fora da lista não recebe `Access-Control-Allow-Origin`. O resto da API não responde CORS.
- O e-mail não vai para log (o adaptador do Mongo segura o log de comandos do driver em INFO).

## Obras e almoxarifado

- `GET /obras` → `[Obra]`
- `GET /obras/resumo` (qualquer papel) → `[{obra: Obra, pendencias, falta_comprar, pedidos_abertos,
  valor_pedidos_abertos, nf_para_conferir, ultima_atividade}]` (por nome da obra), para a página inicial.
  `pendencias` = propostas pendentes · `falta_comprar` = insumos com `Conta.comprar > 0` (a mesma Conta de
  `GET …/insumos`) · `pedidos_abertos` = pedidos fora de `conferido`/`cancelado` e `valor_pedidos_abertos` a soma do
  `valor` deles (centavos) · `nf_para_conferir` = pedidos `recebido` · `ultima_atividade` = `criado_em` da entrada mais
  nova do registro da obra, ou `null`. Somado na API: uma consulta por coleção para o tenant inteiro (sem agregação no
  banco) e uma leitura de 1 documento do registro por obra.
- `GET /obras/{obra_id}/insumos` → `[{insumo: Insumo, conta: Conta}]`, onde
  `Conta = {precisa, saldo, a_caminho, cotando, transferencia, falta, comprar}` calculada por `regras.conta_de_falta`
  (a_caminho e cotando vêm dos pedidos da obra; transferencia = 0 no MVP-0).
- `GET /obras/{obra_id}/insumos/{insumo_id}/movimentos?limite=50` → `[Movimento]` (mais novos primeiro)
- `POST /obras/{obra_id}/movimentos` `{insumo_id, tipo, quantidade, destino?, origem_doc?, chave_idempotencia}` →
  `Movimento`. Grava o movimento **e** atualiza o `Saldo` na mesma transação; mesma chave = mesmo resultado (201 na 1ª, 200 nas repetidas).
  `tipo` ∈ `entrada | saida | ajuste` (transferência fica para depois); mesma chave com outro conteúdo → 409.
  Saída de curva A sem `destino` → 422. Papel `admin`+. Gera `EntradaRegistro` com `desfazer` (estorno).
- `POST /obras/{obra_id}/importar` (multipart, CSV `;` ou `,`, UTF-8 ou Windows-1252): colunas
  `nome;unidade;saldo;precisa_ate_o_fim;curva`. Cria insumos e um movimento de AJUSTE inicial. Responde a prévia
  (`?aplicar=false`) ou aplica (`?aplicar=true`). Número ambíguo ("2.760") volta como erro da linha.
  Resposta sempre `{aplicado, linhas:[{linha, nome, unidade, saldo, precisa_ate_o_fim, curva}], erros:[{linha, campo, mensagem}]}`;
  com qualquer erro nada é gravado (`aplicado: false`). Nome já existente na obra é erro da linha.

## Pedidos

- `GET /obras/{obra_id}/pedidos?status=…` → `[Pedido]`
- `POST /obras/{obra_id}/pedidos` `{insumo_id, quantidade, fornecedor, valor, previsao?}` → `Pedido` (status em_cotacao; número PED-NNNN por tenant)
- `POST /pedidos/{pedido_id}/avancar` `{para}` → `Pedido`. Valida em `regras.validar_transicao`. Para `conferido`:
  cria o movimento de ENTRADA (origem_doc = número) na mesma transação.

## Propostas, aprovação e registro

- `POST /propostas` `{obra_id, tipo, titulo, conta, justificativa, fonte, dados}` → `Proposta` (pendente). Só valida e
  guarda: **não executa**. Qualquer papel propõe (o agente usa o token de quem conversa); `dados` inválido → 422.
  `dados` por tipo: `criar_pedido` = corpo do POST de pedido · `avancar_pedido` = `{pedido_id, para}` ·
  `ajuste_estoque` = `{insumo_id, quantidade, motivo}`.
- `GET /propostas?status=pendente&obra_id=…` → `[Proposta]`
- `POST /propostas/{id}/aprovar` → `{proposta, registro}` (o `registro.id` é o que o Desfazer usa); executa a operação do tipo (papel `admin`+), marca aprovada, grava `EntradaRegistro`
  com `desfazer` preenchido. `POST /propostas/{id}/recusar` `{motivo}`.
- Aprovar grava em `Proposta.registro_id` o ID da `EntradaRegistro` da execução (a tela mostra o Desfazer mesmo depois
  de recarregar); desfazer mantém o `registro_id` e muda a proposta para `desfeita`.
- Proposta decidida traz `decidido_por` (ID) e `decidido_por_nome`. `EntradaRegistro.quem` é o nome legível;
  vem também `quem_nome` (igual) e `usuario_id`.
- `GET /registro?obra_id=…&limite=50` → `[EntradaRegistro]` · `POST /registro/{id}/desfazer` → aplica a operação inversa
  (estorno de movimento, volta de status do pedido) e registra o desfazer.

## Consumo de IA

- `POST /uso` (só o serviço do agente, com token de serviço) `UsoIA` → grava a linha. O corpo traz `tenant_id` e
  `usuario_id` (claims `tid`/`sub` do token que a API já aceitou em `GET /creditos`) e o agente repete o `Authorization`
  do usuário no cabeçalho `X-Canteiro-Usuario`, para a API conferir que batem. O `id` vem no corpo (UUIDv7 do agente):
  repetir a chamada não duplica a linha.
- `GET /uso?de=AAAA-MM-DD&ate=AAAA-MM-DD` → `{total, por_dia:[{dia, custo, tokens}], por_obra:[…], por_rota:[…]}`
  (somado na API a partir das linhas; sem agregação no banco, para ser portável).
- `GET /creditos` → `{saldo_microdolares, franquia, chave_propria:bool, usuario_id, tenant_id}`. O agente consulta **antes** de chamar o modelo;
  sem saldo (e sem chave própria) → 402 com mensagem para a pessoa.
  O 402 traz o mesmo corpo de `Creditos` mais `detail` (mensagem para a pessoa). `POST /uso` aceita o token do usuário
  vencido há até 60 min (a conversa pode passar dos 15 min do acesso; a assinatura continua conferida); corpo que não
  bate com o token → 403; mesmo `id` → 200 sem duplicar. Com créditos (sem chave própria) o custo é descontado do saldo
  na mesma transação.

## Conta e chave própria da Anthropic

- `GET /conta` (qualquer papel) → `{usuario:{id, nome, email, papel}, tenant:{id, nome}, chave_propria, creditos}`.
  `papel` vem do banco nesta requisição. `chave_propria` = `{ativa: true, final, cadastrada_em}` ou `null` sem chave
  conectada. `creditos` é o mesmo corpo de `GET /creditos`, mas **sem o 402**: sem saldo a conta responde 200 com
  `saldo_microdolares` zerado (é a tela onde a pessoa vê isso). Pagamento de créditos ainda não existe.
- `GET /conta/chave-anthropic` (qualquer papel) → `{ativa, final, cadastrada_em}` (`{false, null, null}` sem chave).
  Nunca traz a chave nem o cifrado. `final` = os 4 últimos caracteres (a tela põe o "…").
- `PUT /conta/chave-anthropic` `{chave}` (papel `master`) → `{chave_propria: true, final: "abcd"}`. Guardada cifrada
  (AES-GCM com `CHAVE_CIFRA` do ambiente, atrás da interface `Cofre`; depois Secret Manager). `DELETE` desconecta.
- `GET /interno/chave-anthropic` (só o serviço do agente: `Authorization: Bearer <token de serviço>` +
  `X-Canteiro-Usuario: Bearer <acesso do usuário>`) → `{chave}` com `Cache-Control: no-store`; 404 sem chave própria.

## Sessões de conversa (histórico do agente)

Um documento por sessão (`sessoes`) e um por mensagem (`mensagens`, ID = `{sessao_id}-{seq:06d}`). A sessão é
privada de quem a criou (`dono` = usuário do token); de outra pessoa → 404.

- `POST /sessoes` `{id?, obra_id?, titulo?, abas?}` → `Sessao` (201; mesmo `id` do mesmo dono → 200).
  `Sessao = {id, tenant_id, dono, obra_id, titulo, abas:[obj] (≤ 20), total_mensagens, criado_em, atualizado_em}`.
- `GET /sessoes?obra_id=&limite=30` → `[Sessao]` (mais recentes primeiro) · `GET /sessoes/{id}` →
  `Sessao` · `PATCH /sessoes/{id}` `{titulo?, obra_id?, abas?}`.
- `GET /sessoes/{id}/mensagens?depois_de=-1&limite=200` → `[{id, sessao_id, seq, papel, conteudo, criado_em}]` (por seq).
- `POST /sessoes/{id}/mensagens` `{seq_inicial, mensagens:[{papel: user|assistant, conteudo: texto | [blocos]}]}`
  (até 50) → `Sessao + mensagens` (201). Só acrescenta: `seq_inicial` tem de ser o `total_mensagens` atual; reenvio do
  mesmo lote → 200; outra coisa → 409 com o esperado na mensagem. Mensagem acima de ~800 KB → 422.

## Agente

- `POST /conversa` (serviço do agente, porta própria) `{sessao_id?, obra_id?, texto}` com o mesmo `Authorization` do
  usuário → **SSE**:
  - `event: texto` `{delta}` · `event: ferramenta` `{nome, nivel: consulta|propoe, resumo}` (o "usou almoxarifado…")
  - `event: proposta` `{Proposta}` (vira cartão de decisão na tela) · `event: fim` `{sessao_id, uso:{tokens, custo}}`
  - `event: erro` `{mensagem}` (inclusive "sem créditos")
- Ferramentas (cada uma chama a API acima com o token do usuário):

| Ferramenta | Nível | Chama |
|---|---|---|
| `listar_obras()` | consulta | `GET /obras` |
| `consultar_estoque(obra_id, busca?)` | consulta | `GET /obras/{id}/insumos` |
| `historico_movimentos(obra_id, insumo_id)` | consulta | `GET …/movimentos` |
| `listar_pedidos(obra_id, status?)` | consulta | `GET /obras/{id}/pedidos` |
| `propor_pedido(obra_id, insumo_id, quantidade, fornecedor, valor, justificativa)` | propõe | `POST /propostas` (criar_pedido) |
| `propor_avanco_pedido(obra_id, pedido_id, para, justificativa)` | propõe | `POST /propostas` (avancar_pedido) |
| `propor_ajuste_estoque(obra_id, insumo_id, quantidade, motivo)` | propõe | `POST /propostas` (ajuste_estoque) |

- As ferramentas devolvem números já formatados **e** brutos; o agente nunca faz conta, ele cita a `Conta` da API.

## Dados de exemplo (seed)

Casa Vila Nova do protótipo: insumos bloco (entrou 4.000, saiu 1.050 + 190 de quebra = 2.760), aço (3.940 precisa,
140 saldo, PED-0158 de 3.200 kg em trânsito, PED-0167 de 600 kg em cotação), cimento (precisa 200, saldo 46, PED-0160
de 120 sc a caminho), areia (PED-0157 recebido, 40 m³ na NF). Um usuário `admin` de exemplo, senha só no `.env` local.
