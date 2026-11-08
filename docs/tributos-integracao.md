# Tributos: de onde vêm os dados da Receita

A conta do INSS da obra (`canteiro_dominio.tributos`) segue a IN RFB 2.021/2021 e o Manual do SERO. Ela precisa de
três dados públicos e de dados que só a Receita tem. Este documento diz como cada um chega e o que ainda falta.

## Dados públicos (automáticos)

| Dado | Fonte | Como atualizar |
|---|---|---|
| Tabela VAU (R$/m² por UF, mês e destinação) | SERO, "Consultar Tabela VAU" (precisa de login no e-CAC) | `scripts/coletar-vau-sero.sh` (abaixo) |
| Selic acumulada no mês (correção do art. 31) | Banco Central, SGS série 4390 (API pública) | `python -m canteiro_api.selic` |
| Anexo I (uso de concreto usinado por UF) | IN RFB 2.021/2021 | fixo no domínio; muda só com a IN |

O VAU que vale é o do **mês da aferição** (art. 25 §5), não o do mês do gasto: basta manter o ano corrente.

### Coletar a tabela VAU

O e-CAC bloqueia navegador automatizado ("acesso bloqueado por possuir atributos que o caracteriza como um acesso
automatizado"). Não contornamos: a coleta roda no portal do Maestri em que a **pessoa** entrou no e-CAC (gov.br,
certificado e captcha são dela; o script não guarda sessão). Só lê; o e-CAC aceita robô em volume aceitável das 8h à
meia-noite, e são 27 consultas, uma a uma.

```bash
scripts/coletar-vau-sero.sh            # ano corrente, portal "Portal", importa no contêiner da API
scripts/coletar-vau-sero.sh 2026 Portal
```

Se a sessão caiu, a busca do SERO fica no carregamento e o script para em 30 s sem importar nada: entre de novo no
e-CAC pelo portal e rode outra vez. O importador recusa o arquivo inteiro se uma coluna ou valor vier estranho.

Rotinas sugeridas (criar no canvas; uma vez por semana, porque a importação é idempotente):

```bash
maestri routine create "Selic do INSS da obra" --weekly mon@09:00 \
  --command "Rode 'cd ~/canteiro/canteiro-api && docker compose exec -T api python -m canteiro_api.selic' e diga se entrou mês novo."
maestri routine create "Tabela VAU do SERO" --weekly mon@10:00 \
  --command "Rode 'cd ~/canteiro/canteiro-api && scripts/coletar-vau-sero.sh'. Se a sessão do e-CAC caiu, não contorne login nem captcha: me avise para entrar de novo."
```

## Dados da obra na Receita (ainda manuais)

| Dado | Onde está | Hoje na plataforma |
|---|---|---|
| Remuneração declarada (folha, autônomo, MEI) vinculada ao CNO | eSocial + DCTFWeb | a pessoa classifica o gasto (`folha`, `autonomo_mei`) |
| Folha declarada pelas empreiteiras no CNO | eSocial/DCTFWeb da empreiteira | estimada em 40% da nota com o CNO (`mao_de_obra_nf` + `nf_cno`) |
| DCTFWeb entregue todo mês (fator de ajuste) | DCTFWeb | `dctfweb_em_dia` informado na obra |
| Áreas, destinação, datas da obra | CNO | dados fiscais da obra |
| Aferição já feita (para conferir a nossa conta) | SERO, "Consultar Aferição" | não lida |

### Integra Contador (Serpro)

A API oficial para escritórios de contabilidade. O catálogo (119 serviços em 28 sistemas, consultado em 11/2026)
**não tem SERO, CNO nem eSocial**. Da DCTFWeb tem: consultar recibo (`CONSRECIBO32`), declaração completa
(`CONSDECCOMPLETA33`) e XML (`CONSXMLDECLARACAO38`), gerar guia (`GERARGUIA31`) e transmitir (`TRANSDECLARACAO310`).
Exige certificado e-CNPJ do escritório, contrato na Loja Serpro (cobrança por consulta) e procuração eletrônica do
contribuinte no e-CAC (código 00103 para a DCTFWeb).

Serve, então, para **conferir se a DCTFWeb foi entregue em cada mês** (o `dctfweb_em_dia`) e ler o que ela declarou,
quando o cliente tiver um contador com e-CNPJ. CNO, SERO e a remuneração do eSocial continuam só pelo e-CAC.

### Conector (próximo passo)

O mesmo modelo do Peritus: navegador visível, login feito pela pessoa, sessão só em memória, nada de senha ou cookie
gravado. A coleta do VAU já funciona assim (pelo portal do Maestri). Falta mapear, com um CPF ou CNPJ que **tenha obra no
CNO**, as telas "Consultar obra" (CNO) e "Consultar Aferição" (SERO), para puxar áreas, datas e o valor que a
Receita apurou, e comparar com a nossa conta.

## O que precisa de revisão de contador

Os percentuais estão em `PARAMETROS_INSS`, cada um com o artigo da IN. O ponto mais incerto é a nota de empreiteira:
o SERO abate a folha que a empreiteira declarou no CNO, e a plataforma estima essa folha em 40% da nota (o percentual
do art. 22, que a IN usa na aferição por nota fiscal).
