"""Limite de tamanho do corpo da requisição, antes de qualquer rota.

O FastAPI lê e interpreta o corpo (multipart inclusive) antes de rodar as dependências de autenticação: sem este
filtro, um POST anônimo de 2 GB em /obras/x/orcamento/ler seria lido inteiro para só então virar 401. Middleware ASGI
puro (sem BaseHTTPMiddleware) porque é preciso ver o fluxo de `receive` mensagem a mensagem, e não o corpo já lido.
"""
import json
import re

MIB = 1024 * 1024
LIMITE_PADRAO = 1 * MIB
# arquivo de até 5 MB (`_ler_arquivo` em rotas/obras.py) + o envelope do multipart e os outros campos do formulário
LIMITE_UPLOAD = 6 * MIB
# comprovante de até 4 MiB (`MAX_ARQUIVO` em servicos/gastos.py) + o envelope e a legenda. Folga de 1 MiB de propósito:
# entre 4 e 5 MiB o corpo chega na rota, que responde 413 dizendo o limite do arquivo (e não o do corpo).
LIMITE_COMPROVANTE = 5 * MIB
# lote de mensagens: até 9 MiB de conteúdo (`MAX_BYTES_LOTE` em servicos/sessoes.py) + o JSON em volta
LIMITE_MENSAGENS = 10 * MIB

_ROTAS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^/obras/[^/]+/(orcamento/(ler|importar|versoes)|importar)$"), LIMITE_UPLOAD),
    (re.compile(r"^/obras/[^/]+/comprovantes$"), LIMITE_COMPROVANTE),
    (re.compile(r"^/sessoes/[^/]+/mensagens$"), LIMITE_MENSAGENS),
]


def limite_da_rota(caminho: str) -> int:
    for padrao, limite in _ROTAS:
        if padrao.match(caminho):
            return limite
    return LIMITE_PADRAO


class _Estourou(Exception):
    pass


class LimiteCorpo:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limite = limite_da_rota(scope["path"])
        tamanho = _content_length(scope)
        if tamanho is not None and tamanho > limite:
            # recusa sem ler um byte do corpo
            return await _recusar(send, limite)

        # Content-Length ausente (chunked) ou mentiroso: conta o que chega de fato e para de ler ao passar do limite.
        lidos = 0
        estado = {"estourou": False, "respondeu": False}

        async def receive_contado():
            nonlocal lidos
            msg = await receive()
            if msg["type"] == "http.request":
                lidos += len(msg.get("body", b""))
                if lidos > limite:
                    estado["estourou"] = True
                    raise _Estourou
            return msg

        async def send_vigiado(msg):
            if estado["estourou"]:
                # A exceção pode ter virado outra resposta lá dentro (o FastAPI transforma erro ao ler o corpo em
                # 400): quem decide a resposta, nesse caso, é este filtro.
                if msg["type"] == "http.response.start" and not estado["respondeu"]:
                    estado["respondeu"] = True
                    await _recusar(send, limite)
                return
            if msg["type"] == "http.response.start":
                estado["respondeu"] = True
            await send(msg)

        try:
            await self.app(scope, receive_contado, send_vigiado)
        except _Estourou:
            if not estado["respondeu"]:
                estado["respondeu"] = True
                await _recusar(send, limite)
        except Exception:
            if not estado["estourou"]:
                raise
            if not estado["respondeu"]:
                estado["respondeu"] = True
                await _recusar(send, limite)


def _content_length(scope) -> int | None:
    for nome, valor in scope.get("headers", []):
        if nome == b"content-length":
            try:
                return int(valor)
            except ValueError:
                return None     # inválido: o servidor HTTP já recusa; aqui cai na contagem do fluxo
    return None


async def _recusar(send, limite: int) -> None:
    corpo = json.dumps({"detail": f"Corpo da requisição maior que o permitido ({limite // MIB} MiB)."},
                       ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(corpo)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": corpo})
