"""O que o arquivo do comprovante é, pelos bytes, e não pelo `Content-Type` que o cliente diz.

O tipo declarado é palpite do navegador (ou mentira de quem chama a API direto): o arquivo vai para o modelo e para o
navegador de quem revisa, então vale o que os bytes dizem. As medidas da imagem saem do cabeçalho, sem decodificar:
a Anthropic recusa imagem com lado acima de 8000 px, e um print comprido de Pix passa disso.
"""
import struct
from dataclasses import dataclass

LADO_MAX = 8000


@dataclass(frozen=True)
class TipoArquivo:
    tipo: str                               # image/jpeg, image/png, image/webp, application/pdf
    extensao: str
    medidas: tuple[int, int] | None = None  # (largura, altura) da imagem, quando o cabeçalho diz


def _png(b: bytes) -> tuple[int, int] | None:
    # IHDR é sempre o primeiro bloco: largura e altura logo depois da assinatura e do nome do bloco
    return struct.unpack(">II", b[16:24]) if len(b) >= 24 and b[12:16] == b"IHDR" else None


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(b: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            return None
        marcador = b[i + 1]
        if marcador == 0xFF:            # preenchimento entre marcadores
            i += 1
            continue
        if marcador in (0xD8, 0x01) or 0xD0 <= marcador <= 0xD7:
            i += 2
            continue
        tamanho = struct.unpack(">H", b[i + 2:i + 4])[0]
        if marcador in _SOF:
            altura, largura = struct.unpack(">HH", b[i + 5:i + 9])
            return largura, altura
        i += 2 + tamanho
    return None


def _webp(b: bytes) -> tuple[int, int] | None:
    bloco = b[12:16]
    if bloco == b"VP8X" and len(b) >= 30:
        return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
    if bloco == b"VP8 " and len(b) >= 30:
        largura, altura = struct.unpack("<HH", b[26:30])
        return largura & 0x3FFF, altura & 0x3FFF
    if bloco == b"VP8L" and len(b) >= 25:
        v = int.from_bytes(b[21:25], "little")
        return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
    return None


def detectar(conteudo: bytes) -> TipoArquivo | None:
    """O tipo pelos primeiros bytes; None = não é foto (JPG, PNG, WEBP) nem PDF."""
    if conteudo.startswith(b"\xff\xd8\xff"):
        return TipoArquivo("image/jpeg", "jpg", _jpeg(conteudo))
    if conteudo.startswith(b"\x89PNG\r\n\x1a\n"):
        return TipoArquivo("image/png", "png", _png(conteudo))
    if conteudo[:4] == b"RIFF" and conteudo[8:12] == b"WEBP":
        return TipoArquivo("image/webp", "webp", _webp(conteudo))
    if conteudo.startswith(b"%PDF-"):
        return TipoArquivo("application/pdf", "pdf")
    return None
