from datetime import date

import pytest

from canteiro_dominio.etapas import (
    CHAVES_FIXAS,
    ETAPAS_FIXAS,
    MAX_EXTRAS_POR_OBRA,
    MAX_NOME,
    MAX_TOPICOS_POR_ETAPA,
    OUTROS,
    PREFIXO_EXTRA,
    ativar_extra,
    ativar_topico,
    chave_extra,
    indice_da_arvore,
    montar_arvore,
    nome_da_etapa,
    nomes_da_base,
    origem_da_etapa,
    validar_classificacao,
    validar_nome,
)
from canteiro_dominio.gastos import (
    gastos_no_orcamento,
    soma_nos_gastos_avulsos,
    validar_gasto,
    validar_reclassificacao,
)
from canteiro_dominio.leitura import (
    AVISO_A_PAGAR,
    AVISO_INCERTO,
    LeituraComprovante,
    centavos_lidos,
    montar_revisao,
)
from canteiro_dominio.modelos import Comprovante, Gasto, StatusComprovante
from canteiro_dominio.regras import RegraViolada

HOJE = date(2026, 11, 5)
CNPJ = "11222333000181"   # válido, inventado


def arvore():
    extras = [{"id": "e1", "nome": "Muro de arrimo"}, {"id": "e2", "nome": "Antiga", "arquivado": True}]
    topicos = [
        {"id": "t1", "etapa": "eletrica", "nome": "Quadro geral"},
        {"id": "t2", "etapa": "eletrica", "nome": "Arquivado", "arquivado": True},
        {"id": "t3", "etapa": "x-e1", "nome": "Mão de obra"},
    ]
    return montar_arvore(extras, topicos)


def campo(certeza, *valores):
    return {"certeza": certeza, "opcoes": [{"valor": v, "motivo": f"motivo de {v}"} for v in valores]}


def leitura(**kw):
    base = {
        "tipo_documento": "comprovante_pix",
        "situacao_pagamento": "pago",
        "valor": campo("alta", "1.234,56"),
        "data_pagamento": campo("alta", "2026-11-03"),
        "favorecido": campo("alta", "João da Silva"),
        "cnpj_favorecido": None,
        "descricao": campo("alta", "mão de obra do eletricista"),
        "classificacao": {"certeza": "alta", "opcoes": [
            {"etapa": "eletrica", "topico_id": "t1", "motivo": "legenda diz quadro"}]},
        "observacoes": None,
    }
    return LeituraComprovante.model_validate({**base, **kw})


def revisao(leit=None, **kw):
    return montar_revisao(leit if leit is not None else leitura(**kw), arvore=arvore(), hoje=HOJE,
                          fornecedores=[{"id": "f1", "nome": "Elétrica Central", "cnpj": CNPJ}])


# --- etapas fixas ---

def test_chaves_fixas_nunca_mudam():
    # gastos gravados apontam para estas chaves: mudar ou tirar uma deixa gasto órfão. Só se acrescenta (antes de
    # "outros") — e aí este teste muda junto, de propósito.
    assert [e.chave for e in ETAPAS_FIXAS] == [
        "terreno", "projetos", "preliminares", "fundacao", "estrutura", "alvenaria", "hidrossanitaria", "eletrica",
        "especiais", "impermeabilizacao", "cobertura", "esquadrias", "revestimento_interno", "fachada", "pisos",
        "bancadas", "pintura", "elevadores", "equipamentos", "administracao", "pessoal", "limpeza", "vendas", "outros",
    ]


def test_chave_fixa_nao_colide_com_extra():
    assert len(CHAVES_FIXAS) == len(ETAPAS_FIXAS)
    assert not any(c.startswith(PREFIXO_EXTRA) for c in CHAVES_FIXAS)
    assert all(e.nome and e.inclui for e in ETAPAS_FIXAS)


def test_arvore_poe_extras_antes_de_outros_e_tira_arquivados():
    a = arvore()
    chaves = [e.chave for e in a]
    assert chaves[-1] == OUTROS
    assert chaves[-2] == chave_extra("e1")
    assert "x-e2" not in chaves
    assert len(a) == len(ETAPAS_FIXAS) + 1
    eletrica = next(e for e in a if e.chave == "eletrica")
    assert [t.nome for t in eletrica.topicos] == ["Quadro geral"]
    extra = next(e for e in a if e.chave == "x-e1")
    assert not extra.fixa and [t.id for t in extra.topicos] == ["t3"]


