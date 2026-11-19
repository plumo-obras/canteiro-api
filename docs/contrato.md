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

## Obras

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
- `GET /obras/resumo` (qualquer papel) → `[{obra: Obra, pendencias, ultima_atividade, prazo, tem_orcamento}]` (por
  nome da obra), para a página inicial. `tem_orcamento` = contrato vigente.
  `prazo` = o `Prazo` do `GET /obras/{id}/cronograma` (término contratual e projetado, atraso), `null` sem cronograma.
  `pendencias` = propostas pendentes · `ultima_atividade` = `criado_em` da entrada mais nova do registro da obra, ou
  `null`. Somado na API: uma consulta por coleção para o tenant inteiro (sem agregação no banco) e uma leitura de 1
  documento do registro por obra.
- **Compras, pedidos e estoque saíram do produto** (11/2026). As rotas deles respondem **410** com
  `{"detail": "Compras, pedidos e estoque saíram do Canteiro. Lance o que foi pago em Gastos, pelo comprovante."}`:
  `GET`/`POST /obras/{id}/insumos`, `GET /obras/{id}/insumos/{insumo_id}/movimentos`, `GET`/`POST
  /obras/{id}/movimentos`, `POST /obras/{id}/importar`, `POST /obras/{id}/orcamento/importar`, `POST
  /obras/{id}/orcamento/materiais`, `GET`/`POST /obras/{id}/pedidos`, `POST /pedidos/{id}/avancar`, `PATCH
  /pedidos/{id}`, `GET`/`POST /pedidos/{id}/recebimentos`. O que sobrou no banco sai com o script de limpeza
  (`docs/deploy.md`, seção 10).

## Orçamento por planilha

Em dois passos, sem estado no servidor: a tela manda o arquivo para `ler`, mostra a sugestão de colunas, e manda o
mesmo arquivo de novo com o mapeamento confirmado para `…/orcamento/versoes`. Aceita `.xlsx` e `.csv` (até 5 MB); `.xls` → 422
pedindo para salvar como `.xlsx` ou `.csv`. Papel `admin`+.

