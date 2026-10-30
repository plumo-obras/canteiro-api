# Contrato do MVP-0 (API central × agente × web)

Fonte da verdade entre as três frentes. Mudou aqui, mudou para todos: quem altera avisa no relatório.
Modelos de documento: `packages/dominio/src/canteiro_dominio/modelos.py`. Regras: `regras.py`.
Quantidade em **milésimos** e dinheiro em **centavos** (inteiros) em todo JSON; a formatação ("2.760", "R$ 472,00")
é do front e das mensagens do agente.

## Autenticação (login próprio)

- `POST /auth/entrar` `{email, senha}` → `{acesso, renovacao, usuario:{id, nome, tenant_id, papel}}`.
  Acesso: JWT curto (15 min). Renovação: opaco, guardado no banco (hash), revogável, troca a cada uso.
- `POST /auth/renovar` `{renovacao}` → novo par. `POST /auth/sair` revoga.
- Senha com argon2. Limite de tentativas por e-mail e por IP. Papéis no banco: `viewer` (lê), `admin` (aprova e
  opera), `master` (+usuários). O papel **nunca** vem do token: a API resolve no banco a cada requisição.
- Todas as rotas abaixo exigem `Authorization: Bearer <acesso>` e só enxergam o `tenant_id` do usuário.

## Obras e almoxarifado

- `GET /obras` → `[Obra]`
- `GET /obras/{obra_id}/insumos` → `[{insumo: Insumo, conta: Conta}]`, onde
  `Conta = {precisa, saldo, a_caminho, cotando, transferencia, falta, comprar}` calculada por `regras.conta_de_falta`
  (a_caminho e cotando vêm dos pedidos da obra; transferencia = 0 no MVP-0).
- `GET /obras/{obra_id}/insumos/{insumo_id}/movimentos?limite=50` → `[Movimento]` (mais novos primeiro)
- `POST /obras/{obra_id}/movimentos` `{insumo_id, tipo, quantidade, destino?, origem_doc?, chave_idempotencia}` →
  `Movimento`. Grava o movimento **e** atualiza o `Saldo` na mesma transação; mesma chave = mesmo resultado (201 na 1ª, 200 nas repetidas).
- `POST /obras/{obra_id}/importar` (multipart, CSV `;` ou `,`, UTF-8 ou Windows-1252): colunas
  `nome;unidade;saldo;precisa_ate_o_fim;curva`. Cria insumos e um movimento de AJUSTE inicial. Responde a prévia
  (`?aplicar=false`) ou aplica (`?aplicar=true`). Número ambíguo ("2.760") volta como erro da linha.

## Pedidos

- `GET /obras/{obra_id}/pedidos?status=…` → `[Pedido]`
- `POST /obras/{obra_id}/pedidos` `{insumo_id, quantidade, fornecedor, valor, previsao?}` → `Pedido` (status em_cotacao; número PED-NNNN por tenant)
- `POST /pedidos/{pedido_id}/avancar` `{para}` → `Pedido`. Valida em `regras.validar_transicao`. Para `conferido`:
  cria o movimento de ENTRADA (origem_doc = número) na mesma transação.

## Propostas, aprovação e registro

- `POST /propostas` `{obra_id, tipo, titulo, conta, fonte, dados}` → `Proposta` (pendente). Só valida e guarda: **não executa**.
  `dados` por tipo: `criar_pedido` = corpo do POST de pedido · `avancar_pedido` = `{pedido_id, para}` ·
  `ajuste_estoque` = `{insumo_id, quantidade, motivo}`.
- `GET /propostas?status=pendente&obra_id=…` → `[Proposta]`
- `POST /propostas/{id}/aprovar` → executa a operação do tipo (papel `admin`+), marca aprovada, grava `EntradaRegistro`
  com `desfazer` preenchido. `POST /propostas/{id}/recusar` `{motivo}`.
- `GET /registro?obra_id=…&limite=50` → `[EntradaRegistro]` · `POST /registro/{id}/desfazer` → aplica a operação inversa
  (estorno de movimento, volta de status do pedido) e registra o desfazer.

## Consumo de IA

- `POST /uso` (só o serviço do agente, com token de serviço) `UsoIA` → grava a linha.
- `GET /uso?de=AAAA-MM-DD&ate=AAAA-MM-DD` → `{total, por_dia:[{dia, custo, tokens}], por_obra:[…], por_rota:[…]}`
  (somado na API a partir das linhas; sem agregação no banco, para ser portável).
- `GET /creditos` → `{saldo_microdolares, franquia, chave_propria:bool}`. O agente consulta **antes** de chamar o modelo;
  sem saldo (e sem chave própria) → 402 com mensagem para a pessoa.

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
| `propor_avanco_pedido(pedido_id, para, justificativa)` | propõe | `POST /propostas` (avancar_pedido) |
| `propor_ajuste_estoque(obra_id, insumo_id, quantidade, motivo)` | propõe | `POST /propostas` (ajuste_estoque) |

- As ferramentas devolvem números já formatados **e** brutos; o agente nunca faz conta, ele cita a `Conta` da API.

## Dados de exemplo (seed)

Casa Vila Nova do protótipo: insumos bloco (entrou 4.000, saiu 1.050 + 190 de quebra = 2.760), aço (3.940 precisa,
140 saldo, PED-0158 de 3.200 kg em trânsito, PED-0167 de 600 kg em cotação), cimento (precisa 200, saldo 46, PED-0160
de 120 sc a caminho), areia (PED-0157 recebido, 40 m³ na NF). Um usuário `admin` de exemplo, senha só no `.env` local.
