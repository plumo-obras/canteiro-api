# Arquitetura do Canteiro (proposta, 30/10/2026)

Ponto de partida: o protótipo (conversa como home, agente que usa ferramentas e propõe ações com OK do usuário,
telas como abas da sessão). Decisões do fundador: **API central em FastAPI**, **MongoDB**, **agente separado
da API**, **eventos via Google Pub/Sub**, **assinatura + consumo de IA por cliente**.

## 1. Visão geral

```
                 ┌──────────────── Web (Next.js) ─────────────────┐       Celular da mestre
                 │ conversa · abas de tela · aprovações           │       (WhatsApp)
                 └───────┬──────────────────────────▲─────────────┘            │
                REST/JSON│                     SSE  │ (tokens do agente)        │ webhook
                         ▼                          │                           ▼
   ┌──────────────────────────────────┐   ┌─────────┴──────────────┐   ┌──────────────────────┐
   │ API CENTRAL (FastAPI)            │   │ SERVIÇO DO AGENTE       │   │ INGESTÃO (workers)   │
   │ domínio: obras, insumos,         │◄──┤ (Python, Anthropic SDK) │   │ WhatsApp · áudio→    │
   │ estoque, pedidos, NF, medições,  │   │ loop do agente + tools  │   │ texto · NF-e · clima │
   │ RDO, equipe, cronograma,         │   │ = endpoints da API      │   │ · rastreio · import  │
   │ propostas, registro, billing     │   │ (via MCP)               │   │   de MS Project      │
   │ RBAC + multi-tenant              │   └────▲───────────┬────────┘   └──────────┬───────────┘
   └────┬─────────────────────┬───────┘        │ consome   │ publica                │ publica
        │ grava               │ outbox → publica│          │                        │
        ▼                     ▼                 │          ▼                        ▼
   ┌──────────┐      ┌──────────────────────────┴──────────────────────────────────────────┐
   │ MongoDB  │      │ Google Pub/Sub: dominio.eventos · agente.tarefas · agente.resultados │
   │ (Atlas)  │      │ (chave de ordenação por obra · dead-letter · idempotência)           │
   └──────────┘      └──────────────────────────────────────────────────────────────────────┘
        GCS (plantas, fotos, NF em PDF/XML) · Cloud Scheduler (briefing, rotinas) · Claude API
```

### Dois caminhos para o agente: não ponha a conversa na fila

| Caminho | Quando | Como |
|---|---|---|
| **Interativo** (a pessoa pergunta) | Conversa, "Perguntar sobre isto" | A web chama o serviço do agente direto (HTTP), que **transmite a resposta por SSE** enquanto usa as ferramentas. Fila aqui só soma latência e complica o streaming. |
| **Assíncrono / proativo** (o mundo muda) | NF chegou, rastreio andou, áudio da mestre, clima virou, 07:00 do briefing | A API publica o evento no Pub/Sub; o agente consome, decide se vale agir e devolve **propostas** (que aparecem no briefing e em Aprovações) por `agente.resultados` → API. |

É exatamente o desenho do protótipo: o agente **propõe** e a API **grava** só com o OK.

## 2. API central (FastAPI)

- **Dona de todo dado e de toda regra.** Contas determinísticas ficam aqui, nunca no LLM:
  `falta = precisa − saldo − a caminho − cotando − transferência`, curva ABC, medição pago × executado, janelas de clima.
- **Escrita do agente = proposta.** O agente nunca grava direto: chama `POST /propostas` (o que quer fazer, a
  conta, a fonte). A pessoa aprova na UI → a API executa e registra no log (quem, quando, origem: agente, WhatsApp,
  tela) → "Desfazer" é uma operação inversa registrada.
- **Multi-tenant e RBAC no servidor:** `tenant_id` em todo documento e todo índice; papel resolvido no banco, não
  no token. O agente age **com o token do usuário** (ou um token de serviço com escopo do tenant e do papel), então
  nunca enxerga nem faz mais do que a pessoa poderia.
- **Eventos com outbox:** a mudança e o evento são gravados juntos (mesma transação Mongo); um relay publica no
  Pub/Sub. Sem isso, um crash entre "gravou" e "publicou" faz o agente perder eventos.
- **Contrato público:** OpenAPI → gera o SDK do front e as ferramentas do agente (ver §4).

## 3. MongoDB agora, Firestore (Firebase) depois

