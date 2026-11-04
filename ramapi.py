"""
Ligação ao Roblox Account Manager (RAM) através da API HTTP local dele.

O que este módulo faz (e só isto):
  * lê a lista de contas escrita pelo utilizador (parse_accounts);
  * pede ao RAM para LANÇAR uma conta numa experiência (RamClient.launch), com
    o mesmo pedido que o wrapper oficial do RAM usa:
        http://localhost:<porta>/LaunchAccount?Account=<conta>&PlaceId=<id>
        [&JobId=<id>] [&JoinVIP=true] [&Password=<palavra-passe>]
  * testa se o RAM está a responder (RamClient.ping).

O que NÃO faz, de propósito: nunca chama GetCookie nem qualquer outro endpoint
(há uma lista fixa de métodos permitidos) e só fala com 'localhost'. Nunca vê
cookies das tuas contas. A palavra-passe da API nunca aparece em erros nem logs.

Nota: o formato do pedido vem do código público do RAM original (RAMAccount.lua).
Não foi testado contra um RAM real, e o RAM original está arquivado desde 2024.
"""

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

DEFAULT_PORT = 7963
MAX_ACCOUNTS = 20
MAX_PLACE_ID = 2**63 - 1
RAM_HOST = "localhost"  # fixo: a palavra-passe e os comandos nunca saem deste computador
ALLOWED_METHODS = ("LaunchAccount",)  # nada de GetCookie & companhia
MIN_PASSWORD_LEN = 6  # o wrapper oficial do RAM só envia palavras-passe com 6+ caracteres

_NAME = re.compile(r"^[\w.\- ]{1,50}$")
_JOB = re.compile(r"^[0-9A-Za-z-]{8,64}$")


class RamError(Exception):
    """Falha a falar com o RAM. A mensagem nunca contém o URL nem a palavra-passe."""


@dataclass(frozen=True)
class AccountSpec:
    name: str  # nome da conta no RAM
    place_id: int
    job: str = ""  # JobId, ou link de servidor privado (começa por http)

    @property
    def vip(self) -> bool:
        return self.job.lower().startswith("http")


def _parse_place(text: str) -> Optional[int]:
    text = text.strip()
    if not text or not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number <= MAX_PLACE_ID else None


def parse_accounts(text, default_place="") -> Tuple[List[AccountSpec], List[str]]:
    """
    Lê uma conta por linha:   utilizador[, PlaceId[, JobId ou link de servidor privado]]
    Linhas vazias e linhas que começam por # são ignoradas. Se o PlaceId faltar,
    usa 'default_place'. Devolve (contas, erros).
    """
    accounts: List[AccountSpec] = []
    errors: List[str] = []
    seen = set()
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) > 3:
            errors.append(f"Linha {number}: há campos a mais (usa: conta, PlaceId, JobId).")
            continue
        name = parts[0]
        if not _NAME.match(name):
            errors.append(f"Linha {number}: nome de conta inválido ('{name[:30]}').")
            continue
        place_text = parts[1] if len(parts) > 1 and parts[1] else str(default_place or "")
        place = _parse_place(place_text)
        if place is None:
            errors.append(
                f"Linha {number} ({name}): falta um Place ID válido (escreve-o na linha ou no campo Place ID)."
            )
            continue
        job = parts[2] if len(parts) > 2 else ""
        if job and not job.lower().startswith("http") and not _JOB.match(job):
            errors.append(f"Linha {number} ({name}): JobId inválido (usa o JobId ou um link https de servidor privado).")
            continue
        if name.lower() in seen:
            errors.append(f"Linha {number}: a conta '{name}' está repetida.")
            continue
        seen.add(name.lower())
        accounts.append(AccountSpec(name, place, job))
    if len(accounts) > MAX_ACCOUNTS:
        errors.append(f"Máximo de {MAX_ACCOUNTS} contas.")
    return accounts, errors


def validate_ram_settings(accounts_text, default_place, port, password) -> List[str]:
    """Erros da configuração do modo RAM (lista vazia se estiver tudo bem)."""
    errors: List[str] = []
    accounts, parse_errors = parse_accounts(accounts_text, default_place)
    errors.extend(parse_errors)
    if not accounts and not parse_errors:
        errors.append("Modo RAM: escreve pelo menos uma conta (uma por linha).")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        errors.append("Porta do RAM: tem de ser um número inteiro entre 1 e 65535.")
    if password and len(password) < MIN_PASSWORD_LEN:
        errors.append(
            f"Palavra-passe do RAM: o wrapper oficial só a envia com {MIN_PASSWORD_LEN} ou mais caracteres."
        )
    return errors


class RamClient:
    """Cliente mínimo da API local do RAM. 'opener' é substituível nos testes."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        password: str = "",
        *,
        opener: Optional[Callable[..., object]] = None,
        timeout: float = 20.0,
    ):
        self._port = int(port)
        self._password = password or ""
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout

    def _url(self, method: str, params: List[Tuple[str, str]]) -> str:
        if method not in ALLOWED_METHODS:
            raise RamError(f"Método do RAM não permitido: {method}")
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        if len(self._password) >= MIN_PASSWORD_LEN:
            query += "&Password=" + urllib.parse.quote(self._password, safe="")
        return f"http://{RAM_HOST}:{self._port}/{method}?{query}"

    def _get(self, url: str) -> Tuple[int, str]:
        """Devolve (código HTTP, corpo). Nunca levanta com o URL na mensagem."""
        try:
            with self._opener(url, timeout=self._timeout) as response:  # type: ignore[misc]
                body = response.read(4096).decode("utf-8", "ignore")
                return getattr(response, "status", 200), body
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(4096).decode("utf-8", "ignore")
            except Exception:
                body = ""
            return exc.code, body
        except urllib.error.URLError as exc:
            raise RamError(
                f"Não consegui ligar ao RAM na porta {self._port} ({exc.reason}). "
                "O Web Server/API do RAM está ligado nas definições do RAM e a porta é a mesma?"
            ) from None
        except OSError as exc:
            raise RamError(f"Falha a falar com o RAM: {exc}") from None

    def launch(self, spec: AccountSpec) -> str:
        """Pede ao RAM para lançar a conta. Devolve o texto de resposta do RAM (curto)."""
        params: List[Tuple[str, str]] = [("Account", spec.name), ("PlaceId", str(spec.place_id))]
        if spec.job:
            params.append(("JobId", spec.job))
            if spec.vip:
                params.append(("JoinVIP", "true"))
        status, body = self._get(self._url("LaunchAccount", params))
        text = " ".join(body.split())[:120]
        if status in (401, 403):
            raise RamError(
                "O RAM recusou o pedido (HTTP %d): palavra-passe errada, ou o LaunchAccount está desativado "
                "nas definições da API do RAM." % status
            )
        if status != 200:
            raise RamError(f"O RAM respondeu com o erro HTTP {status}. {text}".strip())
        if "invalid account" in text.lower():
            raise RamError(f"O RAM não conhece a conta '{spec.name}'. Escreve o nome exatamente como está no RAM.")
        return text

    def ping(self) -> Tuple[bool, str]:
        """Testa só se há alguma coisa a responder na porta (não envia comandos nem a palavra-passe)."""
        url = f"http://{RAM_HOST}:{self._port}/"
        try:
            status, _ = self._get(url)
        except RamError as exc:
            return False, str(exc)
        return True, f"O RAM respondeu na porta {self._port} (HTTP {status})."
