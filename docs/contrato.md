# Contrato do MVP-0 (API central × agente × web)

Fonte da verdade entre as três frentes. Mudou aqui, mudou para todos: quem altera avisa no relatório.
Modelos de documento: `dominio/src/canteiro_dominio/modelos.py`. Regras: `regras.py`.
Quantidade em **milésimos** e dinheiro em **centavos** (inteiros) em todo JSON; a formatação ("2.760", "R$ 472,00")
é do front e das mensagens do agente.

## Autenticação (login próprio)

- `POST /auth/entrar` `{email, senha}` → `{acesso, renovacao, usuario:{id, nome, tenant_id, papel}}`.
  Acesso: JWT curto (15 min). Renovação: opaco, guardado no banco (hash), revogável, troca a cada uso.
- `POST /auth/renovar` `{renovacao}` → novo par **e** `usuario` (mesmo corpo do `/auth/entrar`). `POST /auth/sair` revoga.
  Token já trocado que reaparece: até `JANELA_REUSO_RENOVACAO_SEGUNDOS` (10 s) depois da troca → **409**
  `{detail: "Renovação em andamento; tente de novo."}`, sem emitir token e sem revogar nada (é a renovação que estava
  no ar quando a página recarregou; o cliente espera o cookie novo e tenta de novo). Depois disso → 401 e a família
  inteira é revogada (reuso = vazamento). 401 também para token desconhecido, revogado ou vencido.
- `POST /auth/senha` (qualquer papel, `Authorization: Bearer <acesso>`) `{senha_atual, senha_nova}` → **o mesmo corpo
  do `/auth/entrar`** (`{acesso, renovacao, usuario}`): uma sessão nova para o aparelho que trocou.
  - Regras, conferidas **antes** da senha atual e sem gastar tentativa → 422 com `detail` pronto: "Informe a senha
    atual." · "A nova senha precisa ter de 8 a 256 caracteres." · "…diferente da atual." · "…não pode ser o seu e-mail."
    (comparação sem maiúsculas e espaços nas pontas).
  - Senha atual errada → **401** `{detail: "Senha atual incorreta."}`, **sem** `WWW-Authenticate`: a sessão continua
    válida, o cliente não deve desconectar ninguém. Cada erro conta no **mesmo limite do login** (por e-mail do usuário e
    por IP, reserva transacional antes do argon2); estourou → 429 (e o `/auth/entrar` desse e-mail também fica em 429
    até a janela passar). Troca certa zera o contador do e-mail.
  - Sucesso: hash novo (argon2) e `sessoes_validas_desde` no usuário na mesma transação. **Todas** as sessões caem,
    inclusive a de quem trocou (que recebe a nova): renovação de família nascida antes da troca → 401 no `/auth/renovar`,
    e acesso emitido antes dela → 401 em qualquer rota ("Sua senha foi trocada. Entre de novo."), sem esperar os 15 min.
    As pontas vivas das famílias também ficam `revogado`. Registro "Trocou a própria senha", `obra_id` nulo, **sem
    `desfazer`** (como a chave da Anthropic: a receita guardaria o hash velho, e desfazer devolveria uma senha que a
    pessoa trocou por achar que vazou). Nada da senha vai ao registro.
- Senha com argon2. Limite de tentativas por e-mail e por IP. O IP só vem do `X-Forwarded-For` quando quem chama está
  em `TRUSTED_PROXIES` (IPs/redes separados por vírgula no `.env`, vazio por padrão); vale o último endereço da cadeia
  que não é proxy de confiança. Papéis no banco: `viewer` (lê), `admin` (aprova e
  opera), `master` (+usuários). O papel **nunca** vem do token: a API resolve no banco a cada requisição.
- Todas as rotas abaixo exigem `Authorization: Bearer <acesso>` e só enxergam o `tenant_id` do usuário.
- Erros: `{detail: "mensagem pronta para a pessoa"}`. 401 sem sessão · 403 papel insuficiente · 404 não existe (ou é
  de outro cliente) · 409 conflito (já decidido, chave reutilizada, fora de ordem) · 413 corpo grande demais · 422 regra de
  negócio · 429 tentativas.
- Tamanho do corpo, conferido antes do login: até 1 MiB; 6 MiB nas rotas de upload (`…/orcamento/ler`,
  `…/orcamento/importar`, `…/orcamento/versoes`, `/obras/{id}/importar`, `/obras/{id}/comprovantes`); 10 MiB em `POST /sessoes/{id}/mensagens`.
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
  Dados fiscais (todos opcionais; ver **Tributos da obra**): `cno` (só os 12 dígitos; aceita pontuação na entrada,
  outro tamanho → 422; `""` = não informado) · `proprietario` ∈ `pf | pj` · `destinacao` ∈ `residencial_unifamiliar |
  residencial_multifamiliar | casa_popular | conjunto_habitacional | comercial | edificio_garagens | galpao` (as da
  tabela VAU do SERO) · `material` ∈ `alvenaria | mista | madeira` (os quatro podem ser
  `null`) · `area_complementar`, `area_descoberta` em milésimos de m² · `vau` à mão, em centavos por m² (0 = usar a tabela VAU do SERO) ·
  `vau_referencia` `"AAAA-MM"` ou `null` · `finalidade` ∈ `uso_proprio | venda | prestacao_servico` ou `null` ·
  `valor_terreno` em centavos · `unidades` (imóveis residenciais vendidos) · `categoria` ∈ `obra_nova | acrescimo |
  reforma | demolicao` ou `null` (= reforma se a `tipologia` é reforma, senão obra nova) · `dctfweb_em_dia` `true |
  false | null` (a DCTFWeb foi entregue em todos os meses da obra: condição do fator de ajuste).
- `POST /obras` (admin) `{nome, chave_idempotencia, …os campos acima, todos opcionais}` → `Obra + registro_id`. Nasce
  `em_orcamento`. ID derivado da chave: a repetição devolve a mesma obra (201 na 1ª, 200 nas repetidas); a mesma chave
  com outro nome → 409. Registro "Criou a obra X", **sem desfazer** (a porta de armazenamento não apaga).
- `PATCH /obras/{obra_id}` (admin) `{…qualquer campo da obra}` → `Obra + registro_id`. Só os campos enviados contam, e
  os que já têm aquele valor são ignorados; se nada muda, `registro_id: null` e nenhum registro. Registro "Atualizou a
  obra X: área, padrão" com `desfazer` `restaurar_obra`, que volta os valores antigos e é recusado (409) se algum
  daqueles campos mudou de novo depois. `nome: null` → 422.
- `GET /obras/resumo` (qualquer papel) → `[{obra: Obra, pendencias, falta_comprar, pedidos_abertos,
  valor_pedidos_abertos, nf_para_conferir, ultima_atividade, prazo, tem_orcamento}]` (por nome da obra), para a página
  inicial. `tem_orcamento` = contrato vigente ou algum insumo com `precisa_ate_o_fim > 0`.
  `prazo` = o `Prazo` do `GET /obras/{id}/cronograma` (término contratual e projetado, atraso), `null` sem cronograma.
  `pendencias` = propostas pendentes · `falta_comprar` = insumos com `Conta.comprar > 0` (a mesma Conta de
  `GET …/insumos`) · `pedidos_abertos` = pedidos fora de `conferido`/`cancelado` e `valor_pedidos_abertos` o que eles
  ainda vão custar (centavos): o `valor` inteiro em cotação; depois, só a parte que falta entregar de cada linha · `nf_para_conferir` = pedidos `recebido` · `ultima_atividade` = `criado_em` da entrada mais
  nova do registro da obra, ou `null`. Somado na API: uma consulta por coleção para o tenant inteiro (sem agregação no
  banco) e uma leitura de 1 documento do registro por obra.
- `GET /obras/{obra_id}/insumos` → `[{insumo: Insumo, conta: Conta, ultimo_preco}]`, onde
  `Conta = {precisa, saldo, a_caminho, cotando, transferencia, falta, comprar, sobra, sem_previsao, texto}` calculada
  por `regras.conta_de_falta` (a_caminho e cotando vêm dos pedidos da obra; transferencia = 0 no MVP-0).
  - `sem_previsao` = `precisa` 0: ninguém lançou a previsão (a falta não é calculada; **não** quer dizer "não precisa").
    `sobra` (milésimos) = o que tem e vem além do que precisa (0 sem previsão).
  - `texto` = a linha de conta pronta (`regras.texto_da_conta`, domínio ≥ 0.3.0), sempre aritmética verdadeira, em três
    formas: com falta `"precisa 200 sc − saldo 46 sc − a caminho 120 sc − em cotação 0 sc − transferência 0 sc = falta
    34 sc"`; com sobra `"saldo 32 m³ + a caminho 40 m³ + em cotação 0 m³ + transferência 0 m³ = 72 m³ para precisa
    60 m³: sobra 12 m³, falta 0"`; sem previsão `"sem previsão lançada para a obra (…) · saldo 60 sc · a caminho 0 sc
    · …"`. Saldo negativo aparece como 0 com o valor do livro entre parênteses.
  - `ultimo_preco` = `{valor_unitario (centavos por unidade), pedido_id, numero, fornecedor, data ("AAAA-MM-DD", dia de
    criação no fuso de São Paulo), status}` da linha do pedido **não cancelado** mais novo desse insumo na obra, ou
    `null` se nunca foi comprado. É o "quanto custa 10 sacos a mais?" sem dividir total por quantidade.
- `Insumo.valor_unitario` (centavos, 0 = sem preço) é o preço unitário do orçamento. `Insumo.etapa` = chave da etapa
  do orçamento onde ele mais pesa (`null` = cadastrado à mão ou anterior à aprovação): agrupa "Falta comprar" e sugere a
  etapa da linha do pedido. Na `Conta`, `medido` = o que já foi medido de um serviço (não é saldo, mas desconta da falta).
- `Insumo.tipo` ∈ `material | servico` (padrão `material`). Serviço (terraplanagem, aterro, empreiteiro) não tem
  estoque: movimento de entrada/saída/ajuste nele → 422, saldo sempre 0, e "a caminho" na Conta é o contratado ainda
  não medido.
