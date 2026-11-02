"""Cronograma físico-financeiro, medição mensal por etapa e prazo projetado.

O cronograma diz quanto de cada etapa deve ser executado em cada mês; a medição diz quanto foi. O prazo projetado
compara os dois pelo % da obra (ponderado pelo orçado atual de cada etapa): se a última medição chegou a 18% e o
cronograma previa 18% dois meses antes, a obra está dois meses atrasada e termina dois meses depois. É a conta que o
engenheiro faz na curva S, sem prometer mais precisão do que a medição tem.
"""
from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Cronograma, Medicao
from canteiro_dominio.regras import RegraViolada
from canteiro_dominio.tipos import fmt_reais

from ..auth import UsuarioAtual
from ..esquemas import (
    CronogramaCorpo,
    CronogramaObra,
    EtapaCronograma,
    LinhaBase,
    MedicaoCorpo,
    MesCronograma,
    Prazo,
)
from ..modelos import doc
from ..nucleo import Conflito, ErroNegocio, Operacao, evento, executar_e_registrar, obra_do_tenant
from .estoque import id_derivado
from .versoes import orcamento_da_obra

CEM = 10000
MESES_DEPOIS_DO_FIM = 12               # medição até um ano depois do término contratual (obra atrasada ainda mede)


def id_cronograma(tenant_id: str, obra_id: str) -> str:
    return id_derivado(tenant_id, "cronograma", obra_id)


def id_medicao(tenant_id: str, obra_id: str, chave: str) -> str:
    return id_derivado(tenant_id, "medicao", obra_id, chave)


def _mes_idx(mes: str) -> int:
    a, m = mes.split("-")
    return int(a) * 12 + int(m) - 1


