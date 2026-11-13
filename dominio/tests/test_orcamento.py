"""Regras do orçamento da obra que moram no domínio: andamento da linha de pedido e tópicos da numeração."""
import pytest

from canteiro_dominio.etapas import ativar_topico, montar_arvore
from canteiro_dominio.gastos import gastos_no_orcamento
from canteiro_dominio.modelos import StatusPedido
from canteiro_dominio.orcamento import andamento_da_linha, numero_do_topico, recebido_da_linha, topicos_dos_itens
from canteiro_dominio.regras import RegraViolada


def linha(quantidade=10_000, total=100_00, recebida=0):
    return {"quantidade": quantidade, "total": total, "recebida": recebida}


def test_recebido_pelo_preco_do_pedido():
    assert recebido_da_linha(linha(recebida=2_500)) == 25_00
    assert recebido_da_linha(linha(quantidade=3_000, total=100_00, recebida=1_000)) == 33_33
    assert recebido_da_linha(linha(quantidade=0)) == 0


def test_andamento_da_linha():
    assert andamento_da_linha(StatusPedido.CANCELADO, linha()) == (0, 0, 0)
    assert andamento_da_linha(StatusPedido.EM_COTACAO, linha()) == (100_00, 0, 0)
    assert andamento_da_linha(StatusPedido.APROVADO, linha()) == (0, 100_00, 0)
    assert andamento_da_linha(StatusPedido.PARCIAL, linha(recebida=4_000)) == (0, 100_00, 40_00)
    # entrega a mais também custa: compromete o entregue
    assert andamento_da_linha(StatusPedido.CONFERIDO, linha(recebida=12_000)) == (0, 120_00, 120_00)
    # encerrado com sobra: só o que chegou
    assert andamento_da_linha(StatusPedido.CONFERIDO, linha(recebida=6_000)) == (0, 60_00, 60_00)


# --- tópicos do orçamento ---

def _l(tipo, numero, etapa, descricao):
    return {"tipo": tipo, "numero": numero, "etapa": etapa, "descricao": descricao}


def test_topico_e_o_nivel_2_e_o_mesmo_nome_e_um_so():
    assert [numero_do_topico(n) for n in ("7", "7.1", "7.1.2", "", "A1.2")] == [None, "7.1", "7.1", None, "A1.2"]
    itens = [
        _l("etapa", "7", "7", "PAREDES"),
        _l("etapa", "7.1", "7", "Cerâmica tijolo"),          # título do grupo
        _l("item", "7.1.1", "7", "Tijolo 9 furos"),
        _l("item", "7.1.2", "7", "Argamassa"),
        _l("item", "7.2", "7", "Mão de Obra"),               # o próprio item nomeia o tópico
        _l("etapa", "11", "11", "ESQUADRIAS"),
        _l("item", "11.1", "11", "Portas"),
        _l("item", "11.2", "11", "  portas "),               # mesmo nome (sem caixa e espaço) = o mesmo tópico
        _l("item", "11.6", "11", "Mão de Obra"),             # mesmo nome em outra etapa = outro tópico
        _l("item", "2.1.1", "2", "Escavação"),               # sem a linha 2.1: sem tópico
        _l("item", "", "2", "Solto"),
    ]
    r = topicos_dos_itens(itens)
    assert [(t.etapa, t.nome) for t in r.topicos] == [
        ("7", "Cerâmica tijolo"), ("7", "Mão de Obra"), ("11", "Portas"), ("11", "Mão de Obra")]
    assert {i: (t.etapa, t.nome) for i, t in r.do_item.items()} == {
        2: ("7", "Cerâmica tijolo"), 3: ("7", "Cerâmica tijolo"), 4: ("7", "Mão de Obra"), 6: ("11", "Portas"),
        7: ("11", "Portas"), 8: ("11", "Mão de Obra")}
    assert r.cheias == {}


def test_etapa_com_mais_de_30_topicos_fica_sem():
    itens = [_l("item", f"1.{n}", "1", f"Serviço {n}") for n in range(1, 32)] + [_l("item", "2.1", "2", "Aço")]
    r = topicos_dos_itens(itens)
    assert r.cheias == {"1": 31}
    assert [(t.etapa, t.nome) for t in r.topicos] == [("2", "Aço")] and list(r.do_item) == [31]


def test_arvore_com_topicos_do_orcamento_e_manuais():
    orcamento = [("1", "PRELIMINARES"), ("2", "FUNDAÇÕES")]
    do_orc = [{"etapa": "1", "id": "orc-a", "nome": "Placa"}, {"etapa": "2", "id": "m-1", "nome": "Estacas"},
              {"etapa": "9", "id": "orc-z", "nome": "Fora da árvore"}]
    manuais = [{"id": "m-1", "etapa": "2", "nome": "estacas"}, {"id": "m-2", "etapa": "2", "nome": "Blocos"}]
    a = montar_arvore([], manuais, orcamento, do_orc)
    assert [[(t.id, t.nome, t.origem) for t in e.topicos] for e in a] == [
        [("orc-a", "Placa", "orcamento")],
        # o manual reaproveitado aparece uma vez, como do orçamento; o outro manual vem depois
        [("m-1", "Estacas", "orcamento"), ("m-2", "Blocos", "manual")]]
    # sem orçamento, tópico do orçamento não entra (a base é a lista padrão)
    assert all(t.origem == "manual" for e in montar_arvore([], manuais, (), do_orc) for t in e.topicos)

    # o manual novo não repete o nome do tópico do orçamento, e eles contam no limite
    with pytest.raises(RegraViolada, match="Já existe"):
        ativar_topico({}, {}, id_=None, etapa="1", nome="placa", chaves_base={"1", "2"}, topicos_orcamento=do_orc)
    cheio = [{"etapa": "1", "id": f"orc-{n}", "nome": f"T{n}"} for n in range(30)]
    with pytest.raises(RegraViolada, match="30 tópicos"):
        ativar_topico({}, {}, id_=None, etapa="1", nome="Novo", chaves_base={"1"}, topicos_orcamento=cheio)
    # renomear o manual reaproveitado não esbarra nele mesmo
    assert ativar_topico({}, {"m-1": {"etapa": "2", "nome": "estacas"}}, id_="m-1", etapa="2", nome="Estacas",
                         chaves_base={"1", "2"}, topicos_orcamento=do_orc) == "Estacas"


def test_gastos_avulsos_por_topico():
    gastos = [{"etapa": "1", "topico_id": "orc-a", "valor": 100}, {"etapa": "1", "topico_id": None, "valor": 50},
              {"etapa": "1", "topico_id": "orc-a", "valor": 10, "pedido_id": "p"},     # pagamento de pedido: não soma
              {"etapa": "x-9", "topico_id": "t", "valor": 7}]
    r = gastos_no_orcamento(gastos, {"1"})
    assert r.por_etapa == {"1": 150} and r.por_topico == {"1": {"orc-a": 100, None: 50}}
    assert r.fora_do_orcamento == 7