- `POST /obras/{obra_id}/insumos` `{nome, unidade, tipo?="material", curva?="C", precisa_ate_o_fim?=0, valor_unitario?=0}` → `Insumo` + `registro_id`
  (admin). O que chegou ou foi comprado fora da planilha do orçamento. Nome repetido na obra (sem diferenciar
  maiúsculas e espaços) → 409. Com `precisa_ate_o_fim` 0 o insumo não entra na conta de falta. Registro "Cadastrou o
  insumo X (un, curva C)" com `desfazer` `restaurar_insumos` (ver abaixo).
- **Desfazer de cadastro e importação de insumos** (`restaurar_insumos`): a porta não apaga, então o insumo criado é
  **arquivado** (`Insumo.arquivado = true`, domínio ≥ 0.2.0): sai de `GET …/insumos`, do resumo e da aprovação do
  orçamento, e movimento ou pedido nele → 404. Saldo inicial de importação sai por um AJUSTE oposto. Os campos que uma
  importação de orçamento sobrescreveu voltam ao valor de antes. Tudo ou nada: 409 se qualquer insumo do lote teve
  movimento ou foi editado depois. Refazer reativa (e relança o ajuste). Cadastrar ou importar de novo o mesmo nome
  reaproveita o documento arquivado (o ID sai do nome).
- `GET /obras/{obra_id}/insumos/{insumo_id}/movimentos?limite=50&antes_de=…` → `[Movimento]` (mais novos primeiro;
  paginação em "Listagens paginadas")
