"""Gastos da obra: árvore (etapas fixas, extras e tópicos), comprovantes a revisar e gastos confirmados.

Fluxo: a pessoa envia o comprovante → o serviço do agente lê (IA) e grava a leitura → a revisão abre com o máximo
preenchido → uma pessoa (admin) confirma e só então nasce o `Gasto`, com registro e Desfazer. A IA nunca grava gasto.
"""
import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.etapas import (
    ETAPAS_FIXAS,
    MAX_EXTRAS_POR_OBRA,
    MAX_TOPICOS_POR_ETAPA,
    PREFIXO_EXTRA,
    EtapaNaArvore,
    chave_extra,
    montar_arvore,
    validar_nome,
)
from canteiro_dominio.gastos import conta_no_realizado, validar_gasto
from canteiro_dominio.leitura import LeituraComprovante, montar_revisao
from canteiro_dominio.modelos import (
    Comprovante,
    EtapaExtra,
    Gasto,
    StatusComprovante,
    StatusPedido,
    Topico,
    agora,
)
from canteiro_dominio.tipos import fmt_reais

from ..armazenamento.base import normalizar
from ..auth import UsuarioAtual
from ..esquemas import ConfirmarCorpo, EdicaoArvoreCorpo, LeituraCorpo
from ..modelos import doc
from ..nucleo import (
    Conflito,
    ErroNegocio,
    NaoEncontrado,
    Operacao,
    evento,
    executar_e_registrar,
    listar_pagina,
    obra_do_tenant,
    receita_inversa,
    verbo_desfazer,
)
from . import fornecedores
from .estoque import id_derivado

_SP = ZoneInfo("America/Sao_Paulo")
# O Claude lê estes formatos; HEIC (foto de iPhone) a tela converte para JPEG antes de enviar.
TIPOS_ACEITOS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
MAX_ARQUIVO = 5 * 1024 * 1024
MAX_LEGENDA = 300
_NOMES_FIXOS = {e.chave: e.nome for e in ETAPAS_FIXAS}


def hoje() -> date:
    """O dia de quem usa (São Paulo), não o do servidor em UTC: às 22h o Pix de hoje não pode virar "amanhã"."""
    return datetime.now(_SP).date()


def _agora() -> datetime:
    return normalizar(agora())


# --- árvore da obra ---

