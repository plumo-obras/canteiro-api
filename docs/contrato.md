# Contrato do MVP-0 (API central × agente × web)

Fonte da verdade entre as três frentes. Mudou aqui, mudou para todos: quem altera avisa no relatório.
Modelos de documento: `dominio/src/canteiro_dominio/modelos.py`. Regras: `regras.py`.
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
  de outro cliente) · 409 conflito (já decidido, chave reutilizada, fora de ordem) · 413 corpo grande demais · 422 regra de
  negócio · 429 tentativas.
- Tamanho do corpo, conferido antes do login: até 1 MiB; 6 MiB nas rotas de upload (`…/orcamento/ler`,
  `…/orcamento/importar`, `…/orcamento/versoes`, `/obras/{id}/importar`); 10 MiB em `POST /sessoes/{id}/mensagens`.
  Acima disso → 413, sem ler o resto (vale também para corpo em partes, sem `Content-Length`).
- OpenAPI em `GET /openapi.json` (e `/docs`).

## Lista de espera (landing pública, sem login)

Única rota que o navegador chama direto (a landing da Plumo, `site/` no canteiro-web, em outro domínio). Não exige token.

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

- `GET /obras` → `[Obra]`. `Obra` = `{id, nome, cidade, cep, uf, endereco, verba, status, tipologia, area_construida,
  pavimentos, padrao, regime, cliente, inicio, prazo_meses, numero_contrato, bdi, data_base}`; `status` ∈ `em_orcamento | em_andamento | concluida`.
  `tipologia` ∈ `casa | sobrado | predio | galpao | reforma | comercial | outro` · `padrao` ∈ `baixo | normal | alto` ·
  `regime` ∈ `empreitada | administracao` (os três podem ser `null`) · `area_construida` em milésimos de m² ·
  `inicio` `"AAAA-MM-DD"` ou `null` · `pavimentos`/`prazo_meses` 0 = não informado · `verba` em centavos (é o
  **valor do contrato** com o cliente; a tela não diz mais "verba do dono") · `numero_contrato` (≤ 40, `""`) · `bdi` em
  centésimos de ponto percentual (22,91% = 2291; 0 = não informado; ≤ 10000) · `data_base` `"AAAA-MM"` ou `null` (mês
  dos preços de referência).
- `POST /obras` (admin) `{nome, chave_idempotencia, …os campos acima, todos opcionais}` → `Obra + registro_id`. Nasce
  `em_orcamento`. ID derivado da chave: a repetição devolve a mesma obra (201 na 1ª, 200 nas repetidas); a mesma chave
  com outro nome → 409. Registro "Criou a obra X", **sem desfazer** (a porta de armazenamento não apaga).
- `PATCH /obras/{obra_id}` (admin) `{…qualquer campo da obra}` → `Obra + registro_id`. Só os campos enviados contam, e
  os que já têm aquele valor são ignorados; se nada muda, `registro_id: null` e nenhum registro. Registro "Atualizou a
  obra X: área, padrão" com `desfazer` `restaurar_obra`, que volta os valores antigos e é recusado (409) se algum
  daqueles campos mudou de novo depois. `nome: null` → 422.
- `GET /obras/resumo` (qualquer papel) → `[{obra: Obra, pendencias, falta_comprar, pedidos_abertos,
  valor_pedidos_abertos, nf_para_conferir, ultima_atividade, prazo}]` (por nome da obra), para a página inicial.
  `prazo` = o `Prazo` do `GET /obras/{id}/cronograma` (término contratual e projetado, atraso), `null` sem cronograma.
  `pendencias` = propostas pendentes · `falta_comprar` = insumos com `Conta.comprar > 0` (a mesma Conta de
  `GET …/insumos`) · `pedidos_abertos` = pedidos fora de `conferido`/`cancelado` e `valor_pedidos_abertos` o que eles
  ainda vão custar (centavos): o `valor` inteiro em cotação; depois, só a parte que falta entregar de cada linha · `nf_para_conferir` = pedidos `recebido` · `ultima_atividade` = `criado_em` da entrada mais
  nova do registro da obra, ou `null`. Somado na API: uma consulta por coleção para o tenant inteiro (sem agregação no
  banco) e uma leitura de 1 documento do registro por obra.
- `GET /obras/{obra_id}/insumos` → `[{insumo: Insumo, conta: Conta}]`, onde
  `Conta = {precisa, saldo, a_caminho, cotando, transferencia, falta, comprar}` calculada por `regras.conta_de_falta`
  (a_caminho e cotando vêm dos pedidos da obra; transferencia = 0 no MVP-0).
