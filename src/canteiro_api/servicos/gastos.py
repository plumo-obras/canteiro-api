"""Gastos da obra: árvore (etapas do orçamento ou a lista padrão, extras e tópicos), comprovantes a revisar e gastos
confirmados.

Fluxo: a pessoa envia o comprovante → o serviço do agente lê (IA) e grava a leitura → a revisão abre com o máximo
preenchido → uma pessoa (admin) confirma e só então nasce o `Gasto`, com registro e Desfazer. A IA nunca grava gasto.
"""
import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.etapas import (
    CHAVES_FIXAS,
    EtapaNaArvore,
    ativar_extra,
    ativar_topico,
    chave_extra,
    indice_da_arvore,
    montar_arvore,
    nome_da_etapa,
    nomes_da_base,
    origem_da_etapa,
    validar_nome,
)
from canteiro_dominio.gastos import (
    gastos_no_orcamento,
    soma_nos_gastos_avulsos,
    validar_gasto,
    validar_reclassificacao,
)
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
from ..esquemas import ConfirmarCorpo, EdicaoArvoreCorpo, FiscalGastoCorpo, LeituraCorpo, ReclassificarCorpo
from ..modelos import doc
from ..nucleo import (
    Conflito,
    ErroNegocio,
    GrandeDemais,
    NaoEncontrado,
    Operacao,
    evento,
    executar_e_registrar,
    listar_pagina,
    obra_do_tenant,
    receita_inversa,
    verbo_desfazer,
)
from . import fornecedores, tipo_arquivo, versoes
from .estoque import id_derivado

_SP = ZoneInfo("America/Sao_Paulo")
# Limites por tipo. A web repassa o envio por uma função da Vercel, que recusa corpo acima de 4,5 MB com um 413 sem
# explicação: com 4 MiB de arquivo + o envelope do multipart, o corpo cabe lá e quem passa do limite recebe a nossa
# mensagem. A imagem vai ao modelo em base64 (+1/3), e a Anthropic recusa imagem acima de 5 MB: 3,5 MB cru fica abaixo
# disso. HEIC (foto de iPhone) a tela converte para JPEG antes de enviar.
MAX_ARQUIVO = 4 * 1024 * 1024
MAX_IMAGEM = 3_500_000
MAX_LEGENDA = 300


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


async def base_da_arvore(leitor, tenant_id: str, obra_id: str) -> list[tuple[str, str]]:
    """As etapas do orçamento vigente, (código, descrição), na ordem dele: as mesmas de `GET …/etapas` e da apropriação
    de pedidos. Vazio = obra sem orçamento, e a árvore usa a lista padrão.

    Lida fora da transação (é consulta, e a porta não consulta dentro dela). Uma importação de orçamento no mesmo
    segundo de um tópico novo pode deixar o tópico numa etapa que acabou de sair: ele só não aparece."""
    return [(e.etapa, e.descricao) for e in await versoes.etapas_da_obra(leitor, tenant_id, obra_id)]


def _chaves_base(base: list[tuple[str, str]]):
    return {c for c, _ in base if c} or CHAVES_FIXAS


async def arvore_da_obra(leitor, tenant_id: str, obra_id: str,
                         base: list[tuple[str, str]] | None = None) -> list[EtapaNaArvore]:
    await obra_do_tenant(leitor, tenant_id, obra_id)
    if base is None:
        base = await base_da_arvore(leitor, tenant_id, obra_id)
    return montar_arvore(*await _extras_e_topicos(leitor, tenant_id, obra_id), base)