- `POST /obras/{obra_id}/orcamento/ler` (multipart `arquivo`, form `planilha?` = nome da aba) →
  `{planilhas: [str], planilha, linha_cabecalho, colunas: [{indice, titulo}], amostra: [[str]], sugestao: Mapeamento,
  data_base}`.
  O cabeçalho é a 1ª das 30 primeiras linhas com pelo menos dois títulos conhecidos (pula título e dados da obra);
  `linha_cabecalho` é 1-based e os índices de coluna são 0-based. `amostra` = até 8 linhas de dados como texto
  (`37,9`, datas `dd/mm/aaaa`). CSV tem uma aba só, `"CSV"`.
  Sem `planilha`, a aba é a de cabeçalho mais completo (mais tipos de coluna reconhecidos: item, código, descrição,
  quantidade…); empate fica com a 1ª, e sem cabeçalho em nenhuma, a 1ª com conteúdo. A planilha de licitação traz
  encargos e composições com cabeçalho também, e a aba do orçamento é a que reconhece mais colunas.
  - **Coluna sem título com dado** (nas 300 linhas abaixo do cabeçalho) também vem em `colunas`, com `titulo`
    `"Coluna C"` (a letra do Excel); coluna sem título e vazia não vem. As linhas de dados podem ser mais largas que o
    cabeçalho, e `amostra` acompanha. É a planilha de custo feita à mão, em que só os valores têm título.
  - Sem coluna com título de item/descrição, a sugestão procura nas colunas sem título: `item` = a 1ª só com números
    de item ("7", "7.1"; na amostra o número aparece "7,1"), com algum ponto ou vírgula; `nome` = a de mais texto (pelo
    menos 12 letras na amostra). A pessoa confere na tela.
  - `data_base` = `"AAAA-MM"` achado num título de coluna ("ORÇADO SET/23" → `"2023-09"`, "preço 09/2023" →
    `"2023-09"`) ou, sem isso, nas linhas acima do cabeçalho que falam de referência ("Sinapi não desonerado - Data
    referência técnica: 18/05/2023" → `"2023-05"`), ou `null`. Só sugestão para a data-base da obra
    (`PATCH /obras/{id}`); nada é gravado na leitura.
  - `banco` = o banco de preços dito nessas linhas do topo, em maiúsculas, com a desoneração quando escrita
    (`"SINAPI não desonerado"`, `"SICRO"`), ou `null`. Só para mostrar.
- `Mapeamento` = `{planilha, linha_cabecalho, item, codigo, fonte, nome, unidade, quantidade, valor_unitario,
  valor_total, complemento}` (cada coluna é índice ou `null`). Sinônimos da sugestão: item/nº (número do
  item, "3.2.1") · código/cód · fonte/banco/base/referência · descrição/discriminação/insumo/material/serviço ·
  un/und/unid/unidade · quant/qtd/qtde · "unit" (preço unitário; também o preço solto, "preço (R$)"/valor/custo/P.U.,
  quando há outra coluna de total) · total/orçado · complemento.
- A planilha é o **orçamento da obra** (serviços por etapa, `POST …/orcamento/versoes`, abaixo).

### Materiais do projeto como orçamento (etapa "Estrutura")

- `POST /obras/{obra_id}/orcamento/estrutura` (JSON `{etapa?, itens: [{nome, unidade, quantidade, valor_unitario}],
  aplicar, chave_idempotencia?}`, admin) → `PreviaVersao`. Os materiais do projeto viram uma **versão do orçamento**
  (uma etapa, "Estrutura" por padrão): vira **contrato** se a obra ainda não tem um;
  com contrato e a obra ainda em orçamento, **soma ao contrato** (o mesmo `somar` de `…/orcamento/versoes`: etapa pelo
  nome, `repetido_de`, Desfazer volta o anterior); com o orçamento aprovado, **aditivo**. `ignorar_itens?` (lista de
  `ordem`, ≤ 500) tira itens da prévia e da gravação. Reaproveita a importação de versão (prévia, gravação em versão, Desfazer). `quantidade` em
  milésimos, `valor_unitario` em centavos; 201 quando grava.

### Orçamento da obra (EAP, em versões)

O orçamento é etapa → item, como a planilha da construtora: cada linha vira um item, com o número, o código e a fonte
dela, **sem juntar linhas repetidas** (o mesmo serviço em duas etapas são dois itens), e o total fecha no centavo com a
planilha. Cada importação é uma **versão**: o **contrato** (só um vigente; reimportar substitui a versão inteira) ou um
**aditivo** (soma ao contrato, que fica intacto). As duas têm Desfazer.

- `VersaoOrcamento` = `{id, obra_id, tipo: "contrato" | "aditivo", numero, descricao, data, arquivo, itens, etapas,
  total, ativa, substitui, criado_em}`. `numero`: 0 no contrato, 1, 2… nos aditivos (o maior ativo + 1). `data`
  `"AAAA-MM-DD"` ou `null`. `itens`/`etapas` = quantos de cada; `total` em centavos (soma dos itens). `ativa: false` =
  desfeita ou substituída. `substitui` = a versão de contrato que esta tirou de vigência (ou `null`). `lista_etapas`
  = `[{etapa, descricao}]` e `lista_topicos` = `[{etapa, id, nome}]` (os tópicos do orçamento desta versão; versão
  gravada antes do campo não o tem, e a API monta pelos itens). `padronizada` = as linhas estão nas etapas da lista
  padrão (ver **Orçamento padronizado**); `natureza` = `"custo"` | `"venda"` e `bdi` (centésimos de %, só na venda):
  ver **Custo e venda**. Os três faltam no documento gravado antes deles e valem `false`, `"custo"` e 0.
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
  descricao, complemento, unidade, quantidade, valor_unitario, total, linha, topico_id}`. `complemento` = a medida,
  marca ou detalhe da linha ("0,60 x 2,10"), da coluna `complemento` (≤ 80; `""` sem ela e nos itens gravados antes do
  campo). `topico_id` = o tópico do orçamento em que o item conta (`null` = sem tópico). `grupo` = a etapa da planilha
  de onde o item veio, no orçamento padronizado, quando ela não tem o nome da etapa padrão (`""` nos outros); é o
  tópico do item. `valor_unitario_venda`/`total_venda` = o preço do cliente, só na planilha de venda (`null` nas
  outras); aí `valor_unitario`/`total` são o custo. `ordem` = posição na versão (0, 1, 2…); `numero` =
  o da coluna item ("3.2.1", "A1.2", pode ser `""`); `nivel` = 1 + pontos do número (sem número: etapa 1, item 2, e 3 no item logo abaixo de um título de nível 2, o
  detalhe do grupo que a planilha não numera);
  `etapa` = a chave da etapa raiz: o que vem antes do 1º ponto ("2" para "2.1.1"); item sem ponto fica na última linha de
  etapa; etapa sem número tem como chave o próprio nome normalizado (sem acento, minúsculo), e é assim que a "FUNDAÇÕES"
  de um aditivo sem numeração soma na do contrato; `""` antes da 1ª etapa; `linha` = linha da planilha (1-based). Etapa tem unidade `""` e quantidade/valores 0.
- `POST /obras/{obra_id}/orcamento/versoes` (multipart `arquivo`, `mapeamento` = JSON, `tipo` = `contrato` | `aditivo`,
  `descricao?` (≤ 200), `data?`, `aplicar` = bool, `chave_idempotencia` (obrigatória com `aplicar`), `etapas_itens?`,
  `somar?` = bool (padrão `false`), `ignorar_itens?` = JSON lista de `ordem`, `padronizar?` = bool (padrão `false`),
  `destinos?` = JSON, `natureza?` = `custo` | `venda` (padrão `custo`), `bdi?` = 1..10000) → `PreviaVersao` =
  `{aplicado, tipo, itens: [ItemPrevia], ignoradas, erros, avisos: [{linha, mensagem}], total, n_itens, n_etapas,
  etapas: [{etapa, descricao, total}], topicos: [{etapa, id, nome, total}], substitui: {versao_id, total, itens} | null,
  total_apos, padronizada, encaixe, etapas_disponiveis, natureza, bdi, total_venda, versao_id, registro_id}`
  (`ItemPrevia` = `ItemOrcamento` sem `id/obra_id/versao_id`, mais `etapa_origem`, `etapa_planilha` e `repetido_de`;
  `versao_id`/`registro_id` só depois de aplicar; `padronizada`, `encaixe` e `etapas_disponiveis` no **Orçamento
  padronizado**; `natureza`, `bdi` e `total_venda` em **Custo e venda**).
  - `ignorar_itens` (JSON `[3, 7]`, as `ordem` da prévia; inválido → 422): esses itens saem da prévia e da gravação
    (ordem que não é de item é ignorada). Entra na assinatura de idempotência, como `somar` e `etapas_itens`.
  - `nome` tem de estar mapeado (422 "Diga qual coluna é a descrição."), e `quantidade` ou `valor_total` (422 "Diga
    qual coluna é a quantidade ou o valor total."). Sem `unidade` mapeada a planilha é lida (a de custo feita à mão
    não tem): as linhas numeradas com valor entram como verba, as outras ficam de fora, e a prévia abre com um aviso
    `linha: 0` dizendo isso.
  - **Etapa**: linha com descrição, sem unidade e sem quantidade, que não é subtotal/total (descrição começando por
    "subtotal", "total" ou "soma" → ignorada; na linha **sem número**, também "custo total", "custo geral" e "custo mão
    de obra", o pé da etapa da planilha de custo). **Item**: descrição + unidade + quantidade ≠ 0. Resto vazio →
    ignorado.
  - **Verba**: linha com número de nível 2 ou mais ("7.7"), **sem unidade**, com valor (a coluna total; sem ela,
    quantidade × unitário) maior que zero e **sem linha embaixo dela** na numeração vira item com `unidade: "vb"`,
    `quantidade` 1000 (1 vb) e `valor_unitario` = `total` = o valor. Aviso por verba: "7.7 Materiais diversos: sem
    unidade, entra como verba (1 vb de R$ 154.859,70)."; se a linha trazia quantidade, o aviso diz que ela ficou de
    fora ("… A quantidade 34 ficou de fora: ponha a unidade na planilha para entrar por quantidade.").
  - **Subtotal de grupo**: linha numerada com linhas embaixo dela ("7.1" com "7.1.1", "7.1.2") é título do grupo
    (`tipo: "etapa"`, nível dela), mesmo que tenha unidade, quantidade e valor: o valor dela é subtotal, e somar junto
    contaria duas vezes. A linha numerada sem unidade seguida de itens **sem número** também é título do grupo (os
    itens são dela); a com unidade seguida de item sem número continua item. Se a linha do título trazia valor e ele não fecha com a soma das linhas embaixo, aviso "1.1
    Portas diz R$ 2.500,00 e as linhas dele somam R$ 2.100,00: vale a soma das linhas.". Etapa (nível 1) com valor e
    nenhuma linha: o valor não entra, com aviso "A etapa 2 LIMPEZA tem R$ 500,00 e nenhuma linha: o valor não entra
    no orçamento. Ponha o valor numa linha 2.1.".
  - `total` da linha = a coluna total, quando mapeada e preenchida (é o número do contrato); senão quantidade ×
    unitário arredondado. `valor_unitario` = a coluna unitária; sem ela, total ÷ quantidade.
  - **Erro da linha** (nada é gravado): número inválido/ambíguo, quantidade negativa no contrato (no aditivo, negativa
    = supressão, vale), descrição > 300, complemento > 80, unidade > 12, código/fonte/número > 40. Mais de 2000 linhas
    → erro.
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
- **Tópicos do orçamento** (regra em `canteiro_dominio.orcamento.topicos_dos_itens`): o **nível 2 da numeração** é
  tópico da etapa. O nome é a descrição da linha "7.1", seja ela título do grupo ou o próprio item; os itens "7.1.x"
  caem nele, e os itens sem número logo abaixo do título "7.1" (o grupo que a planilha feita à mão não numera:
  `nivel` 3, decidido na leitura da planilha; ao somar, a linha solta que entra depois de um grupo do contrato não
  entra nele, e a que vai para outra etapa deixa o grupo) também. No aditivo, o item que guarda o número do contrato
  ("7.1.3") sem trazer a linha "7.1" cai no tópico em que o contrato tem esse número (também no aditivo gravado antes
  de `lista_topicos`). A mesma descrição (sem acento, caixa e espaços) na mesma etapa é **um tópico só** ("11.1
  Portas" e "11.2 Portas" de medidas diferentes); a mesma em outra etapa é outro ("Mão de Obra" da 7 e da 11). Item
  sem linha de nível 2 que o nomeie ("2.1.1" sem a linha "2.1"), item de nível 1 e item sem número fora de um grupo
  ficam sem tópico; planilha sem numeração não tem tópicos. A etapa do tópico é a do item. Etapa com mais de 30
  tópicos fica **sem nenhum**, com aviso
  `linha: 0` na prévia ("A etapa 1 SERVIÇOS tem 31 tópicos na numeração (o limite é 30): ela fica sem os tópicos do
  orçamento, e o gasto entra só na etapa."). `topicos` da prévia = os que os itens da prévia tocam, com a soma deles
  (no somar, só o que entra), nas chaves de etapa da prévia; cada `ItemPrevia` traz o `topico_id`.
  - **ID**: `orc-` + um ID derivado da obra, da etapa e do nome normalizado: reimportar a mesma planilha devolve os
    mesmos IDs, e o gasto continua no tópico. Tópico **manual** ativo com o mesmo nome na mesma etapa empresta o ID
    dele (o gasto já lançado nele continua no lugar), e na árvore aparece uma vez, como do orçamento.
  - Gravados na versão (`lista_topicos`) e em cada item (`topico_id`) pela importação, pelo somar, pela ordem das
    etapas e pelo `organizar-fases` (o tópico vai com o item para a etapa padrão, e o Desfazer volta; só os títulos
    de nível 1 viram os das etapas, e a linha de nível 2 fica com o nome e vai para a etapa do primeiro item abaixo
    dela); o Desfazer de uma versão leva os tópicos dela junto. Não são documento: reordenar e somar continuam livres com eles, e só o
    lançamento (gasto, tópico manual…) numa etapa que mudaria de número trava, como antes.
- **Orçamento padronizado** (`padronizar=true`, só no contrato novo; com `somar` ou `tipo=aditivo` → 422): cada linha
  da planilha é **encaixada numa etapa da lista padrão** (`ETAPAS_FIXAS`, as 24 da obra sem orçamento), e a obra fica
  comparável com as outras. Regra em `canteiro_dominio.encaixe.etapa_no_contexto`: a linha que diz o que é ("Porta
  0,60", "Pintura látex") vai para a etapa dela, mesmo numa etapa mista da planilha ("ACABAMENTOS"); a linha genérica
  ("Mão de obra", "Materiais diversos", uma verba) herda a etapa do título da planilha ("7 PAREDES E PAINÉIS" →
  `alvenaria`); concreto, fôrma e armação num título de fundação são `fundacao`; sem nada que indique, `outros`.
  - A chave da etapa é a da lista (`"alvenaria"`), com o nome dela ("Alvenaria e vedações") e `numero` = a posição na
    lista; as etapas saem na ordem da lista. O gasto lançado na lista padrão antes da planilha continua na etapa (não
    vira "sem etapa do orçamento").
  - **Tópico** = a etapa da planilha de onde a linha veio (`grupo`): "11 ESQUADRIA MADEIRA" e "12 ESQUADRIA METÁLICA"
    viram dois tópicos de Esquadrias, e a "Mão de Obra" de cada uma fica no seu. Quando a etapa da planilha já tem o
    nome da etapa padrão ("ALVENARIA", "FUNDAÇÕES"), o tópico continua saindo da numeração (nível 2).
  - `encaixe` (prévia) = `[{etapa_planilha, descricao, destino, destino_nome, escolhida, total, etapas: [{etapa, nome,
    total, itens}]}]`: cada etapa da planilha, o destino da maior parte do valor e, na etapa mista, a divisão pelas
    etapas padrão. `etapas_disponiveis` = `[{etapa, descricao}]` da lista padrão.
  - `destinos` (JSON) = o que a pessoa trocou: `{"7": "alvenaria"}` leva a etapa inteira da planilha, `{"linha:12":
    "pisos"}` uma linha só (a `linha` da planilha), e a linha vale sobre a etapa. Etapa fora da lista → 422 "Etapa
    desconhecida". `padronizar` e `destinos` entram na assinatura de idempotência.
  - Depois de gravado: o **aditivo** encaixa sozinho (a linha que casa com o contrato pelo código fica na etapa e no
    tópico dele; a que não casa é encaixada como na importação); **somar** → 422 ("Este orçamento está nas etapas
    padrão: importe a planilha inteira de novo…"); **reordenar** as etapas → 422 (a ordem é a da lista).
- `POST /obras/{obra_id}/orcamento/organizar-fases` (admin) → `{itens, estimadas, fases: [nome], registro_id}`. Encaixa
  o contrato vigente na lista padrão, se ele ainda não está (o orçamento importado sem `padronizar`, ou o organizado
  pelas fases de antes da lista), e **prevê a obra completa**: das fases de obra que já têm orçamento estima o porte e
  descreve os serviços típicos das que faltam (`canteiro_dominio.cronograma.estimar_fases_faltantes`), gravados como
  itens com `fonte: "estimativa"` na etapa padrão da fase (`encaixe.etapa_da_estimativa`). O estimado **não é orçado**:
  fica em `estimado` (ver Orçado × gasto), não entra no `total` da versão, e o cronograma usa
  orçado + estimado como peso. `fases` = os nomes das etapas do orçamento depois. 422 se não há orçamento, se nada muda
  ("O orçamento já está nas etapas padrão, e a obra já está prevista inteira.") ou se há aditivo num contrato ainda fora
  da lista; 409 se há gasto numa etapa que mudaria de sentido ("Há gasto lançado nas etapas 7…").
  O item que já está numa etapa padrão fica onde está (a escolha da pessoa não se desfaz). Registro "Previu a obra
  completa: N etapas, M estimadas", Desfazer `restaurar_etapas` (volta etapa, tópico e `grupo` dos itens, os títulos e
  `padronizada`; zera os estimados criados).
- **Custo e venda** (`natureza`): `custo` (padrão) = a planilha já é custo (a de quem orça à mão). `venda` = a planilha
  é o preço do cliente, com BDI: cada item guarda o preço em `valor_unitario_venda`/`total_venda` e fica com o custo,
  preço ÷ (1 + `bdi`) linha a linha (`canteiro_dominio.orcamento.custo_da_venda`, arredondado ao centavo). `bdi` vem do
  campo ou, sem ele, do BDI da obra; venda sem nenhum dos dois → 422 "Diga o BDI da planilha de venda…". A prévia traz
  `natureza`, `bdi` e `total_venda` (`total` é o custo); `natureza`/`bdi` entram na assinatura de idempotência. O
  orçado × gasto é sempre custo contra custo.
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
  - **Pranchas depois das fundações** (só no somar): se o vigente é só das pranchas (uma etapa, gravada por
    `…/orcamento/estrutura`) e a planilha traz uma etapa cujo nome tem "fundação"/"fundações" ou "infraestrutura",
    a etapa das pranchas vai para **logo depois** dela (da última, se vierem em sequência); sem ela, para logo
    depois da primeira cujo nome tem "preliminar" ou "canteiro". As etapas do contrato somado são renumeradas
    1..N nessa ordem (os itens trocam só o prefixo: "2.1" → "1.1"). Sem etapa assim, a das pranchas fica onde
    está (adivinhar a ordem da obra por outros nomes erraria mais do que acertaria). A **prévia continua com as
    chaves de antes** (são elas que valem em `etapas_itens`), traz em `numeracao` o número com que cada etapa que
    muda é gravada (`{"1": "3", "2": "1", "3": "2"}`; a tela mostra esse) e um aviso com `linha: 0`: "Ao somar, Estrutura (das pranchas) vai para depois de
    FUNDAÇÕES, e as etapas são renumeradas nessa ordem." Se já há lançamento numa etapa que mudaria de
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
  409; **lançamento numa etapa que mudaria de número** (gasto não estornado, tópico, cronograma) → 409 "Já há
  lançamento (gasto, tópico ou cronograma) na etapa 3 FUNDAÇÕES:
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
- **Orçado × gasto**: cada etapa, cada tópico e os totais trazem `gasto` (centavos) = os gastos confirmados. Os nomes
  de quando havia compras (`cotando`, `comprometido`, `realizado`, `gastos_avulsos`) saíram. `totais.sem_etapa` = o gasto que conta na obra sem estar numa etapa do orçamento
  (extra, a definir, lista padrão, etapa de versão desfeita; já somado nos totais). `atual` é o orçado da planilha como
  veio: na de **venda** (`natureza: "venda"`) já é o custo, e o preço do cliente vem à parte em `venda` (por etapa e em
  `totais`; `null` sem planilha de venda); na planilha de engenheiro importada como `custo` ele ainda é preço de
  contrato (com BDI). `estimado` (por etapa e em `totais`) = os serviços que a previsão da obra estimou
  (`fonte: "estimativa"`), **fora** de `contratado`/`atual`/`saldo`; a etapa só estimada aparece com `atual` 0. As
  etapas saem na ordem de `lista_etapas` das versões vigentes (contrato primeiro).
- **Gastos no orçamento** (regra em `canteiro_dominio.gastos.gastos_no_orcamento`): todo gasto confirmado e não
  estornado é dinheiro que saiu e soma, pelo `valor`, no `gasto` da etapa dele, mexendo no `saldo` e no `estourou`. O
  gasto que era pagamento de pedido (`pedido_id`
  gravado antes de Compras sair) conta como qualquer outro. `totais.gastos` = `{total, nas_etapas, fora_do_orcamento,
  na_lista_padrao, sem_etapa_do_orcamento, a_definir}`, todos já somados nos totais: `fora_do_orcamento` = gastos nas
  **etapas extras** (não engordam etapa do orçamento); `sem_etapa_do_orcamento` = gastos numa chave que não é etapa
  vigente nem extra (lançados na lista padrão antes da importação, ou etapa de versão desfeita; reclassifique com
  `PATCH /gastos/{id}`); `na_lista_padrao` = obra sem orçamento, gastos na lista padrão.
- **Contas prontas** (a tela e o agente só mostram): cada etapa traz `saldo` (= `atual − gasto`, centavos; negativo
  quando estourou) e `estourou` (gasto > `atual`); `totais` traz `saldo` (o mesmo, da obra) e
  `aditivos_pct` = aditivos sobre o contratado em **centésimos de %** (290 = 2,9%; arredondado ao mais próximo) ou
  `null` sem contrato. A base (`contratado`) é o total do **contrato vigente**: o orçamento aprovado inteiro, já com o
  que foi somado antes de aprovar (pranchas + planilha), não só a primeira importação. Sem aditivo de verdade, 0. É
  número neutro (não há limite legal: obra privada).
- **Por tópico** (`etapas[].topicos`): cada etapa aberta em `[{topico_id, nome, origem, contratado, aditivos, atual,
  gasto, saldo, estourou}]`, com as mesmas contas da etapa. Entram, nesta
  ordem: os tópicos do orçamento da etapa (mesmo sem nada lançado; `contratado`/`aditivos` = a soma dos itens dele),
  os tópicos manuais que têm gasto na etapa (`origem: "manual"`, orçado 0), os de versão desligada
  que têm (`origem: "orcamento"`, orçado 0: continuam sem renomear nem arquivar) e
  **"Sem tópico"** (`topico_id: null`, `origem: null`: os itens sem tópico e os gastos lançados só na etapa; some
  quando tudo nele é 0). Somados, dão a etapa. Etapa sem tópico nenhum → `topicos: []`. Cada item
  de `itens` traz o `topico_id` dele (também nas versões gravadas antes do campo, montado pela numeração).
- `PATCH /obras/{obra_id}/orcamento/itens/{item_id}` (admin) `{quantidade?, valor_unitario?}` → `{item: ItemOrcamento,
  total_versao, registro_id}`. O total da linha vira quantidade × unitário, e a diferença soma no total da versão.
  Título de etapa, quantidade zero, quantidade negativa fora de aditivo, preço negativo ou nada mudou → 422; versão fora
  de vigência → 409; item de outra obra → 404. **Orçamento aprovado:** só o preço de item que estava em R$ 0,00; o
  resto → 409 "lance um aditivo". Desfazer/Refazer `editar_item_orcamento`: 409 se a linha
  mudou de novo, ou se a edição é de antes da aprovação e a obra já foi aprovada.
- `POST /obras/{obra_id}/orcamento/aprovar` (admin) → `Obra + registro_id`. Tira a obra de `em_orcamento` para
  `em_andamento` e marca `aprovada`: o orçado atual (contrato + aditivos vigentes) passa a ser o que cronograma e gastos
  seguem. Já aprovado → 409; sem contrato vigente → 422 "Importe o orçamento antes de aprovar."; versões mudadas entre
  a leitura e a gravação → 409. Registro "Aprovou o orçamento de X (N itens, R$ total)" com `desfazer`
  `reabrir_orcamento` (volta para `em_orcamento` e `aprovada: false`; 409 se a obra já não está em andamento). A tela
  confirma antes, mostrando total e nº de itens.
- **Com a obra aprovada:** aditivo soma e o Desfazer dele tira. Trocar o contrato → 409 "desfaça a aprovação ou lance
  um aditivo"; importar contrato sem nenhum vigente e retirar o contrato também dão 409 quando a obra passou pelo
  Aprovar (`Obra.aprovada`). Obra antiga em andamento, que nunca foi aprovada, segue podendo.
- `status: "desfazendo_aprovacao"` (com `desfazendo_registro_id`) é de quando o Desfazer da aprovação rodava em lotes e
  podia parar no meio. Não nasce mais; a obra que ficou assim recusa importar versão, editar linha e aprovar (409) até
  o `POST /registro/{id}/desfazer` daquela aprovação, que a devolve a `em_orcamento`.

## Hoje na obra

- `GET /obras/{obra_id}/hoje?hoje=AAAA-MM-DD` (qualquer papel; padrão: hoje em São Paulo; sem chamada ao modelo) →
  `{hoje, resumo: {situacao, frase, esperando, semana_atual, semanas_total}, pontos: [Ponto], mais, semana: [Evento]}`.
  `situacao` = `no_prazo` | `atrasada` | `sem_medicao` (a mesma regra do placar); `frase` só diz a situação; `esperando` =
  propostas pendentes + pontos `pede_voce` (mesmo os cortados); `semana_atual` é `null` sem cronograma ou antes do início, e passa de
  `semanas_total` se a obra atrasar; `mais` = pontos fora do limite.
- `Ponto` = `{tipo, prioridade, titulo, detalhe, acao: {tela, rotulo, filtro?}}`, no máximo 6, um por tipo, em ordem
  `pede_voce`, `atencao`, `a_caminho`, `ok`. `pede_voce`: `desfazendo_aprovacao`, `sem_orcamento`, `gastos_a_conferir`.
  Proposta pendente não vira ponto: a tela mostra um cartão para cada uma, e ela conta em `esperando`. `atencao`:
  `etapas_estouradas`, `etapas_atrasadas`, `itens_sem_preco`, `orcamento_so_pranchas` (só em orçamento),
  `gastos_sem_etapa`. `acao.tela` ∈ `projeto | orcamento | cronograma | gastos | aprovacoes | historico`.
- `Evento` = `{data, tipo: "inicio_etapa", titulo, detalhe, acao}`, de hoje até hoje + 7, por data.

## Listagens paginadas

Registro, propostas, gastos, comprovantes e sessões vêm do mais novo para o mais antigo e paginam por `antes_de`:
- a página seguinte se pede com `antes_de` = o campo de data do ÚLTIMO item recebido (`criado_em`; nas sessões,
  `atualizado_em`), sem mexer nos outros filtros;
- o cursor é **exclusivo** (`< antes_de`) e a página **nunca parte um empate**: se o item seguinte tem o mesmo instante
  do último, todos os daquele instante vêm nesta página, que pode passar um pouco do `limite`. Assim nada se repete
  entre páginas e nada some na virada (com `<=` um empate do tamanho da página voltaria para sempre);
- acabou quando a página vier com **menos** itens que o `limite`;
- `antes_de` em ISO 8601 **com fuso** (`2026-11-01T13:00:00.123Z` ou `-03:00`); sem fuso → 422. A API compara no
  milissegundo, como grava.

## Etapas e fornecedores

- `GET /obras/{obra_id}/etapas` → `[{etapa, descricao}]`: as etapas das versões vigentes do orçamento (contrato
  primeiro, depois as que nasceram em aditivo). Lista vazia = obra sem orçamento; aí a etapa é opcional em tudo.
- `GET /fornecedores` → `[Fornecedor]` por nome; `Fornecedor` = `{id, nome, cnpj, contato, telefone, email}` (cnpj só
  dígitos ou `""`). São do tenant, valem para todas as obras. A leitura do comprovante casa o CNPJ lido com o
  cadastrado.
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
    totais}`. `base` ∈ `medicao` (avanço = última medição) | `gastos` (sem medição: gasto ÷ orçado faz o papel do
    avanço) | `sem_cronograma` (tudo em `totais.sem_mes`).
  - `meses`: `[{mes, pago, previsto, projetado, projetado_no_ritmo}]`, do primeiro mês com pagamento ou do início do
    cronograma até o último mês projetado. `pago` = gastos confirmados pelo mês do
    pagamento; `previsto` = o `mensal[].previsto` do cronograma vigente; `projetado` = o que falta, **pelo cronograma**
    (o resto do plano empurrado por `atraso_meses`, nada antes do mês corrente); `projetado_no_ritmo` = o mesmo
    esticado pelo ritmo medido (`ritmo_pct` = % medido ÷ % previsto na última medição, entre 1000 e 10000; 10000 sem
    medição, e aí os dois cenários são iguais). Os dois somam `totais.falta − totais.sem_mes`.
  - `etapas`: `[{etapa, descricao, atual, gasto, executado_pct, falta, devido_agora, sem_cronograma}]`. `falta` =
    orçado atual − gasto (nunca negativo: não projeta economia nem estouro por tendência); `devido_agora` =
    executado pela medição e ainda não gasto, no mês corrente.
  - `totais`: `{orcado, gasto, pago, falta, custo_final, desvio, sem_mes}`; `gasto` = `pago` (os gastos confirmados); `custo_final` = gasto + Σ falta das etapas; `desvio` = custo_final − orçado (o estouro que já
    houve, e gasto fora do orçamento).