async def _extras_e_topicos(leitor, tenant_id: str, obra_id: str) -> tuple[list[dict], list[dict]]:
    extras = await leitor.consultar(Consulta("etapas_extras", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    topicos = await leitor.consultar(Consulta("topicos", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    return extras, topicos


async def arvore_da_obra(leitor, tenant_id: str, obra_id: str) -> list[EtapaNaArvore]:
    await obra_do_tenant(leitor, tenant_id, obra_id)
    return montar_arvore(*await _extras_e_topicos(leitor, tenant_id, obra_id))


def _nome_etapa(chave: str, extras: dict[str, dict]) -> str:
    if chave in _NOMES_FIXOS:
        return _NOMES_FIXOS[chave]
    x = extras.get(chave.removeprefix(PREFIXO_EXTRA))
    return x["nome"] if x else chave


async def criar_etapa_extra(arm, u: UsuarioAtual, obra_id: str, nome: str) -> dict:
    arvore = await arvore_da_obra(arm, u.tenant_id, obra_id)
    if sum(1 for e in arvore if not e.fixa) >= MAX_EXTRAS_POR_OBRA:
        raise ErroNegocio(f"A obra já tem {MAX_EXTRAS_POR_OBRA} etapas extras. Arquive uma antes.")
    limpo = validar_nome(nome, ja_usados=[e.nome for e in arvore])
    d = doc(EtapaExtra(tenant_id=u.tenant_id, obra_id=obra_id, nome=limpo))

    async def corpo(tx):
        op = Operacao(obra_id=obra_id, aplicar=lambda: (tx.criar("etapas_extras", d), d)[1],
                      texto=lambda r: f"Criou a etapa {limpo}",
                      desfazer=lambda r: _receita_criacao("etapas_extras", d["id"]))
        _, reg = executar_e_registrar(tx, u, op)
        return {**d, "chave": chave_extra(d["id"]), "registro_id": reg["id"]}

    return await arm.em_transacao(corpo)


async def criar_topico(arm, u: UsuarioAtual, obra_id: str, etapa: str, nome: str) -> dict:
    arvore = await arvore_da_obra(arm, u.tenant_id, obra_id)
    no = next((e for e in arvore if e.chave == etapa), None)
    if no is None:
        raise ErroNegocio("Escolha uma etapa da obra.")
    if len(no.topicos) >= MAX_TOPICOS_POR_ETAPA:
        raise ErroNegocio(f"{no.nome} já tem {MAX_TOPICOS_POR_ETAPA} tópicos. Arquive um antes.")
    limpo = validar_nome(nome, ja_usados=[t.nome for t in no.topicos])
    d = doc(Topico(tenant_id=u.tenant_id, obra_id=obra_id, etapa=etapa, nome=limpo))

    async def corpo(tx):
        op = Operacao(obra_id=obra_id, aplicar=lambda: (tx.criar("topicos", d), d)[1],
                      texto=lambda r: f"Criou o tópico {limpo} em {no.nome}",
                      desfazer=lambda r: _receita_criacao("topicos", d["id"]))
        _, reg = executar_e_registrar(tx, u, op)
        return {**d, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo)


def _receita_criacao(colecao: str, id_: str) -> dict:
    # a porta não apaga: desfazer a criação é arquivar, e refazer é desarquivar
    return {"op": "editar_arvore", "colecao": colecao, "id": id_, "antes": {"arquivado": True},
            "depois": {"arquivado": False}}


_ROTULO_COLECAO = {"etapas_extras": "a etapa", "topicos": "o tópico"}


def _texto_edicao(colecao: str, antigo: dict, mudou: dict) -> str:
    alvo = f"{_ROTULO_COLECAO[colecao]} {antigo['nome']}"
    partes = []
    if "nome" in mudou:
        partes.append(f"Renomeou {alvo} para {mudou['nome']}")
    if mudou.get("arquivado") is True:
        partes.append(f"Arquivou {alvo}" if not partes else "e arquivou")
    if mudou.get("arquivado") is False:
        partes.append(f"Reativou {alvo}" if not partes else "e reativou")
    return " ".join(partes)


async def editar_item_arvore(arm, u: UsuarioAtual, colecao: str, id_: str, corpo: EdicaoArvoreCorpo) -> dict:
    atual = await arm.obter(colecao, u.tenant_id, id_)
    if not atual:
        raise NaoEncontrado("Etapa não encontrada." if colecao == "etapas_extras" else "Tópico não encontrado.")
    mudancas = corpo.model_dump(exclude_none=True)
    arvore = await arvore_da_obra(arm, u.tenant_id, atual["obra_id"])
    if colecao == "etapas_extras":
        irmaos = [e.nome for e in arvore if e.chave != chave_extra(id_)]
        cheio = sum(1 for e in arvore if not e.fixa) >= MAX_EXTRAS_POR_OBRA
    else:
        no = next((e for e in arvore if e.chave == atual["etapa"]), None)
        if no is None and mudancas.get("arquivado") is False:
            raise ErroNegocio("A etapa deste tópico está arquivada: reative a etapa antes.")
        irmaos = [t.nome for t in (no.topicos if no else ()) if t.id != id_]
        cheio = no is not None and len(no.topicos) >= MAX_TOPICOS_POR_ETAPA
    reativando = mudancas.get("arquivado") is False and atual.get("arquivado")
    if reativando and cheio:
        raise ErroNegocio("Limite atingido: arquive outro antes de reativar este.")
    if "nome" in mudancas or reativando:
        # reativar também confere o nome: enquanto estava arquivado, outro pode ter nascido com ele
        mudancas["nome"] = validar_nome(mudancas.get("nome", atual["nome"]), ja_usados=irmaos)

    async def corpo_tx(tx):
        d = await tx.obter(colecao, u.tenant_id, id_)
        mudou = {k: v for k, v in mudancas.items() if d.get(k) != v}
        if not mudou:
            raise ErroNegocio("Nada mudou.")
        antes = {k: d.get(k) for k in mudou}

        def aplicar():
            tx.atualizar(colecao, u.tenant_id, id_, {**mudou, "atualizado_em": _agora()})
            return {**d, **mudou}

        op = Operacao(obra_id=d["obra_id"], aplicar=aplicar, texto=lambda r: _texto_edicao(colecao, d, mudou),
                      desfazer=lambda r: {"op": "editar_arvore", "colecao": colecao, "id": id_, "antes": antes,
                                          "depois": mudou})
        novo, reg = executar_e_registrar(tx, u, op)
        extra = {"chave": chave_extra(id_)} if colecao == "etapas_extras" else {}
        return {**novo, **extra, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def preparar_desfazer_arvore(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    colecao = receita["colecao"]
    d = await tx.obter(colecao, u.tenant_id, receita["id"])
    if not d:
        raise NaoEncontrado("Não encontrado.")
    if any(d.get(k) != v for k, v in receita["depois"].items()):
        raise Conflito(f"{d['nome']} foi editado de novo depois; não dá para desfazer esta mudança.")

    def aplicar():
        tx.atualizar(colecao, u.tenant_id, d["id"], {**receita["antes"], "atualizado_em": _agora()})
        return {**d, **receita["antes"]}

    return Operacao(obra_id=d["obra_id"], aplicar=aplicar,
                    texto=lambda r: f"{verbo_desfazer(receita)}: {_ROTULO_COLECAO[colecao]} {d['nome']}",
                    desfazer=lambda r: receita_inversa(receita))


# --- comprovantes ---

def id_comprovante(tenant_id: str, obra_id: str, sha256: str) -> str:
    """O mesmo arquivo na mesma obra é o mesmo comprovante: foto enviada duas vezes não vira dois gastos."""
    return id_derivado(tenant_id, "comprovante", obra_id, sha256)


async def enviar_comprovante(arm, arquivos, u: UsuarioAtual, obra_id: str, conteudo: bytes, tipo: str,
                             legenda: str) -> tuple[dict, bool]:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    tipo = (tipo or "").split(";")[0].strip().lower()
    if tipo not in TIPOS_ACEITOS:
        raise ErroNegocio("Envie uma foto (JPG, PNG ou WEBP) ou um PDF.")
    if not conteudo:
        raise ErroNegocio("O arquivo chegou vazio. Tente de novo.")
    if len(conteudo) > MAX_ARQUIVO:
        raise ErroNegocio("Arquivo maior que 5 MB.")
    sha = hashlib.sha256(conteudo).hexdigest()
    id_ = id_comprovante(u.tenant_id, obra_id, sha)
    if ja := await arm.obter("comprovantes", u.tenant_id, id_):
        return ja, True
    caminho = f"{obra_id}/{id_}.{TIPOS_ACEITOS[tipo]}"
    # o arquivo antes do documento: documento sem arquivo quebraria a revisão; arquivo sem documento é só sobra
    await arquivos.salvar(u.tenant_id, caminho, conteudo)
    d = doc(Comprovante(id=id_, tenant_id=u.tenant_id, obra_id=obra_id, arquivo=caminho, tipo_conteudo=tipo,
                        tamanho=len(conteudo), sha256=sha, legenda=" ".join(legenda.split())[:MAX_LEGENDA],
                        quem=u.nome, usuario_id=u.id))

    async def corpo(tx):
        if ja := await tx.obter("comprovantes", u.tenant_id, id_):
            return ja, True
        tx.criar("comprovantes", d)
        # a fila do agente proativo (WhatsApp, leitura assíncrona) parte daqui quando o relay existir
        evento(tx, u.tenant_id, "comprovante.recebido", obra_id, {"comprovante_id": id_})
        return d, False

    return await arm.em_transacao(corpo)


async def comprovante_do_tenant(leitor, tenant_id: str, comprovante_id: str) -> dict:
    c = await leitor.obter("comprovantes", tenant_id, comprovante_id)
    if not c:
        raise NaoEncontrado("Comprovante não encontrado.")
    return c


async def arquivo_do_comprovante(arm, arquivos, u: UsuarioAtual, comprovante_id: str) -> tuple[bytes, str]:
    c = await comprovante_do_tenant(arm, u.tenant_id, comprovante_id)
    conteudo = await arquivos.ler(u.tenant_id, c["arquivo"])
    if conteudo is None:
        raise NaoEncontrado("O arquivo deste comprovante não foi encontrado.")
    return conteudo, c["tipo_conteudo"]


async def detalhe(arm, u: UsuarioAtual, comprovante_id: str) -> dict:
    c = await comprovante_do_tenant(arm, u.tenant_id, comprovante_id)
    arvore = await arvore_da_obra(arm, u.tenant_id, c["obra_id"])
    leitura = LeituraComprovante.model_validate(c["leitura"]) if c.get("leitura") else None
    revisao = montar_revisao(leitura, arvore=arvore, hoje=hoje(),
                             fornecedores=await fornecedores.listar(arm, u.tenant_id))
    if c.get("erro_leitura") and leitura is None:
        revisao.avisos.insert(0, c["erro_leitura"])
    return {**c, "revisao": revisao}


async def gravar_leitura(arm, u: UsuarioAtual, comprovante_id: str, corpo: LeituraCorpo) -> dict:
    async def corpo_tx(tx):
        c = await tx.obter("comprovantes", u.tenant_id, comprovante_id)
        if not c:
            raise NaoEncontrado("Comprovante não encontrado.")
        if c["status"] != StatusComprovante.A_REVISAR.value:
            raise Conflito("Este comprovante já foi revisado.")
        campos = {"erro_leitura": corpo.erro, "lido_em": _agora(), "modelo_leitura": corpo.modelo,
                  "atualizado_em": _agora()}
        # leitura nova que falhou não apaga a anterior: a revisão continua com o que já tinha sido lido
        if corpo.leitura is not None:
            campos["leitura"] = corpo.leitura.model_dump()
        tx.atualizar("comprovantes", u.tenant_id, comprovante_id, campos)
        return {**c, **campos}

    return await arm.em_transacao(corpo_tx)


async def listar_comprovantes(arm, u: UsuarioAtual, obra_id: str, status: StatusComprovante | None, limite: int,
                              antes_de: datetime | None) -> list[dict]:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    filtros = [Filtro("obra_id", "==", obra_id)]
    if status:
        filtros.append(Filtro("status", "==", status.value))
    return await listar_pagina(arm, Consulta("comprovantes", u.tenant_id, filtros, [("criado_em", "desc")],
                                             limite=limite), "criado_em", antes_de)


# --- confirmar, descartar e desfazer ---

async def confirmar(arm, u: UsuarioAtual, comprovante_id: str, corpo: ConfirmarCorpo) -> dict:
    c = await comprovante_do_tenant(arm, u.tenant_id, comprovante_id)
    obra_id = c["obra_id"]
    arvore = await arvore_da_obra(arm, u.tenant_id, obra_id)
    validado = validar_gasto(valor=corpo.valor, data_pagamento=corpo.data_pagamento, favorecido=corpo.favorecido,
                             descricao=corpo.descricao, etapa=corpo.etapa, topico_id=corpo.topico_id, arvore=arvore,
                             hoje=hoje())
    if corpo.fornecedor_id and not await arm.obter("fornecedores", u.tenant_id, corpo.fornecedor_id):
        raise ErroNegocio("Fornecedor não encontrado.")
    no = next(e for e in arvore if e.chave == corpo.etapa)
    topico = next((t for t in no.topicos if t.id == corpo.topico_id), None)
    rotulo = f"{no.nome} › {topico.nome}" if topico else no.nome
    g = doc(Gasto(tenant_id=u.tenant_id, obra_id=obra_id, comprovante_id=comprovante_id, valor=corpo.valor,
                  data_pagamento=corpo.data_pagamento, favorecido=validado.favorecido,
                  fornecedor_id=corpo.fornecedor_id, descricao=validado.descricao, etapa=corpo.etapa,
                  topico_id=corpo.topico_id, pedido_id=corpo.pedido_id, quem=u.nome, usuario_id=u.id))

    async def corpo_tx(tx):
        atual = await tx.obter("comprovantes", u.tenant_id, comprovante_id)
        if atual["status"] != StatusComprovante.A_REVISAR.value:
            raise Conflito("Este comprovante já foi revisado (confirmado ou descartado).")
        pedido = None
        if corpo.pedido_id:
            pedido = await tx.obter("pedidos", u.tenant_id, corpo.pedido_id)
            if not pedido or pedido["obra_id"] != obra_id:
                raise ErroNegocio("Pedido não encontrado nesta obra.")
            if pedido["status"] in (StatusPedido.CANCELADO.value, StatusPedido.EM_COTACAO.value):
                raise ErroNegocio(f"O {pedido['numero']} não foi comprado (está "
                                  f"{'cancelado' if pedido['status'] == 'cancelado' else 'em cotação'}).")

        def aplicar():
            tx.criar("gastos", g)
            tx.atualizar("comprovantes", u.tenant_id, comprovante_id,
                         {"status": StatusComprovante.CONFIRMADO.value, "gasto_id": g["id"],
                          "atualizado_em": _agora()})
            if pedido:
                # o pedido foi lido para decidir: a transação grava nele (regra do Firestore × Mongo)
                tx.atualizar("pedidos", u.tenant_id, pedido["id"], {"pago": pedido.get("pago", 0) + corpo.valor})
            return g

        def texto(_):
            base = f"Gasto de {fmt_reais(corpo.valor)} em {rotulo}: {validado.descricao} ({validado.favorecido})"
            return base + (f" · pagamento do {pedido['numero']}" if pedido else "")

        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=texto,
                      desfazer=lambda r: {"op": "estornar_gasto", "gasto_id": g["id"]})
        _, reg = executar_e_registrar(tx, u, op)
        evento(tx, u.tenant_id, "gasto.confirmado", obra_id, {"gasto_id": g["id"], "comprovante_id": comprovante_id})
        comp = {**atual, "status": StatusComprovante.CONFIRMADO.value, "gasto_id": g["id"]}
        return {"gasto": g, "comprovante": comp, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def preparar_estorno_gasto(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfazer a confirmação: o gasto sai das contas e o comprovante volta para a fila, para corrigir e confirmar
    de novo. Sem refazer: confirmar de novo pela revisão é o caminho (a pessoa provavelmente quer mudar algo)."""
    g = await tx.obter("gastos", u.tenant_id, receita["gasto_id"])
    if not g:
        raise NaoEncontrado("Gasto não encontrado.")
    if g.get("estornado"):
        raise Conflito("Este gasto já foi desfeito.")
    c = await tx.obter("comprovantes", u.tenant_id, g["comprovante_id"]) if g.get("comprovante_id") else None
    if c and (c["status"] != StatusComprovante.CONFIRMADO.value or c.get("gasto_id") != g["id"]):
        raise Conflito("O comprovante deste gasto mudou depois; não dá para desfazer.")
    pedido = await tx.obter("pedidos", u.tenant_id, g["pedido_id"]) if g.get("pedido_id") else None

    def aplicar():
        tx.atualizar("gastos", u.tenant_id, g["id"], {"estornado": True})
        if c:
            tx.atualizar("comprovantes", u.tenant_id, c["id"], {"status": StatusComprovante.A_REVISAR.value,
                                                                 "gasto_id": None, "atualizado_em": _agora()})
        if pedido:
            tx.atualizar("pedidos", u.tenant_id, pedido["id"], {"pago": max(0, pedido.get("pago", 0) - g["valor"])})
        return {**g, "estornado": True}

    return Operacao(obra_id=g["obra_id"], aplicar=aplicar,
                    texto=lambda r: f"Desfeito: gasto de {fmt_reais(g['valor'])} ({g['descricao']}); "
                                    "o comprovante voltou para a revisão",
                    desfazer=lambda r: None)


async def descartar(arm, u: UsuarioAtual, comprovante_id: str, motivo: str) -> dict:
    motivo = " ".join(motivo.split())

    async def corpo_tx(tx):
        c = await tx.obter("comprovantes", u.tenant_id, comprovante_id)
        if not c:
            raise NaoEncontrado("Comprovante não encontrado.")
        if c["status"] != StatusComprovante.A_REVISAR.value:
            raise Conflito("Este comprovante já foi revisado (confirmado ou descartado).")
        campos = {"status": StatusComprovante.DESCARTADO.value, "motivo_descarte": motivo, "atualizado_em": _agora()}

        def aplicar():
            tx.atualizar("comprovantes", u.tenant_id, comprovante_id, campos)
            return {**c, **campos}

        quando = c["criado_em"].astimezone(_SP).strftime("%d/%m") if isinstance(c["criado_em"], datetime) else ""
        op = Operacao(obra_id=c["obra_id"], aplicar=aplicar,
                      texto=lambda r: f"Descartou o comprovante enviado por {c.get('quem') or 'alguém'}"
                                      + (f" em {quando}" if quando else "") + (f": {motivo}" if motivo else ""),
                      desfazer=lambda r: {"op": "reabrir_comprovante", "comprovante_id": comprovante_id})
        novo, reg = executar_e_registrar(tx, u, op)
        return {**novo, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def preparar_reabrir(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    c = await tx.obter("comprovantes", u.tenant_id, receita["comprovante_id"])
    if not c:
        raise NaoEncontrado("Comprovante não encontrado.")
    if c["status"] != StatusComprovante.DESCARTADO.value:
        raise Conflito("Este comprovante não está mais descartado.")

    def aplicar():
        campos = {"status": StatusComprovante.A_REVISAR.value, "motivo_descarte": "", "atualizado_em": _agora()}
        tx.atualizar("comprovantes", u.tenant_id, c["id"], campos)
        return {**c, **campos}

    return Operacao(obra_id=c["obra_id"], aplicar=aplicar,
                    texto=lambda r: "Desfeito: o comprovante descartado voltou para a revisão",
                    desfazer=lambda r: None)


# --- leitura dos gastos ---

async def listar_gastos(arm, u: UsuarioAtual, obra_id: str, etapa: str | None, limite: int,
                        antes_de: datetime | None) -> list[dict]:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    filtros = [Filtro("obra_id", "==", obra_id), Filtro("estornado", "==", False)]
    if etapa:
        filtros.append(Filtro("etapa", "==", etapa))
    return await listar_pagina(arm, Consulta("gastos", u.tenant_id, filtros, [("criado_em", "desc")], limite=limite),
                               "criado_em", antes_de)


async def resumo(arm, u: UsuarioAtual, obra_id: str) -> dict:
    """Gastos por etapa e tópico, somados na API (sem agregação no banco, para ser portável). Inclui etapa e tópico
    já arquivados: o dinheiro que saiu continua lá."""
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    gastos = await arm.consultar(Consulta("gastos", u.tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                   Filtro("estornado", "==", False)]))
    a_revisar = await arm.consultar(Consulta("comprovantes", u.tenant_id, [
        Filtro("obra_id", "==", obra_id), Filtro("status", "==", StatusComprovante.A_REVISAR.value)]))
    extras, topicos = await _extras_e_topicos(arm, u.tenant_id, obra_id)
    extras_por_id = {x["id"]: x for x in extras}
    topicos_por_id = {t["id"]: t for t in topicos}
    ordem = {e.chave: i for i, e in enumerate(montar_arvore(extras, []))}

    por_etapa: dict[str, dict[str | None, int]] = {}
    pedidos = 0
    for g in gastos:
        if not conta_no_realizado(g):
            pedidos += g["valor"]
            continue
        t = por_etapa.setdefault(g["etapa"], {})
        t[g.get("topico_id")] = t.get(g.get("topico_id"), 0) + g["valor"]

    etapas = []
    for chave in sorted(por_etapa, key=lambda k: (ordem.get(k, len(ordem)), k)):
        por_topico = por_etapa[chave]
        itens = [{"topico_id": tid, "total": v,
                  "nome": topicos_por_id[tid]["nome"] if tid in topicos_por_id else "Sem tópico"}
                 for tid, v in por_topico.items()]
        itens.sort(key=lambda x: (x["topico_id"] is None, x["nome"]))
        etapas.append({"etapa": chave, "nome": _nome_etapa(chave, extras_por_id), "total": sum(por_topico.values()),
                       "topicos": itens})
    return {"total": sum(e["total"] for e in etapas), "pagamentos_de_pedidos": pedidos, "a_revisar": len(a_revisar),
            "etapas": etapas}