def test_arvore_ordena_pela_criacao():
    from datetime import UTC, datetime
    t = [{"id": "b", "etapa": "pisos", "nome": "Segundo", "criado_em": datetime(2026, 2, 1, tzinfo=UTC)},
         {"id": "a", "etapa": "pisos", "nome": "Primeiro", "criado_em": datetime(2026, 1, 1, tzinfo=UTC)}]
    pisos = next(e for e in montar_arvore([], t) if e.chave == "pisos")
    assert [x.nome for x in pisos.topicos] == ["Primeiro", "Segundo"]


def test_nome_arrumado_e_sem_repetir():
    assert validar_nome("  Bloco   B ") == "Bloco B"
    with pytest.raises(RegraViolada, match="Dê um nome"):
        validar_nome("   ")
    with pytest.raises(RegraViolada, match=str(MAX_NOME)):
        validar_nome("x" * (MAX_NOME + 1))
    with pytest.raises(RegraViolada, match="Já existe: Instalações elétricas"):
        validar_nome("instalacoes  ELETRICAS", ja_usados=["Fundação", "Instalações elétricas"])


def test_classificacao_exige_etapa_da_obra_e_topico_dela():
    a = arvore()
    validar_classificacao(a, "eletrica", "t1")
    validar_classificacao(a, "x-e1", None)
    with pytest.raises(RegraViolada, match="Escolha uma etapa"):
        validar_classificacao(a, "x-e2", None)          # arquivada
    with pytest.raises(RegraViolada, match="não está na etapa Fundação"):
        validar_classificacao(a, "fundacao", "t1")
    with pytest.raises(RegraViolada):
        validar_classificacao(a, "eletrica", "t2")      # tópico arquivado


# --- revisão: os três estados ---

def test_sem_leitura_abre_tudo_vazio():
    r = montar_revisao(None, arvore=arvore(), hoje=HOJE)
    assert {r.valor.estado, r.data_pagamento.estado, r.favorecido.estado, r.descricao.estado,
            r.classificacao.estado} == {"vazio"}
    assert r.avisos == []


def test_certeza_alta_vem_lida_e_marcada():
    r = revisao()
    assert r.valor.estado == "lido" and r.valor.escolhido == 0
    assert r.valor.opcoes[0].valor == 123456 and r.valor.opcoes[0].rotulo == "R$ 1.234,56"
    assert r.data_pagamento.opcoes[0].valor == "2026-11-03" and r.data_pagamento.opcoes[0].rotulo == "03/11/2026"
    assert r.classificacao.estado == "lido"
    assert r.classificacao.opcoes[0].rotulo == "Instalações elétricas › Quadro geral"
    assert r.avisos == []


def test_duvida_mostra_opcoes_sem_nenhuma_marcada():
    r = revisao(valor=campo("baixa", "1.234,56", "1.200,00"))
    assert r.valor.estado == "conferir" and r.valor.escolhido is None
    assert [o.valor for o in r.valor.opcoes] == [123456, 120000]
    assert r.valor.opcoes[1].motivo == "motivo de 1.200,00"


def test_certeza_alta_com_varias_opcoes_e_contradicao():
    r = revisao(favorecido=campo("alta", "João", "Maria"))
    assert r.favorecido.estado == "conferir" and r.favorecido.escolhido is None


def test_opcao_ilegivel_cai_e_sem_nenhuma_fica_vazio():
    r = revisao(valor=campo("baixa", "abc", "350,00"), data_pagamento=campo("alta", "31/02/2026"))
    assert [o.valor for o in r.valor.opcoes] == [35000]
    assert r.data_pagamento.estado == "vazio" and r.data_pagamento.opcoes == []


def test_alta_com_unica_opcao_ilegivel_nao_vira_lido():
    assert revisao(valor=campo("alta", "0,00")).valor.estado == "vazio"


def test_no_maximo_tres_opcoes_sem_repetir():
    r = revisao(descricao=campo("baixa", "cimento", " cimento ", "areia", "brita", "cal"))
    assert [o.valor for o in r.descricao.opcoes] == ["cimento", "areia", "brita"]


@pytest.mark.parametrize("texto, centavos", [
    ("1.234,56", 123456), ("R$ 1.234", 123400), ("12.50", 1250), ("350", 35000), ("1.234.567,89", 123456789),
    ("0,00", None), ("-5,00", None), ("abc", None), ("", None), ("NaN", None),
])
def test_valor_como_vem_no_comprovante(texto, centavos):
    assert centavos_lidos(texto) == centavos


def test_data_no_futuro_ou_antiga_vai_para_conferir():
    r = revisao(data_pagamento=campo("alta", "2026-12-01"))
    assert r.data_pagamento.estado == "conferir" and r.data_pagamento.escolhido is None
    assert "A data lida é depois de hoje: confira." in r.avisos
    r = revisao(data_pagamento=campo("alta", "2024-11-03"))
    assert r.data_pagamento.estado == "conferir"
    assert "A data lida tem mais de um ano: confira." in r.avisos