- `Insumo.valor_unitario` (centavos, 0 = sem preço) é o preço unitário do orçamento.
- `Insumo.tipo` ∈ `material | servico` (padrão `material`). Serviço (terraplanagem, aterro, empreiteiro) não tem
  estoque: movimento de entrada/saída/ajuste nele → 422, saldo sempre 0, e "a caminho" na Conta é o contratado ainda
  não medido.
- `POST /obras/{obra_id}/insumos` `{nome, unidade, tipo?="material", curva?="C", precisa_ate_o_fim?=0, valor_unitario?=0}` → `Insumo` + `registro_id`
  (admin). O que chegou ou foi comprado fora da planilha do orçamento. Nome repetido na obra (sem diferenciar
  maiúsculas e espaços) → 409. Com `precisa_ate_o_fim` 0 o insumo não entra na conta de falta. Sem desfazer.
- `GET /obras/{obra_id}/insumos/{insumo_id}/movimentos?limite=50&antes_de=…` → `[Movimento]` (mais novos primeiro;
  paginação em "Listagens paginadas")
- `POST /obras/{obra_id}/movimentos` `{insumo_id, tipo, quantidade, destino?, etapa?, origem_doc?, chave_idempotencia}` →
  `Movimento + registro_id` (o ID da `EntradaRegistro` criada: é o que o Desfazer usa logo depois da ação; derivado
  do movimento, então a repetição da chave devolve o mesmo `registro_id`). Grava o movimento **e** atualiza o `Saldo` na mesma transação; mesma chave = mesmo resultado (201 na 1ª, 200 nas repetidas).
  `tipo` ∈ `entrada | saida | ajuste` (transferência fica para depois); mesma chave com outro conteúdo → 409.
  Saída de curva A sem `destino` → 422. `etapa` é a apropriação do consumo (uma chave de `GET …/etapas`): obrigatória
  em **saída** quando a obra tem orçamento importado; etapa que não existe → 422; obra sem orçamento → deixe em branco.
  Papel `admin`+. Gera `EntradaRegistro` com `desfazer` (estorno).
- `Movimento.quem` é o **nome** de quem lançou (o que a tela mostra); o ID do usuário vem em `usuario_id` (`null` em
  movimento semeado). Vale para movimento da tela, da conferência de pedido, de proposta aprovada e da importação.
- `POST /obras/{obra_id}/importar` (multipart, CSV `;` ou `,`, UTF-8 ou Windows-1252): colunas
  `nome;unidade;saldo;precisa_ate_o_fim;curva`. Cria insumos e um movimento de AJUSTE inicial. Responde a prévia
  (`?aplicar=false`) ou aplica (`?aplicar=true`). Número ambíguo ("2.760") volta como erro da linha.
  Resposta sempre `{aplicado, linhas:[{linha, nome, unidade, saldo, precisa_ate_o_fim, curva}], erros:[{linha, campo, mensagem}]}`;
  com qualquer erro nada é gravado (`aplicado: false`). Nome já existente na obra é erro da linha. É a importação
  antiga, no nosso formato de colunas; a do orçamento (abaixo) lê a planilha que a construtora já tem.

## Orçamento por planilha

Em dois passos, sem estado no servidor: a tela manda o arquivo para `ler`, mostra a sugestão de colunas, e manda o
mesmo arquivo de novo com o mapeamento confirmado para `importar`. Aceita `.xlsx` e `.csv` (até 5 MB); `.xls` → 422
pedindo para salvar como `.xlsx` ou `.csv`. Papel `admin`+.

- `POST /obras/{obra_id}/orcamento/ler` (multipart `arquivo`, form `planilha?` = nome da aba) →
  `{planilhas: [str], planilha, linha_cabecalho, colunas: [{indice, titulo}], amostra: [[str]], sugestao: Mapeamento}`.
  O cabeçalho é a 1ª das 30 primeiras linhas com pelo menos dois títulos conhecidos (pula título e dados da obra);
  `linha_cabecalho` é 1-based e os índices de coluna são 0-based. `amostra` = até 8 linhas de dados como texto
  (`37,9`, datas `dd/mm/aaaa`). CSV tem uma aba só, `"CSV"`.
- `Mapeamento` = `{planilha, linha_cabecalho, item, codigo, fonte, nome, unidade, quantidade, valor_unitario,
  valor_total, saldo, curva}` (cada coluna é índice ou `null`). Sinônimos da sugestão: item/nº (número do item, "3.2.1")
  · código/cód · fonte/banco/base/referência · descrição/discriminação/insumo/material/serviço · un/und/unid/unidade ·
  quant/qtd/qtde · "unit" (preço unitário) · total · saldo/estoque · curva/abc. `item`, `codigo`, `fonte` só valem no
  orçamento (EAP); `saldo` e `curva`, só na lista de materiais.