## Resumo de uma obra ("como está a obra?")

- `GET /obras/{obra_id}/resumo?hoje=AAAA-MM-DD` (qualquer papel; `hoje` padrão = hoje em São Paulo) → `ResumoDaObra` =
  `{obra: Obra, hoje, cronograma, financeiro, pendencias}`, tudo somado na API a partir do que
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
  - `financeiro`: `{tem_orcamento, contratado, aditivos, aditivos_pct, orcado_atual, gasto, saldo, falta_gastar,
    realizado_pct, pago, etapas_estouradas: [{etapa, descricao, saldo}], gastos}` (os
    totais de `GET …/orcamento`, e `gastos` = o mesmo `totais.gastos`). Para o placar da obra: `falta_gastar` = orçado
    atual − gasto (nunca negativo; gasto + falta fecham no orçado) e `realizado_pct` = gasto ÷ orçado em centésimos
    de ponto (`null` sem orçado). `pago` = todo gasto confirmado e não estornado, o mesmo `totais.pago` do desembolso e
    o mesmo número do `gasto`.
  - `pendencias`: `{propostas, titulos}` (até 5 títulos, as mais novas primeiro).

## Gastos da obra: comprovantes, leitura por IA e revisão

Gasto é **dinheiro que saiu**, numa data, numa etapa. A pessoa envia o comprovante (Pix, NF paga, boleto
pago, recibo, papel à mão), o serviço do agente lê com IA e preenche o máximo, e uma pessoa (admin) confere e confirma.
Só a confirmação cria o `Gasto`; até lá o comprovante fica "a revisar" e não conta em nada. Regras em
`dominio/src/canteiro_dominio/{etapas,leitura,gastos}.py`.