- `GET /obras/{obra_id}/movimentos?limite=50&antes_de=…` (qualquer papel) → `[Movimento]` de **todos** os insumos da
  obra, mais novos primeiro (o "histórico do almoxarifado" sem insumo; mesma paginação)
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
  com qualquer erro nada é gravado (`aplicado: false`). Nome já existente na obra (sem diferenciar maiúsculas e
  espaços) é erro da linha; o nome é gravado com os espaços arrumados e o ID sai dele, o mesmo do cadastro pela tela
  (duas importações iguais ao mesmo tempo não duplicam: a segunda → 409). Aplicada, a resposta traz `registro_ids`
  (um registro "Importou N insumo(s) da planilha" por lote de 100, com `desfazer` `restaurar_insumos`). É a importação
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
  erros: [{linha, campo, mensagem}], total, novos, atualizados, juntadas, registro_ids}` (`registro_ids` só quando
  aplicada: um por lote).
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
    atualizados (R$ X)" por lote, com `desfazer` `restaurar_insumos`: arquiva os novos e devolve aos atualizados
    `precisa_ate_o_fim`, `valor_unitario` e `curva` de antes.

### Materiais do projeto estrutural

- `POST /obras/{obra_id}/orcamento/materiais` (JSON `{origem, itens: [{nome, unidade, quantidade, valor_unitario}],
  aplicar}`, admin) → o mesmo `PreviaOrcamento` da lista de materiais. É a lista que o canteiro-cad tira das pranchas
  (`GET /v1/projetos/materiais`): a web manda o nome do insumo do SINAPI quando ele existe e o preço de referência.
  - `quantidade` em milésimos (> 0), `valor_unitario` em centavos (0 = sem preço); até 500 itens; `origem` (até 80
    caracteres) entra no registro: "Importou os materiais do {origem}: N novos, M atualizados (R$ X)".
  - Unidade em ASCII do tradutor vira a do almoxarifado (`m3` → `m³`, `m2` → `m²`). O mesmo nome repetido soma; com
    outra unidade, ou unidade diferente da do insumo já cadastrado, é erro da linha e nada é gravado.
  - Daí em diante é a lista de materiais: **atualiza** troca `precisa_ate_o_fim`, `valor_unitario` e `curva`; a curva
    sai do peso no valor; lotes de 100 com `desfazer` (`"Desfeito: importação dos materiais do {origem}"`). O que falta
    comprar é a `Conta` de sempre (precisa − saldo − a caminho).

### Materiais do projeto como orçamento (etapa "Estrutura")

- `POST /obras/{obra_id}/orcamento/estrutura` (JSON `{etapa?, itens: [{nome, unidade, quantidade, valor_unitario}],
  aplicar, chave_idempotencia?}`, admin) → `PreviaVersao`. Os materiais do projeto viram uma **versão do orçamento**
  (uma etapa, "Estrutura" por padrão), em vez da lista de materiais: vira **contrato** se a obra ainda não tem um;
  com contrato e a obra ainda em orçamento, **soma ao contrato** (o mesmo `somar` de `…/orcamento/versoes`: etapa pelo
  nome, `repetido_de`, Desfazer volta o anterior); com o orçamento aprovado, **aditivo**. `ignorar_itens?` (lista de
  `ordem`, ≤ 500) tira itens da prévia e da gravação. Reaproveita a importação de versão (prévia, gravação em versão, Desfazer). `quantidade` em
  milésimos, `valor_unitario` em centavos; 201 quando grava. É alternativa ao `…/orcamento/materiais` (lista de
  materiais em Compras): aqui o projeto entra como orçamento por etapa.

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
  Em `GET …/orcamento` cada versão traz também: `origem` = `"somar"` (contrato gravado pelo somar, da planilha ou de
  `…/estrutura`) | `"reordenar"` (pela ordem das etapas) | `null` (as outras, e as gravadas antes deste campo);
  `rotulo` = o nome para a pessoa, sem nome de arquivo do sistema ("Materiais das pranchas" no lugar de
  "Estrutura (projeto).csv"; "Orçamento com a planilha somada" / "Orçamento com os materiais das pranchas somados";
  "Etapas reordenadas"; nas outras, `descricao` ou `arquivo`); `desfazer_texto` = o que o Desfazer dela faz, pronto
  para mostrar ("Volta o orçamento para antes de somar a planilha (R$ 50.977,41, 9 itens)." no somar, com o total e
  os itens do contrato que volta; "Volta as etapas do orçamento para a ordem de antes." no reordenar) ou `null` (a
  tela usa o texto dela). `situacao` = `"vigente"` (`ativa`) | `"substituida"` (saiu porque outra versão tomou o
  lugar: reimportação, somar, reordenar) | `"desfeita"` (saiu pelo Desfazer dela mesma). `ativa: false` sozinho não
  separa as duas; a API resolve pelo registro de cada versão, então a cadeia pranchas → somar → reordenar dá
  substituida, substituida, vigente, e desfazer a reordenação dá substituida, vigente, desfeita. O `arquivo` gravado não muda: o sufixo " (projeto).csv" é como a API reconhece as pranchas.
- `ItemOrcamento` = `{id, obra_id, versao_id, ordem, tipo: "etapa" | "item", numero, nivel, etapa, codigo, fonte,
  descricao, unidade, quantidade, valor_unitario, total, linha}`. `ordem` = posição na versão (0, 1, 2…); `numero` =
  o da coluna item ("3.2.1", "A1.2", pode ser `""`); `nivel` = 1 + pontos do número (sem número: etapa 1, item 2);
  `etapa` = a chave da etapa raiz: o que vem antes do 1º ponto ("2" para "2.1.1"); item sem ponto fica na última linha de
  etapa; etapa sem número tem como chave o próprio nome normalizado (sem acento, minúsculo), e é assim que a "FUNDAÇÕES"
  de um aditivo sem numeração soma na do contrato; `""` antes da 1ª etapa; `linha` = linha da planilha (1-based). Etapa tem unidade `""` e quantidade/valores 0.
- `POST /obras/{obra_id}/orcamento/versoes` (multipart `arquivo`, `mapeamento` = JSON, `tipo` = `contrato` | `aditivo`,
  `descricao?` (≤ 200), `data?`, `aplicar` = bool, `chave_idempotencia` (obrigatória com `aplicar`), `etapas_itens?`,
  `somar?` = bool (padrão `false`), `ignorar_itens?` = JSON lista de `ordem`) → `PreviaVersao` =
  `{aplicado, tipo, itens: [ItemPrevia], ignoradas, erros, avisos: [{linha, mensagem}], total, n_itens, n_etapas,
  etapas: [{etapa, descricao, total}], substitui: {versao_id, total, itens} | null, total_apos, versao_id, registro_id}`
  (`ItemPrevia` = `ItemOrcamento` sem `id/obra_id/versao_id`, mais `etapa_origem`, `etapa_planilha` e `repetido_de`;
  `versao_id`/`registro_id` só depois de aplicar).
  - `ignorar_itens` (JSON `[3, 7]`, as `ordem` da prévia; inválido → 422): esses itens saem da prévia e da gravação
    (ordem que não é de item é ignorada). Entra na assinatura de idempotência, como `somar` e `etapas_itens`.
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
- **Somar ao orçamento** (`somar=true`, antes de aprovar): completa o orçamento com outra planilha (o caso de quem
  levou as pranchas e agora traz a planilha), **sem aditivo**. Só com `tipo=contrato`, a obra em orçamento e um contrato
  vigente; senão 422 ("Somar vale para o orçamento, não para o aditivo." / "O orçamento já foi aprovado: para
  acrescentar itens, lance um aditivo." / "Ainda não há orçamento para somar…"). Grava um **contrato novo** = as
  linhas do vigente (com as edições feitas nele) + as da planilha, cada uma no fim da sua etapa e as etapas novas no
  fim; ele `substitui` o vigente, então `GET …/orcamento` mostra um contrato só e `aditivos` = 0. Prévia: `itens`,
  `total`, `n_itens`, `etapas` = **só o que entra**; `substitui` = o vigente; `total_apos` = `substitui.total + total`
  (o orçamento depois de somar; `null` fora do somar). Registro "Somou N itens ao orçamento de X (+R$ T)", desfazer
  `desligar_versao` (com `somou: true`): o somado sai e o anterior volta ("Desfeito: os itens somados ao orçamento de X
  saíram (o anterior volta a valer)"). 409 se o vigente mudou (edição, outra importação) entre a prévia e a gravação.
  Somado com mais de 2000 linhas → 422. A mesma chave devolve a versão gravada: aí `itens`/`total` são os do contrato
  somado inteiro. Depois de aprovado nada muda: contrato novo continua 409 e o caminho é o aditivo.
  - **Pranchas depois dos preliminares** (só no somar): se o vigente é só das pranchas (uma etapa, gravada por
    `…/orcamento/estrutura`) e a planilha traz uma etapa cujo nome tem "preliminar" ou "canteiro", a etapa das
    pranchas vai para **logo depois** dela e as etapas do contrato somado são renumeradas 1..N nessa ordem (os itens
    trocam só o prefixo: "2.1" → "1.1"). Sem etapa assim, a das pranchas fica onde está (adivinhar a ordem da obra
    por outros nomes erraria mais do que acertaria). A **prévia continua com os números de antes** (são eles que
    valem em `etapas_itens`) e traz um aviso com `linha: 0`: "Ao somar, Estrutura (das pranchas) vai para depois de
    SERVIÇOS PRELIMINARES, e as etapas são renumeradas nessa ordem." Se já há lançamento numa etapa que mudaria de
    número (ver "Ordem das etapas"), não reordena nem avisa.
- **Ordem das etapas** (antes de aprovar): `POST /obras/{obra_id}/orcamento/etapas/ordem` (JSON `{ordem: [chave da
  etapa], chave_idempotencia (8–64), versao_id?}`, admin) → `{versao_id, registro_id, etapas: [{etapa, descricao}]}` (201; a mesma
  chave com a mesma ordem → 200 com o mesmo corpo; com outra ordem → 409). `ordem` = **todas** as chaves das etapas do
  contrato vigente, cada uma uma vez, na ordem nova (`etapas[].etapa` de `GET …/orcamento`). Grava um **contrato
  novo** no lugar do vigente (a mesma mecânica do somar): as mesmas linhas, com as edições de preço, as etapas
  renumeradas 1..N na ordem pedida e os itens junto ("1.2" da etapa que virou a 3 fica "3.2"); etapa sem numeração
  (chave pelo nome) só muda de lugar. `etapas` da resposta = as etapas já renumeradas. Registro "Reordenou as etapas
  do orçamento de X", desfazer `desligar_versao` (com `reordenou: true`): o anterior volta ("Desfeito: as etapas do
  orçamento de X voltaram à ordem de antes"). Erros: sem orçamento → 422; lista que não é permutação das etapas
  (falta, sobra ou repete) → 422 "Mande todas as etapas do orçamento, cada uma uma vez: 1, 2, 3."; a mesma ordem de
  hoje → 422 "Nada mudou…"; `versao_id` (o contrato vigente que a tela viu; a web manda sempre) que não é mais o
  vigente → 409 "O orçamento mudou desde que você abriu a tela; recarregue e confira a ordem." (depois de uma
  reordenação as chaves são sempre 1..N, e só o `versao_id` denuncia a tela velha); obra aprovada → 409; aditivo vigente → 409; orçamento mudou entre a leitura e a gravação →
  409; **lançamento numa etapa que mudaria de número** (gasto não estornado, linha de pedido não cancelado, tópico,
  movimento, insumo, cronograma) → 409 "Já há lançamento (gasto, pedido, tópico ou cronograma) na etapa 3 FUNDAÇÕES:
  renumerar levaria o lançamento para outra etapa." (renumerar trocaria calado a etapa dele); viewer → 403; obra de
  outro cliente → 404.
  - **Desfazer de uma mudança de ordem** (a reordenação, ou o somar que reordenou): a receita guarda
    `renumeradas` = `{chave: etapa que ela é no contrato novo}` das chaves que o contrato de antes também tem com outro
    sentido. Se há lançamento (o mesmo critério acima) numa delas, o Desfazer → 409 "Já há lançamento (…) na etapa 1
    FUNDAÇÕES depois da mudança de ordem: desfazer levaria o lançamento para outra etapa. Mude a etapa dele antes de
    desfazer." Chave que só o contrato novo tem não trava: desfeito, o lançamento nela aparece "sem etapa do
    orçamento", à vista.
- **Etapa pelo nome** (no somar e no aditivo): a etapa da planilha casa com a do orçamento vigente (contrato e
  aditivos) pelo **nome** (sem acento, sem caixa, espaços e pontuação normalizados), nunca pelo número: "1 SERVIÇOS
  PRELIMINARES" não entra na "1 Estrutura". Casou → os itens vão para ela (`etapa_origem: "nome"` quando a chave muda).
  Não casou → **etapa nova**, com o número da planilha se ele está livre, senão o próximo livre (maior número + 1);
  `etapa_planilha` guarda o original. Etapa da planilha sem linha de título não tem nome para comparar e fica na chave
  que veio. **Números:** etapa nova que mudou de número troca o prefixo ("1.2" → "3.2"); no somar, item que entra em
  etapa que já existe continua a numeração dela (2.3, 2.4…, sem repetir); no aditivo, item que casou na mesma etapa,
  ou foi pelo código ou pela escolha, guarda o número da planilha (ele aponta o item do contrato que acresce ou
  suprime). No somar, o título da etapa que já existe não se repete.
- **Item repetido** (no somar e no aditivo, só na prévia): item da planilha que parece já estar no orçamento vigente
  ganha `repetido_de: {item_id, descricao, quantidade, unidade}` (o item vigente) e um aviso em `avisos` ("Concreto
  fck 25 sapatas parece repetir Concreto usinado fck 25 (20 m³), que já está no orçamento."). Critério: o mesmo
  `fonte|codigo` (código com algum dígito); ou a mesma unidade e a mesma 1ª palavra com um número em comum, sem
  número que desdiga ("Concreto fck 25 sapatas" × "Concreto usinado fck 25" e "Aço CA-50 10 mm" × "Aço CA-50" sim;
  "Forma de madeira" × "Porta de madeira" e "fck 25" × "fck 30" não); ou a descrição igual. Supressão (quantidade
  negativa) não é marcada. Só avisa: a pessoa tira com `ignorar_itens`.
- `GET /obras/{obra_id}/orcamento` (qualquer papel) → `{versoes: [VersaoOrcamento], itens: [ItemOrcamento + {curva}],
  etapas: [{etapa, descricao, contratado, aditivos, atual}], totais: {contratado, aditivos, atual, n_itens, n_etapas}}`.
  `versoes`: todas, a mais nova primeiro (inclusive as desligadas, para o histórico), cada uma com o `registro_id` da
  importação (o Desfazer dela). `itens`: só das versões vigentes,
  contrato primeiro e aditivos pela ordem do número, cada um pela `ordem`. `curva` (A/B/C) dos itens pelo `total` da
  linha, na régua do 80/95%, só entre itens com total > 0 (etapas: `null`). `etapas` soma por `etapa` (descrição da
  linha de etapa do contrato; a do aditivo se a etapa só existe nele). Sem nenhuma versão: listas vazias e totais 0.
- **Aditivo nas etapas do contrato:** no `tipo=aditivo`, primeiro a etapa pelo nome (acima); item de etapa que não
  casou vai para a etapa do contrato onde o mesmo `fonte|codigo` aparece (`etapa_origem: "codigo"`); o resto fica na
  etapa da planilha (`"planilha"`, eventualmente com o número trocado). O campo
  `etapas_itens` (JSON `{"ordem": "etapa"}`) aponta a etapa de itens específicos (`"escolhida"`; etapa que não existe
  no orçamento nem na planilha → 422; vale a chave da prévia ou a original da planilha). `etapa_planilha` guarda a etapa original. Título de etapa do aditivo que ficou
  sem item some da versão.
- **Previsto × realizado** (Fase 3): cada etapa e os totais trazem também `cotando`, `comprometido` e `realizado`
  (centavos), somados das linhas dos pedidos da obra pela `etapa` de cada linha. `cotando` = pedidos em cotação ·
  `comprometido` = pedidos de aprovado em diante pelo valor da linha (o maior entre o pedido e o entregue; pedido
  encerrado com sobra compromete só o que chegou) · `realizado` = o que já foi entregue ou medido, pelo preço do pedido.
  Cancelado não conta. `totais.sem_etapa` = o pedido sem etapa ou em etapa que não está nas versões vigentes (já somado
  nos totais). Atenção: `atual` é preço de contrato (com BDI e mão de obra); comprometido e realizado são custo de
  compra.
- **Gastos no realizado** (regra em `canteiro_dominio.gastos.gastos_no_orcamento`): o **gasto avulso** (comprovante
  confirmado **sem** `pedido_id`, não estornado) é dinheiro que saiu e soma, pelo `valor`, no `comprometido` **e** no
  `realizado` da etapa dele, mexendo no `saldo` e no `estourou`; cada etapa traz `gastos_avulsos` (a parte que veio de
  gasto, já somada). **Pagamento de pedido não soma**: o custo do pedido já está no realizado pela entrega; somar o
  pagamento contaria duas vezes. `totais.gastos` = `{total, nas_etapas, fora_do_orcamento, na_lista_padrao,
  sem_etapa_do_orcamento}`, todos já somados no `comprometido`/`realizado`/`saldo` dos totais: `fora_do_orcamento` =
  gastos nas **etapas extras** (não engordam etapa do orçamento); `sem_etapa_do_orcamento` = gastos numa chave que não
  é etapa vigente nem extra (lançados na lista padrão antes da importação, ou etapa de versão desfeita; reclassifique
  com `PATCH /gastos/{id}`); `na_lista_padrao` = obra sem orçamento, gastos na lista padrão.
- **Contas prontas** (a tela e o agente só mostram): cada etapa traz `saldo` (= `atual − comprometido`, centavos;
  negativo quando estourou) e `estourou` (`comprometido > atual`); `totais` traz `saldo` (o mesmo, da obra) e
  `aditivos_pct` = aditivos sobre o contratado em **centésimos de %** (290 = 2,9%; arredondado ao mais próximo) ou
  `null` sem contrato. A base (`contratado`) é o total do **contrato vigente**: o orçamento aprovado inteiro, já com o
  que foi somado antes de aprovar (pranchas + planilha), não só a primeira importação. Sem aditivo de verdade, 0. É
  número neutro (não há limite legal: obra privada).
- `PATCH /obras/{obra_id}/orcamento/itens/{item_id}` (admin) `{quantidade?, valor_unitario?}` → `{item: ItemOrcamento,
  total_versao, registro_id}`. O total da linha vira quantidade × unitário, e a diferença soma no total da versão.
  Título de etapa, quantidade zero, quantidade negativa fora de aditivo, preço negativo ou nada mudou → 422; versão fora
  de vigência → 409; item de outra obra → 404. **Orçamento aprovado:** só o preço de item que estava em R$ 0,00 (o
  `valor_unitario` do insumo acompanha, com o preço médio quando o insumo junta mais de uma linha; insumo cadastrado com
  preço próprio fica com ele); o resto → 409 "lance um aditivo". Desfazer/Refazer `editar_item_orcamento`: 409 se a linha
  mudou de novo, ou se a edição é de antes da aprovação e a obra já foi aprovada.
- `POST /obras/{obra_id}/orcamento/aprovar` → `Obra + {registro_id, insumos_criados, insumos_atualizados}`. Tira a obra de
  `em_orcamento` para `em_andamento` e, **com contrato vigente, gera Compras**: cada item com quantidade > 0 das versões
  vigentes vira insumo (`precisa_ate_o_fim` = quantidade, `valor_unitario`, `etapa`). O que veio das pranchas é
  `material`; o que veio de planilha, `servico`. Mesmo nome em mais de uma linha = um insumo só (quantidades somadas,
  preço médio ponderado, etapa de maior valor). Insumo já cadastrado não duplica: fica o maior `precisa_ate_o_fim`, e o
  preço só entra se o dele for 0. Mesmo nome com unidades diferentes (no orçamento, ou contra o cadastrado) → 422
  listando os itens, sem aprovar. Grava em lotes e só a última transação vira o status e o registro (repetir depois de
  uma queda é seguro). Desfazer: arquiva os insumos gerados (reaprovar reaproveita os IDs) e devolve os atualizados; 409
  se algum gerado teve pedido não cancelado ou movimento não estornado, se foi editado depois, ou se há aditivo lançado
  após a aprovação (desfaça o aditivo primeiro). **Em lotes e retomável:** se parar no meio (queda ou 409), a obra fica
  com `status: "desfazendo_aprovacao"` e `desfazendo_registro_id` (o registro a desfazer de novo); o mesmo
  `POST /registro/{id}/desfazer` continua de onde parou e só no fim a obra volta a `em_orcamento`. Nesse estado Compras
  está pela metade, e criar pedido, receber entrega, lançar movimento, cadastrar ou importar insumo, editar linha,
  importar versão e aprovar → 409; cancelar pedido e estornar movimento continuam valendo, para resolver o que travou.
- **Com a obra aprovada:** aditivo mexe em Compras na hora (item novo vira insumo, item existente soma, supressão reduz
  até 0; até 200 itens por aditivo; unidade diferente → 422), e o Desfazer do aditivo volta isso. Trocar o contrato →
  409 "desfaça a aprovação ou lance um aditivo"; importar contrato sem nenhum vigente e retirar o contrato também dão
  409 quando a obra passou pelo Aprovar (`Obra.aprovada`). Obra antiga em andamento, que nunca foi aprovada, segue
  podendo. Uma aprovação que parou antes de virar o status não aparece em Compras nem conta em `tem_orcamento`.
- Sem contrato (lista de materiais), a aprovação não gera nada: só muda o status.
  Já aprovado → 409; sem contrato vigente **e** sem insumo com `precisa_ate_o_fim > 0` → 422. Registro "Aprovou o
  orçamento de X (N itens, R$ total)" com o orçado atual (contrato + aditivos vigentes; sem contrato, "N insumos" da
  lista de materiais) com `desfazer` `reabrir_orcamento` (volta para
  `em_orcamento`; 409 se a obra já não está em andamento). A tela confirma antes, mostrando total e nº de itens.

## Hoje na obra

- `GET /obras/{obra_id}/hoje?hoje=AAAA-MM-DD` (qualquer papel; padrão: hoje em São Paulo; sem chamada ao modelo) →
  `{hoje, resumo: {situacao, frase, esperando, semana_atual, semanas_total}, pontos: [Ponto], mais, semana: [Evento]}`.
  `situacao` = `no_prazo` | `atrasada` | `sem_medicao` (a mesma regra do placar); `frase` só diz a situação; `esperando` =
  propostas pendentes + pontos `pede_voce` (mesmo os cortados); `semana_atual` é `null` sem cronograma ou antes do início, e passa de
  `semanas_total` se a obra atrasar; `mais` = pontos fora do limite.
- `Ponto` = `{tipo, prioridade, titulo, detalhe, acao: {tela, rotulo, filtro?}}`, no máximo 6, um por tipo, em ordem
  `pede_voce`, `atencao`, `a_caminho`, `ok`. `pede_voce`: `desfazendo_aprovacao`, `sem_orcamento`, `gastos_a_conferir`.
  Proposta pendente não vira ponto: a tela mostra um cartão para cada uma, e ela conta em `esperando`. `atencao`: `entregas_atrasadas`, `falta_comprar` (`filtro.etapa`), `etapas_estouradas`,
  `etapas_atrasadas`, `itens_sem_preco`, `orcamento_so_pranchas` (só em orçamento), `gastos_sem_etapa`. `a_caminho`:
  `entregas_semana`. `falta_comprar` (todo insumo com `conta.comprar > 0`, material ou serviço, como a tela de Compras): etapas em andamento e as que começam em até 4 semanas (o cronograma é mensal: conta
  do dia 1 do 1º mês); sem cronograma, a 1ª etapa do orçamento com falta.
- `Evento` = `{data, tipo: "entrega" | "inicio_etapa", titulo, detalhe, acao}`, de hoje até hoje + 7, por data.

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
  recebido_em ("AAAA-MM-DD" da última entrega, null se nada chegou), criado_em,
  linhas: [{insumo_id, nome, unidade, tipo, etapa, quantidade, valor_unitario, total, recebida,
  falta_entregar}]}`. Nome, unidade e tipo são cópia do insumo no momento do pedido. `valor` = soma dos `total`;
  `recebida` soma as entregas; `recebimentos` = quantas entregas valem. `falta_entregar` (milésimos, calculado pela
  API com `regras.falta_na_linha`) = `quantidade − recebida`, nunca negativo, e 0 em pedido `conferido` (encerrado)
  ou `cancelado`; vem no `GET …/pedidos` e nas respostas de criar, avançar e editar. Pedido gravado antes das linhas
  sai no mesmo formato, com uma linha.
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
- `POST /obras/{obra_id}/pedidos` aceita `chave_idempotencia?` (8 a 64): a mesma chave devolve o pedido já criado (200,
  mesmo `id` e `registro_id`, no estado de agora); com outro conteúdo → 409.
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
- **De `parcial`, `para = conferido` só encerra:** `quantidade_recebida` ou `nf` junto → 422 ("De parcial, conferir só
  encerra o resto…"); antes eram descartados em silêncio. O que chegou entra por `POST …/recebimentos` (ou proposta
  `registrar_entrega`).
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
  - `etapas`: `[{etapa, descricao, atual, percentuais, medido_acumulado, valores_mes, medido_mes,
    previsto_ate_referencia, situacao, atraso_pct}]`, todas as do orçamento vigente. Contas prontas (aditivas):
    `valores_mes` = R$ previsto de cada mês (centavos, o mesmo arredondamento do `mensal`); `medido_mes` = % medido em
    cada mês (centésimos, alinhado com `mensal`); `previsto_ate_referencia` = % previsto acumulado até o mês da última
    medição (0 sem medição); `situacao` (`regras.situacao_etapa`) ∈ `concluida | atrasada | adiantada | em_dia |
    nao_comecou` nesse mês (`null` sem cronograma) — a mesma regra que o Gantt da web calcula hoje no navegador, com
    "adiantada" separado de "em dia"; `atraso_pct` = centésimos que o medido está abaixo do previsto (0 se não atrasada).
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
- `GET /obras/{obra_id}/desembolso?hoje=AAAA-MM-DD` (qualquer papel; `hoje` padrão = hoje em São Paulo) → projeção de
  desembolso: o que já saiu por mês e o que ainda vai sair até o fim. Só leitura; a conta e o porquê de cada regra em
  `servicos/desembolso.py`.
  - `{mes_atual, base, mes_referencia, atraso_meses, ritmo_pct, termino_projetado, termino_no_ritmo, meses, etapas,
    totais}`. `base` ∈ `medicao` (avanço = última medição) | `gastos` (sem medição: realizado ÷ orçado faz o papel do
    avanço) | `sem_cronograma` (tudo em `totais.sem_mes`).
  - `meses`: `[{mes, pago, previsto, projetado, projetado_no_ritmo}]`, do primeiro mês com pagamento ou do início do
    cronograma até o último mês projetado. `pago` = gastos confirmados (avulsos e pagamentos de pedido) pelo mês do
    pagamento; `previsto` = o `mensal[].previsto` do cronograma vigente; `projetado` = o que falta, **pelo cronograma**
    (o resto do plano empurrado por `atraso_meses`, nada antes do mês corrente); `projetado_no_ritmo` = o mesmo
    esticado pelo ritmo medido (`ritmo_pct` = % medido ÷ % previsto na última medição, entre 1000 e 10000; 10000 sem
    medição, e aí os dois cenários são iguais). Os dois somam `totais.falta − totais.sem_mes`.
  - `etapas`: `[{etapa, descricao, atual, realizado, executado_pct, falta, devido_agora, sem_cronograma}]`. `falta` =
    orçado atual − realizado (nunca negativo: não projeta economia nem estouro por tendência); `devido_agora` =
    executado pela medição e ainda não realizado, no mês corrente.
  - `totais`: `{orcado, realizado, pago, falta, custo_final, desvio, entregue_sem_pagamento, sem_mes}`; `custo_final`
    = realizado + Σ falta das etapas, que é `pago + falta` salvo quando o pago adiantado passa de tudo o que falta (o
    crédito que sobra não aparece na série); `desvio` = custo_final − orçado (o estouro que já houve, e gasto
    fora do orçamento); `entregue_sem_pagamento` = realizado − pago (pedido entregue sem pagamento registrado; está no
    mês corrente). Pago adiantado desconta dos primeiros meses, sem mês negativo.