- A planilha é uma de duas coisas, e a tela pergunta antes de importar: o **orçamento da obra** (serviços por etapa,
  `POST …/orcamento/versoes`, abaixo) ou uma **lista de materiais** (insumos para comprar e estocar, `…/importar`).

### Lista de materiais

- `POST /obras/{obra_id}/orcamento/importar` (multipart `arquivo`, `mapeamento` = JSON, `aplicar` = bool) →
  `PreviaOrcamento` = `{aplicado, linhas: [LinhaOrcamento], ignoradas: [{linha, texto, motivo}],
  erros: [{linha, campo, mensagem}], total, novos, atualizados, juntadas}`.
  - `nome`, `unidade` e `quantidade` têm de estar mapeados (422).
  - **Ignorada** (não é erro): sem descrição, sem unidade (título de etapa ou subtotal) ou quantidade vazia/zero.
  - **Erro da linha**: número inválido ou ambíguo ("2.760"), negativo, curva fora de A/B/C, descrição > 120 ou unidade
    > 12 caracteres, o mesmo nome com outra unidade na planilha, ou unidade diferente da do insumo já cadastrado.
    Com qualquer erro nada é gravado.
  - `valor_unitario` vem da coluna unitária; sem ela, de `total / quantidade`; sem as duas, 0. Aceita "R$".
  - O mesmo insumo em várias linhas (nome sem diferenciar maiúsculas e espaços, mesma unidade) vira uma linha só:
    quantidades somadas e preço médio ponderado. `juntadas` conta as linhas absorvidas.
  - `curva`: a da coluna, se mapeada; senão ABC pelo valor (A até 80% do total, o item que cruza os 80% incluído;
    B até 95%; resto C; sem preço = C). Planilha sem preço nenhum → B.
  - `LinhaOrcamento` = `{linhas: [int], nome, unidade, quantidade, valor_unitario, total, curva, saldo,
    situacao: "novo" | "atualiza", precisa_antes, valor_antes}`. **atualiza** = já existe insumo com esse nome na obra:
    troca `precisa_ate_o_fim`, `valor_unitario` e `curva` (o saldo da planilha só vale para insumo novo).
  - Gravação em lotes de 100 por transação; insumo novo tem o mesmo ID do cadastro pela tela (nome repetido depois
    colide em vez de duplicar) e saldo > 0 vira um AJUSTE inicial. Registro "Importou o orçamento: N novos, M
    atualizados (R$ X)" por lote, **sem desfazer**: a prévia é a rede de segurança, e corrigir é reimportar.

### Orçamento da obra (EAP, em versões)

O orçamento é etapa → item, como a planilha da construtora: cada linha vira um item, com o número, o código e a fonte
dela, **sem juntar linhas repetidas** (o mesmo serviço em duas etapas são dois itens), e o total fecha no centavo com a
planilha. Cada importação é uma **versão**: o **contrato** (só um vigente; reimportar substitui a versão inteira) ou um
**aditivo** (soma ao contrato, que fica intacto). As duas têm Desfazer. A importação do orçamento **não cria insumos**:
material de almoxarifado vem da lista de materiais, do cadastro ou (Fase 2) do pedido.

- `VersaoOrcamento` = `{id, obra_id, tipo: "contrato" | "aditivo", numero, descricao, data, arquivo, itens, etapas,
  total, ativa, substitui, criado_em}`. `numero`: 0 no contrato, 1, 2… nos aditivos (o maior ativo + 1). `data`
  `"AAAA-MM-DD"` ou `null`. `itens`/`etapas` = quantos de cada; `total` em centavos (soma dos itens). `ativa: false` =
  desfeita ou substituída. `substitui` = a versão de contrato que esta tirou de vigência (ou `null`).
- `ItemOrcamento` = `{id, obra_id, versao_id, ordem, tipo: "etapa" | "item", numero, nivel, etapa, codigo, fonte,
  descricao, unidade, quantidade, valor_unitario, total, linha}`. `ordem` = posição na versão (0, 1, 2…); `numero` =
  o da coluna item ("3.2.1", "A1.2", pode ser `""`); `nivel` = 1 + pontos do número (sem número: etapa 1, item 2);
  `etapa` = a chave da etapa raiz: o que vem antes do 1º ponto ("2" para "2.1.1"); item sem ponto fica na última linha de
  etapa; etapa sem número tem como chave o próprio nome normalizado (sem acento, minúsculo), e é assim que a "FUNDAÇÕES"
  de um aditivo sem numeração soma na do contrato; `""` antes da 1ª etapa; `linha` = linha da planilha (1-based). Etapa tem unidade `""` e quantidade/valores 0.