async def _nomes(leitor, tenant_id: str, obra_id: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """{etapa do orçamento: descrição}, {id da extra: nome} e {id do tópico: nome}, com os arquivados: gasto antigo
    continua com nome."""
    base = await base_da_arvore(leitor, tenant_id, obra_id)
    extras, topicos = await _extras_e_topicos(leitor, tenant_id, obra_id)
    return dict(base), {x["id"]: x["nome"] for x in extras}, {t["id"]: t["nome"] for t in topicos}


def _com_etapa(g: dict, orcamento: dict[str, str], extras: dict[str, str]) -> dict:
    """O gasto com o nome e a origem da etapa dele: a lista de lançados mostra "sem etapa do orçamento" sem a tela
    precisar cruzar com a árvore."""
    return {**g, "etapa_nome": nome_da_etapa(g["etapa"], orcamento=orcamento, extras=extras),
            "etapa_origem": origem_da_etapa(g["etapa"], orcamento)}


def _rotulo(etapa: str, topico_id: str | None, orcamento: dict[str, str], extras: dict[str, str],
            topicos: dict[str, str]) -> str:
    nome = nome_da_etapa(etapa, orcamento=orcamento, extras=extras)
    return f"{nome} › {topicos[topico_id]}" if topico_id in topicos else nome


def _id_indice(tenant_id: str, obra_id: str) -> str:
    return id_derivado(tenant_id, "arvore", obra_id)


async def _indice_inicial(arm, tenant_id: str, obra_id: str) -> dict | None:
    """Fora da transação: o índice montado das coleções, só para a obra que ainda não tem um (None se já tem). Se
    outro pedido criar o índice no meio do caminho, a transação lê o dele e ignora este."""
    if await arm.obter("arvores", tenant_id, _id_indice(tenant_id, obra_id)):
        return None
    return indice_da_arvore(*await _extras_e_topicos(arm, tenant_id, obra_id))


async def _indice_na_tx(tx, tenant_id: str, obra_id: str, inicial: dict | None) -> dict | None:
    d = await tx.obter("arvores", tenant_id, _id_indice(tenant_id, obra_id))
    if d:
        return {"extras": dict(d.get("extras") or {}), "topicos": dict(d.get("topicos") or {})}
    return inicial


def _gravar_indice(tx, tenant_id: str, obra_id: str, indice: dict) -> None:
    # gravado sempre que lido para decidir: é o que serializa duas mudanças na árvore ao mesmo tempo
    tx.salvar("arvores", {"id": _id_indice(tenant_id, obra_id), "tenant_id": tenant_id, "obra_id": obra_id,
                          "extras": indice["extras"], "topicos": indice["topicos"], "atualizado_em": _agora()})


async def _exigir_indice(tx, tenant_id: str, obra_id: str, inicial: dict | None) -> dict:
    indice = await _indice_na_tx(tx, tenant_id, obra_id, inicial)
    if indice is None:      # só se o índice sumisse entre a leitura de fora e a transação (a porta não apaga)
        raise Conflito("A árvore da obra mudou agora há pouco; tente de novo.")
    return indice


async def criar_etapa_extra(arm, u: UsuarioAtual, obra_id: str, nome: str) -> dict:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    inicial = await _indice_inicial(arm, u.tenant_id, obra_id)
    base = await base_da_arvore(arm, u.tenant_id, obra_id)

    async def corpo(tx):
        # limite e nome conferidos DENTRO da transação, contra o índice: duas criações simultâneas não furam
        indice = await _exigir_indice(tx, u.tenant_id, obra_id, inicial)
        limpo = ativar_extra(indice["extras"], id_=None, nome=nome, nomes_base=nomes_da_base(base))
        d = doc(EtapaExtra(tenant_id=u.tenant_id, obra_id=obra_id, nome=limpo))

        def aplicar():
            tx.criar("etapas_extras", d)
            _gravar_indice(tx, u.tenant_id, obra_id, {**indice, "extras": {**indice["extras"], d["id"]: limpo}})
            return d

        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=lambda r: f"Criou a etapa {limpo}",
                      desfazer=lambda r: _receita_criacao("etapas_extras", d["id"]))
        _, reg = executar_e_registrar(tx, u, op)
        return {**d, "chave": chave_extra(d["id"]), "registro_id": reg["id"]}

    return await arm.em_transacao(corpo)


