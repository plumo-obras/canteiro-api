"""Configuração lida do ambiente (ou do `.env` local, que nunca vai para o git). Nenhum valor secreto tem padrão."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_RAIZ = Path(__file__).resolve().parents[2]  # raiz do repositório (src/canteiro_api/config.py)


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=_RAIZ / ".env", extra="ignore")

    # desenvolvimento | producao. Em produção a API recusa subir com o que perderia dado (veja `conferir`).
    ambiente: str = "desenvolvimento"

    armazenamento: str = "mongo"                 # mongo | memoria
    mongo_url: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongo_db: str = "canteiro"

    jwt_segredo: str = ""
    acesso_minutos: int = 15
    renovacao_dias: int = 30
    # Token de renovação já trocado que reaparece dentro desta janela recebe 409 em vez de derrubar a família: é a
    # renovação que estava no ar quando a página recarregou (o navegador não guardou o cookie novo). Curta de
    # propósito: depois dela, reuso volta a ser tratado como vazamento.
    janela_reuso_renovacao_segundos: int = 10

    # token com que o serviço do agente fala com as rotas internas (POST /uso, GET /interno/chave-anthropic)
    token_servico_agente: str = ""
    # 32 bytes em base64: cifra a chave própria dos clientes no banco (depois: Secret Manager)
    chave_cifra: str = ""

    # IPs/redes (vírgula) dos proxies de confiança (ex.: o servidor do Next). Vazio = nunca ler X-Forwarded-For.
    trusted_proxies: str = ""
    # Segredo que o Next (Vercel, sem IP fixo) manda em X-Canteiro-Proxy junto com X-Canteiro-Ip-Cliente.
    # Com ele, a API aceita o IP do visitante informado pelo Next para o limite de tentativas. Vazio = desligado.
    proxy_segredo: str = ""
    # Cabeçalho com o IP do visitante que a borda SOBRESCREVE (o cliente não consegue forjar). No Render, que põe a
    # Cloudflare na frente de tudo e não limpa o X-Forwarded-For enviado pelo cliente: "cf-connecting-ip".
    ip_cabecalho: str = ""

    # limite de tentativas de login (janela deslizante simples)
    tentativas_por_email: int = 5
    tentativas_por_ip: int = 30
    janela_tentativas_minutos: int = 15
    # cadastros na lista de espera por IP na mesma janela (rota pública, toda chamada conta)
    lista_espera_por_ip: int = 10

    # origens (vírgula) que podem chamar a API do navegador — hoje só a lista de espera da landing. Nunca "*".
    cors_origens: str = "http://localhost:4321,http://127.0.0.1:4321"

    # Onde ficam os arquivos dos comprovantes: banco (padrão; pedaços no próprio banco), supabase (bucket privado do
    # Supabase Storage), disco (pasta local, só desenvolvimento: o disco do contêiner some a cada deploy) ou memoria
    # (testes). Em produção, só banco ou supabase.
    arquivos: str = "banco"
    arquivos_dir: str = str(_RAIZ / ".arquivos")
    # ARQUIVOS=supabase: URL do projeto e a chave secreta (sb_secret_... ou a service_role antiga). A chave só existe no
    # ambiente do Render: ela passa por cima de qualquer regra de acesso do projeto.
    supabase_url: str = ""
    supabase_chave_servico: str = ""
    supabase_bucket: str = "comprovantes"

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
        if self.ambiente not in ("desenvolvimento", "producao"):
            raise RuntimeError(f"AMBIENTE desconhecido: {self.ambiente!r} (use desenvolvimento ou producao).")
        fora = self.arquivos not in ("banco", "supabase") or self.armazenamento != "mongo"
        if self.ambiente == "producao" and fora:
            # Comprovante é a prova do gasto: no disco do Render ele some no próximo deploy ou quando o serviço dorme.
            # Melhor não subir do que aceitar o envio e perder o arquivo antes da revisão.
            raise RuntimeError("Em produção, ARQUIVOS tem de ser banco ou supabase e ARMAZENAMENTO tem de ser mongo "
                               "(disco e memória perdem os comprovantes a cada deploy).")
        if self.arquivos == "supabase" and not (self.supabase_url.startswith("https://")
                                                and self.supabase_chave_servico):
            # falha na subida, não no primeiro envio de comprovante
            raise RuntimeError("ARQUIVOS=supabase precisa de SUPABASE_URL (https) e SUPABASE_CHAVE_SERVICO.")