- `POST /obras/{obra_id}/orcamento/versoes` (multipart `arquivo`, `mapeamento` = JSON, `tipo` = `contrato` | `aditivo`,
  `descricao?` (≤ 200), `data?`, `aplicar` = bool, `chave_idempotencia` (obrigatória com `aplicar`)) → `PreviaVersao` =
  `{aplicado, tipo, itens: [ItemPrevia], ignoradas, erros, avisos: [{linha, mensagem}], total, n_itens, n_etapas,
  etapas: [{etapa, descricao, total}], substitui: {versao_id, total, itens} | null, versao_id, registro_id}`
  (`ItemPrevia` = `ItemOrcamento` sem `id/obra_id/versao_id`; `versao_id`/`registro_id` só depois de aplicar).
  - `nome`, `unidade` e `quantidade` têm de estar mapeados (422).
  - **Etapa**: linha com descrição, sem unidade e sem quantidade, que não é subtotal/total (descrição começando por
    "subtotal", "total" ou "soma" → ignorada). **Item**: descrição + unidade + quantidade ≠ 0. Resto vazio → ignorado.
  - `total` da linha = a coluna total, quando mapeada e preenchida (é o número do contrato); senão quantidade ×
    unitário arredondado. `valor_unitario` = a coluna unitária; sem ela, total ÷ quantidade.
  - **Erro da linha** (nada é gravado): número inválido/ambíguo, quantidade negativa no contrato (no aditivo, negativa
    = supressão, vale), descrição > 300, unidade > 12, código/fonte/número > 40. Mais de 2000 linhas → erro.
  - **Aviso** (grava mesmo assim): o mesmo código (e fonte) com unidades diferentes na planilha: "O código 92916 aparece em m² (linha
    37) e em kg (linha 44): uma das duas unidades está errada." Um aviso por linha divergente.
  - Contrato com outro contrato vigente → `substitui` diz qual sai (a tela avisa antes de aplicar). Aditivo sem
    contrato vigente → 422.
  - Aplicar: os itens são gravados primeiro, em lotes, ligados à versão ainda inexistente (invisíveis); a última
    transação cria a versão, tira a anterior de vigência e grava o registro "Importou o contrato de X (N itens, R$ T)"
    / "Importou o 1º aditivo de X: descrição (+R$ T)", com `desfazer` `desligar_versao`. A mesma chave devolve a
    versão já gravada (200); a mesma chave com outro arquivo/tipo → 409.
  - Desfazer (`desligar_versao`): a versão sai de vigência e a que ela substituiu volta. 409 se ela já não está
    vigente (outra reimportação passou por cima) ou se, ao desfazer um contrato, há aditivo vigente criado depois dele.
- `GET /obras/{obra_id}/orcamento` (qualquer papel) → `{versoes: [VersaoOrcamento], itens: [ItemOrcamento + {curva}],
  etapas: [{etapa, descricao, contratado, aditivos, atual}], totais: {contratado, aditivos, atual, n_itens, n_etapas}}`.
  `versoes`: todas, a mais nova primeiro (inclusive as desligadas, para o histórico), cada uma com o `registro_id` da
  importação (o Desfazer dela). `itens`: só das versões vigentes,
  contrato primeiro e aditivos pela ordem do número, cada um pela `ordem`. `curva` (A/B/C) dos itens pelo `total` da
  linha, na régua do 80/95%, só entre itens com total > 0 (etapas: `null`). `etapas` soma por `etapa` (descrição da
  linha de etapa do contrato; a do aditivo se a etapa só existe nele). Sem nenhuma versão: listas vazias e totais 0.
- **Aditivo nas etapas do contrato:** no `tipo=aditivo`, cada item vai para a etapa do contrato onde o mesmo
  `fonte|codigo` aparece (`etapa_origem: "codigo"`); o resto fica na etapa da planilha (`"planilha"`). O campo
  `etapas_itens` (JSON `{"ordem": "etapa"}`) aponta a etapa de itens específicos (`"escolhida"`; etapa que não existe
  no contrato nem no aditivo → 422). `etapa_planilha` guarda a etapa original. Título de etapa do aditivo que ficou
  sem item some da versão.
