"""O motor de cronograma por visão de obra: a duração vem da quantidade × produtividade, as dependências e os lags de
processo (cura) posicionam no tempo, e o total é o caminho crítico — nada de proporção de preço."""
import pytest

from canteiro_dominio.cronograma import (
    REFERENCIA,
    CronogramaInvalido,
    Servico,
    ServicoDoPlano,
    classificar,
    duracao_dias,
    montar_plano,
)


class TestDuracao:
    def test_sai_da_quantidade_e_da_produtividade(self):
        # 30 m³ de concreto × 2,5 HH/m³ = 75 HH; equipe de 5 em jornada de 8h → 75/40 = 1,875 → 2 dias úteis
        assert duracao_dias(30_000, 2.5, 5) == 2

    def test_dobro_da_quantidade_perto_do_dobro_de_dias(self):
        d1 = duracao_dias(60_000, 0.9, 3)  # 60 m² de fôrma
        d2 = duracao_dias(120_000, 0.9, 3)  # 120 m²
        assert d2 > d1 and abs(d2 - 2 * d1) <= 1  # escala com a quantidade (a menos do arredondamento)

    def test_mais_gente_menos_dias(self):
        assert duracao_dias(100_000, 1.0, 8) < duracao_dias(100_000, 1.0, 4)

    def test_um_dia_comecado_conta_inteiro(self):
        assert duracao_dias(1_000, 2.5, 5) == 1  # pouca quantidade, mas é um dia na obra

    def test_sem_quantidade_sem_dias(self):
        assert duracao_dias(0, 2.5, 5) == 0


class TestClassificacao:
    @pytest.mark.parametrize(
        "descricao,esperado",
        [
            ("Concreto fck25 para sapata", Servico.FUNDACAO_CONCRETO),
            ("Escavação manual de vala", Servico.MOVIMENTO_TERRA),
            ("Fôrma de madeira para laje", Servico.FORMA),
            ("Aço CA-50 estrutural", Servico.ARMACAO),
            ("Concreto fck30 em pilar", Servico.ESTRUTURA_CONCRETO),
            ("Alvenaria de bloco cerâmico", Servico.ALVENARIA),
            ("Telhado com telha cerâmica", Servico.COBERTURA),
            ("Pintura látex duas demãos", Servico.PINTURA),
            ("Reboco interno", Servico.REVESTIMENTO_INTERNO),
            ("Porcelanato 60x60", Servico.REVESTIMENTO_CERAMICO),
            ("Peça sem nome conhecido", Servico.OUTRO),
        ],
    )
    def test_casa_a_familia(self, descricao, esperado):
        assert classificar(descricao) == esperado

    def test_sem_acento_e_maiuscula(self):
        assert classificar("FORMA DE SAPATA") == Servico.FORMA


class TestPlano:
    def _fundacao(self):
        # a sequência clássica da fundação: fôrma → armação → concreto (com cura) → e a alvenaria depois
        return [
            ServicoDoPlano(id="forma", descricao="Fôrma de sapata", quantidade_milesimos=60_000, unidade="m2"),
            ServicoDoPlano(id="armacao", descricao="Armação de sapata", quantidade_milesimos=800_000, unidade="kg",
                           depende_de=["forma"]),
            ServicoDoPlano(id="concreto", descricao="Concreto fck25 sapata", quantidade_milesimos=30_000, unidade="m3",
                           depende_de=["armacao"]),
            ServicoDoPlano(id="alvenaria", descricao="Alvenaria de bloco", quantidade_milesimos=200_000, unidade="m2",
                           depende_de=["concreto"]),
        ]

    def test_dependencia_empurra_o_inicio(self):
        plano = montar_plano(self._fundacao())
        ag = {s.id: s for s in plano.servicos}
        # armação só começa quando a fôrma termina
        assert ag["armacao"].inicio_dia == ag["forma"].fim_dia
        # concreto só começa quando a armação termina
        assert ag["concreto"].inicio_dia == ag["armacao"].fim_dia

    def test_cura_atrasa_o_dependente(self):
        plano = montar_plano(self._fundacao())
        ag = {s.id: s for s in plano.servicos}
        cura = REFERENCIA[Servico.FUNDACAO_CONCRETO].lag_pos_dias
        assert cura > 0
        # a alvenaria espera o concreto curar (fim + lag), não só terminar
        assert ag["alvenaria"].inicio_dia == ag["concreto"].fim_dia + cura

    def test_servicos_sem_relacao_correm_juntos(self):
        servicos = [
            ServicoDoPlano(id="a", descricao="Fôrma de sapata", quantidade_milesimos=60_000, unidade="m2"),
            ServicoDoPlano(id="b", descricao="Serviços preliminares", quantidade_milesimos=100_000, unidade="m2"),
        ]
        plano = montar_plano(servicos)
        assert all(s.inicio_dia == 0 for s in plano.servicos)  # ambos no dia 0

    def test_total_e_o_caminho_critico(self):
        plano = montar_plano(self._fundacao())
        ag = {s.id: s for s in plano.servicos}
        # o total bate com o fim (liberado) do último da cadeia
        assert plano.dias_uteis == ag["alvenaria"].liberado_dia
        assert plano.critico == ["forma", "armacao", "concreto", "alvenaria"]

    def test_dependencia_inexistente_falha(self):
        with pytest.raises(CronogramaInvalido):
            montar_plano([ServicoDoPlano(id="x", descricao="Fôrma", quantidade_milesimos=10_000,
                                         depende_de=["nao_existe"])])

    def test_ciclo_falha(self):
        with pytest.raises(CronogramaInvalido):
            montar_plano([
                ServicoDoPlano(id="a", descricao="Fôrma", quantidade_milesimos=10_000, depende_de=["b"]),
                ServicoDoPlano(id="b", descricao="Armação", quantidade_milesimos=10_000, depende_de=["a"]),
            ])

    def test_planejador_pode_sobrepor_a_referencia(self):
        # equipe e HH do planejador prevalecem sobre a referência da família
        base = montar_plano([ServicoDoPlano(id="c", descricao="Concreto sapata", quantidade_milesimos=30_000)])
        reforcado = montar_plano([ServicoDoPlano(id="c", descricao="Concreto sapata", quantidade_milesimos=30_000,
                                                 equipe=20)])
        assert reforcado.servicos[0].duracao_dias <= base.servicos[0].duracao_dias
        assert reforcado.servicos[0].equipe == 20