def _mes(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _pct(parte: int, todo: int) -> int:
    return (parte * CEM + todo // 2) // todo if todo else 0


def _fmt_pct(c: int) -> str:
    return f"{c / 100:.1f}".replace(".", ",") + "%"


async def _etapas_orcadas(arm, tenant_id: str, obra_id: str) -> dict[str, tuple[str, int]]:
    """{etapa: (descrição, orçado atual)} das versões vigentes; é a base dos pesos do cronograma e da medição."""
    o = await orcamento_da_obra(arm, tenant_id, obra_id)
    return {e.etapa: (e.descricao, e.atual) for e in o.etapas if e.etapa}


def validar_cronograma(corpo: CronogramaCorpo, orcadas: dict) -> list[dict]:
    if not orcadas:
        raise ErroNegocio("Importe o orçamento antes de montar o cronograma: ele é feito por etapa.")
    vistas: set[str] = set()
    etapas = []
    for e in corpo.etapas:
        if e.etapa not in orcadas:
            raise ErroNegocio(f"A etapa {e.etapa} não existe no orçamento desta obra.")
        if e.etapa in vistas:
            raise ErroNegocio(f"A etapa {e.etapa} aparece duas vezes.")
        vistas.add(e.etapa)
        if len(e.percentuais) != corpo.meses:
            raise ErroNegocio(f"A etapa {e.etapa} tem {len(e.percentuais)} meses; o cronograma tem {corpo.meses}.")
        if any(p < 0 for p in e.percentuais):
            raise ErroNegocio(f"A etapa {e.etapa} tem percentual negativo.")
        soma = sum(e.percentuais)
        if soma not in (0, CEM):
            raise ErroNegocio(f"A etapa {e.etapa} soma {_fmt_pct(soma)}; tem de fechar 100% (ou ficar toda em zero).")
        if soma:
            etapas.append({"etapa": e.etapa, "percentuais": e.percentuais})
    return etapas


async def salvar(arm, u: UsuarioAtual, obra_id: str, corpo: CronogramaCorpo) -> dict:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    etapas = validar_cronograma(corpo, await _etapas_orcadas(arm, u.tenant_id, obra_id))
    id_ = id_cronograma(u.tenant_id, obra_id)

    async def corpo_tx(tx):
        atual = await tx.obter("cronogramas", u.tenant_id, id_)
        obra = await obra_do_tenant(tx, u.tenant_id, obra_id) if corpo.atualizar_prazo_da_obra else None
        if atual and atual.get("ultima_medicao"):
            fim = _mes_idx(corpo.inicio) + corpo.meses - 1 + MESES_DEPOIS_DO_FIM
            if _mes_idx(corpo.inicio) > _mes_idx(atual["ultima_medicao"]) or fim < _mes_idx(atual["ultima_medicao"]):
                raise ErroNegocio(f"Já há medição em {atual['ultima_medicao']}: o cronograma tem de cobrir esse mês.")
        vigente = {"inicio": corpo.inicio, "meses": corpo.meses, "etapas": etapas}
        # cronograma de antes da linha de base (sem `base`): a base passa a ser o anterior, não este, senão a
        # primeira reprogramação já apagaria o atraso
        if atual and not atual.get("base"):
            atual = {**atual, "base": {k: atual[k] for k in ("inicio", "meses", "etapas")}}
        nova_base = not atual or corpo.nova_linha_base
        campos = {**vigente, "versao": (atual["versao"] + 1) if atual else 1,
                  "base": vigente if nova_base else atual["base"],
                  "revisoes": 0 if nova_base else (atual.get("revisoes") or 0) + 1}
        antes = ({"inicio": atual["inicio"], "meses": atual["meses"], "etapas": atual["etapas"],
                  "base": atual["base"], "revisoes": atual.get("revisoes") or 0} if atual else None)
        prazo_antes = obra.get("prazo_meses", 0) if obra else None

        def aplicar():
            if obra is not None:
                tx.atualizar("obras", u.tenant_id, obra_id, {"prazo_meses": corpo.meses})
            if atual:
                tx.atualizar("cronogramas", u.tenant_id, id_, campos)
                return {**atual, **campos}
            d = doc(Cronograma(id=id_, tenant_id=u.tenant_id, obra_id=obra_id, **campos))
            tx.criar("cronogramas", d)
            return d

        def texto(d):
            verbo = "Refez" if atual else "Montou"
            prazo = f"{corpo.meses} meses a partir de {corpo.inicio}"
            if atual and atual["meses"] != corpo.meses:
                prazo += f" (era {atual['meses']})"
            if obra is not None and prazo_antes != corpo.meses:
                prazo += f" · prazo da obra {prazo_antes or '—'} → {corpo.meses} meses"
            base = " · nova linha de base" if atual and nova_base else ""
            return f"{verbo} o cronograma: {len(etapas)} etapas, {prazo}{base}"

        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=texto,
                      desfazer=lambda d: {"op": "restaurar_cronograma", "cronograma_id": id_, "antes": antes,
                                          "versao": campos["versao"], "obra_id": obra_id,
                                          "prazo_antes": prazo_antes, "prazo_depois": corpo.meses if obra else None})
        _, reg = executar_e_registrar(tx, u, op)
        evento(tx, u.tenant_id, "cronograma.salvo", obra_id, {"versao": campos["versao"]})
        return reg["id"]

    registro_id = await arm.em_transacao(corpo_tx)
    return (await cronograma_da_obra(arm, u.tenant_id, obra_id)).model_copy(update={"registro_id": registro_id})


async def preparar_restaurar(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    c = await tx.obter("cronogramas", u.tenant_id, receita["cronograma_id"])
    if not c or c["versao"] != receita["versao"]:
        raise Conflito("O cronograma foi editado de novo depois; não dá para desfazer esta edição.")
    antes = receita["antes"]
    if antes is None and c.get("medicoes"):
        raise Conflito("Já há medição neste cronograma; desfaça as medições antes.")
    if antes and c.get("ultima_medicao"):
        m, ini = _mes_idx(c["ultima_medicao"]), _mes_idx(antes["inicio"])
        if not ini <= m <= ini + antes["meses"] - 1 + MESES_DEPOIS_DO_FIM:
            raise Conflito(f"A medição de {c['ultima_medicao']} fica fora do cronograma anterior; desfaça-a antes.")
    obra = None
    if receita.get("prazo_depois") is not None:
        # o aditivo de prazo mudou a obra junto: o desfazer volta os dois, ou nenhum
        obra = await obra_do_tenant(tx, u.tenant_id, receita["obra_id"])
        if obra.get("prazo_meses") != receita["prazo_depois"]:
            raise Conflito("O prazo da obra mudou de novo depois; não dá para desfazer esta reprogramação.")

    def aplicar():
        # sem versão anterior, "desfazer" deixa o cronograma vazio (a porta de armazenamento não apaga)
        campos = antes or {"etapas": [], "meses": 0, "base": None, "revisoes": 0}
        campos = {**campos, "versao": c["versao"] + 1}
        tx.atualizar("cronogramas", u.tenant_id, c["id"], campos)
        if obra is not None:
            tx.atualizar("obras", u.tenant_id, obra["id"], {"prazo_meses": receita["prazo_antes"]})
        return {**c, **campos}

    def texto(d):
        t = "Desfeito: o cronograma voltou à versão anterior" if antes else "Desfeito: o cronograma foi retirado"
        if obra is not None:
            t += f" e o prazo da obra voltou a {receita['prazo_antes'] or '—'} meses"
        return t

    return Operacao(obra_id=c["obra_id"], aplicar=aplicar, texto=texto, desfazer=lambda d: None)


async def medir(arm, u: UsuarioAtual, obra_id: str, corpo: MedicaoCorpo) -> tuple[dict, bool, str]:
    """Devolve (medição, criada agora?, registro_id)."""
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    id_ = id_medicao(u.tenant_id, obra_id, corpo.chave_idempotencia)
    id_registro = id_derivado(u.tenant_id, "registro", id_)
    if ja := await arm.obter("medicoes", u.tenant_id, id_):
        pedido = {e.etapa: e.pct_acumulado for e in corpo.etapas}
        if ja["mes"] != corpo.mes or any(x["pct_acumulado"] != pedido[x["etapa"]] for x in ja["etapas"]
                                         if x["etapa"] in pedido):
            raise Conflito("Esta chave de idempotência já foi usada para outra medição.")
        return ja, False, id_registro
    orcadas = await _etapas_orcadas(arm, u.tenant_id, obra_id)
    total = sum(v for _, v in orcadas.values())

    async def corpo_tx(tx):
        c = await tx.obter("cronogramas", u.tenant_id, id_cronograma(u.tenant_id, obra_id))
        if not c or not c.get("etapas"):
            raise ErroNegocio("Monte o cronograma antes de medir.")
        m = _mes_idx(corpo.mes)
        if c.get("ultima_medicao") and m <= _mes_idx(c["ultima_medicao"]):
            raise RegraViolada(f"A última medição é de {c['ultima_medicao']}: a nova tem de ser de um mês depois.")
        ini = _mes_idx(c["inicio"])
        if not ini <= m <= ini + c["meses"] - 1 + MESES_DEPOIS_DO_FIM:
            raise RegraViolada(f"{corpo.mes} está fora do cronograma (começa em {c['inicio']}).")
        anterior = dict(c.get("acumulado", {}))
        novo = dict(anterior)
        for e in corpo.etapas:
            if e.etapa not in orcadas:
                raise ErroNegocio(f"A etapa {e.etapa} não existe no orçamento desta obra.")
            if e.pct_acumulado < anterior.get(e.etapa, 0):
                raise RegraViolada(f"A etapa {e.etapa} já estava em {_fmt_pct(anterior[e.etapa])}: o acumulado não "
                                   "diminui (para corrigir, desfaça a medição anterior).")
            novo[e.etapa] = e.pct_acumulado
        linhas = []
        for etapa, acum in novo.items():
            periodo = acum - anterior.get(etapa, 0)
            if etapa in {e.etapa for e in corpo.etapas} or periodo:
                orcado = orcadas.get(etapa, ("", 0))[1]
                linhas.append({"etapa": etapa, "pct_acumulado": acum, "pct_periodo": periodo,
                               "valor_periodo": (orcado * periodo + CEM // 2) // CEM})
        valor = sum(x["valor_periodo"] for x in linhas)
        pct_obra = _pct(sum(orcadas[e][1] * a for e, a in novo.items() if e in orcadas), total * CEM) if total \
            else 0
        n = c.get("medicoes", 0) + 1
        med = Medicao(id=id_, tenant_id=u.tenant_id, obra_id=obra_id, numero=n, mes=corpo.mes, etapas=linhas,
                      valor=valor, pct_obra_acumulado=pct_obra, observacao=corpo.observacao.strip(),
                      anterior=anterior, mes_anterior=c.get("ultima_medicao"), quem=u.nome, usuario_id=u.id)

        def aplicar():
            d = doc(med)
            tx.criar("medicoes", d)
            tx.atualizar("cronogramas", u.tenant_id, c["id"],
                         {"medicoes": n, "ultima_medicao": corpo.mes, "acumulado": novo})
            evento(tx, u.tenant_id, "medicao.criada", obra_id, {"medicao_id": id_, "numero": n})
            return d

        op = Operacao(obra_id=obra_id, aplicar=aplicar,
                      texto=lambda d: f"{n}ª medição ({corpo.mes}): {fmt_reais(valor)} no mês · "
                                      f"{_fmt_pct(pct_obra)} da obra executada",
                      desfazer=lambda d: {"op": "desfazer_medicao", "medicao_id": id_})
        d, _ = executar_e_registrar(tx, u, op, registro_id=id_registro)
        return d

    return await arm.em_transacao(corpo_tx), True, id_registro


async def preparar_desfazer_medicao(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    med = await tx.obter("medicoes", u.tenant_id, receita["medicao_id"])
    if not med or med.get("desfeito"):
        raise Conflito("Esta medição já foi desfeita.")
    c = await tx.obter("cronogramas", u.tenant_id, id_cronograma(u.tenant_id, med["obra_id"]))
    if not c or c.get("medicoes") != med["numero"]:
        raise Conflito("Há medição depois desta; desfaça a mais recente primeiro.")

    def aplicar():
        tx.atualizar("cronogramas", u.tenant_id, c["id"], {"medicoes": med["numero"] - 1,
                                                          "ultima_medicao": med["mes_anterior"],
                                                          "acumulado": med["anterior"]})
        tx.atualizar("medicoes", u.tenant_id, med["id"], {"desfeito": True})
        return {**med, "desfeito": True}

    return Operacao(obra_id=med["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {med['numero']}ª medição ({med['mes']}) retirada",
                    desfazer=lambda d: None)


async def cronograma_da_obra(arm, tenant_id: str, obra_id: str) -> CronogramaObra:
    obra = await obra_do_tenant(arm, tenant_id, obra_id)
    orcadas = await _etapas_orcadas(arm, tenant_id, obra_id)
    c = await arm.obter("cronogramas", tenant_id, id_cronograma(tenant_id, obra_id))
    if not c or not c.get("etapas"):
        etapas = [EtapaCronograma(etapa=e, descricao=d, atual=v, percentuais=[]) for e, (d, v) in orcadas.items()]
        termino = None
        if obra.get("inicio") and obra.get("prazo_meses"):
            termino = _mes(_mes_idx(obra["inicio"][:7]) + obra["prazo_meses"] - 1)
        return CronogramaObra(etapas=etapas, prazo=Prazo(termino_contratual=termino))
    meds = await arm.consultar(Consulta("medicoes", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    meds = sorted((m for m in meds if not m.get("desfeito")), key=lambda m: m["numero"])
    plano = {e["etapa"]: e["percentuais"] for e in c["etapas"]}
    acumulado = c.get("acumulado", {})
    etapas = [EtapaCronograma(etapa=e, descricao=d, atual=v, percentuais=plano.get(e, [0] * c["meses"]),
                              medido_acumulado=acumulado.get(e, 0)) for e, (d, v) in orcadas.items()]
    total = sum(e.atual for e in etapas)
    ini = _mes_idx(c["inicio"])
    ultimo = max([c["meses"] - 1] + [_mes_idx(m["mes"]) - ini for m in meds])
    por_mes = {_mes_idx(m["mes"]) - ini: m for m in meds}
    ref = _mes_idx(meds[-1]["mes"]) - ini if meds else None
    mensal: list[MesCronograma] = []
    prev_acum = med_acum = 0
    for k in range(ultimo + 1):
        previsto = sum((e.atual * e.percentuais[k] + CEM // 2) // CEM for e in etapas if k < len(e.percentuais))
        prev_acum += previsto
        linha = MesCronograma(mes=_mes(ini + k), previsto=previsto, previsto_acumulado=prev_acum,
                              pct_previsto_acumulado=_pct(prev_acum, total))
        if ref is not None and k <= ref:
            m = por_mes.get(k)
            med_acum += m["valor"] if m else 0
            linha.medido, linha.medido_acumulado = (m["valor"] if m else 0), med_acum
            # o % medido sai da própria medição (acumulado por etapa); no mês sem medição repete o anterior
            linha.pct_medido_acumulado = m["pct_obra_acumulado"] if m else (
                mensal[-1].pct_medido_acumulado if mensal else 0)
        mensal.append(linha)
    base = c.get("base") or {"inicio": c["inicio"], "meses": c["meses"], "etapas": c["etapas"]}
    b_ini = _mes_idx(base["inicio"])
    serie_base = _serie({e["etapa"]: e["percentuais"] for e in base["etapas"]}, orcadas, base["meses"])
    for k, linha in enumerate(mensal):
        j = ini + k - b_ini
        linha.pct_base_acumulado = 0 if j < 0 else serie_base[min(j, len(serie_base) - 1)]
    termino = ini + c["meses"] - 1
    termino_base = b_ini + base["meses"] - 1
    prazo = Prazo(termino_contratual=_mes(termino), termino_base=_mes(termino_base))
    if ref is not None:
        medido = meds[-1]["pct_obra_acumulado"]
        decimos = round((ref - _mes_equivalente([x.pct_previsto_acumulado for x in mensal], medido)) * 10)
        decimos_base = round((ini + ref - b_ini - _mes_equivalente(serie_base, medido)) * 10)
        atraso, atraso_base = _arredonda_meio(decimos), _arredonda_meio(decimos_base)
        prazo = Prazo(termino_contratual=_mes(termino), termino_projetado=_mes(termino + atraso),
                      atraso_meses=atraso, atraso_decimos=decimos,
                      pct_previsto_na_medicao=mensal[ref].pct_previsto_acumulado, pct_medido=medido,
                      mes_referencia=meds[-1]["mes"], termino_base=_mes(termino_base),
                      atraso_base_meses=atraso_base, atraso_base_decimos=decimos_base)
    return CronogramaObra(inicio=c["inicio"], meses=c["meses"], etapas=etapas, mensal=mensal, medicoes=meds,
                          prazo=prazo, base=LinhaBase(inicio=base["inicio"], meses=base["meses"]),
                          revisoes=c.get("revisoes") or 0)


def _arredonda_meio(decimos: int) -> int:
    # meio mês arredonda para longe do zero: atraso de 0,5 já empurra o término
    return (decimos + 5) // 10 if decimos >= 0 else -((-decimos + 5) // 10)


def _serie(plano: dict[str, list[int]], orcadas: dict[str, tuple[str, int]], meses: int) -> list[int]:
    """% acumulado previsto da obra, mês a mês, pelo plano e pelos pesos de hoje (orçado atual de cada etapa)."""
    total = sum(v for _, v in orcadas.values())
    acum, saida = 0, []
    for k in range(meses):
        acum += sum(orcadas.get(e, ("", 0))[1] * p[k] for e, p in plano.items() if k < len(p))
        saida.append(_pct(acum, total * CEM) if total else 0)
    return saida


def _mes_equivalente(serie: list[int], medido: int) -> float:
    """Em que ponto da série o % previsto acumulado vale o % medido, em meses com fração (fim do mês k = k; antes do
    1º mês = -1). Interpolar dentro do mês evita dizer "em dia" para 1,5% medido contra 9,2% previsto, que é o que a
    conta em meses inteiros fazia no primeiro mês."""
    anterior = 0
    for k, atual in enumerate(serie):
        if medido == 0 and atual > 0:
            return k - 1
        if medido > 0 and atual >= medido:
            return k - 1 + (medido - anterior) / (atual - anterior)
        anterior = atual
    return len(serie) - 1