**Uma árvore só, a do orçamento quando ele existe.** Obra **com orçamento** (alguma versão vigente): as etapas de
gasto são as **etapas do orçamento** (as mesmas de `GET …/etapas`, na ordem dele; chave = o código da etapa, como
`"18"`, a mesma chave de `GET …/orcamento` e do cronograma) mais as **etapas extras** da
obra. Obra **sem orçamento**: a **lista padrão** de 24 etapas (chave estável como `fundacao`, `eletrica`, `pessoal`)
mais as extras antes de "Outros". O gasto soma no `gasto` da etapa do orçamento (ver "Gastos no orçamento"); o das extras fica "fora do orçamento". Gasto lançado na lista padrão antes de a obra importar o
orçamento **continua contando** (nos totais) e aparece como **"sem etapa do orçamento"** até alguém reclassificar.

`origem` (em `/arvore`, no resumo e nos lançados) diz de onde vem a etapa: `orcamento` · `padrao` (lista padrão, só em
obra sem orçamento) · `extra` · `sem_etapa_do_orcamento` (chave que não está mais na árvore: lista padrão numa obra
que agora tem orçamento, ou etapa de versão desfeita; só no resumo e nos lançados) · `a_definir` (confirmado sem etapa,
`etapa: null` e `etapa_nome: "Sem etapa"`; só no resumo e nos lançados).