- **Previsto × realizado** (Fase 3): cada etapa e os totais trazem também `cotando`, `comprometido` e `realizado`
  (centavos), somados das linhas dos pedidos da obra pela `etapa` de cada linha. `cotando` = pedidos em cotação ·
  `comprometido` = pedidos de aprovado em diante pelo valor da linha (o maior entre o pedido e o entregue; pedido
  encerrado com sobra compromete só o que chegou) · `realizado` = o que já foi entregue ou medido, pelo preço do pedido.
  Cancelado não conta. `totais.sem_etapa` = o gasto sem etapa ou em etapa que não está nas versões vigentes (já somado
  nos totais). Saldo da etapa = `atual − comprometido`. Atenção: `atual` é preço de contrato (com BDI e mão de obra);
  comprometido e realizado são custo de compra.
- `POST /obras/{obra_id}/orcamento/aprovar` → `Obra + registro_id`. Tira a obra de `em_orcamento` para `em_andamento`.
  Já aprovado → 409; sem contrato vigente **e** sem insumo com `precisa_ate_o_fim > 0` → 422. Registro "Aprovou o
  orçamento de X (N itens, R$ total)" com o orçado atual (contrato + aditivos vigentes; sem contrato, "N insumos" da
  lista de materiais) com `desfazer` `reabrir_orcamento` (volta para
  `em_orcamento`; 409 se a obra já não está em andamento). A tela confirma antes, mostrando total e nº de itens.

## Listagens paginadas

Registro, propostas, pedidos, movimentos e sessões vêm do mais novo para o mais antigo e paginam por `antes_de`:
- a página seguinte se pede com `antes_de` = o campo de data do ÚLTIMO item recebido (`criado_em`; nas sessões,
  `atualizado_em`), sem mexer nos outros filtros;
- o cursor é **exclusivo** (`< antes_de`) e a página **nunca parte um empate**: se o item seguinte tem o mesmo instante
  do último, todos os daquele instante vêm nesta página, que pode passar um pouco do `limite`. Assim nada se repete
  entre páginas e nada some na virada (com `<=` um empate do tamanho da página voltaria para sempre);
- acabou quando a página vier com **menos** itens que o `limite`;
- `antes_de` em ISO 8601 **com fuso** (`2026-11-01T13:00:00.123Z` ou `-03:00`); sem fuso → 422. A API compara no
  milissegundo, como grava.

## Suprimentos: pedidos, entregas e fornecedores

- `GET /obras/{obra_id}/etapas` → `[{etapa, descricao}]`: as etapas das versões vigentes do orçamento (contrato
  primeiro, depois as que nasceram em aditivo). Lista vazia = obra sem orçamento; aí a etapa é opcional em tudo.
- `GET /obras/{obra_id}/pedidos?status=…&limite=100&antes_de=…` → `[Pedido]` (mais novos primeiro). `status` pode
  repetir (`?status=enviado&status=em_transito`, até 30 valores): vira um filtro "in". `limite` até 500.
- `Pedido` = `{id, numero, obra_id, fornecedor, fornecedor_id, previsao, observacao, status, valor, recebimentos,
  criado_em, linhas: [{insumo_id, nome, unidade, tipo, etapa, quantidade, valor_unitario, total, recebida}]}`. Nome,
  unidade e tipo são cópia do insumo no momento do pedido. `valor` = soma dos `total`; `recebida` soma as entregas;
  `recebimentos` = quantas entregas valem. Pedido gravado antes das linhas sai no mesmo formato, com uma linha.
- Status: `em_cotacao → aprovado → enviado → em_transito → (recebido) → parcial → conferido`, e `cancelado` até antes
  de receber. `parcial` = recebeu parte, o resto continua "a caminho". A tela chama `conferido` de **Recebido**.
  Conta: `cotando` soma as linhas dos pedidos em cotação; `a_caminho` soma o que **falta entregar** em cada linha
  (`quantidade − recebida`, nunca negativo) dos pedidos de aprovado a parcial.
- `POST /obras/{obra_id}/pedidos` (admin) `{fornecedor, fornecedor_id?, previsao?, observacao?, aprovado?, linhas:
  [{insumo_id, quantidade, valor_unitario?, total?, etapa?}]}` → `Pedido + registro_id` (201; PED-NNNN por tenant).
  1 a 50 linhas; o mesmo insumo duas vezes → 422. `total` padrão = quantidade × unitário arredondado ao centavo
  (informe quando o total combinado não fecha). Com orçamento importado, toda linha exige `etapa` válida. `aprovado`
  `true` → nasce `aprovado`. O formato antigo de um insumo só (`{insumo_id, quantidade, fornecedor, valor}`) ainda é
  aceito e vira uma linha com `total = valor`. Registro: `"PED-0168 criado: 2 itens (Cimento e Brita 1) com X (R$ …)"`;
  desfazer `cancelar_pedido` só enquanto o pedido estiver no status de nascimento.