## Resumo de uma obra ("como está a obra?")

- `GET /obras/{obra_id}/resumo?hoje=AAAA-MM-DD` (qualquer papel; `hoje` padrão = hoje em São Paulo) → `ResumoDaObra` =
  `{obra: Obra, hoje, cronograma, financeiro, entregas, almoxarifado, pendencias}`, tudo somado na API a partir do que
  as outras rotas já calculam. Cada bloco diz quando o dado não existe.
  - `cronograma`: `{cadastrado, mes_corrente, termino_contratual, termino_projetado, termino_original, atraso_decimos,
    atraso_base_decimos, revisoes, mes_ultima_medicao, medicoes, pct_previsto_na_medicao, pct_medido, previsto_no_mes,
    pct_previsto_acumulado_no_mes, etapas_do_mes, proximo_mes, previsto_no_proximo_mes,
    pct_previsto_acumulado_no_proximo_mes, etapas_do_proximo_mes, proximo_mes_com_inicio, proximas_etapas,
    etapas_atrasadas}`. `etapas_do_mes` = etapas com % previsto no mês corrente (`{etapa, descricao, pct_no_mes,
    valor_no_mes, previsto_ate_referencia, medido_acumulado, situacao, comeca}`), maior valor primeiro;
    `etapas_do_proximo_mes` = **todas** as etapas com % previsto no mês seguinte (as que continuam e as que começam),
    maior valor primeiro; `comeca` = a etapa começa naquele mês (sem medição e sem % previsto antes dele);
    `proximas_etapas` = as que **começam** no primeiro mês depois do corrente em que alguma começa (etapa já medida,
    ou com situação diferente de `nao_comecou`, nunca "começa"); `etapas_atrasadas` = `{etapa, descricao, medido_acumulado,
    previsto_ate_referencia, atraso_pct}`, maior atraso primeiro. `cadastrado: false` = sem cronograma (só
    `termino_contratual` pela obra).
  - `financeiro`: `{tem_orcamento, contratado, aditivos, aditivos_pct, orcado_atual, cotando, comprometido, realizado,
    saldo, falta_gastar, realizado_pct, etapas_estouradas: [{etapa, descricao, saldo}], gastos}` (os totais de `GET …/orcamento`;
    `realizado` e `comprometido` já com os gastos avulsos, e `gastos` = o mesmo `totais.gastos`). Para o placar da obra:
    `falta_gastar` = orçado atual − realizado (nunca negativo; realizado + falta fecham no orçado) e `realizado_pct` =
    realizado ÷ orçado em centésimos de ponto (`null` sem orçado). `pago` = todo gasto confirmado e não estornado
    (avulso e pagamento de pedido) e `entregue_a_pagar` = `max(0, realizado − pago)`, a mesma conta de `totais.pago` e
    `totais.entregue_sem_pagamento` do desembolso: **realizado ("gasto até hoje") = pago + entregue_a_pagar**, salvo
    quando se pagou além do entregue (adiantamento), em que `entregue_a_pagar` = 0 e `pago` > `realizado`.
  - `entregas`: `{hoje, atrasadas, proximos_7_dias, sem_previsao, nf_para_conferir}`; cada entrega =
    `{pedido_id, numero, fornecedor, status, previsao, itens: [{nome, unidade, falta_entregar}]}`, só pedidos de
    aprovado a parcial, pela `previsao` "AAAA-MM-DD" (outro formato conta em `sem_previsao`).
  - `almoxarifado`: `{insumos, falta_comprar: [{insumo_id, nome, unidade, falta, texto}], sem_previsao,
    consumo_projetado: false}` (materiais; o consumo por semana ainda não é projetado).
  - `pendencias`: `{propostas, titulos}` (até 5 títulos, as mais novas primeiro).