**Árvore de gasto:**

- `GET /obras/{obra_id}/arvore` (qualquer papel) → `[{chave, nome, inclui, fixa, origem, topicos: [{id, nome,
  origem}]}]`.
  Com orçamento: as etapas dele (`origem: "orcamento"`, `fixa: true`, `inclui: ""`, `nome` = "18 INSTALAÇÃO ELÉTRICA
  - 220V", com o código na frente; etapa sem número da planilha fica só com a descrição) e depois as extras
  (`origem: "extra"`, `fixa: false`, chave `x-<id>`). Sem orçamento: as 24 da lista padrão (`origem: "padrao"`,
  `fixa: true`), as extras antes de "Outros". Os **tópicos** de cada etapa ficam presos à chave dela (no orçamento, o
  código). Arquivados não aparecem; tópico de uma etapa que saiu da árvore também não.
  - Tópico `origem: "orcamento"` = a linha de nível 2 da numeração do orçamento vigente (ver "Tópicos do orçamento"),
    na ordem da planilha, antes dos manuais; `origem: "manual"` = criado por `POST …/topicos`, pela criação. Tópico do
    orçamento não se renomeia nem se arquiva (a tela não oferece): ele muda quando o orçamento é reimportado.
- `POST /obras/{obra_id}/etapas-extras` `{nome}` e `POST /obras/{obra_id}/topicos` `{etapa, nome}` (admin) → 201 +
  `registro_id`. Nome até 60, sem repetir (sem diferenciar acento e maiúscula) entre as etapas da obra (com orçamento:
  a descrição de cada etapa dele, com ou sem o código; sem: a lista padrão) / os tópicos da etapa, os do orçamento
  inclusive → 422. `etapa` do tópico tem de estar na árvore de hoje → 422. Até 20 etapas extras por obra e 30 tópicos
  por etapa (os do orçamento contam). Desfazer a
  criação arquiva; refazer reativa. Limite e nome único valem também com requisições simultâneas e no Desfazer/Refazer
  (reativar ou voltar o nome antigo que outro pegou nesse meio-tempo → 422 "Já existe: …").
- `PATCH /etapas-extras/{id}` e `PATCH /topicos/{id}` `{nome?, arquivado?}` (admin) → renomeia e/ou arquiva/reativa,
  com Desfazer e Refazer. Nada mudou → 422. Arquivado sai da árvore e não recebe gasto novo; gasto antigo fica.
  Tópico do orçamento (ID `orc-…`, ou o manual que o orçamento reaproveitou) → 422 "Este tópico vem do orçamento (a
  linha de nível 2 da planilha): ele muda quando o orçamento é reimportado, não por aqui." O Desfazer/Refazer da
  criação ou da edição do manual que o orçamento reaproveitou responde o mesmo 422.

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
  fornecedor_id?}` → `{gasto, comprovante, registro_id}` (201). Campo a mais (como o antigo `pedido_id`) → 422. `etapa: null` (obrigatório mandar o campo) é
  o "ver depois": sem tópico, conta no gasto da obra e em nenhuma etapa (`a_definir` em
  `totais.gastos` e em `GET …/gastos/resumo`, com uma linha `etapa: null`); escolhe-se a etapa depois com
  `PATCH /gastos/{id}`. Valor > 0 em centavos; data `AAAA-MM-DD`
  até hoje (fuso de São Paulo); favorecido e descrição obrigatórios; etapa da árvore de hoje (com orçamento, um código
  dele ou uma extra) e tópico daquela etapa → senão
  422 e o comprovante continua na fila. Já revisado → 409. Registro "Gasto de R$ 350,00 em Instalações elétricas ›
  Quadro: mão de obra (João da Silva)", desfazer `estornar_gasto`: o gasto sai das contas (`estornado`) e o
  comprovante volta para a fila.
- `POST /comprovantes/{id}/descartar` `{motivo?}` (admin) → `Comprovante + registro_id`. O arquivo fica guardado.
  Desfazer `reabrir_comprovante` volta para a fila.

**Gastos:**

- `Gasto` = `{id, obra_id, comprovante_id, valor, data_pagamento, favorecido, fornecedor_id, descricao, etapa,
  topico_id, quem, usuario_id, estornado, criado_em, natureza_fiscal, ibs_cbs, nf_cno, retencao}`.
- `GET /obras/{obra_id}/gastos?etapa=…&limite=100&antes_de=…` → confirmados e não estornados (mais novos primeiro),
  cada um com `etapa_nome` (o nome de hoje; chave antiga da lista padrão mantém o nome dela, ex. "Fundação") e
  `etapa_origem` (`orcamento` | `padrao` | `extra` | `sem_etapa_do_orcamento`): a tela marca "sem etapa do orçamento"
  e oferece mudar a etapa.
- `GET /obras/{obra_id}/gastos/resumo` → `{total, a_revisar, tem_orcamento, fora_do_orcamento,
  sem_etapa_do_orcamento, a_definir, etapas: [{etapa, nome, origem, total, topicos: [{topico_id, nome, total}]}]}`.
  `total` = os gastos confirmados (é o que soma no orçamento: igual a `totais.gastos.total` de `GET …/orcamento`);
  `fora_do_orcamento` e `sem_etapa_do_orcamento` = as partes do `total` nas extras e em chave fora da árvore (com
  orçamento; sem orçamento é 0). `topico_id: null` = "Sem tópico". Etapas na ordem da árvore; inclui etapa e tópico já
  arquivados; as `sem_etapa_do_orcamento` vêm por último.
- `PATCH /gastos/{gasto_id}` `{etapa, topico_id?}` (admin) → `Gasto + {etapa_nome, etapa_origem, registro_id}`. Muda a
  etapa (e o tópico; sem `topico_id` = sem tópico) de um gasto confirmado: é como o "sem etapa do orçamento" vai para
  uma etapa do orçamento e passa a somar no gasto dela. Destino tem de estar na árvore de hoje e o tópico naquela
  etapa → 422; mesma etapa e tópico → 422 "Nada mudou."; gasto estornado → 409; não existe → 404; viewer → 403.
  Registro "Mudou o gasto de R$ 120,00 (descrição) de
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
  - `a_classificar`/`valor_a_classificar`: gastos sem natureza fiscal (contam zero no INSS); `pendentes` e
    `sem_nota`: até 20 de cada, maiores primeiro, `{id, valor, data_pagamento, favorecido, descricao,
    natureza_fiscal, nf_cno}`; `sem_nota` traz a mão de obra sem nota e a com nota sem o CNO.
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
  `tipo` = `cronograma` (`dados` = o corpo do `PUT /obras/{id}/cronograma`, validado contra o orçamento na criação).
  Os tipos de compra (`criar_pedido`, `avancar_pedido`, `ajuste_estoque`, `registrar_entrega`) saíram: criar → **410**;
  os antigos continuam no histórico (`GET /propostas`).
  - **`efeito`** (decidido pela API ao criar): `cronograma`. É o que o botão da tela diz. Proposta antiga pode trazer
    um efeito de compra (`compra`, `entrada`, `encerramento`, `cancelamento`, `status`, `ajuste`) ou vir sem o campo.
- `GET /propostas?status=pendente&obra_id=…&limite=100&antes_de=…` → `[Proposta]` (mais novas primeiro)
- `POST /propostas/{id}/aprovar` (corpo opcional) → `{proposta, registro}` (o `registro.id` é o que o Desfazer usa);
  executa a operação do tipo (papel `admin`+), marca aprovada, grava `EntradaRegistro` com `desfazer` preenchido.
  Proposta de compra que ficou pendente → **410** (só se recusa). `ajustes` não vale para o cronograma: aprove ou
  recuse (422). `POST /propostas/{id}/recusar` `{motivo}`.
- Aprovar grava em `Proposta.registro_id` o ID da `EntradaRegistro` da execução (a tela mostra o Desfazer mesmo depois
  de recarregar); desfazer mantém o `registro_id` e muda a proposta para `desfeita`.
- Proposta decidida traz `decidido_por` (ID) e `decidido_por_nome`. `EntradaRegistro.quem` é o nome legível;
  vem também `quem_nome` (igual) e `usuario_id`.
- Respostas de escrita que já trazem o ID do registro, para a tela oferecer **Desfazer** logo depois: as de obra,
  orçamento, gasto, fornecedor, cronograma e medição (campo `registro_id`) e `POST /propostas/{id}/aprovar`
  (`registro.id`).
- `GET /registro?obra_id=…&limite=50&antes_de=…` → `[EntradaRegistro]` (mais novas primeiro) · `POST /registro/{id}/desfazer` → aplica a operação inversa
  e registra o desfazer. Entrada de compra de antes da saída (pedido, entrega, movimento, insumo) que ainda traz
  Desfazer → **410**; a limpeza do banco tira esses Desfazer.
- **Refazer** é o mesmo `POST /registro/{id}/desfazer` aplicado à entrada "Desfeito: …": quando o desfazer tem
  inverso (medição, cronograma, edição de obra/fornecedor/linha do orçamento), a entrada "Desfeito" vem com `desfazer.refazer:
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
- `GET /conta/chave-anthropic` (qualquer papel) → `{ativa, final, cadastrada_em, conectada_por}` (tudo `null`/`false`
  sem chave). Nunca traz a chave nem o cifrado. `final` = os 4 últimos caracteres (a tela põe o "…");
  `conectada_por` = nome de quem conectou (null se a pessoa não existe mais). A chave colada tem as bordas limpas e
  precisa casar `^sk-ant-[A-Za-z0-9_-]+$`; o 422 de validação nunca devolve o valor recusado.