- `PATCH /pedidos/{pedido_id}` (admin) `{previsao?, observacao?}` → `Pedido + registro_id`. Registro "PED-0168:
  previsão 2026-11-10 → 2026-11-24", desfazer `editar_pedido` (409 se editado de novo depois).
- `POST /pedidos/{pedido_id}/recebimentos` (admin) `{nf_numero?, nf_data?, nf_valor?, linhas: [{indice, quantidade}],
  chave_idempotencia}` → `{recebimento, pedido, registro_id}` (201; a mesma chave devolve a mesma entrega com 200, e
  com outras quantidades → 409). Vale de `aprovado` a `parcial`. Linha de material dá ENTRADA no almoxarifado na
  mesma transação (`origem_doc` "PED-0168 · NF 1187", `etapa` da linha); linha de serviço só soma o medido. Entregar a
  mais é aceito. Tudo entregue → `conferido`; senão → `parcial`. Registro: `"PED-0168 · 1ª entrega (NF 1187): 120 sc
  de Cimento; 18 de 25 m³ de Brita 1 · faltam 7 m³ de Brita 1"` ("medição" quando só há serviço). Desfazer
  `desfazer_recebimento` estorna as entradas e volta as quantidades e o status, **só para a última entrega** (409 se
  houver outra depois ou se o pedido foi encerrado).
- `Recebimento` = `{id, pedido_id, pedido_numero, numero, nf_numero, nf_data, nf_valor, valor, criado_em, quem,
  linhas: [{indice, insumo_id, nome, unidade, tipo, etapa, quantidade, valor}], movimentos}`. `valor` = as linhas pelo
  preço do pedido; a tela compara com `nf_valor`. `GET /pedidos/{pedido_id}/recebimentos` → as que valem, em ordem.
- `POST /pedidos/{pedido_id}/avancar` `{para, quantidade_recebida?, nf?}` → `Pedido + registro_id`. Valida em
  `regras.validar_transicao`. `para = conferido` tem dois sentidos: de `parcial`, **encerra** o resto (o fornecedor
  não manda; sai do "a caminho"; registro "PED-0157 encerrado: o resto não vem (4 m³ de Areia média)", desfazer
  `voltar_pedido`); de `recebido`, recebe tudo o que falta numa entrega só (o caminho que o agente usa), com
  `quantidade_recebida` (só em pedido de uma linha) e `nf` — vira um `Recebimento` igual ao de cima.
  `quantidade_recebida`/`nf` com outro destino → 422.
- `GET /fornecedores` → `[Fornecedor]` por nome; `Fornecedor` = `{id, nome, cnpj, contato, telefone, email}` (cnpj só
  dígitos ou `""`). São do tenant, valem para todas as obras.
- `POST /fornecedores` (admin) `{nome, cnpj?, contato?, telefone?, email?}` → `Fornecedor + registro_id` (201). CNPJ
  com ou sem pontuação, dígitos verificadores conferidos (inválido → 422); CNPJ repetido, ou o mesmo nome sem CNPJ →
  409 "Já cadastrado: X". Sem desfazer.
- `PATCH /fornecedores/{id}` (admin) com os mesmos campos, parciais → `Fornecedor + registro_id`, desfazer
  `editar_fornecedor`.

## Cronograma físico-financeiro e medição

Percentual em centésimos de ponto (12,5% = 1250; 100% = 10000), mês `"AAAA-MM"`. O % da obra é ponderado pelo orçado
atual de cada etapa (contrato + aditivos vigentes).

- `GET /obras/{obra_id}/cronograma` → `{inicio, meses, base, revisoes, etapas, mensal, medicoes, prazo}`. Sem cronograma: `inicio`
  `null`, `meses` 0, as etapas do orçamento com `percentuais: []` e `prazo.termino_contratual` pelo `inicio` +
  `prazo_meses` da obra (ou `null`).
  - `etapas`: `[{etapa, descricao, atual, percentuais, medido_acumulado}]`, todas as do orçamento vigente.
  - `mensal`: `[{mes, previsto, previsto_acumulado, pct_previsto_acumulado, medido, medido_acumulado,
    pct_medido_acumulado}]`, do início ao fim do cronograma (ou até a última medição, se for depois). `previsto` = Σ
    orçado atual × % da etapa no mês. Os campos de medido são `null` depois da última medição.
  - `prazo`: `{termino_contratual, termino_projetado, atraso_meses, pct_previsto_na_medicao, pct_medido,
    mes_referencia}`. Referência = mês da última medição. Atraso = referência − 1º mês em que o previsto acumulado
    alcança o % medido, interpolado dentro do mês: `atraso_decimos` (1,4 mês = 14; negativo = adiantada) e `atraso_meses`
    arredondado (meio mês para cima). Projetado = contratual + `atraso_meses`. Sem medição, só o contratual.
