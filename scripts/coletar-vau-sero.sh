#!/usr/bin/env bash
# Coleta a tabela VAU do SERO para as 27 UFs e importa na API.
#
# Só lê: abre "Consultar Tabela VAU" (exibirTabelaVAU.jsf), escolhe ano e UF, Buscar, copia a tabela. Roda no portal do
# Maestri em que a PESSOA entrou no e-CAC (login, certificado e captcha são dela; o script não toca neles nem guarda
# sessão). O e-CAC aceita robô em volume aceitável das 8h à meia-noite: são 27 consultas, uma a uma.
#
#   scripts/coletar-vau-sero.sh [ANO] [PORTAL]        # padrão: o ano corrente, portal "Portal"
#   IMPORTAR="docker compose exec -T api python -m canteiro_api.vau" scripts/coletar-vau-sero.sh
set -euo pipefail
ANO="${1:-$(date +%Y)}"
PORTAL="${2:-Portal}"
SAIDA="${SAIDA:-$(mktemp -d)}/vau-sero-$ANO.txt"
IMPORTAR="${IMPORTAR:-docker compose exec -T api python -m canteiro_api.vau}"
UFS=("AC:Acre" "AL:Alagoas" "AP:Amapá" "AM:Amazonas" "BA:Bahia" "CE:Ceará" "DF:Distrito Federal"
     "ES:Espírito Santo" "GO:Goiás" "MA:Maranhão" "MT:Mato Grosso" "MS:Mato Grosso do Sul" "MG:Minas Gerais"
     "PA:Pará" "PB:Paraíba" "PR:Paraná" "PE:Pernambuco" "PI:Piauí" "RJ:Rio de Janeiro" "RN:Rio Grande do Norte"
     "RS:Rio Grande do Sul" "RO:Rondônia" "RR:Roraima" "SC:Santa Catarina" "SP:São Paulo" "SE:Sergipe"
     "TO:Tocantins")

maestri portal navigate "$PORTAL" "https://www3.cav.receita.fazenda.gov.br/sero/exibirTabelaVAU.jsf" >/dev/null
sleep 3
if [[ "$(maestri portal evaluate "$PORTAL" "!!document.querySelector('#uf')")" != "true" ]]; then
  echo "A tela da tabela VAU não abriu: entre no e-CAC pelo portal \"$PORTAL\" e rode de novo." >&2
  exit 1
fi

: > "$SAIDA"
for par in "${UFS[@]}"; do
  sigla="${par%%:*}"; nome="${par#*:}"
  maestri portal select "$PORTAL" "#ano" "$ANO" >/dev/null
  maestri portal select "$PORTAL" "#uf" "$nome" >/dev/null
  maestri portal click "$PORTAL" "#btBuscar" >/dev/null
  # a tabela da consulta anterior fica na tela até a nova chegar: espera a linha da UF pedida
  texto=""
  for _ in $(seq 1 30); do
    sleep 1
    texto="$(maestri portal evaluate "$PORTAL" \
      "(t=>t&&t.innerText.split('\n')[1]?.split('\t')[1]==='$sigla'?t.innerText:'')(document.querySelector('#tabelaIndices'))")"
    [[ -n "$texto" ]] && break
  done
  if [[ -z "$texto" ]]; then
    echo "Sem a tabela de $sigla em 30 s; nada foi importado." >&2
    exit 1
  fi
  printf '%s\n' "$texto" >> "$SAIDA"
  echo "$sigla: $(($(printf '%s\n' "$texto" | wc -l) - 1)) meses"
  sleep 1
done

echo "Tabela salva em $SAIDA"
# o importador recusa o arquivo inteiro se uma coluna ou um valor vier estranho
cat "$SAIDA" | $IMPORTAR /dev/stdin
