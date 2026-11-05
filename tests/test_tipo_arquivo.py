"""Tipo e medidas do arquivo pelo cabeçalho, sem decodificar a imagem."""
import struct

from canteiro_api.servicos.tipo_arquivo import detectar


def test_jpeg_com_medidas_no_sof():
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 1200, 800) + b"\x03" + b"\x00" * 9
    t = detectar(b"\xff\xd8" + app0 + sof + b"\xff\xd9")
    assert (t.tipo, t.extensao, t.medidas) == ("image/jpeg", "jpg", (800, 1200))


def test_png_webp_e_pdf():
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 640, 480)
    assert detectar(png).medidas == (640, 480)
    vp8x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (1999).to_bytes(3, "little") + (2999).to_bytes(3, "little")
    assert (detectar(vp8x).tipo, detectar(vp8x).medidas) == ("image/webp", (2000, 3000))
    assert detectar(b"%PDF-1.4\n...").tipo == "application/pdf"


def test_o_que_nao_e_foto_nem_pdf():
    for b in (b"", b"oi", b"GIF89a....", b"<svg xmlns=", b"MZ\x90\x00"):
        assert detectar(b) is None


def test_jpeg_sem_sof_nao_quebra():
    assert detectar(b"\xff\xd8\xff\xe0lixo").medidas is None