- `PUT /obras/{obra_id}/cronograma` (admin) `{inicio, meses (1–60), etapas: [{etapa, percentuais}]}` → o mesmo do GET
  + `registro_id`. Substitui o cronograma inteiro. Cada etapa soma exatamente 10000 ou é toda zero (fica fora);
  `percentuais` com `meses` posições; etapa fora do orçamento, repetida ou obra sem orçamento → 422. Com medição já
  lançada, o novo cronograma tem de cobrir o mês dela (→ 422). Registro "Montou/Refez o cronograma: N etapas, M meses a
  partir de AAAA-MM (era K)", desfazer `restaurar_cronograma` (409 se editado de novo depois).
  - **Linha de base:** o primeiro cronograma vira a base (`base: {inicio, meses}`); os PUTs seguintes são
    reprogramações (`revisoes` conta) e mantêm a base, a menos que venha `nova_linha_base: true`. `mensal[].
    pct_base_acumulado` e `prazo.{termino_base, atraso_base_decimos, atraso_base_meses}` medem contra a base: é o
    atraso que reprogramar não apaga.
  - **Aditivo de prazo:** `atualizar_prazo_da_obra: true` grava `meses` em `obra.prazo_meses` na mesma transação; o
    desfazer volta o cronograma **e** o prazo (409 se o prazo mudou de novo depois).
- `POST /obras/{obra_id}/medicoes` (admin) `{mes, etapas: [{etapa, pct_acumulado}], observacao?, chave_idempotencia}` →
  `{medicao, cronograma, registro_id}` (201; a mesma chave devolve a mesma medição com 200; com outro conteúdo → 409).
  Exige cronograma (→ 422). `mes` depois da última medição e entre o início e 12 meses após o término; acumulado entre
  0 e 10000 e nunca menor que o anterior (corrigir = desfazer). Etapa que não vem repete o acumulado anterior.
  `Medicao` = `{id, numero, mes, etapas: [{etapa, pct_acumulado, pct_periodo, valor_periodo}], valor,
  pct_obra_acumulado, observacao, quem, criado_em}`; `valor_periodo` = orçado atual × % do período. Registro "3ª
  medição (2026-03): R$ X no mês · Y% da obra executada"; desfazer `desfazer_medicao` só da última.

## Propostas, aprovação e registro

- `POST /propostas` `{obra_id, tipo, titulo, conta, justificativa, fonte, dados}` → `Proposta` (pendente). Só valida e
  guarda: **não executa**. Qualquer papel propõe (o agente usa o token de quem conversa); `dados` inválido → 422.
  `dados` por tipo: `criar_pedido` = `{insumo_id, quantidade, fornecedor, valor, previsao?}` (o corpo do POST de
  pedido **sem** `aprovado`) · `avancar_pedido` = `{pedido_id, para, quantidade_recebida?, nf?}` (as mesmas regras do
  `POST …/avancar`) · `ajuste_estoque` = `{insumo_id, quantidade, motivo}`.
- `GET /propostas?status=pendente&obra_id=…&limite=100&antes_de=…` → `[Proposta]` (mais novas primeiro)
- `POST /propostas/{id}/aprovar` `{ajustes?}` (corpo opcional) → `{proposta, registro}` (o `registro.id` é o que o
  Desfazer usa); executa a operação do tipo (papel `admin`+), marca aprovada, grava `EntradaRegistro` com `desfazer`
  preenchido. `POST /propostas/{id}/recusar` `{motivo}`.
- Aprovar `criar_pedido` cria o pedido já **`aprovado`** (não `em_cotacao`): a aprovação da proposta é a aprovação da
  compra. O texto do registro termina em `· aprovado`.