async def criar_topico(arm, u: UsuarioAtual, obra_id: str, etapa: str, nome: str) -> dict:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    inicial = await _indice_inicial(arm, u.tenant_id, obra_id)
    base = await base_da_arvore(arm, u.tenant_id, obra_id)

    async def corpo(tx):
        indice = await _exigir_indice(tx, u.tenant_id, obra_id, inicial)
        limpo = ativar_topico(indice["extras"], indice["topicos"], id_=None, etapa=etapa, nome=nome,
                              chaves_base=_chaves_base(base))
        nome_etapa = nome_da_etapa(etapa, orcamento=dict(base), extras=indice["extras"])
        d = doc(Topico(tenant_id=u.tenant_id, obra_id=obra_id, etapa=etapa, nome=limpo))

        def aplicar():
            tx.criar("topicos", d)
            topicos = {**indice["topicos"], d["id"]: {"etapa": etapa, "nome": limpo}}
            _gravar_indice(tx, u.tenant_id, obra_id, {**indice, "topicos": topicos})
            return d

        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=lambda r: f"Criou o tópico {limpo} em {nome_etapa}",
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


def _indice_com(indice: dict, colecao: str, item: dict, mudou: dict, base: list[tuple[str, str]] | None) -> dict:
    """O índice depois de `item` receber `mudou` ({nome?, arquivado?}). Confere limite e nome quando o item termina
    ativo (criado, renomeado ou reativado); arquivado só sai do índice. `base` = as etapas do orçamento lidas antes da
    transação (None = não se sabe: confere o nome contra a lista padrão e não confere a etapa do tópico)."""
    final = {**item, **mudou}
    extras, topicos = dict(indice["extras"]), dict(indice["topicos"])
    alvo = extras if colecao == "etapas_extras" else topicos
    if final.get("arquivado"):
        if "nome" in mudou:     # renomear um arquivado: sem irmãos para comparar, mas o nome continua com regra
            validar_nome(final["nome"])
        alvo.pop(item["id"], None)
    elif colecao == "etapas_extras":
        extras[item["id"]] = ativar_extra(extras, id_=item["id"], nome=final["nome"],
                                          nomes_base=nomes_da_base(base) if base is not None else None)
    else:
        nome = ativar_topico(extras, topicos, id_=item["id"], etapa=item["etapa"], nome=final["nome"],
                             chaves_base=_chaves_base(base) if base is not None else None)
        topicos[item["id"]] = {"etapa": item["etapa"], "nome": nome}
    return {"extras": extras, "topicos": topicos}