# --- revisão: classificação ---

def test_etapa_inventada_cai_e_topico_inventado_deixa_a_etapa():
    r = revisao(classificacao={"certeza": "baixa", "opcoes": [
        {"etapa": "marcenaria", "topico_id": None, "motivo": "inventada"},
        {"etapa": "eletrica", "topico_id": "nao-existe", "motivo": "tópico inventado"},
        {"etapa": "eletrica", "topico_id": "t2", "motivo": "tópico arquivado"},
        {"etapa": "fundacao", "topico_id": "t1", "motivo": "tópico de outra etapa"},
    ]})
    assert [(o.etapa, o.topico_id) for o in r.classificacao.opcoes] == [("eletrica", None), ("fundacao", None)]
    assert r.classificacao.opcoes[0].rotulo == "Instalações elétricas"
    assert r.classificacao.estado == "conferir"


def test_classificacao_em_etapa_extra():
    r = revisao(classificacao={"certeza": "alta", "opcoes": [
        {"etapa": "x-e1", "topico_id": "t3", "motivo": "muro"}]})
    assert r.classificacao.estado == "lido"
    assert r.classificacao.opcoes[0].rotulo == "Muro de arrimo › Mão de obra"


# --- revisão: fornecedor pelo CNPJ e avisos ---

def test_cnpj_de_fornecedor_cadastrado_vira_a_opcao_marcada():
    r = revisao(favorecido=campo("baixa", "ELETRICA CENTRAL COMERCIO LTDA"), cnpj_favorecido="11.222.333/0001-81")
    assert r.favorecido.estado == "lido" and r.favorecido.escolhido == 0
    assert r.favorecido.opcoes[0].fornecedor_id == "f1"
    assert r.favorecido.opcoes[0].motivo == "CNPJ 11.222.333/0001-81, fornecedor cadastrado"
    assert r.favorecido.opcoes[1].valor == "ELETRICA CENTRAL COMERCIO LTDA"


def test_cnpj_invalido_ou_desconhecido_nao_muda_nada():
    assert revisao(cnpj_favorecido="11222333000182").favorecido.opcoes[0].fornecedor_id is None
    assert revisao(cnpj_favorecido="11444777000161").favorecido.opcoes[0].fornecedor_id is None


def test_documento_que_nao_comprova_pagamento_avisa():
    assert revisao(situacao_pagamento="a_pagar").avisos[0] == AVISO_A_PAGAR
    assert revisao(situacao_pagamento="incerto").avisos[0] == AVISO_INCERTO


def test_esquema_da_leitura_sai_para_o_agente():
    # o agente manda este esquema como saída estruturada do modelo: sem limites de tamanho que ela não aceita
    esquema = str(LeituraComprovante.model_json_schema())
    assert "maxLength" not in esquema and "maxItems" not in esquema and "minimum" not in esquema


# --- gasto ---

def confirmar(**kw):
    base = dict(valor=35000, data_pagamento="2026-11-03", favorecido=" João  da Silva ", descricao="mão de obra",
                etapa="eletrica", topico_id="t1", arvore=arvore(), hoje=HOJE)
    return validar_gasto(**{**base, **kw})


def test_gasto_confirmado_volta_com_textos_arrumados():
    g = confirmar()
    assert g.favorecido == "João da Silva" and g.descricao == "mão de obra"


@pytest.mark.parametrize("mudanca, mensagem", [
    ({"valor": 0}, "maior que zero"),
    ({"data_pagamento": "03/11/2026"}, "Informe a data"),
    ({"data_pagamento": "2026-11-06"}, "depois de hoje"),
    ({"favorecido": "  "}, "quem recebeu"),
    ({"descricao": ""}, "o que foi pago"),
    ({"descricao": "x" * 201}, "até 200"),
    ({"etapa": "inexistente", "topico_id": None}, "Escolha uma etapa"),
    ({"etapa": "pisos"}, "não está na etapa Pisos"),
])
def test_gasto_invalido_e_recusado(mudanca, mensagem):
    with pytest.raises(RegraViolada, match=mensagem):
        confirmar(**mudanca)


def test_indice_so_guarda_o_que_esta_ativo():
    extras = [{"id": "e1", "nome": "Muro"}, {"id": "e2", "nome": "Velha", "arquivado": True}]
    topicos = [{"id": "t1", "etapa": "pisos", "nome": "Térreo"},
               {"id": "t2", "etapa": "pisos", "nome": "Velho", "arquivado": True}]
    assert indice_da_arvore(extras, topicos) == {"extras": {"e1": "Muro"},
                                                 "topicos": {"t1": {"etapa": "pisos", "nome": "Térreo"}}}


