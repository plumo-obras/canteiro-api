"""Documentos que só a API conhece (login, cliente, outbox). Os de negócio ficam em `canteiro_dominio.modelos`."""
import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from canteiro_dominio.modelos import Documento, agora
from canteiro_dominio.tipos import novo_id

from .armazenamento.base import normalizar

# Documentos procurados sem saber o cliente (login por e-mail, renovação opaca, limite de tentativas) moram neste
# tenant de sistema: a porta continua exigindo tenant em toda leitura e nenhum dado de cliente fica aqui.
TENANT_SISTEMA = "_sistema"


class Papel(StrEnum):
    VIEWER = "viewer"
    ADMIN = "admin"
    MASTER = "master"


NIVEL = {Papel.VIEWER: 0, Papel.ADMIN: 1, Papel.MASTER: 2}


def id_de_email(email: str) -> str:
    """ID do índice de e-mail. Hash porque o ID não deve carregar o e-mail cru (e o Firestore limita caracteres)."""
    return "email-" + hashlib.sha256(normalizar_email(email).encode()).hexdigest()


def normalizar_email(email: str) -> str:
    return email.strip().lower()


class Tenant(BaseModel):
    """O cliente. ID = tenant_id (o documento é do próprio tenant)."""
    id: str = Field(default_factory=novo_id)
    tenant_id: str = ""
    nome: str
    saldo_microdolares: int = 0
    franquia_microdolares: int = 0
    chave_propria: bool = False
    criado_em: datetime = Field(default_factory=agora)

    def model_post_init(self, _: Any) -> None:
        self.tenant_id = self.tenant_id or self.id


class Usuario(Documento):
    nome: str
    email: str
    papel: Papel = Papel.VIEWER
    senha_hash: str
    ativo: bool = True
    # Trocar a senha derruba as outras sessões: renovação de família mais antiga e acesso emitido antes disso deixam
    # de valer (ver auth.trocar_senha). None = nunca trocou.
    sessoes_validas_desde: datetime | None = None


class IndiceEmail(BaseModel):
    """Garante um usuário por e-mail (unicidade pelo ID do documento) e diz em que tenant ele está."""
    id: str
    tenant_id: str = TENANT_SISTEMA
    usuario_id: str
    tenant_usuario: str


class Renovacao(BaseModel):
    """Token de renovação: guardamos só o hash. A família liga as trocas sucessivas (reuso revoga todas)."""
    id: str                                   # sha256 do token
    tenant_id: str = TENANT_SISTEMA
    familia: str
    usuario_id: str
    tenant_usuario: str
    expira_em: datetime
    usado: bool = False
    # quando foi trocado: reaparecer logo depois é a corrida de uma recarga de página, não vazamento (ver auth.renovar)
    usado_em: datetime | None = None
    revogado: bool = False
    criado_em: datetime = Field(default_factory=agora)
    # quando a família nasceu (o login), herdado a cada troca: é o que se compara com `Usuario.sessoes_validas_desde`.
    # None = família anterior ao campo, tratada como antiga.
    familia_desde: datetime | None = None


class InscritoEspera(BaseModel):
    """Inscrito na lista de espera da landing. ID = hash do e-mail normalizado: repetir o cadastro não duplica."""
    id: str
    tenant_id: str = TENANT_SISTEMA
    email: str
    nome: str | None = None
    empresa: str | None = None
    papel: str | None = None
    obras_ativas: int | None = None
    origem: str | None = None
    criado_em: datetime = Field(default_factory=agora)


def id_de_inscrito(email: str) -> str:
    return "espera-" + hashlib.sha256(normalizar_email(email).encode()).hexdigest()


# Ainda não há relay em produção (`eventos.publicar_pendentes` só roda nos testes) e toda transação de negócio grava
# um evento: sem prazo, a outbox cresceria para sempre. Sete dias cobrem um relay parado por um fim de semana longo;
# quando o relay existir, evento mais velho que isso já não serve para ninguém (o estado atual está nas coleções).
RETENCAO_OUTBOX = timedelta(days=7)


class EventoOutbox(Documento):
    """Gravado na mesma transação da mudança; o relay publica no Pub/Sub depois."""
    tipo: str                                 # pedido.criado, estoque.movimento…
    obra_id: str | None = None
    chave_ordenacao: str                      # obra (ordem por obra no Pub/Sub)
    dados: dict[str, Any] = {}
    publicado: bool = False
    publicado_em: datetime | None = None
    # instante em que o TTL apaga o evento (Mongo e Firestore leem o vencimento do próprio campo)
    expira_em: datetime | None = None

    def model_post_init(self, _: Any) -> None:
        self.expira_em = self.expira_em or self.criado_em + RETENCAO_OUTBOX


def doc(modelo: BaseModel) -> dict[str, Any]:
    """Documento já no formato do banco (data em milissegundo, enum como texto): o que a rota devolve na 1ª vez é
    igual ao que devolve lido do banco nas seguintes."""
    return normalizar(modelo.model_dump())