- **Ajustar e aprovar**: `ajustes` corrige campos de `dados` antes de executar. Chaves aceitas por tipo:
  `criar_pedido` → `quantidade, fornecedor, valor, previsao` (só em proposta de uma linha) · `avancar_pedido` → `quantidade_recebida, nf` (só se
  `dados.para = conferido`, senão 422) · `ajuste_estoque` → `quantidade, motivo`. Chave fora da lista → 422. Os dados
  mesclados são revalidados com as regras da criação (ex.: ajuste de zero → 422); erro deixa a proposta pendente.
  Na mesma transação a proposta grava `dados` (o que foi executado), `dados_originais` (o que o agente propôs) e
  `ajustada: true`, e o texto do registro ganha o sufixo `· ajustado: quantidade 34 → 30 sc; fornecedor A → B`
  (`—` para campo que estava vazio). Sem corpo, `ajustes` vazio ou com os mesmos valores = aprovação normal
  (`ajustada: false`, `dados_originais: null`).
- Aprovar grava em `Proposta.registro_id` o ID da `EntradaRegistro` da execução (a tela mostra o Desfazer mesmo depois
  de recarregar); desfazer mantém o `registro_id` e muda a proposta para `desfeita`.
- Proposta decidida traz `decidido_por` (ID) e `decidido_por_nome`. `EntradaRegistro.quem` é o nome legível;
  vem também `quem_nome` (igual) e `usuario_id`.
- Respostas de escrita que já trazem o ID do registro, para a tela oferecer **Desfazer** logo depois: `POST
  /obras/{id}/movimentos`, `POST /obras/{id}/pedidos` e `POST /pedidos/{id}/avancar` (campo `registro_id`) e `POST
  /propostas/{id}/aprovar` (`registro.id`).
- `GET /registro?obra_id=…&limite=50&antes_de=…` → `[EntradaRegistro]` (mais novas primeiro) · `POST /registro/{id}/desfazer` → aplica a operação inversa
  (estorno de movimento, volta de status do pedido) e registra o desfazer.
- **Refazer** é o mesmo `POST /registro/{id}/desfazer` aplicado à entrada "Desfeito: …": quando o desfazer tem
  inverso (medição, cronograma, edição de obra/pedido/fornecedor), a entrada "Desfeito" vem com `desfazer.refazer:
  true` e `desfazer.texto` = o texto da ação original; a nova entrada é "Refeito: <texto original>", com desfazer
  normal. Refazer a medição só vale se o cronograma ficou como o desfazer deixou (senão 409 "lance a medição de
  novo"); as edições, só se ninguém mexeu nos mesmos campos depois. Entradas "Desfeito" anteriores a esta regra não
  têm receita.

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
- `GET /sessoes?obra_id=&limite=30&antes_de=…` → `[Sessao]` (mais recentes primeiro; aqui `antes_de` compara com
  `atualizado_em`, a ordem da lista) · `GET /sessoes/{id}` →
  `Sessao` · `PATCH /sessoes/{id}` `{titulo?, obra_id?, abas?}`.
- `GET /sessoes/{id}/mensagens?depois_de=-1&limite=200` → `[{id, sessao_id, seq, papel, conteudo, criado_em}]` (por seq).
- `POST /sessoes/{id}/mensagens` `{seq_inicial, mensagens:[{papel: user|assistant, conteudo: texto | [blocos]}]}`
  (até 50) → `Sessao + mensagens` (201). Só acrescenta: `seq_inicial` tem de ser o `total_mensagens` atual; reenvio do
  mesmo lote → 200; outra coisa → 409 com o esperado na mensagem. Mensagem acima de ~800 KB → 422; lote com mais de
  9 MiB de conteúdo somado → 413 (o lote é uma transação só, e o Firestore recusa commit acima de 10 MiB): divida.

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
| `propor_avanco_pedido(obra_id, pedido_id, para, justificativa, quantidade_recebida?, nf?)` | propõe | `POST /propostas` (avancar_pedido) |
| `propor_ajuste_estoque(obra_id, insumo_id, quantidade, motivo)` | propõe | `POST /propostas` (ajuste_estoque) |

- As ferramentas devolvem números já formatados **e** brutos; o agente nunca faz conta, ele cita a `Conta` da API.

## Dados de exemplo (seed)

Casa Vila Nova do protótipo: insumos bloco (entrou 4.000, saiu 1.050 + 190 de quebra = 2.760), aço (3.940 precisa,
140 saldo, PED-0158 de 3.200 kg em trânsito, PED-0167 de 600 kg em cotação), cimento (precisa 200, saldo 46, PED-0160
de 120 sc a caminho), areia (PED-0157 recebido, 40 m³ na NF). Movimentos semeados com `quem = "Joana (mestre)"`.
Um usuário `admin` de exemplo, senha só no `.env` local.
