"""Regras de negócio. Contas ficam aqui (código determinístico), nunca no modelo de IA."""
from dataclasses import dataclass

CEM = 10000                       # 100% em centésimos de ponto percentual


class RegraViolada(ValueError):
    """Erro de negócio com mensagem pronta para mostrar ao usuário."""


def cnpj_valido(cnpj: str) -> bool:
    """14 dígitos com os dois verificadores (módulo 11). Sequência repetida ("000…") passa na conta e é recusada."""
    if len(cnpj) != 14 or not cnpj.isdigit() or len(set(cnpj)) == 1:
        return False
    def dv(base: str) -> str:
        pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2][-len(base):]
        r = sum(int(c) * w for c, w in zip(base, pesos, strict=True)) % 11
        return "0" if r < 2 else str(11 - r)
    return cnpj[12] == dv(cnpj[:12]) and cnpj[13] == dv(cnpj[:13])


@dataclass(frozen=True)
class SituacaoEtapa:
    situacao: str                 # concluida | atrasada | adiantada | em_dia | nao_comecou
    previsto: int                 # % previsto acumulado até o mês de referência (centésimos)
    falta: int                    # quanto o medido está abaixo do previsto (centésimos; 0 se não está atrasada)


def situacao_etapa(percentuais: list[int], medido: int, ref: int | None) -> SituacaoEtapa:
    """Situação de uma etapa no mês de referência (o da última medição; `ref` é o índice dele no cronograma).

    É a regra que a tela do cronograma mostrava calculando no navegador; aqui ela é a mesma para a tela e o agente.
    Sem medição (`ref` None) o previsto até a referência é zero. "Adiantada" é o medido acima do previsto, que a tela
    contava como "em dia".
    """
    previsto = 0 if ref is None or ref < 0 else sum(percentuais[: ref + 1])
    if medido >= CEM:
        return SituacaoEtapa("concluida", previsto, 0)
    if medido < previsto:
        return SituacaoEtapa("atrasada", previsto, previsto - medido)
    if not medido and not previsto:
        return SituacaoEtapa("nao_comecou", previsto, 0)
    if medido > previsto:
        return SituacaoEtapa("adiantada", previsto, 0)
    return SituacaoEtapa("em_dia", previsto, 0)