def test_ativar_extra_confere_limite_e_nome():
    assert ativar_extra({}, id_=None, nome=" Muro  de arrimo ") == "Muro de arrimo"
    with pytest.raises(RegraViolada, match="Já existe"):
        ativar_extra({}, id_=None, nome="fundação")                  # nome de etapa fixa
    with pytest.raises(RegraViolada, match="Já existe"):
        ativar_extra({"e1": "Muro"}, id_=None, nome="MURO")
    cheio = {f"e{i}": f"Extra {i}" for i in range(MAX_EXTRAS_POR_OBRA)}
    with pytest.raises(RegraViolada, match="Arquive"):
        ativar_extra(cheio, id_=None, nome="Nova")
    with pytest.raises(RegraViolada, match="Arquive"):
        ativar_extra(cheio, id_="arquivada", nome="Nova")              # reativar também conta
    # renomear uma ativa não esbarra no limite, e o próprio nome não conta como repetido
    assert ativar_extra(cheio, id_="e0", nome="extra 0") == "extra 0"


def test_ativar_topico_confere_etapa_limite_e_nome():
    extras = {"e1": "Muro"}
    topicos = {"t1": {"etapa": "pisos", "nome": "Térreo"}, "t2": {"etapa": "pintura", "nome": "Garagem"}}
    assert ativar_topico(extras, topicos, id_=None, etapa="pisos", nome="Garagem") == "Garagem"   # outra etapa
    assert ativar_topico(extras, topicos, id_=None, etapa=chave_extra("e1"), nome="X") == "X"
    with pytest.raises(RegraViolada, match="Já existe"):
        ativar_topico(extras, topicos, id_=None, etapa="pisos", nome="terreo")
    with pytest.raises(RegraViolada, match="Escolha uma etapa"):
        ativar_topico(extras, topicos, id_=None, etapa=chave_extra("e9"), nome="X")
    with pytest.raises(RegraViolada, match="reative a etapa"):
        ativar_topico(extras, topicos, id_="t9", etapa=chave_extra("e9"), nome="X")
    cheio = {f"t{i}": {"etapa": "pisos", "nome": f"T{i}"} for i in range(MAX_TOPICOS_POR_ETAPA)}
    with pytest.raises(RegraViolada, match="Arquive"):
        ativar_topico(extras, cheio, id_=None, etapa="pisos", nome="Novo")
    assert ativar_topico(extras, cheio, id_="t0", etapa="pisos", nome="Novo") == "Novo"


def test_so_gasto_avulso_soma_no_total_de_gastos():
    assert soma_nos_gastos_avulsos({"pedido_id": None})
    assert not soma_nos_gastos_avulsos({"pedido_id": "p1"})         # o pedido já soma pela entrega
    assert not soma_nos_gastos_avulsos({"pedido_id": None, "estornado": True})


def test_documentos_novos_nascem_com_padrao_certo():
    c = Comprovante(tenant_id="t", obra_id="o", arquivo="a", tipo_conteudo="image/jpeg", tamanho=1, sha256="h")
    assert c.status == StatusComprovante.A_REVISAR and c.leitura is None and c.gasto_id is None
    g = Gasto(tenant_id="t", obra_id="o", valor=1, data_pagamento="2026-11-03", favorecido="x", descricao="y",
              etapa="outros")
    assert soma_nos_gastos_avulsos(g.model_dump())


# --- árvore a partir do orçamento e gastos no realizado ---

ORCAMENTO = [("1", "SERVIÇOS PRELIMINARES"), ("18", "INSTALAÇÃO ELÉTRICA - 220V"), ("fundacoes", "FUNDAÇÕES"),
             ("18", "repetida no aditivo")]


def arvore_orcamento():
    extras = [{"id": "e1", "nome": "Taxas"}]
    topicos = [{"id": "t1", "etapa": "18", "nome": "Quadro"}, {"id": "t2", "etapa": "eletrica", "nome": "Antigo"}]
    return montar_arvore(extras, topicos, ORCAMENTO)