## Gastos da obra: comprovantes, leitura por IA e revisão

Gasto é **dinheiro que saiu**, numa data, numa etapa. A pessoa envia o comprovante (Pix, NF paga, boleto
pago, recibo, papel à mão), o serviço do agente lê com IA e preenche o máximo, e uma pessoa (admin) confere e confirma.
Só a confirmação cria o `Gasto`; até lá o comprovante fica "a revisar" e não conta em nada. Regras em
`dominio/src/canteiro_dominio/{etapas,leitura,gastos}.py`.

**Uma árvore só, a do orçamento quando ele existe.** Obra **com orçamento** (alguma versão vigente): as etapas de
gasto são as **etapas do orçamento** (as mesmas de `GET …/etapas`, na ordem dele; chave = o código da etapa, como
`"18"`, a mesma chave de `GET …/orcamento`, do cronograma e da apropriação de pedidos) mais as **etapas extras** da
obra. Obra **sem orçamento**: a **lista padrão** de 24 etapas (chave estável como `fundacao`, `eletrica`, `pessoal`)
mais as extras antes de "Outros". O gasto avulso soma no `realizado` da etapa do orçamento (ver "Gastos no realizado"
no orçamento); o das extras fica "fora do orçamento". Gasto lançado na lista padrão antes de a obra importar o
orçamento **continua contando** (nos totais) e aparece como **"sem etapa do orçamento"** até alguém reclassificar.

`origem` (em `/arvore`, no resumo e nos lançados) diz de onde vem a etapa: `orcamento` · `padrao` (lista padrão, só em
obra sem orçamento) · `extra` · `sem_etapa_do_orcamento` (chave que não está mais na árvore: lista padrão numa obra
que agora tem orçamento, ou etapa de versão desfeita; só no resumo e nos lançados) · `a_definir` (confirmado sem etapa,
`etapa: null` e `etapa_nome: "Sem etapa"`; só no resumo e nos lançados).

**Árvore de gasto:**