- `PUT /conta/chave-anthropic` `{chave}` (papel `admin` ou acima) → `{chave_propria: true, final: "abcd"}`. Antes de
  guardar, a API confere a chave na Anthropic (`GET /v1/models`, não gasta token): recusada → 422 com a mensagem,
  Anthropic fora do ar → 503; nos dois casos a chave anterior continua. Não confere saldo da organização. Guardada
  cifrada (AES-GCM com `CHAVE_CIFRA` do ambiente, atrás da interface `Cofre`; depois Secret Manager). `DELETE`
  (admin) desconecta. Conectar e desconectar entram no registro **sem `desfazer`**, por decisão: a receita teria de
  guardar o segredo (desfazer a desconexão). Voltar atrás é `PUT`/`DELETE`.
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
  - `event: texto` `{delta}` · `event: ferramenta` `{nome, nivel: consulta|propoe, resumo}` (o "consultou o orçamento…"),
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
| `consultar_orcamento(obra_id, busca?)` | consulta | `GET /obras/{id}/orcamento` |
| `consultar_cronograma(obra_id, mes?)` | consulta | `GET /obras/{id}/cronograma` (por etapa; `mes` detalha o que está previsto no mês) |
| `consultar_tributos(obra_id)` | consulta | `GET /obras/{id}/tributos` (INSS da aferição e IBS/CBS estimados, com as linhas da conta e o que reduz) |
| `calcular(expressao, unidade_resultado?)` | consulta | nenhuma (calculadora local em Decimal, sem `eval`) |
| `propor_cronograma(obra_id, …)` | propõe | `GET /obras/{id}/orcamento` + `POST /propostas` (cronograma) |