async def editar_item_arvore(arm, u: UsuarioAtual, colecao: str, id_: str, corpo: EdicaoArvoreCorpo) -> dict:
    atual = await arm.obter(colecao, u.tenant_id, id_)
    if not atual:
        raise NaoEncontrado("Etapa não encontrada." if colecao == "etapas_extras" else "Tópico não encontrado.")
    obra_id = atual["obra_id"]
    mudancas = corpo.model_dump(exclude_none=True)
    inicial = await _indice_inicial(arm, u.tenant_id, obra_id)
    base = await base_da_arvore(arm, u.tenant_id, obra_id)

    async def corpo_tx(tx):
        d = await tx.obter(colecao, u.tenant_id, id_)
        indice = await _exigir_indice(tx, u.tenant_id, obra_id, inicial)
        pedido = dict(mudancas)
        if "nome" in pedido:
            pedido["nome"] = " ".join(pedido["nome"].split())
        mudou = {k: v for k, v in pedido.items() if d.get(k) != v}
        if not mudou:
            raise ErroNegocio("Nada mudou.")
        # reativar também confere o nome: enquanto estava arquivado, outro pode ter nascido com ele
        novo_indice = _indice_com(indice, colecao, d, mudou, base)
        antes = {k: d.get(k) for k in mudou}

        def aplicar():
            tx.atualizar(colecao, u.tenant_id, id_, {**mudou, "atualizado_em": _agora()})
            _gravar_indice(tx, u.tenant_id, obra_id, novo_indice)
            return {**d, **mudou}

        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=lambda r: _texto_edicao(colecao, d, mudou),
                      desfazer=lambda r: {"op": "editar_arvore", "colecao": colecao, "id": id_, "antes": antes,
                                          "depois": mudou})
        novo, reg = executar_e_registrar(tx, u, op)
        extra = {"chave": chave_extra(id_)} if colecao == "etapas_extras" else {}
        return {**novo, **extra, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def base_para_desfazer_arvore(leitor, tenant_id: str, receita: dict) -> list[tuple[str, str]] | None:
    """Fora da transação, antes do Desfazer/Refazer de uma edição da árvore: as etapas do orçamento daquela obra."""
    d = await leitor.obter(receita["colecao"], tenant_id, receita["id"])
    return await base_da_arvore(leitor, tenant_id, d["obra_id"]) if d else None


async def preparar_desfazer_arvore(tx, u: UsuarioAtual, receita: dict,
                                   base: list[tuple[str, str]] | None = None) -> Operacao:
    colecao = receita["colecao"]
    d = await tx.obter(colecao, u.tenant_id, receita["id"])
    if not d:
        raise NaoEncontrado("Não encontrado.")
    if any(d.get(k) != v for k, v in receita["depois"].items()):
        raise Conflito(f"{d['nome']} foi editado de novo depois; não dá para desfazer esta mudança.")
    # Refazer uma criação reativa, e desfazer um renomear devolve o nome antigo: os dois passam pelas mesmas regras
    # (limite e nome único). Obra sem índice (gravada antes dele) só acontece em dado antigo de desenvolvimento: aplica
    # sem conferir, e o índice nasce na próxima criação.
    indice = await _indice_na_tx(tx, u.tenant_id, d["obra_id"], None)
    novo_indice = _indice_com(indice, colecao, d, receita["antes"], base) if indice is not None else None

    def aplicar():
        tx.atualizar(colecao, u.tenant_id, d["id"], {**receita["antes"], "atualizado_em": _agora()})
        if novo_indice is not None:
            _gravar_indice(tx, u.tenant_id, d["obra_id"], novo_indice)
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
    if not conteudo:
        raise ErroNegocio("O arquivo chegou vazio. Tente de novo.")
    # vale o que os bytes dizem, não o tipo que o cliente declarou: o navegador chuta pela extensão (print de PNG salvo
    # como .jpg chega "image/jpeg"), e é o tipo real que vai para a leitura e para o navegador de quem revisa
    detectado = tipo_arquivo.detectar(conteudo)
    if detectado is None:
        raise ErroNegocio("Envie uma foto (JPG, PNG ou WEBP) ou um PDF.")
    eh_pdf = detectado.tipo == "application/pdf"
    if len(conteudo) > (MAX_ARQUIVO if eh_pdf else MAX_IMAGEM):
        raise GrandeDemais("PDF maior que 4 MB." if eh_pdf
                           else "Foto maior que 3,5 MB. Tire de novo com menos resolução ou reduza antes de enviar.")
    if detectado.medidas and max(detectado.medidas) > tipo_arquivo.LADO_MAX:
        raise ErroNegocio(f"Imagem comprida demais (mais de {tipo_arquivo.LADO_MAX} px): recorte só o comprovante.")
    legenda = " ".join(legenda.split())
    if len(legenda) > MAX_LEGENDA:
        raise ErroNegocio(f"A legenda pode ter até {MAX_LEGENDA} caracteres.")
    sha = hashlib.sha256(conteudo).hexdigest()
    id_ = id_comprovante(u.tenant_id, obra_id, sha)
    if ja := await arm.obter("comprovantes", u.tenant_id, id_):
        return ja, True
    caminho = f"{obra_id}/{id_}.{detectado.extensao}"
    # o arquivo antes do documento: documento sem arquivo quebraria a revisão; arquivo sem documento é só sobra
    await arquivos.salvar(u.tenant_id, caminho, conteudo)
    d = doc(Comprovante(id=id_, tenant_id=u.tenant_id, obra_id=obra_id, arquivo=caminho, tipo_conteudo=detectado.tipo,
                        tamanho=len(conteudo), sha256=sha, legenda=legenda,
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
            ja_pago = pedido.get("pago", 0)
            if ja_pago + corpo.valor > pedido["valor"]:
                # pagar mais que o pedido é engano (comprovante de outro pedido, valor digitado errado) ou um acréscimo
                # que ainda não está no pedido: nos dois casos, quem decide é a pessoa, não a soma
                raise Conflito(f"Com este gasto, o pago do {pedido['numero']} passaria do valor do pedido "
                               f"({fmt_reais(ja_pago)} já pagos de {fmt_reais(pedido['valor'])}). Confira o valor, "
                               "ligue a outro pedido ou confirme sem pedido.")

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
    pagina = await listar_pagina(arm, Consulta("gastos", u.tenant_id, filtros, [("criado_em", "desc")],
                                               limite=limite), "criado_em", antes_de)
    orcamento, extras, _ = await _nomes(arm, u.tenant_id, obra_id)
    return [_com_etapa(g, orcamento, extras) for g in pagina]


async def resumo(arm, u: UsuarioAtual, obra_id: str) -> dict:
    """Gastos por etapa e tópico, somados na API (sem agregação no banco, para ser portável). Inclui etapa e tópico
    já arquivados, e a etapa que não é mais da árvore (lista padrão de antes do orçamento): o dinheiro que saiu
    continua lá, marcado "sem etapa do orçamento", depois das outras."""
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    gastos = await versoes.gastos_da_obra(arm, u.tenant_id, obra_id)
    a_revisar = await arm.consultar(Consulta("comprovantes", u.tenant_id, [
        Filtro("obra_id", "==", obra_id), Filtro("status", "==", StatusComprovante.A_REVISAR.value)]))
    base = await base_da_arvore(arm, u.tenant_id, obra_id)
    orcamento = dict(base)
    extras, topicos = await _extras_e_topicos(arm, u.tenant_id, obra_id)
    extras_nomes = {x["id"]: x["nome"] for x in extras}
    topicos_por_id = {t["id"]: t for t in topicos}
    ordem = {e.chave: i for i, e in enumerate(montar_arvore(extras, [], base))}

    por_etapa: dict[str, dict[str | None, int]] = {}
    pedidos = 0
    for g in gastos:
        if not soma_nos_gastos_avulsos(g):
            pedidos += g["valor"]
            continue
        t = por_etapa.setdefault(g["etapa"], {})
        t[g.get("topico_id")] = t.get(g.get("topico_id"), 0) + g["valor"]

    def posicao(k: str):
        return (origem_da_etapa(k, orcamento) == "sem_etapa_do_orcamento", ordem.get(k, len(ordem)), k)

    etapas = []
    for chave in sorted(por_etapa, key=posicao):
        por_topico = por_etapa[chave]
        itens = [{"topico_id": tid, "total": v,
                  "nome": topicos_por_id[tid]["nome"] if tid in topicos_por_id else "Sem tópico"}
                 for tid, v in por_topico.items()]
        itens.sort(key=lambda x: (x["topico_id"] is None, x["nome"]))
        etapas.append({"etapa": chave, "nome": nome_da_etapa(chave, orcamento=orcamento, extras=extras_nomes),
                       "origem": origem_da_etapa(chave, orcamento), "total": sum(por_topico.values()),
                       "topicos": itens})
    # a mesma repartição que o orçamento usa (domínio): o que aparece aqui fecha com o realizado de lá
    no_orcamento = gastos_no_orcamento(gastos, orcamento)
    return {"total": no_orcamento.total, "pagamentos_de_pedidos": pedidos, "a_revisar": len(a_revisar),
            "tem_orcamento": bool(orcamento), "fora_do_orcamento": no_orcamento.fora_do_orcamento,
            "sem_etapa_do_orcamento": no_orcamento.sem_etapa_do_orcamento, "etapas": etapas}


# --- reclassificar: mudar a etapa de um gasto já confirmado ---

def _chave_rotulo(etapa: str, topico_id: str | None) -> str:
    return f"{etapa}|{topico_id or ''}"


async def reclassificar(arm, u: UsuarioAtual, gasto_id: str, corpo: ReclassificarCorpo) -> dict:
    """Leva um gasto para outra etapa (e tópico) da árvore de hoje. É o caminho do gasto "sem etapa do orçamento"
    (lançado na lista padrão antes de a obra importar orçamento) até a etapa certa do orçamento, onde ele passa a
    somar no realizado. Com Desfazer, que devolve a etapa de antes (mesmo que ela não esteja mais na árvore)."""
    g = await arm.obter("gastos", u.tenant_id, gasto_id)
    if not g:
        raise NaoEncontrado("Gasto não encontrado.")
    if g.get("estornado"):
        raise Conflito("Este gasto foi desfeito: não dá para mudar a etapa.")
    obra_id = g["obra_id"]
    base = await base_da_arvore(arm, u.tenant_id, obra_id)
    validar_reclassificacao(g, etapa=corpo.etapa, topico_id=corpo.topico_id,
                            arvore=await arvore_da_obra(arm, u.tenant_id, obra_id, base))
    orcamento, extras, topicos = await _nomes(arm, u.tenant_id, obra_id)
    antes = {"etapa": g["etapa"], "topico_id": g.get("topico_id")}
    depois = {"etapa": corpo.etapa, "topico_id": corpo.topico_id}
    # os nomes vão na receita: o Desfazer roda dentro da transação, onde não dá para consultar o orçamento
    rotulos = {_chave_rotulo(**x): _rotulo(x["etapa"], x["topico_id"], orcamento, extras, topicos)
               for x in (antes, depois)}

    async def corpo_tx(tx):
        atual = await tx.obter("gastos", u.tenant_id, gasto_id)
        if atual.get("estornado"):
            raise Conflito("Este gasto foi desfeito: não dá para mudar a etapa.")
        if {"etapa": atual["etapa"], "topico_id": atual.get("topico_id")} != antes:
            raise Conflito("A etapa deste gasto mudou agora há pouco. Abra de novo e confira.")

        def aplicar():
            tx.atualizar("gastos", u.tenant_id, gasto_id, depois)
            return {**atual, **depois}

        receita = {"op": "reclassificar_gasto", "gasto_id": gasto_id, "antes": antes, "depois": depois,
                   "rotulos": rotulos}
        op = Operacao(obra_id=obra_id, aplicar=aplicar,
                      texto=lambda r: f"Mudou o gasto de {fmt_reais(g['valor'])} ({g['descricao']}) de "
                                      f"{rotulos[_chave_rotulo(**antes)]} para {rotulos[_chave_rotulo(**depois)]}",
                      desfazer=lambda r: receita)
        novo, reg = executar_e_registrar(tx, u, op)
        evento(tx, u.tenant_id, "gasto.reclassificado", obra_id, {"gasto_id": gasto_id})
        return {**_com_etapa(novo, orcamento, extras), "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def preparar_reclassificar(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfazer (e Refazer) a mudança de etapa. Volta para a etapa de antes sem conferir a árvore: é o estado que o
    gasto já teve, e a pessoa pode reclassificar de novo. Se a etapa do gasto mudou depois, não desfaz por cima."""
    g = await tx.obter("gastos", u.tenant_id, receita["gasto_id"])
    if not g:
        raise NaoEncontrado("Gasto não encontrado.")
    if g.get("estornado"):
        raise Conflito("Este gasto foi desfeito depois; não dá para mudar a etapa dele.")
    if {"etapa": g["etapa"], "topico_id": g.get("topico_id")} != receita["depois"]:
        raise Conflito("A etapa deste gasto mudou de novo depois; não dá para desfazer esta mudança.")
    volta = receita["antes"]

    def aplicar():
        tx.atualizar("gastos", u.tenant_id, g["id"], volta)
        return {**g, **volta}

    nome = receita.get("rotulos", {}).get(_chave_rotulo(**volta), volta["etapa"])
    return Operacao(obra_id=g["obra_id"], aplicar=aplicar,
                    texto=lambda r: f"{verbo_desfazer(receita)}: o gasto de {fmt_reais(g['valor'])} "
                                    f"({g['descricao']}) voltou para {nome}",
                    desfazer=lambda r: receita_inversa(receita))


# --- natureza fiscal: o que o pagamento comprova no INSS da obra e o IBS/CBS da nota ---

ROTULOS_FISCAIS = {
    None: "a classificar", "folha": "folha (eSocial)", "mao_de_obra_nf": "mão de obra com nota",
    "gps": "GPS paga com o CNO", "autonomo_mei": "autônomo ou MEI no eSocial",
    "pre_moldado": "pré-moldado com nota", "concreto_usinado": "concreto usinado com nota",
    "mao_de_obra_sem_nf": "mão de obra sem nota", "material": "material", "equipamento": "equipamento",
    "outros": "outros",
}


_CAMPOS_FISCAIS = {"natureza_fiscal": None, "ibs_cbs": 0, "nf_cno": None, "retencao": 0}


def _fiscal_de(g: dict) -> dict:
    return {k: g.get(k, padrao) for k, padrao in _CAMPOS_FISCAIS.items()}


def _texto_fiscal(g: dict, f: dict) -> str:
    detalhes = [x for x in ("com o CNO da obra" if f.get("nf_cno") else "",
                            f"retenção {fmt_reais(f['retencao'])}" if f.get("retencao") else "",
                            f"IBS/CBS {fmt_reais(f['ibs_cbs'])}" if f.get("ibs_cbs") else "") if x]
    return (f"o gasto de {fmt_reais(g['valor'])} ({g['descricao']}) como {ROTULOS_FISCAIS[f['natureza_fiscal']]}"
            + (f" ({', '.join(detalhes)})" if detalhes else ""))


async def classificar_fiscal(arm, u: UsuarioAtual, gasto_id: str, corpo: FiscalGastoCorpo) -> dict:
    """Marca o que o gasto comprova para os tributos. Com Desfazer, como a mudança de etapa."""
    g = await arm.obter("gastos", u.tenant_id, gasto_id)
    if not g:
        raise NaoEncontrado("Gasto não encontrado.")
    if corpo.ibs_cbs > g["valor"] or corpo.retencao > g["valor"]:
        raise ErroNegocio("O IBS/CBS e a retenção da nota não podem passar do valor do gasto.")
    obra_id = g["obra_id"]
    orcamento, extras, _ = await _nomes(arm, u.tenant_id, obra_id)
    # CNO e retenção só fazem sentido na nota de mão de obra: nas outras naturezas voltam ao padrão
    nota = corpo.natureza_fiscal == "mao_de_obra_nf"
    depois = {"natureza_fiscal": corpo.natureza_fiscal, "ibs_cbs": corpo.ibs_cbs,
              "nf_cno": corpo.nf_cno if nota else None, "retencao": corpo.retencao if nota else 0}

    async def corpo_tx(tx):
        atual = await tx.obter("gastos", u.tenant_id, gasto_id)
        if atual.get("estornado"):
            raise Conflito("Este gasto foi desfeito: não dá para classificar.")
        antes = _fiscal_de(atual)
        if antes == depois:
            raise ErroNegocio("Nada mudou.")

        def aplicar():
            tx.atualizar("gastos", u.tenant_id, gasto_id, depois)
            return {**atual, **depois}

        receita = {"op": "fiscal_gasto", "gasto_id": gasto_id, "antes": antes, "depois": depois}
        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=lambda r: f"Classificou {_texto_fiscal(g, depois)}",
                      desfazer=lambda r: receita)
        novo, reg = executar_e_registrar(tx, u, op)
        evento(tx, u.tenant_id, "gasto.fiscal", obra_id, {"gasto_id": gasto_id})
        return {**_com_etapa(novo, orcamento, extras), "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def preparar_desfazer_fiscal(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Volta a classificação de antes, se ninguém classificou de novo depois (senão apagaria a correção)."""
    g = await tx.obter("gastos", u.tenant_id, receita["gasto_id"])
    if not g:
        raise NaoEncontrado("Gasto não encontrado.")
    if g.get("estornado"):
        raise Conflito("Este gasto foi desfeito depois; não dá para mudar a classificação dele.")
    # receita antiga (antes do CNO e da retenção) compara só os campos que ela tem
    if {k: v for k, v in _fiscal_de(g).items() if k in receita["depois"]} != receita["depois"]:
        raise Conflito("A classificação deste gasto mudou de novo depois; não dá para desfazer esta mudança.")
    volta = receita["antes"]

    def aplicar():
        tx.atualizar("gastos", u.tenant_id, g["id"], volta)
        return {**g, **volta}

    return Operacao(obra_id=g["obra_id"], aplicar=aplicar,
                    texto=lambda r: f"{verbo_desfazer(receita)}: voltou {_texto_fiscal(g, volta)}",
                    desfazer=lambda r: receita_inversa(receita))