- `GET /obras/{obra_id}/arvore` (qualquer papel) → `[{chave, nome, inclui, fixa, origem, topicos: [{id, nome}]}]`.
  Com orçamento: as etapas dele (`origem: "orcamento"`, `fixa: true`, `inclui: ""`, `nome` = "18 INSTALAÇÃO ELÉTRICA
  - 220V", com o código na frente; etapa sem número da planilha fica só com a descrição) e depois as extras
  (`origem: "extra"`, `fixa: false`, chave `x-<id>`). Sem orçamento: as 24 da lista padrão (`origem: "padrao"`,
  `fixa: true`), as extras antes de "Outros". Os **tópicos** de cada etapa ficam presos à chave dela (no orçamento, o
  código). Arquivados não aparecem; tópico de uma etapa que saiu da árvore também não.
- `POST /obras/{obra_id}/etapas-extras` `{nome}` e `POST /obras/{obra_id}/topicos` `{etapa, nome}` (admin) → 201 +
  `registro_id`. Nome até 60, sem repetir (sem diferenciar acento e maiúscula) entre as etapas da obra (com orçamento:
  a descrição de cada etapa dele, com ou sem o código; sem: a lista padrão) / os tópicos da etapa → 422. `etapa` do
  tópico tem de estar na árvore de hoje → 422. Até 20 etapas extras por obra e 30 tópicos por etapa. Desfazer a
  criação arquiva; refazer reativa. Limite e nome único valem também com pedidos simultâneos e no Desfazer/Refazer
  (reativar ou voltar o nome antigo que outro pegou nesse meio-tempo → 422 "Já existe: …").
- `PATCH /etapas-extras/{id}` e `PATCH /topicos/{id}` `{nome?, arquivado?}` (admin) → renomeia e/ou arquiva/reativa,
  com Desfazer e Refazer. Nada mudou → 422. Arquivado sai da árvore e não recebe gasto novo; gasto antigo fica.

**Comprovantes:**

- `POST /obras/{obra_id}/comprovantes` (multipart `arquivo` + `legenda?`, **qualquer papel**) → `Comprovante` (201;
  200 com `ja_existia: true` se o mesmo arquivo já foi enviado nesta obra: o ID sai do sha256). O tipo sai dos **bytes**
  (JPEG `FF D8 FF`, PNG `89 50 4E 47…`, WEBP `RIFF….WEBP`, PDF `%PDF-`), não do `Content-Type` declarado, que é
  ignorado (o navegador chuta pela extensão); `tipo_conteudo` devolve o tipo real. Formato fora da lista → 422. Limites:
  JPG, PNG ou WEBP até **3,5 MB** (3.500.000 bytes; a imagem vai ao modelo em base64) e lado até 8000 px (mais → 422);
  PDF até **4 MB** (4 × 1024 × 1024 bytes; a função da Vercel que repassa o envio recusa corpo acima de 4,5 MB).
  Arquivo maior → **413** com a mensagem do limite; corpo inteiro acima de 5 MiB → 413 antes do login. A tela reduz a
  foto para ~1 MB e converte HEIC para JPEG. `legenda` = "do que é?" de quem enviou, **até 300 caracteres** (mais →
  422).
  Nasce `a_revisar` e grava o evento `comprovante.recebido`.
- `Comprovante` = `{id, obra_id, arquivo, tipo_conteudo, tamanho, sha256, legenda, quem, usuario_id, status:
  a_revisar | confirmado | descartado, leitura, erro_leitura, lido_em, modelo_leitura, gasto_id, motivo_descarte,
  criado_em, atualizado_em}`.
- `GET /obras/{obra_id}/comprovantes?status=a_revisar&limite=100&antes_de=…` → a fila (mais novos primeiro).
- `GET /comprovantes/{id}/arquivo` → o arquivo (`Cache-Control: private, no-store`). Só do tenant; o armazenamento é
  privado e separa por tenant de novo: no próprio banco (`ARQUIVOS=banco`, padrão e o único aceito em produção), em
  pedaços de 512 KiB por documento e um índice gravado por último na mesma transação, que confere o sha256 ao ler
  (pedaço faltando ou trocado → 404, nunca outra foto); `disco` só em desenvolvimento (o disco do Render some a cada
  deploy). `arquivo` = `<obra_id>/<id>.<ext>` (o tenant vem à parte). Estornar ou desfazer um gasto e descartar um
  comprovante **não apagam o arquivo** (é a prova do pagamento). Política de retenção (LGPD) ainda a decidir: hoje
  nada se apaga.
- `PUT /interno/comprovantes/{id}/leitura` (**só o serviço do agente**: token de serviço + `X-Canteiro-Usuario`, como o
  `POST /uso`) `{leitura?, erro?, modelo?}` (um dos dois) → `Comprovante`. `leitura` = `LeituraComprovante`
  (`dominio/.../leitura.py`, o esquema da saída estruturada do modelo): `{tipo_documento, situacao_pagamento: pago |
  a_pagar | incerto, valor, data_pagamento, favorecido, descricao: {certeza: alta | baixa, opcoes: [{valor, motivo}]},
  cnpj_favorecido, classificacao: {certeza, opcoes: [{etapa, topico_id, motivo}]}, observacoes}`. `erro` = frase para
  a pessoa ("A leitura automática falhou agora…"); não apaga uma leitura anterior. Comprovante já revisado → 409.
- `GET /comprovantes/{id}` → `Comprovante + revisao`, o formulário pronto (recalculado a cada abertura, porque a árvore
  muda: com orçamento, as opções são etapas dele e extras; uma sugestão da lista padrão é descartada): `valor`, `data_pagamento`, `favorecido`, `descricao` = `{estado: lido | conferir | vazio, opcoes: [{valor,
  rotulo, motivo, fornecedor_id}], escolhido}`; `classificacao` = `{estado, opcoes: [{etapa, topico_id, rotulo,
  motivo}], escolhido}`; mais `tipo_documento`, `situacao_pagamento`, `avisos: [str]`, `observacoes`.
  - **lido**: certeza alta e uma opção válida, já marcada (`escolhido: 0`). **conferir**: dúvida; até 3 opções com o
    motivo e **nenhuma marcada** (`escolhido: null`). **vazio**: não leu. A tela sempre oferece a resposta própria da
    pessoa no mesmo destaque das opções.
  - Opção ilegível, etapa fora da árvore e tópico inventado (a etapa fica) são descartados. Data no futuro ou com mais
    de um ano → conferir + aviso. CNPJ lido que bate com fornecedor cadastrado → opção marcada com `fornecedor_id`.
    `a_pagar`/`incerto` → aviso de que o documento não comprova pagamento. Leitura que falhou → o erro vem em `avisos`.
- `POST /comprovantes/{id}/confirmar` (admin) `{valor, data_pagamento, favorecido, descricao, etapa, topico_id?,
  fornecedor_id?, pedido_id?}` → `{gasto, comprovante, registro_id}` (201). `etapa: null` (obrigatório mandar o campo) é
  o "ver depois": sem tópico, conta no realizado e no comprometido da obra e em nenhuma etapa (`a_definir` em
  `totais.gastos` e em `GET …/gastos/resumo`, com uma linha `etapa: null`); escolhe-se a etapa depois com
  `PATCH /gastos/{id}`. Valor > 0 em centavos; data `AAAA-MM-DD`
  até hoje (fuso de São Paulo); favorecido e descrição obrigatórios; etapa da árvore de hoje (com orçamento, um código
  dele ou uma extra) e tópico daquela etapa → senão
  422 e o comprovante continua na fila. Já revisado → 409. Registro "Gasto de R$ 350,00 em Instalações elétricas ›
  Quadro: mão de obra (João da Silva)", desfazer `estornar_gasto`: o gasto sai das contas (`estornado`) e o
  comprovante volta para a fila.
  - **Com `pedido_id`** (pedido da obra, de aprovado em diante): o gasto é o pagamento do pedido. Soma em
    `Pedido.pago` e **não** no total da etapa nem no realizado do orçamento, porque o custo do pedido já conta pela
    entrega. `pago` nunca passa do
    `valor` do pedido: confirmar um gasto que passaria → **409** (o comprovante continua na fila; a pessoa corrige o
    valor, liga a outro pedido ou confirma sem pedido). O estorno tira de `pago` só o valor daquele gasto.
- `POST /comprovantes/{id}/descartar` `{motivo?}` (admin) → `Comprovante + registro_id`. O arquivo fica guardado.
  Desfazer `reabrir_comprovante` volta para a fila.

**Gastos:**

- `Gasto` = `{id, obra_id, comprovante_id, valor, data_pagamento, favorecido, fornecedor_id, descricao, etapa,
  topico_id, pedido_id, quem, usuario_id, estornado, criado_em}`.
- **Gasto fora do orçado** (derivado, não se edita): `fora_do_orcado: true` no gasto **avulso** (sem `pedido_id`,
  não estornado) de obra **com orçamento**; pagamento de pedido e obra sem orçamento → `false`. Vem em
  `GET …/gastos`, na resposta do confirmar (`gasto`) e do `PATCH /gastos/{id}`. **Só marca**: o gasto continua
  somando no realizado da etapa dele, como antes. Critério escolhido por ser o confiável: casar a descrição do
  comprovante ("vidros 4mm", "diária do pedreiro") com o serviço do orçamento erra para os dois lados; é o pedido que
  liga o dinheiro a um item. A tela diz "lançado fora de pedido". (Não confundir com o `fora_do_orcamento` já
  existente, que é só a parte nas **etapas extras**.)
- `GET /obras/{obra_id}/gastos?etapa=…&limite=100&antes_de=…` → confirmados e não estornados (mais novos primeiro),
  cada um com `fora_do_orcado`, `etapa_nome` (o nome de hoje; chave antiga da lista padrão mantém o nome dela, ex. "Fundação") e
  `etapa_origem` (`orcamento` | `padrao` | `extra` | `sem_etapa_do_orcamento`): a tela marca "sem etapa do orçamento"
  e oferece mudar a etapa.
- `GET /obras/{obra_id}/gastos/resumo` → `{total, pagamentos_de_pedidos, a_revisar, tem_orcamento, fora_do_orcamento,
  fora_do_orcado, sem_etapa_do_orcamento, a_definir, entregue_a_pagar, etapas: [{etapa, nome, origem, total,
  fora_do_orcado, entregue_a_pagar, topicos: [{topico_id, nome, total}]}]}`. `entregue_a_pagar` = pedidos entregues e ainda não pagos: o mesmo número de
  `financeiro.entregue_a_pagar` em `GET …/resumo` e de `totais.entregue_sem_pagamento` do desembolso. Por etapa: para
  cada pedido, o recebido menos o que já se pagou dele, repartido pelas etapas das linhas na proporção do recebido
  (linha sem etapa → `etapa: null`); o total da obra (que já desconta o pagamento adiantado de outro pedido) é então
  dividido na proporção desse bruto, com o centavo que sobra no maior resto: **a soma das etapas fecha sempre no
  total**. Etapa que só tem entregue a pagar aparece com `total` 0 e `topicos` vazio. `total` =
  gastos avulsos (é o que soma no realizado do orçamento: igual a `totais.gastos.total` de `GET …/orcamento`);
  `fora_do_orcamento` e `sem_etapa_do_orcamento` = as partes do `total` nas extras e em chave fora da árvore (com
  orçamento; sem orçamento é 0). `fora_do_orcado` (total e por etapa) = a parte do `total` lançada **fora de pedido**
  numa obra com orçamento (ver "Gasto fora do orçado"); sem orçamento é 0. `topico_id: null` = "Sem tópico". Etapas na ordem da árvore; inclui etapa e tópico já
  arquivados; as `sem_etapa_do_orcamento` vêm por último.
- `PATCH /gastos/{gasto_id}` `{etapa, topico_id?}` (admin) → `Gasto + {etapa_nome, etapa_origem, registro_id}`. Muda a
  etapa (e o tópico; sem `topico_id` = sem tópico) de um gasto confirmado: é como o "sem etapa do orçamento" vai para
  uma etapa do orçamento e passa a somar no realizado dela. Destino tem de estar na árvore de hoje e o tópico naquela
  etapa → 422; mesma etapa e tópico → 422 "Nada mudou."; gasto estornado → 409; não existe → 404; viewer → 403.
  Vale também para pagamento de pedido (que continua sem somar). Registro "Mudou o gasto de R$ 120,00 (descrição) de
  Fundação para 2 FUNDAÇÕES", desfazer `reclassificar_gasto`: volta para a etapa de antes (mesmo que ela não esteja
  mais na árvore), com Refazer; 409 se a etapa do gasto mudou de novo depois ou se ele foi estornado.

## Tributos da obra (estimativa: INSS da aferição e IBS/CBS)

A plataforma refaz, durante a obra, a conta que a Receita faz no fim: o **INSS da aferição** (CNO/SERO, IN RFB
2.021/2021) e o **IBS/CBS** (LC 214/2025, regime de bens imóveis). É estimativa: todos os percentuais vêm em
`parametros`, com a fonte, para o contador conferir; a conta é do domínio (`canteiro_dominio.tributos`).

- **Tabela VAU (SERO).** O valor do m² que a aferição usa sai por UF, mês e destinação (não pelo padrão), na tela
  "Consultar Tabela VAU" do SERO. A API guarda a tabela no tenant de sistema (um documento por UF:mês:destinação),
  importada por `python -m canteiro_api.vau ARQUIVO` (o JSON de `src/canteiro_api/dados/` ou o texto da tela do SERO
  copiado como está; coluna ou valor estranho recusa o arquivo inteiro); o seed importa o JSON do repositório.
  `scripts/coletar-vau-sero.sh` coleta as 27 UFs no portal em que a pessoa entrou no e-CAC (ver
  `docs/tributos-integracao.md`).
  `GET /vau?uf=SP&destinacao=galpao` (qualquer papel) → `[{uf, mes: "AAAA-MM", destinacao, vau, fonte}]`, mais novo
  primeiro.
