"""O encaixe da planilha da pessoa na lista padrão de etapas."""
import pytest

from canteiro_dominio.cronograma import SERVICOS_POR_FASE
from canteiro_dominio.encaixe import (
    etapa_da_estimativa,
    etapa_no_contexto,
    etapa_pelo_nome,
    fase_da_etapa,
    nome_da_etapa,
    ordem_da_etapa,
)
from canteiro_dominio.etapas import CHAVES_FIXAS


@pytest.mark.parametrize(("descricao", "titulo", "esperada"), [
    # a planilha de quem orça à mão: o título diz a etapa, a linha é genérica
    ("Materiais diversos", "PAREDES E PAINES", "alvenaria"),
    ("Mão de Obra", "PAREDES E PAINES", "alvenaria"),
    ("Mão de Obra", "ESQUADRIA METÁLICA", "esquadrias"),
    ("Portas", "ESQUADRIA MADEIRA", "esquadrias"),
    # a linha que diz o que é fica na etapa dela, mesmo numa etapa mista
    ("Pintura látex", "ACABAMENTOS", "pintura"),
    ("Porcelanato", "ACABAMENTOS", "pisos"),
    # concreto e armação dentro de uma etapa de fundação são fundação
    ("Concreto fck 25 MPa", "FUNDAÇÕES", "fundacao"),
    ("Armação de aço CA-60 5,0mm", "FUNDAÇÕES", "fundacao"),
    ("Concreto fck 25 MPa", "SUPERESTRUTURA", "estrutura"),
    ("Escavação", "INFRAESTRUTURA", "fundacao"),
    # o mais específico vence o genérico
    ("Alvenaria de bloco de concreto", "", "alvenaria"),
    ("Revestimento cerâmico de parede", "", "revestimento_interno"),
    ("Revestimento cerâmico de piso", "", "pisos"),
    ("Pintura de parede", "", "pintura"),
    ("Impermeabilização do baldrame", "", "impermeabilizacao"),
    ("Tapume de obra", "7 PAREDES", "preliminares"),
    ("Limpeza do terreno", "", "preliminares"),
    ("Tubo PVC 100mm", "INSTALAÇÕES", "hidrossanitaria"),
    ("Fiação 2,5mm", "INSTALAÇÕES", "eletrica"),
    ("Bacia sanitária", "LOUÇAS E METAIS", "hidrossanitaria"),
    # palavra inteira: "gasto" não é gás
    ("Central de gás", "", "especiais"),
    ("Gastos gerais", "DIVERSOS", "outros"),
    # "Materiais diversos" é linha genérica, não "Outros": herda o título
    ("Materiais diversos", "ESQUADRIA MADEIRA", "esquadrias"),
    ("Mão de obra", "", "outros"),
])
def test_etapa_no_contexto(descricao, titulo, esperada):
    assert etapa_no_contexto(descricao, titulo) == esperada


def test_nome_da_etapa_padrao_decide_antes_das_palavras():
    assert etapa_pelo_nome("Pisos") == "pisos"
    assert etapa_pelo_nome("Bancadas, soleiras e peitoris") == "bancadas"
    assert etapa_pelo_nome("   ") is None
    assert etapa_pelo_nome("Mão de obra") is None


def test_ordem_e_nome_sao_os_da_lista():
    assert ordem_da_etapa("terreno") == 0 and ordem_da_etapa("outros") == len(CHAVES_FIXAS) - 1
    assert nome_da_etapa("alvenaria") == "Alvenaria e vedações"


def test_toda_estimativa_da_previsao_cai_numa_etapa_da_mesma_fase():
    for fase, servicos in SERVICOS_POR_FASE.items():
        for descricao, *_ in servicos:
            etapa = etapa_da_estimativa(fase, descricao)
            assert etapa in CHAVES_FIXAS and fase_da_etapa(etapa) == fase, (fase, descricao, etapa)
    assert etapa_da_estimativa("Instalações", "Instalação elétrica (pontos e fiação)") == "eletrica"
    assert etapa_da_estimativa("Revestimento", "Contrapiso") == "pisos"