O agente não faz compras, pedidos nem controle de estoque (saíram do produto em 11/2026).

- As ferramentas devolvem números já formatados **e** brutos e datas para gente ("16/11/2026", "ago/2027"); o agente
  nunca faz conta de cabeça: cita o que a API manda pronto (gasto, falta gastar e estouro da etapa, `aditivos_pct`,
  situação da etapa) e qualquer outra conta passa por `calcular`.
- Lembretes de "uma vez por conversa" (orçamento sem preço, data futura num documento) vão em
  `lembretes: {chave: texto}` na primeira vez e só como `lembretes_ja_ditos: {chaves, nota}` depois; o agente lê do
  histórico guardado da sessão o que já foi dito.
- A data de hoje (Brasília) vai como bloco de contexto na mensagem do usuário ("(Hoje é quinta-feira, 05/11/2026, …)"),
  nunca na instrução nem nas ferramentas, que continuam byte a byte iguais entre conversas (cache). As de `propor_*` mandam
  `chave_idempotencia` = `"{sessao_id}:{tool_use_id}"` e, com `ja_existia`, dizem ao modelo que nada novo foi criado.

## Dados de exemplo (seed)

Casa Vila Nova do protótipo: a obra com os dados de cadastro e fiscais (sem orçamento: importa-se a planilha pela tela).
Um usuário `admin` de exemplo, senha só no `.env` local.