- **Selic (art. 31).** A remuneração comprovada é corrigida pela Selic acumulada no mês (Banco Central, SGS 4390),
  guardada no tenant de sistema, um documento por ano (`indices/selic:AAAA`, `{meses: {"AAAA-MM": centésimos de
  ponto}}`, só meses fechados). `python -m canteiro_api.selic` baixa do BCB; com `ARQUIVO`, lê o JSON de `dados/`; o
  seed importa o JSON do repositório.
- `GET /obras/{obra_id}/tributos` (qualquer papel) → `{obra_id, cno, vau_referencia, vau_usado, vau_fonte, destinacao,
  categoria, afericao: "AAAA-MM", selic_ate, inss, ibs_cbs, a_classificar, valor_a_classificar, sem_nota, pendentes,
  parametros: {inss, ibs_cbs}, aviso}`. A conta supõe a obra aferida no mês corrente (`afericao`).
  - `inss` = `{situacao: calculado|isenta|sem_dados, motivos, area_equivalente, custo_obra, rmt, fator_social,
    percentual_rmt, percentual_categoria, pre_moldado: reduziu|virou_mista|null, nao_decadente, comprovado,
    comprovado_por_natureza: {natureza: centavos}, credito_concreto, credito_autonomo, base, inss_sem_fator_ajuste,
    inss, fator_ajuste: nao_se_aplica|aplicado|atingido|nao_atingido, fator_ajuste_minimo, creditos_fator_ajuste,
    falta_fator_ajuste, economia_com_notas, mao_de_obra_sem_nf, notas_sem_cno, selic_faltando, avisos, linhas}`.
    A conta segue o Manual do SERO (IN RFB 2.021/2021):
    - área para cálculo (`area_equivalente`) = principal × equivalência da destinação (unifamiliar 89% até 1.000 m²,
      85% acima; multifamiliar 90/86%; comercial e edifício garagem 86/83% (3.000 m²); galpão 95%; casa popular e
      conjunto 98%) + `area_complementar` (coberta) × 50% + `area_descoberta` × 25%; `custo_obra` = VAU × essa área.
    - `rmt` = custo × tipo (alvenaria 20%, mista/madeira 15%; casa popular e conjunto 12%/7%) × categoria (reforma 35%,
      demolição 10%) × edifício garagem 80% × fator social (toda obra de `pf`, pela área total: 20/40/55/70/90%) ×
      pré-moldado (notas ≥ 40% do custo → 30%; abaixo disso a alvenaria conta como mista) × `nao_decadente` (meses
      desde `inicio` fora da decadência: aferida em 2026, decaíram os anos até 2020).
    - `comprovado` = remunerações comprovadas, corrigidas pela Selic (do 2º mês depois do pagamento ao mês anterior +
      1%), sem as decadentes: folha 100%, nota de mão de obra **com `nf_cno`** 40%, GPS ÷ 36,8% — + `credito_concreto`
      (usou concreto usinado: custo × Anexo I da UF/destinação × 5%, × 35% na reforma, 0 na demolição).
      Autônomo/MEI declarado não abate da RMT: `credito_autonomo` = 20% dele sai do débito (a parte patronal).
    - base = RMT − comprovado (≥ 0); `inss_sem_fator_ajuste` = base × 36,8% − `credito_autonomo`.
    - fator de ajuste (só `pf`): remunerações (com autônomo/MEI) ≥ 50% da RMT (área total até 350 m²) ou 70% (acima).
      Atingido e `dctfweb_em_dia: true` → `aplicado`, `inss: 0`; atingido sem a DCTFWeb confirmada → `atingido` (o
      `inss` segue cheio, com aviso); `falta_fator_ajuste` = remuneração declarada que ainda falta.
    - `isenta` = `pf`, unifamiliar ou casa popular, padrão baixo, `finalidade: uso_proprio`, até 70 m² de área total
      e nenhuma mão de obra paga (folha, nota, sem nota, GPS, autônomo). `sem_dados` sem área ou sem VAU.
    - `economia_com_notas` = quanto o INSS cairia se a mão de obra sem nota (`mao_de_obra_sem_nf`) e a com nota sem o
      CNO (`notas_sem_cno`) fossem comprovadas. `avisos`: nota sem o CNO, empresa (`pj`) com nota de mão de obra sem
      `retencao`, pré-moldado abaixo de 40%, fator de ajuste atingido sem DCTFWeb, mês sem Selic (`selic_faltando`).
    Sem `destinacao`, ela sai da `tipologia` (casa/sobrado → unifamiliar, predio → multifamiliar…).
    O VAU é o `vau` à mão da obra (`vau_fonte: informado`, `vau_referencia` = o mês informado) ou, sem ele, o vigente
    da tabela para a `uf` e a destinação, o do mês mais recente até hoje (`vau_fonte: tabela_sero`); sem UF ou sem
    linha na tabela, `vau_usado: 0`, `vau_fonte: null` e `inss.situacao: sem_dados`.
  - `ibs_cbs` = `{situacao: calculado|nao_incide|sem_dados, motivos, aliquota_efetiva, valor_operacao,
    redutor_ajuste, redutor_social, base, debito, creditos, a_pagar, transicao, linhas}`. Regime pleno: alíquota de
    referência 26,5% × (100% − 50% de redução de imóveis). `venda`: base = `verba` − `valor_terreno` − R$ 100.000 por
    unidade residencial; `prestacao_servico`: base = `verba`; a pagar = débito − `creditos` (soma do `ibs_cbs` dos
    gastos), nunca negativo. `uso_proprio` → `nao_incide`. `transicao` = a frase do ano corrente (2026 teste…2033).
  - `linhas`: a conta pronta, linha a linha, para a tela e o agente citarem.
  - `a_classificar`/`valor_a_classificar`: gastos avulsos sem natureza fiscal (contam zero no INSS); `pendentes` e
    `sem_nota`: até 20 de cada, maiores primeiro, `{id, valor, data_pagamento, favorecido, descricao,
    natureza_fiscal, nf_cno}`; `sem_nota` traz a mão de obra sem nota e a com nota sem o CNO. Gasto de pedido conta
    como `material` sem precisar classificar.
- `PATCH /gastos/{gasto_id}/fiscal` `{natureza_fiscal, ibs_cbs?=0, nf_cno?=null, retencao?=0}` (admin) → `Gasto +
  {etapa_nome, etapa_origem, registro_id}`. `natureza_fiscal` ∈ `folha | mao_de_obra_nf | gps | autonomo_mei |
  pre_moldado | concreto_usinado | mao_de_obra_sem_nf | material | equipamento | outros` ou `null` (a classificar).
  `nf_cno` (a nota traz o CNO e o endereço da obra) e `retencao` (os 11% destacados, centavos) só ficam na
  `mao_de_obra_nf`; nas outras naturezas voltam a `null`/0. `ibs_cbs` e `retencao` (centavos, ≤ valor do gasto, senão
  422): o IBS/CBS destacado vira crédito. Nada mudou → 422; estornado → 409; viewer → 403. Registro "Classificou o
  gasto de R$ 10.000,00 (descrição) como mão de obra com nota (com o CNO da obra, retenção R$ 1.100,00)", desfazer
  `fiscal_gasto` com Refazer (409 se a classificação mudou de novo depois).
- `Gasto` ganha `natureza_fiscal` (`null` = a classificar), `ibs_cbs` (centavos, 0), `nf_cno` (`null`) e `retencao`
  (centavos, 0).

## Propostas, aprovação e registro

- `POST /propostas` `{obra_id, tipo, titulo, conta, justificativa, fonte, dados, chave_idempotencia?}` → `Proposta +
  {ja_existia}` (201, pendente). Só valida e guarda: **não executa**. Qualquer papel propõe (o agente usa o token de
  quem conversa); `dados` inválido → 422.
  - **Sem duplicata** (pelo ID do documento, portável para o Firestore): `chave_idempotencia` (≤ 200; o agente manda
    `"{sessao_id}:{tool_use_id}"`) — a mesma chave devolve a proposta já criada, no status em que estiver, com 200 e
    `ja_existia: true`; a mesma chave com outra obra/tipo/dados → 409. Sem chave ou com chave nova: se uma proposta
    **pendente** da mesma obra tem o mesmo `tipo` e os mesmos `dados` (depois de validados; título e justificativa
    não contam), volta ela, com 200 e `ja_existia: true`, e nada é gravado. Aprovar ou recusar libera: depois disso a
    mesma proposta pode ser criada de novo. Por trás, um documento `propostas_pendentes/{id derivado de tenant, obra,
    tipo e dados}` aponta para a pendente, e aprovar/recusar o solta na mesma transação.
  `dados` por tipo: `criar_pedido` = `{insumo_id, quantidade, fornecedor, valor, etapa?, previsao?}` (o corpo do POST
  de pedido **sem** `aprovado`; `etapa` é uma chave de `GET …/etapas`, e o agente a exige quando a obra tem orçamento) · `avancar_pedido` = `{pedido_id, para, quantidade_recebida?, nf?}` (as mesmas regras do
  `POST …/avancar`; de `parcial`, sem quantidade/NF) · `ajuste_estoque` = `{insumo_id, quantidade, motivo}` ·
  `registrar_entrega` = `{pedido_id, linhas: [{indice, quantidade}], nf_numero?, nf_data? ("AAAA-MM-DD"), nf_valor?
  (centavos), confirmar_excesso?=false}` (o corpo de `POST /pedidos/{id}/recebimentos` sem a chave).
  - **`registrar_entrega`** (domínio ≥ 0.3.0, `regras.validar_entrega`): o pedido tem de estar de aprovado a parcial,
    as linhas existir e não repetir; quantidade acima do que falta na linha → 422 "A entrega passa do que falta…" a
    menos que `confirmar_excesso: true` (o "chegou a mais" da tela). A API **preenche a `conta`** com a prévia do
    domínio (`regras.texto_da_entrega`): `"PED-0170 · Brita 1: recebido 18 m³ + esta entrega 7 m³ = 25 de 25 m³ ·
    pedido completo"`. Aprovar revalida contra o pedido de agora (outra entrega no meio → 422, proposta continua
    pendente) e roda o **mesmo** `preparar_receber` do `POST …/recebimentos` (entrada no almoxarifado, NF, status
    parcial/conferido; o ID da entrega sai da proposta); o registro tem `desfazer` `desfazer_recebimento` (só a última
    entrega, como na tela).
  - **`efeito`** (novo campo da `Proposta`, decidido pela API ao criar): `compra` (criar_pedido) · `entrada`
    (registrar_entrega, ou avancar_pedido `conferido` de um pedido que não está parcial) · `encerramento`
    (avancar_pedido `conferido` de parcial) · `cancelamento` · `status` (outros avanços) · `ajuste`. É o que o botão da
    tela diz ("Aprovar entrada" × "Aprovar encerramento"). Aprovar um `avancar_pedido` cujo pedido mudou a ponto de o
    efeito ser outro (era entrada, virou encerramento) → 409 "mudou desde a proposta". Proposta antiga vem sem o campo.