**Decisão (30/10/2026): o banco vai migrar para o Firestore, então tudo precisa ser compatível desde já.** O código fala
só com a porta `canteiro_dominio.armazenamento`, que aceita apenas o que o Firestore aceita: igualdade e `in` (≤ 30),
desigualdade em um campo, ordenação começando por esse campo, limite; sem joins, regex ou aggregation pipeline;
unicidade e idempotência pelo ID do documento; transação com leituras antes das escritas (≤ 500); dinheiro e quantidade
em inteiros (centavos, milésimos); IDs UUIDv7 nossos; nenhuma lista que cresce dentro de um documento. A migração vira
"escrever o adaptador Firestore e passar na mesma suíte de testes de contrato" que hoje roda contra memória e Mongo.
Busca vetorial (base de conhecimento) e eventos ficam atrás de interfaces próprias, porque Atlas e Firestore diferem ali.

### Cuidados que valem nos dois bancos

Documentos encaixam bem em obra, sessão de conversa, RDO, NF (XML vira documento), planta/metadados, cronograma importado.
O cuidado está no que é **dinheiro e estoque**:

1. **Estoque e dinheiro como livro-razão, não como campo editado.** `movimentos` append-only (entrada, saída com
   destino, transferência, ajuste de contagem) com chave de idempotência; o saldo é uma **projeção** (coleção
   `saldos` atualizada na mesma transação, ou recalculável a partir dos movimentos). É o que torna o "Desfazer" e a
   auditoria triviais e impede saldo corrompido por evento repetido.
2. **Transações multi-documento** (replica set / Atlas) quando um ato mexe em várias coleções: aprovar uma
   transferência baixa numa obra e dá entrada em outra.
3. **Relatórios** (custo por etapa, curva ABC, consumo × previsto) por aggregation pipeline + projeções
   pré-calculadas por obra; se um dia a parte financeira exigir muito join, ela pode ir para Postgres sem mudar o resto.

Bônus: **Atlas Vector Search** para buscar dentro do memorial descritivo, contratos e PDFs da obra.

Coleções iniciais: `tenants`, `usuarios`, `obras`, `insumos`, `movimentos`, `saldos`, `pedidos`, `nfs`,
`medicoes`, `contratos`, `equipe`, `rdos`, `cronogramas`, `propostas`, `registro` (auditoria), `sessoes`
(conversa + abas abertas), `eventos_outbox`, `uso_ia` (livro de consumo).

## 4. O agente em 2026: o que mudou desde a "cascata de prompts" no LangGraph

Com os modelos atuais, o padrão de mercado deixou de ser grafo de prompts encadeados e passou a ser **um loop de
agente com boas ferramentas**: o modelo decide quais ferramentas chamar e em que ordem, e o código controla
permissões, aprovação e custo. Grafo explícito continua útil só onde o fluxo é **determinístico** (ex.: pipeline de
conferência de NF), e aí nem precisa de LLM em cada passo.

As quatro formas de montar, do mais controle ao menos infraestrutura:

| Opção | Você escreve | Quem roda o loop / hospeda | Encaixe no Canteiro |
|---|---|---|---|
| **Claude API + Tool Runner** (SDK oficial em Python) | as funções-ferramenta | o SDK roda o loop; **você hospeda** (Cloud Run) | **Recomendado para começar.** Ganchos por turno para aprovação, log, interceptar erro e medir custo; ferramentas são chamadas HTTP à API central |
| Claude API, loop manual | o loop inteiro | você | Só se precisar de um controle que o Tool Runner não dá |
| **Managed Agents** (Anthropic hospeda o loop e um sandbox por sessão) | config do agente + resultado das suas ferramentas | Anthropic | Forte candidato para a parte **proativa**: agendamentos (o briefing das 07:00 sem cron próprio), memória entre sessões, **orçamento em dólar por sessão** (trava de custo por cliente), cofre de credenciais. Ainda em beta |
| LangGraph | o grafo | você | Faz sentido se quiser trocar de provedor de modelo com frequência; hoje adiciona camada sem ganho claro para um agente de ferramentas |

**Ferramentas via MCP.** Exponha as ferramentas da API central como um **servidor MCP** (gerado do OpenAPI, com o
token do usuário). O seu agente usa; e o cliente também pode usar o Canteiro de dentro do Claude/ChatGPT dele (um
concorrente já anunciou isso). Uma ferramenta por intenção de negócio, não por endpoint:
`consultar_estoque(obra, insumo)`, `conta_de_falta(obra, insumo)`, `propor_pedido(...)`, `propor_remarcacao(...)`,
`buscar_preco(termo, cep)`, `janelas_de_clima(obra, atividade)`, `gerar_rdo(obra, dia)`.

