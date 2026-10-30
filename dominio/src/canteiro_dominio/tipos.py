"""Tipos portáveis entre MongoDB e Firestore.

O Firestore não tem decimal nem índice único, então: dinheiro em centavos e quantidade em milésimos (inteiros), e IDs
gerados por nós (UUIDv7 ordena no tempo, o que mantém listagens por criação baratas nos dois bancos).
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated
import uuid

from pydantic import Field

# Quantidade em milésimos da unidade do insumo: 1,5 m³ = 1500. Inteiro para não perder precisão em soma de movimentos.
Milesimos = Annotated[int, Field(description="quantidade em milésimos da unidade (1 un = 1000)")]
# Dinheiro em centavos de real.
Centavos = Annotated[int, Field(description="valor em centavos de real")]


def novo_id() -> str:
    return str(uuid.uuid7())


def _decimal(valor: str | int | float | Decimal) -> Decimal:
    """Lê número de planilha brasileira: vírgula é decimal e ponto é milhar.

    "2.760" é ambíguo (2 mil e 760, ou 2,76 em formato inglês?) e é recusado, como o vm-pay já fazia: adivinhar errado
    em estoque custa caro. Aceita "2.760,0", "2760", "1,5", "1.5" (ponto com 1–2 casas é decimal) e números puros.
    """
    if isinstance(valor, (int, Decimal)):
        return Decimal(valor)
    if isinstance(valor, float):
        return Decimal(str(valor))
    t = valor.strip().replace(" ", "")
    if "," in t:
        return Decimal(t.replace(".", "").replace(",", "."))
    if "." in t:
        partes = t.split(".")
        if len(partes) > 2 or len(partes[-1]) == 3:
            raise ValueError(f"'{valor}' é ambíguo: use vírgula para decimal (ex.: 2.760,0 ou 2,76).")
    return Decimal(t)


def qtd(valor: str | int | float | Decimal) -> int:
    """'1,5' / 1.5 / Decimal('1.5') → 1500 milésimos."""
    return int((_decimal(valor) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def reais(valor: str | int | float | Decimal) -> int:
    """'1.299,90' / 1299.9 → 129990 centavos."""
    return int((_decimal(valor) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_qtd(m: int) -> str:
    """1500 → '1,5'; 2760000 → '2.760'."""
    d = Decimal(m) / 1000
    inteiro, _, frac = f"{d:f}".partition(".")
    inteiro = f"{int(inteiro):,}".replace(",", ".")
    frac = frac.rstrip("0")
    return f"{inteiro},{frac}" if frac else inteiro


def fmt_reais(c: int) -> str:
    """129990 → 'R$ 1.299,90'."""
    sinal = "-" if c < 0 else ""
    c = abs(c)
    return f"{sinal}R$ {c // 100:,}".replace(",", ".") + f",{c % 100:02d}"