def test_com_orcamento_a_arvore_e_a_dele_na_ordem_mais_as_extras():
    a = arvore_orcamento()
    assert [(e.chave, e.nome, e.origem, e.fixa) for e in a] == [
        ("1", "1 SERVIÇOS PRELIMINARES", "orcamento", True),
        ("18", "18 INSTALAÇÃO ELÉTRICA - 220V", "orcamento", True),
        ("fundacoes", "FUNDAÇÕES", "orcamento", True),       # sem número: a chave é o nome, não se repete
        ("x-e1", "Taxas", "extra", False),
    ]
    assert [t.nome for t in a[1].topicos] == ["Quadro"]    # tópico da lista padrão não aparece
    assert all(e.origem == "padrao" for e in montar_arvore([], [], [("", "antes da 1ª etapa")]) if e.fixa)


def test_origem_e_nome_de_qualquer_chave_gravada():
    orc = dict(ORCAMENTO[:3])
    assert [origem_da_etapa(c, orc) for c in ("18", "x-e1", "eletrica")] == [
        "orcamento", "extra", "sem_etapa_do_orcamento"]
    assert [origem_da_etapa(c, {}) for c in ("eletrica", "x-e1", "inventada")] == [
        "padrao", "extra", "sem_etapa_do_orcamento"]
    assert nome_da_etapa("eletrica", orcamento=orc, extras={}) == "Instalações elétricas"
    assert nome_da_etapa("x-e9", orcamento=orc, extras={"e9": "Arquivada"}) == "Arquivada"
    assert nome_da_etapa("18", orcamento=orc, extras={}) == "18 INSTALAÇÃO ELÉTRICA - 220V"


def test_topico_e_extra_conferem_a_base_do_orcamento():
    base = {"1", "18"}
    assert ativar_topico({}, {}, id_=None, etapa="18", nome="Quadro", chaves_base=base) == "Quadro"
    with pytest.raises(RegraViolada, match="Escolha uma etapa"):
        ativar_topico({}, {}, id_=None, etapa="eletrica", nome="Quadro", chaves_base=base)
    # no Desfazer a base não é conhecida: só a extra é conferida
    assert ativar_topico({}, {}, id_="t1", etapa="eletrica", nome="Quadro", chaves_base=None) == "Quadro"
    with pytest.raises(RegraViolada):
        ativar_topico({}, {}, id_="t1", etapa="x-sumiu", nome="Quadro", chaves_base=None)
    nomes = nomes_da_base(ORCAMENTO[:2])
    with pytest.raises(RegraViolada, match="Já existe"):
        ativar_extra({}, id_=None, nome="instalação elétrica - 220v", nomes_base=nomes)
    assert ativar_extra({}, id_=None, nome="Fundação", nomes_base=nomes) == "Fundação"
    assert nomes_da_base([]) == [e.nome for e in ETAPAS_FIXAS]


def _g(valor, etapa, **kw):
    return {"valor": valor, "etapa": etapa, **kw}


def test_gastos_no_orcamento_repartem_por_origem_e_ignoram_pedido_e_estorno():
    gastos = [_g(100, "18"), _g(50, "18"), _g(30, "x-e1"), _g(20, "eletrica"),
              _g(999, "18", pedido_id="p1"), _g(777, "18", estornado=True)]
    r = gastos_no_orcamento(gastos, {"1", "18"})
    assert (r.por_etapa, r.fora_do_orcamento, r.sem_etapa_do_orcamento, r.na_lista_padrao) == ({"18": 150}, 30, 20, 0)
    assert (r.nas_etapas, r.fora_das_etapas, r.total) == (150, 50, 200)
    sem = gastos_no_orcamento(gastos, set())
    assert (sem.por_etapa, sem.na_lista_padrao, sem.sem_etapa_do_orcamento) == ({}, 20, 150)


def test_reclassificar_exige_destino_da_arvore_de_hoje():
    a = arvore_orcamento()
    validar_reclassificacao({"etapa": "eletrica"}, etapa="18", topico_id="t1", arvore=a)
    with pytest.raises(RegraViolada, match="Nada mudou"):
        validar_reclassificacao({"etapa": "18", "topico_id": "t1"}, etapa="18", topico_id="t1", arvore=a)
    with pytest.raises(RegraViolada, match="Escolha uma etapa"):
        validar_reclassificacao({"etapa": "18"}, etapa="eletrica", topico_id=None, arvore=a)


def test_revisao_aceita_o_codigo_do_orcamento():
    leit = leitura(classificacao={"certeza": "baixa", "opcoes": [
        {"etapa": "eletrica", "topico_id": None, "motivo": "lista padrão"},
        {"etapa": "18", "topico_id": "t1", "motivo": "quadro"}]})
    r = montar_revisao(leit, arvore=arvore_orcamento(), hoje=HOJE)
    assert [(o.etapa, o.topico_id, o.rotulo) for o in r.classificacao.opcoes] == [
        ("18", "t1", "18 INSTALAÇÃO ELÉTRICA - 220V › Quadro")]