**Escolha de modelo.** Modelos atuais e preço por milhão de tokens (entrada / saída):
Claude Opus 5.5 ($4 / $20) · Claude Sonnet 5.5 ($2 / $10) · Claude Haiku 5.5 ($0,10 / $0,50) · Claude Fable 5.1
($10 / $50, o mais capaz). O padrão do guia é o Opus 5.5 com esforço ajustado por rota. Usar Sonnet ou Haiku em rotas
simples (classificar um áudio já transcrito, extrair itens de uma NF) é decisão de custo a medir com um conjunto de
testes, não um default.

**O que o Claude não faz e precisa de peça própria:** **áudio** (a mestre manda áudio no WhatsApp) → um serviço de
transcrição antes do agente; **DWG** → conversão (ver `pesquisa/integracoes.md`).

## 5. Eventos (Pub/Sub)

- Tópicos: `dominio.eventos` (pedido.criado, nf.recebida, estoque.movimento, rastreio.atualizado,
  clima.janela_mudou, whatsapp.mensagem…), `agente.tarefas` (o que o agente deve analisar) e `agente.resultados`
  (propostas, documentos gerados).
- **Chave de ordenação = obra**, para eventos da mesma obra chegarem em ordem. **Idempotência** pelo `event_id`
  (Pub/Sub entrega pelo menos uma vez). Dead-letter com alerta.
- **Nem todo evento vira chamada de modelo.** Um filtro barato (regra ou modelo pequeno) decide se vale acordar o
  agente; a maioria dos eventos só atualiza projeções. É a maior economia de token do sistema.

## 6. Cobrança: assinatura + consumo de IA

1. **Livro de consumo (`uso_ia`)**: toda chamada ao modelo grava `tenant_id`, usuário, sessão, rota
   (conversa, briefing, WhatsApp, NF), modelo e o `usage` devolvido pela API (tokens de entrada, de saída, de leitura e
   escrita de cache) e o custo calculado. É a fonte da verdade da fatura; o relatório de custo da Anthropic serve para
   conciliar o total.
2. **Venda em "créditos de IA" em reais, não em tokens.** Token confunde o cliente e o preço por token muda. Ex.:
   plano com franquia mensal de créditos, excedente por pacote; margem sobre o custo real.
3. **Travas por cliente:** checar o saldo de créditos **antes** de chamar o modelo; orçamento de tokens por tarefa
   (`task_budget`) nos loops longos; nos Managed Agents, orçamento em dólar por sessão. Alerta a 80% da franquia.
4. **Pagamento no Brasil:** Pix e boleto pedem um gateway local (Asaas, Iugu, Pagar.me) ou o Stripe com Pix;
   cobrança medida = fechar o mês do livro e lançar o excedente.

**Alavancas de custo** (o que mais pesa na margem):
- **Cache de prompt**: prompt de sistema e lista de ferramentas estáveis no início; contexto da obra depois do
  ponto de cache. Leitura de cache custa uma fração da entrada.
- **Batch API (50% mais barato)** para o que não é ao vivo: briefing noturno de todas as obras, RDO do dia, resumo semanal.
- **Esforço por rota**: baixo em chat simples, alto em planejamento.
- **Compactação** em sessões longas.
- **Conta no código, não no modelo** (falta, ABC, janelas de clima): mais barato e sem erro.

## 7. Segurança

- O agente herda as permissões de quem pergunta; escrita só por proposta aprovada.
- **Injeção de prompt**: texto de WhatsApp, NF, site de loja e planta é **dado**, nunca instrução; as ferramentas
  validam tudo do lado da API.
- Segredos (WhatsApp, provedor de NF, Anthropic) num cofre (Secret Manager); nada no front.
- Dados no Brasil quando possível (Atlas e Cloud Run em `southamerica-east1`); LGPD para dados de equipe (ASO, documentos).

## 8. Infra sugerida (GCP, já que o Pub/Sub é do Google)

Cloud Run (API, agente, workers) · MongoDB Atlas na GCP · Pub/Sub · Cloud Scheduler · GCS · Secret Manager ·
OpenTelemetry para rastrear cada pedido do usuário até as chamadas ao modelo. O Claude pode ser chamado pela API da
Anthropic ou pela Google Cloud: confirme a disponibilidade de cada recurso (Managed Agents, Batch, cache) na
plataforma escolhida antes de fechar.

## 9. Fases

