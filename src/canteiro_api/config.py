"""Configuração lida do ambiente (ou do `.env` local, que nunca vai para o git). Nenhum valor secreto tem padrão."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_RAIZ = Path(__file__).resolve().parents[4]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=(_RAIZ / ".env", _RAIZ / "apps/api/.env"), extra="ignore")

    armazenamento: str = "mongo"                 # mongo | memoria
    mongo_url: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongo_db: str = "canteiro"

    jwt_segredo: str = ""
    acesso_minutos: int = 15
    renovacao_dias: int = 30

    # token com que o serviço do agente fala com as rotas internas (POST /uso, GET /interno/chave-anthropic)
    token_servico_agente: str = ""
    # 32 bytes em base64: cifra a chave própria dos clientes no banco (depois: Secret Manager)
    chave_cifra: str = ""

    # IPs/redes (vírgula) dos proxies de confiança (ex.: o servidor do Next). Vazio = nunca ler X-Forwarded-For.
    trusted_proxies: str = ""
    # Segredo que o Next (Vercel, sem IP fixo) manda em X-Canteiro-Proxy junto com X-Canteiro-Ip-Cliente.
    # Com ele, a API aceita o IP do visitante informado pelo Next para o limite de tentativas. Vazio = desligado.
    proxy_segredo: str = ""

    # limite de tentativas de login (janela deslizante simples)
    tentativas_por_email: int = 5
    tentativas_por_ip: int = 30
    janela_tentativas_minutos: int = 15
    # cadastros na lista de espera por IP na mesma janela (rota pública, toda chamada conta)
    lista_espera_por_ip: int = 10

    # origens (vírgula) que podem chamar a API do navegador — hoje só a lista de espera da landing. Nunca "*".
    cors_origens: str = "http://localhost:4321,http://127.0.0.1:4321"

    # créditos iniciais de um tenant novo (microdólares; 1 US$ = 1.000.000)
    franquia_padrao_microdolares: int = 20_000_000

    # seed: o e-mail é público; a senha só existe no .env local
    seed_admin_email: str = "admin@casavilanova.exemplo"
    seed_admin_senha: str = ""

    def origens_cors(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origens.split(",") if o.strip() and o.strip() != "*"]

    def conferir(self) -> None:
        if len(self.jwt_segredo) < 32:
            raise RuntimeError("JWT_SEGREDO ausente ou curto: defina no .env (veja .env.exemplo).")