- `GET /propostas?status=pendente&obra_id=…&limite=100&antes_de=…` → `[Proposta]` (mais novas primeiro)
- `POST /propostas/{id}/aprovar` `{ajustes?}` (corpo opcional) → `{proposta, registro}` (o `registro.id` é o que o
  Desfazer usa); executa a operação do tipo (papel `admin`+), marca aprovada, grava `EntradaRegistro` com `desfazer`
  preenchido. `POST /propostas/{id}/recusar` `{motivo}`.
- Aprovar `criar_pedido` cria o pedido já **`aprovado`** (não `em_cotacao`): a aprovação da proposta é a aprovação da
  compra. O texto do registro termina em `· aprovado`.
- **Ajustar e aprovar**: `ajustes` corrige campos de `dados` antes de executar. Chaves aceitas por tipo:
  `criar_pedido` → `quantidade, fornecedor, valor, previsao` (só em proposta de uma linha) · `avancar_pedido` → `quantidade_recebida, nf` (só se
  `dados.para = conferido`, senão 422) · `ajuste_estoque` → `quantidade, motivo` · `registrar_entrega` → `linhas,
  nf_numero, nf_data, nf_valor, confirmar_excesso`. Chave fora da lista → 422. Os dados
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
  (somado na API a partir das linhas; sem agregação no banco, para ser portável). `rota` é uma chave, não um texto
  para mostrar (a tela dá o nome): `conversa`, `briefing` e `comprovante` (leitura de comprovante de gasto).
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
  Conectar e desconectar entram no registro **sem `desfazer`**, por decisão: a receita teria de guardar o segredo
  (desfazer a desconexão), e o Desfazer é de admin enquanto a chave é só do master. Voltar atrás é `PUT`/`DELETE`.
- `GET /interno/chave-anthropic` (só o serviço do agente: `Authorization: Bearer <token de serviço>` +
  `X-Canteiro-Usuario: Bearer <acesso do usuário>`) → `{chave}` com `Cache-Control: no-store`; 404 sem chave própria.

## Sessões de conversa (histórico do agente)

Um documento por sessão (`sessoes`) e um por mensagem (`mensagens`, ID = `{sessao_id}-{seq:06d}`). A sessão é
privada de quem a criou (`dono` = usuário do token); de outra pessoa → 404.

- `POST /sessoes` `{id?, obra_id?, titulo?, abas?}` → `Sessao` (201; mesmo `id` do mesmo dono → 200). `id` de uma
  sessão de outra pessoa do cliente **ou de outro cliente** → 404 `"Conversa não encontrada."`, a mesma resposta da
  sessão que não existe (nem o código nem a mensagem revelam que ela existe). `obra_id` de outro cliente → 404
  `"Obra não encontrada."`.
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
  - `event: texto` `{delta}` · `event: ferramenta` `{nome, nivel: consulta|propoe, resumo}` (o "usou almoxarifado…"),
    emitido quando a ferramenta **começa** (a tela mostra o andamento durante uma consulta lenta), uma vez por chamada
  - `event: proposta` `{Proposta + ja_existia}` (vira cartão de decisão na tela; `ja_existia: true` = é a proposta
    igual que já esperava aprovação) · `event: fim` `{sessao_id, uso:{tokens, custo}}`
  - `event: erro` `{mensagem}` (inclusive "sem créditos"). Casos com mensagem própria: API central fora do ar ("O
    sistema está fora do ar agora…"; a volta para ali, sem chamar o modelo de novo, grava o consumo já gasto e ainda
    manda o `fim`); sessão que não abre (a mensagem da API: "Conversa não encontrada." / "Obra não encontrada.");
    outra pergunta na mesma conversa ainda em andamento ("Esta conversa ainda está respondendo…", antes de qualquer
    chamada ao modelo); histórico recusado com 409 por gravação concorrente ("…esta parte não foi salva…", seguido do
    `fim`).
  - **Uma volta por sessão de cada vez**, com trava no processo do agente (roda numa instância só). Com mais de uma
    instância a trava não vale entre elas; aí o `POST /sessoes/{id}/mensagens` fora de sequência (409) é o que segura
    a ordem, e o agente avisa que a parte dele não foi salva.
  - Corpo inválido → 422 `{detail: "mensagem"}` (ex.: "A pergunta tem de ter de 1 a 8.000 caracteres."), como o
    resto do contrato; sem `Authorization: Bearer` → 401.
- Ferramentas (cada uma chama a API acima com o token do usuário):

| Ferramenta | Nível | Chama |
|---|---|---|
| `listar_obras()` | consulta | `GET /obras` |
| `resumo_obra(obra_id)` | consulta | `GET /obras/{id}/resumo` (o "como está a obra?"; usado primeiro em pergunta aberta) |
| `consultar_estoque(obra_id, busca?)` | consulta | `GET /obras/{id}/insumos` (conta, sem previsão, último preço) |
| `historico_movimentos(obra_id, insumo_id?, limite?=30)` | consulta | sem insumo: `GET /obras/{id}/movimentos` (todos os insumos, mais novos primeiro, diz se cortou) + `GET …/insumos` (nomes); com insumo: `GET …/insumos/{id}/movimentos` |
| `listar_pedidos(obra_id, status?, abertos?, busca?)` | consulta | `GET /obras/{id}/pedidos` (`abertos` = `?status=` em cotação a parcial; `busca` por fornecedor ou insumo, filtrada no agente) |
| `consultar_orcamento(obra_id, busca?)` | consulta | `GET /obras/{id}/orcamento` |
| `consultar_cronograma(obra_id, mes?)` | consulta | `GET /obras/{id}/cronograma` (por etapa; `mes` detalha o que está previsto no mês) |
| `listar_fornecedores(busca?)` | consulta | `GET /fornecedores` |
| `consultar_tributos(obra_id)` | consulta | `GET /obras/{id}/tributos` (INSS da aferição e IBS/CBS estimados, com as linhas da conta e o que reduz) |
| `calcular(expressao, unidade_resultado?)` | consulta | nenhuma (calculadora local em Decimal, sem `eval`) |
| `propor_pedido(obra_id, insumo_id, quantidade, fornecedor, justificativa, valor? \| preco_unitario?, etapa?, previsao?)` | propõe | `GET /obras/{id}/etapas` + `POST /propostas` (criar_pedido; com `preco_unitario` o total sai da calculadora e volta em `conta_do_valor`) |
| `propor_entrega(obra_id, pedido_id, linhas, justificativa, nf_numero?, nf_data?, nf_valor?, confirmar_excesso?)` | propõe | `POST /propostas` (registrar_entrega; `nf_data` aceita "hoje"/"ontem", resolvido no fuso de Brasília, e com número de nota sem data fica hoje) |
| `propor_avanco_pedido(obra_id, pedido_id, para, justificativa)` | propõe | `POST /propostas` (avancar_pedido; `conferido` só de parcial = encerrar) |
| `propor_ajuste_estoque(obra_id, insumo_id, motivo, contagem? \| quantidade?)` | propõe | `GET …/insumos` + `POST /propostas` (ajuste_estoque; com `contagem`, a diferença contra o saldo sai da calculadora) |

- `propor_pedido` confere a `etapa` contra `GET …/etapas` antes de propor: etapa que não existe, ou obra com orçamento
  e etapa omitida → erro para o modelo (que pergunta à pessoa), sem criar proposta.

- As ferramentas devolvem números já formatados **e** brutos e datas para gente ("16/11/2026", "ago/2027"); o agente
  nunca faz conta de cabeça: cita a `Conta` da API (falta, `falta_entregar` do pedido, saldo/estouro da etapa,
  `aditivos_pct`, situação da etapa vêm prontos) e qualquer outra conta passa por `calcular`.
- Lembretes de "uma vez por conversa" (falta de previsão ou de cadastro, data futura num documento) vão em
  `lembretes: {chave: texto}` na primeira vez e só como `lembretes_ja_ditos: {chaves, nota}` depois; o agente lê do
  histórico guardado da sessão o que já foi dito.
- A data de hoje (Brasília) vai como bloco de contexto na mensagem do usuário ("(Hoje é quinta-feira, 05/11/2026, …)"),
  nunca na instrução nem nas ferramentas, que continuam byte a byte iguais entre conversas (cache). As de `propor_*` mandam
  `chave_idempotencia` = `"{sessao_id}:{tool_use_id}"` e, com `ja_existia`, dizem ao modelo que nada novo foi criado.

## Dados de exemplo (seed)

Casa Vila Nova do protótipo: insumos bloco (entrou 4.000, saiu 1.050 + 190 de quebra = 2.760), aço (3.940 precisa,
140 saldo, PED-0158 de 3.200 kg em trânsito, PED-0167 de 600 kg em cotação), cimento (precisa 200, saldo 46, PED-0160
de 120 sc a caminho), areia (PED-0157 recebido, 40 m³ na NF). Movimentos semeados com `quem = "Joana (mestre)"`.
Um usuário `admin` de exemplo, senha só no `.env` local.