1. **MVP:** API central (obras, insumos, movimentos, pedidos, propostas, registro) + serviço do agente com Tool
   Runner e SSE + livro de consumo + login. Ingestão: WhatsApp (com transcrição) e importação de planilha.
2. **Proativo:** Pub/Sub + filtro de eventos + briefing em Batch (ou Managed Agents agendado) + NF-e destinada.
3. **Integrações:** MS Project (MPXJ), clima, busca de preço real (servidor + provedor), rastreio, planta (ver e marcar).
4. **Escala:** servidor MCP público para os clientes, Vector Search nos documentos, ERP de obra.

## 11. Chave própria × créditos da plataforma

O cliente escolhe: **créditos do Canteiro** (padrão: a fatura da plataforma traz assinatura mensal/anual + consumo de
IA) ou **chave própria da Anthropic** (o modelo é cobrado na conta dele; a fatura do Canteiro traz só a assinatura, e o
consumo aparece como informativo).

- O **livro de consumo** é o mesmo nos dois casos: cada chamada grava cliente, obra, rota, tokens e custo. A fatura
  mensal e o detalhamento **por dia, por obra e por função** (conversa, briefing, WhatsApp, NF) saem dele.
- Chave própria: cifrada num cofre (Secret Manager), usada só no serviço do agente, **testada ao conectar**.
- Com o loop no nosso Cloud Run, trocar de chave é trocar o cliente da chamada. No Managed Agents, agentes e sessões
  moram na organização da chave, então a chave própria exigiria manter o agente dentro da conta de cada cliente:
  mais um motivo para começar pelo loop próprio.
- Revenda: confirmar nos Termos Comerciais da Anthropic (ou com o time comercial) as condições para repassar consumo com margem.

## 12. Como o agente roda (loop com ferramentas, não grafo de prompts)

Um Cloud Run nosso recebe o evento (ou a pergunta). Dentro dele:

1. **Roteador em código, sem LLM:** pelo tipo de evento escolhe as instruções e o subconjunto de ferramentas do caso
   (ex.: `nf.recebida` → "conferência de recebimento").
2. **Loop do agente** (Tool Runner do SDK): o modelo pede ferramentas (`buscar_pedido`, `ler_nf`,
   `buscar_conhecimento`…), o nosso código executa contra a API central e devolve o resultado, até o modelo terminar.
3. **Saída = propostas** (`propor_abatimento`, `propor_pedido`…), nunca gravação direta.

O grafo encolhe para o roteador do passo 1. Fluxos 100% previsíveis continuam em código comum, e o modelo entra só
onde há julgamento. No Managed Agents muda só quem roda o loop; as ferramentas continuam rodando do nosso lado.

## 13. Base de conhecimento

| Tipo | Exemplos | Acesso do agente |
|---|---|---|
| Dado estruturado e vivo | estoque, pedidos, NF, medições, cronograma | **sempre por ferramenta da API** (nunca pela base vetorial) |
| Conhecimento em texto | projeto, memorial, contratos, RDOs antigos, procedimentos, histórico de problemas | RAG por `buscar_conhecimento(pergunta, obra, tipo)` → trechos **com fonte** (doc, página) para citar |
| Memória aprendida | "a Joana sempre pede palete", "este fornecedor atrasa" | memória por cliente (ferramenta de memória do Claude ou armazenamento próprio) |

- Onde: começar pelo **Atlas Vector Search** (mesmo banco, filtro por cliente/obra na mesma consulta). Alternativa:
  serviço gerenciado do Google (Vertex AI RAG). Embeddings de terceiros (a Anthropic recomenda o Voyage AI; ou o do Google).
- Documento pequeno (um memorial de 40 páginas) pode ir **inteiro no contexto** (1M tokens, com cache), sem RAG.
- **Isolamento por cliente e obra em toda busca, no servidor.** Normas ABNT têm direitos (não indexar sem licença);
  SINAPI é pública. Texto da base é dado, nunca instrução.

## 10. Decisões em aberto

- Agente proativo no seu Cloud Run (Tool Runner + Scheduler) ou no Managed Agents (agendamento, memória e trava de custo prontos, mas beta e mais acoplado).
- Claude pela API da Anthropic ou pela Google Cloud (faturamento único na GCP × recursos disponíveis).
- Gateway de pagamento (Pix/boleto) e o formato do plano (franquia de créditos × pacote).
- Serviço de transcrição de áudio.
- Base de conhecimento: Atlas Vector Search ou serviço gerenciado do Google; provedor de embeddings.
- Preço da assinatura com chave própria × com créditos.
