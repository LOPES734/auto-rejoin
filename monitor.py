"""
monitor.py - Lógica do Roblox Auto Rejoin (sem interface gráfica).

Contém:
  * configuração (config.json) com validação;
  * localização de uma pasta gravável para config.json e logs.txt;
  * registo de eventos (logs.txt + callback para a interface);
  * deteção do processo RobloxPlayerBeta.exe (psutil);
  * a classe Monitor, que corre numa thread própria e tenta reabrir a
    experiência através do Place ID quando o processo desaparece.

Várias contas (opcional, desligado por defeito): com o Roblox Account Manager
(RAM) o programa lança cada conta pela API local do RAM e vigia cada uma por
separado (ver multi.py). Aqui, o Monitor serve uma única conta: recebe um
'launcher' próprio, uma etiqueta e um verificador do processo dessa conta.

Desligado do servidor (opcional, ligado por defeito): quando o Roblox mostra o
ecrã "Disconnected (Error Code: 277)" o processo continua aberto, por isso lê
o log do próprio Roblox (só leitura) e, se aparecer um desligado com um dos
códigos configurados, fecha o Roblox e reentra.

Internet (opcional, ligado por defeito): se a internet cair o tempo suficiente
para o Roblox se desligar do servidor, fecha o Roblox quando a ligação volta e
reentra; se o Roblox fechar sem internet, espera pela ligação sem gastar
tentativas.

Webhook (opcional): se houver um URL de webhook do Discord configurado, envia
uma mensagem quando o Roblox fecha/crasha, quando reabre e quando as
tentativas se esgotam. O envio corre numa thread à parte e nunca bloqueia a
monitorização.

Segurança: este módulo NÃO lê nem guarda credenciais, cookies ou tokens do
Roblox, não injeta código no Roblox e não mexe no cliente. Só pede ao Windows para abrir
um link normal (https://www.roblox.com/games/start?placeId=... ou
roblox://placeId=...), exatamente como se o utilizador o abrisse à mão.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import ramapi

import psutil

APP_NAME = "RobloxAutoRejoin"
PROCESS_NAME = "RobloxPlayerBeta.exe"
LOGGER_NAME = "roblox_auto_rejoin"
CONFIG_FILE = "config.json"
LOG_FILE = "logs.txt"
PORTABLE_FLAG = "portable.flag"
ENV_HOME = "ROBLOX_AUTO_REJOIN_HOME"  # permite forçar a pasta de dados (testes)

MAX_PLACE_ID = 2**63 - 1

# Limites aceites para cada valor numérico: (mínimo, máximo).
# (É um dicionário de módulo para os testes poderem usar tempos muito curtos.)
LIMITS = {
    "max_attempts": (1, 50),
    "poll_interval": (1.0, 60.0),
    "rejoin_delay": (1.0, 300.0),
    "retry_interval": (1.0, 600.0),
    "launch_wait": (5.0, 600.0),
    "stable_session": (10.0, 86400.0),
    "net_down_threshold": (5.0, 3600.0),
    "net_restore_delay": (1.0, 300.0),
    "net_wait_max": (30.0, 86400.0),
    "ram_port": (1, 65535),
    "ram_launch_gap": (0.0, 300.0),
    "ram_launch_timeout": (10.0, 600.0),
}

FIELD_LABELS = {
    "max_attempts": "Máximo de tentativas",
    "poll_interval": "Intervalo de verificação (s)",
    "rejoin_delay": "Espera antes de reabrir (s)",
    "retry_interval": "Pausa entre tentativas (s)",
    "launch_wait": "Tempo a esperar pelo processo após abrir (s)",
    "stable_session": "Duração de uma sessão estável (s)",
    "net_down_threshold": "Internet em baixo há (s)",
    "net_restore_delay": "Espera após a internet voltar (s)",
    "net_wait_max": "Espera máxima por internet (s)",
    "ram_port": "Porta do RAM",
    "ram_launch_gap": "Pausa entre lançamentos de contas (s)",
    "ram_launch_timeout": "Tempo a esperar que uma conta abra (s)",
}

LAUNCH_METHODS = ("auto", "web", "protocol", "exe")
# 277 = perdeu a ligação; 278 = inativo (20 min); 279 = falhou a ligar; 274 = servidor encerrado.
# De propósito NÃO inclui 273 (conta aberta noutro lado) nem 267/268 (expulso): reentrar só pioraria.
DEFAULT_DISCONNECT_CODES = "277,278,279,274"
EXE_CANDIDATES = ("RobloxPlayerLauncher.exe", "RobloxPlayerBeta.exe")
DEFAULT_ROBLOX_PATH = r"C:\Users\PC\AppData\Local\Roblox\Versions\version-9ac8595712b447a7"


# --------------------------------------------------------------------------
# Pastas
# --------------------------------------------------------------------------
def is_frozen() -> bool:
    """True quando corre como .exe criado pelo PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def app_base_dir() -> Path:
    """Pasta onde está o .exe (ou o main.py em desenvolvimento)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def get_data_dir() -> Path:
    """
    Pasta gravável para config.json e logs.txt.

      * Em desenvolvimento (python main.py): a pasta do projeto.
      * No .exe normal: %APPDATA%\\RobloxAutoRejoin (nunca dentro do .exe).
      * No .exe portátil (existe 'portable.flag' ao lado do .exe): a subpasta
        'data' ao lado do .exe.
    Se a pasta preferida não for gravável, usa alternativas.
    """
    candidates: List[Path] = []
    override = os.environ.get(ENV_HOME)
    if override:
        candidates.append(Path(override))
    elif is_frozen():
        base = app_base_dir()
        if (base / PORTABLE_FLAG).exists():
            candidates.append(base / "data")
        appdata = os.environ.get("APPDATA")
        roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        candidates.append(roaming / APP_NAME)
    else:
        candidates.append(app_base_dir())
    candidates.append(Path.home() / f".{APP_NAME}")
    candidates.append(Path(tempfile.gettempdir()) / APP_NAME)

    for candidate in candidates:
        if _is_writable_dir(candidate):
            return candidate
    raise OSError("Não foi encontrada nenhuma pasta gravável para config.json e logs.txt.")


# --------------------------------------------------------------------------
# Validação e configuração
# --------------------------------------------------------------------------
def validate_place_id(value) -> Tuple[bool, Optional[int], str]:
    """Devolve (válido, número, mensagem de erro)."""
    text = "" if value is None else str(value).strip()
    if not text:
        return False, None, "O Place ID está vazio. Indica o número da experiência (ex.: 123456789)."
    if not text.isascii() or not text.isdigit():
        return False, None, "O Place ID deve conter apenas dígitos (ex.: 123456789), sem letras, espaços, sinais ou links."
    number = int(text)
    if number <= 0:
        return False, None, "O Place ID tem de ser maior que 0."
    if number > MAX_PLACE_ID:
        return False, None, "O Place ID é demasiado grande para ser válido."
    return True, number, ""


@dataclass
class Config:
    place_id: str = ""
    max_attempts: int = 5
    poll_interval: float = 3.0
    rejoin_delay: float = 5.0
    retry_interval: float = 10.0
    launch_wait: float = 60.0
    stable_session: float = 120.0
    launch_method: str = "auto"  # auto | web | protocol | exe
    roblox_path: str = DEFAULT_ROBLOX_PATH  # pasta version-... (ou .exe) do Roblox; vazio = não usar
    webhook_url: str = ""  # URL de webhook do Discord; vazio = sem notificações
    net_rejoin: bool = True  # reentrar quando a internet cair e voltar
    net_down_threshold: float = 30.0  # só reinicia o Roblox se a internet esteve em baixo pelo menos isto
    net_restore_delay: float = 10.0  # espera com a internet estável antes de agir
    net_wait_max: float = 3600.0  # máximo a esperar pela internet para reabrir (depois, erro)
    disconnect_rejoin: bool = True  # reentrar quando o log do Roblox mostra que foi desligado do servidor
    disconnect_codes: str = DEFAULT_DISCONNECT_CODES  # códigos de erro que justificam reentrar
    roblox_log_dir: str = ""  # pasta dos logs do Roblox; vazio = procurar automaticamente
    ram_enabled: bool = False  # modo várias contas através do Roblox Account Manager
    ram_port: int = ramapi.DEFAULT_PORT  # porta da API local do RAM
    ram_password: str = ""  # palavra-passe da API do RAM (segredo; só 6+ caracteres são usados)
    ram_accounts: str = ""  # uma conta por linha: nome[, PlaceId[, JobId]]
    ram_launch_gap: float = 8.0  # pausa entre lançamentos (o RAM não gosta de lançamentos em simultâneo)
    ram_launch_timeout: float = 60.0  # quanto esperar que o processo de uma conta apareça

    def validate(self) -> List[str]:
        """Devolve a lista de erros (vazia se a configuração é válida)."""
        errors: List[str] = []
        if not self.ram_enabled:  # no modo RAM, cada conta pode ter o seu Place ID
            ok, _, err = validate_place_id(self.place_id)
            if not ok:
                errors.append(err)
        for name, label in FIELD_LABELS.items():
            value = getattr(self, name)
            low, high = LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{label}: tem de ser um número.")
            elif name in ("max_attempts", "ram_port") and int(value) != value:
                errors.append(f"{label}: tem de ser um número inteiro.")
            elif not (low <= value <= high):
                errors.append(f"{label}: tem de estar entre {low:g} e {high:g}.")
        if self.launch_method not in LAUNCH_METHODS:
            errors.append(f"Método de abertura inválido (usa um de: {', '.join(LAUNCH_METHODS)}).")
        ok, err = validate_webhook_url(self.webhook_url)
        if not ok:
            errors.append(err)
        if self.disconnect_rejoin and parse_disconnect_codes(self.disconnect_codes) is None:
            errors.append("Códigos de desligado: usa números separados por vírgulas (ex.: 277,278,279).")
        if self.ram_enabled:
            errors.extend(
                ramapi.validate_ram_settings(self.ram_accounts, self.place_id, self.ram_port, self.ram_password)
            )
        return errors

    @property
    def place_id_int(self) -> int:
        ok, number, err = validate_place_id(self.place_id)
        if not ok or number is None:
            raise ValueError(err)
        return number


def parse_disconnect_codes(text) -> Optional[Set[int]]:
    """'277, 278' -> {277, 278}. Devolve None se o texto for inválido ou vazio."""
    parts = [p.strip() for p in str(text or "").split(",") if p.strip()]
    if not parts or not all(p.isascii() and p.isdigit() and 1 <= int(p) <= 9999 for p in parts):
        return None
    return {int(p) for p in parts}


def _coerce(name: str, value, default):
    if name in ("net_rejoin", "disconnect_rejoin", "ram_enabled"):
        if isinstance(value, bool):
            return value
        raise ValueError("tem de ser true ou false")
    if isinstance(value, bool):
        raise ValueError("valor booleano não é aceite")
    if name == "place_id":
        return str(value).strip()
    if name == "roblox_path":
        return str(value).strip().strip('"')
    if name == "ram_accounts":
        return str(value).strip()
    if name in ("webhook_url", "disconnect_codes", "roblox_log_dir", "ram_password"):
        return str(value).strip().strip('"') if name == "roblox_log_dir" else str(value).strip()
    if name == "launch_method":
        text = str(value).strip().lower()
        if text not in LAUNCH_METHODS:
            raise ValueError(f"método desconhecido: {value!r}")
        return text
    if isinstance(default, int):
        return int(value)
    return float(value)


def save_config(config: Config, path: Path) -> None:
    """Grava de forma atómica (ficheiro temporário + substituição)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config(path: Path) -> Tuple[Config, List[str]]:
    """
    Lê config.json. Se não existir, cria-o com os valores por defeito.
    Se estiver corrompido, guarda uma cópia (.bad) e cria um novo.
    Devolve (config, avisos).
    """
    warnings: List[str] = []
    config = Config()

    if not path.exists():
        save_config(config, path)
        warnings.append(f"Configuração inicial criada em {path}")
        return config, warnings

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("o ficheiro não contém um objeto JSON")
    except (OSError, ValueError) as exc:
        bad = path.with_name(path.name + ".bad")
        try:
            os.replace(path, bad)
        except OSError:
            pass
        save_config(config, path)
        warnings.append(
            f"{path.name} estava inválido ({exc}). Foi guardada uma cópia em {bad.name} e criada uma configuração nova."
        )
        return config, warnings

    for f in fields(Config):
        if f.name not in raw:
            continue
        try:
            setattr(config, f.name, _coerce(f.name, raw[f.name], f.default))
        except (TypeError, ValueError):
            warnings.append(f"Valor inválido para '{f.name}' em {path.name}; usado o valor por defeito.")
    return config, warnings


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------
class _CallbackHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:  # um log nunca deve rebentar a aplicação
            pass


def setup_logging(log_path: Path, callback: Optional[Callable[[str], None]] = None) -> logging.Logger:
    """Configura o logger: ficheiro logs.txt (com rotação) + callback opcional."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_rar", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler._rar = True  # type: ignore[attr-defined]
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        if callback:
            callback(f"[WARNING] Não foi possível escrever em {log_path}: {exc}")
    if callback:
        cb_handler = _CallbackHandler(callback)
        cb_handler._rar = True  # type: ignore[attr-defined]
        cb_handler.setFormatter(formatter)
        logger.addHandler(cb_handler)
    return logger


# --------------------------------------------------------------------------
# Deteção do processo
# --------------------------------------------------------------------------
def find_process_pids(process_name: str = PROCESS_NAME, iterator: Optional[Callable] = None) -> List[int]:
    """
    Procura processos com o nome indicado (sem distinguir maiúsculas).
    Ignora processos que desapareçam durante a verificação ou aos quais não
    temos acesso.
    """
    iterate = iterator or psutil.process_iter
    target = process_name.lower()
    pids: List[int] = []
    for proc in iterate(["pid", "name"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            if name.lower() == target and proc.is_running():
                pids.append(int(info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return pids


def is_process_running(process_name: str = PROCESS_NAME, iterator: Optional[Callable] = None) -> bool:
    return bool(find_process_pids(process_name, iterator))


# --------------------------------------------------------------------------
# Abertura da experiência
# --------------------------------------------------------------------------
class LaunchError(Exception):
    """Falha ao pedir ao Windows que abra o link."""


def build_urls(place_id: int) -> dict:
    """
    Links normais de abertura de uma experiência (documentação oficial do
    Roblox, "Deep links"):
      web      -> abre a página da experiência no navegador, que lança o Roblox
      protocol -> pede diretamente à aplicação Roblox (protocolo roblox://)
    """
    pid = int(place_id)
    return {
        "web": f"https://www.roblox.com/games/start?placeId={pid}",
        "protocol": f"roblox://placeId={pid}",
    }


def default_opener(url: str) -> None:
    """Abre o link com o mecanismo normal do sistema (como um duplo clique)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
        elif not webbrowser.open(url):
            raise LaunchError("Nenhum programa aceitou abrir o link.")
    except LaunchError:
        raise
    except OSError as exc:
        raise LaunchError(
            f"O Windows não conseguiu abrir o link ({exc}). O Roblox está instalado e o navegador predefinido configurado?"
        ) from exc


def resolve_roblox_exe(path: str) -> Path:
    """Aceita a pasta version-... ou o próprio .exe e devolve o executável a abrir."""
    p = Path(path.strip().strip('"'))
    if p.is_file():
        return p
    if p.is_dir():
        for name in EXE_CANDIDATES:
            if (p / name).is_file():
                return p / name
        raise LaunchError(f"Não encontrei {' nem '.join(EXE_CANDIDATES)} em {p}.")
    raise LaunchError(
        f"O caminho '{p}' não existe. Se o Roblox atualizou, o nome da pasta version-... mudou: "
        "atualiza o caminho (ou apaga-o para usar só os links)."
    )


def default_exe_opener(path: str, url: str) -> None:
    """Abre o executável normal do Roblox dessa pasta, passando-lhe o link roblox://."""
    exe = resolve_roblox_exe(path)
    flags = 0
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(exe), url], cwd=str(exe.parent), creationflags=flags, close_fds=True)
    except OSError as exc:
        raise LaunchError(f"Não foi possível executar {exe.name}: {exc}") from exc


def methods_for_attempt(method: str, attempt: int, use_exe: bool = False) -> List[str]:
    """
    Ordem dos métodos para uma tentativa. Em 'auto' alterna entre o link web
    (tentativas ímpares) e o protocolo roblox:// (tentativas pares); dentro de
    uma tentativa, o seguinte serve de alternativa se o anterior der erro.
    Se há um caminho do Roblox configurado, 'auto' tenta primeiro o executável.
    """
    if method in ("web", "protocol", "exe"):
        return [method]
    order = ["web", "protocol"] if attempt % 2 == 1 else ["protocol", "web"]
    return ["exe"] + order if use_exe else order


# --------------------------------------------------------------------------
# Internet e fecho do Roblox
# --------------------------------------------------------------------------
# Basta UM destino responder para haver internet. O Roblox vem primeiro (é o que
# interessa); os IPs diretos evitam um falso "sem internet" se o DNS falhar ou se
# o site do Roblox estiver em baixo.
NET_HOSTS = (("www.roblox.com", 443), ("1.1.1.1", 443), ("8.8.8.8", 443))
NET_TIMEOUT = 1.5


def check_internet(hosts=NET_HOSTS, timeout: float = NET_TIMEOUT) -> bool:
    """True se conseguir abrir uma ligação TCP a algum dos destinos."""
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def kill_roblox(process_name: str = PROCESS_NAME, wait: float = 5.0, iterator: Optional[Callable] = None) -> int:
    """
    Fecha os processos do Roblox do utilizador (pede para terminar; se não
    terminarem em 'wait' s, força). Devolve quantos processos encontrou.
    """
    iterate = iterator or psutil.process_iter
    target = process_name.lower()
    procs = []
    for proc in iterate(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    def alive(items):
        out = []
        for p in items:
            try:
                if p.is_running():
                    out.append(p)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        return out

    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    deadline = time.monotonic() + wait
    while alive(procs) and time.monotonic() < deadline:
        time.sleep(0.1)
    for p in alive(procs):
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return len(procs)


# --------------------------------------------------------------------------
# Deteção de "desligado do servidor" (log do Roblox, só leitura)
# --------------------------------------------------------------------------
LOG_MAX_READ = 2 * 1024 * 1024  # nunca lê mais do que isto de uma vez
_LOG_LINE = re.compile(r"disconnect|connection lost|error code|kicked", re.IGNORECASE)
# O número tem de vir depois de uma palavra-chave (reason/code/id/error) para não
# confundir com milissegundos de timestamps como "...05.277Z".
_LOG_CODE = re.compile(r"\b(?:reason|code|id|error)\W{0,3}(\d{2,4})\b", re.IGNORECASE)


def roblox_log_dirs(config: "Config") -> List[Path]:
    """Pastas onde o Roblox pode guardar os logs (instalação normal e Microsoft Store)."""
    dirs: List[Path] = []
    if config.roblox_log_dir.strip():
        dirs.append(Path(config.roblox_log_dir.strip().strip('"')))
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    dirs.append(base / "Roblox" / "logs")
    if config.roblox_path.strip():
        for parent in Path(config.roblox_path.strip().strip('"')).parents:
            if parent.name.lower() == "versions":
                dirs.append(parent.parent / "logs")
                break
    try:
        dirs.extend(sorted((base / "Packages").glob("ROBLOXCORPORATION.ROBLOX_*/LocalState/logs")))
    except OSError:
        pass
    unique: List[Path] = []
    for d in dirs:
        if d not in unique:
            unique.append(d)
    return unique


def roblox_process_start_time(process_name: str = PROCESS_NAME) -> Optional[float]:
    """Quando arrancou o processo do Roblox mais antigo (epoch), ou None."""
    target = process_name.lower()
    times: List[float] = []
    try:
        for proc in psutil.process_iter(["name", "create_time"]):
            try:
                if (proc.info.get("name") or "").lower() == target and proc.info.get("create_time"):
                    times.append(float(proc.info["create_time"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        return None
    return min(times) if times else None


class RobloxLogWatcher:
    """
    Lê (sem escrever) o log mais recente do Roblox à procura de uma linha de
    "desligado" com um dos códigos pedidos. Só olha para ficheiros alterados depois
    de o processo atual ter arrancado, para nunca reagir ao log de uma sessão antiga.
    """

    def __init__(
        self,
        dirs: List[Path],
        codes: Set[int],
        *,
        start_time_fn: Optional[Callable[[], Optional[float]]] = None,
        on_file: Optional[Callable[[Path], None]] = None,
        file_fn: Optional[Callable[[], Optional[Path]]] = None,
    ):
        # file_fn: modo "uma conta": lê SEMPRE o log indicado por esta função (o log do
        # processo dessa conta), em vez de procurar o mais recente.
        self._file_fn = file_fn
        self._dirs = list(dirs)
        self._codes = set(codes)
        self._start_time_fn = start_time_fn or roblox_process_start_time
        self._on_file = on_file
        self._since: Optional[float] = None
        self._file: Optional[Path] = None
        self._offset = 0
        self._spent: Set[Path] = set()  # logs que já provocaram uma reentrada: nunca mais se lêem

    def has_dirs(self) -> bool:
        return self._file_fn is not None or any(d.is_dir() for d in self._dirs)

    def attach(self) -> None:
        """Chamar quando um processo novo do Roblox é detetado: recomeça a leitura."""
        try:
            self._since = self._start_time_fn()
        except Exception:
            self._since = None
        self._file = None
        self._offset = 0

    def _latest(self) -> Optional[Tuple[Path, int]]:
        if self._file_fn is not None:
            f = self._file_fn()
            if f is None or f in self._spent:
                return None
            try:
                return f, f.stat().st_size
            except OSError:
                return None
        best: Optional[Tuple[Path, float, int]] = None
        for directory in self._dirs:
            try:
                files = list(directory.glob("*.log"))
            except OSError:
                continue
            for f in files:
                try:
                    st = f.stat()
                except OSError:
                    continue
                if f in self._spent:
                    continue
                if self._since is not None and st.st_mtime < self._since - 1:
                    continue  # log de uma sessão anterior
                # Mais recente por data de modificação. No Windows a data tem pouca resolução
                # (~15 ms), por isso em caso de empate ganha o nome maior (os logs do Roblox
                # têm a data no nome).
                if best is None or (st.st_mtime, f.name) > (best[1], best[0].name):
                    best = (f, st.st_mtime, st.st_size)
        return (best[0], best[2]) if best else None

    def poll(self) -> Optional[Tuple[int, str]]:
        """Devolve (código, linha) se apareceu um desligado desde a última leitura."""
        latest = self._latest()
        if latest is None:
            return None
        path, size = latest
        if path != self._file:
            self._file, self._offset = path, 0
            if self._on_file:
                try:
                    self._on_file(path)
                except Exception:
                    pass
        if size < self._offset:  # ficheiro truncado/recriado
            self._offset = 0
        if size == self._offset:
            return None
        start = max(self._offset, size - LOG_MAX_READ)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return None
        cut = data.rfind(b"\n")
        if cut == -1:
            return None  # linha ainda incompleta: esperar pelo resto
        self._offset = start + cut + 1
        for line in data[: cut + 1].decode("utf-8", "ignore").splitlines():
            if not _LOG_LINE.search(line):
                continue
            for match in _LOG_CODE.finditer(line):
                code = int(match.group(1))
                if code in self._codes:
                    self._spent.add(path)  # este log já "gastou" o seu desligado
                    return code, line.strip()
        return None


# --------------------------------------------------------------------------
# Webhook (Discord)
# --------------------------------------------------------------------------
WEBHOOK_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
_WEBHOOK_PATH = re.compile(r"^/api(/v\d+)?/webhooks/\d+/[\w-]+/?$")
WEBHOOK_USERNAME = "Roblox Auto Rejoin"
WEBHOOK_TIMEOUT = 10.0
WEBHOOK_MAX_QUEUE = 50
COLOR_RED, COLOR_GREEN, COLOR_ORANGE, COLOR_BLUE = 0xD9534F, 0x2FA84F, 0xE67E22, 0x3B8ED0


class WebhookError(Exception):
    """Falha ao enviar para o webhook (a mensagem nunca contém o URL)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def validate_webhook_url(value) -> Tuple[bool, str]:
    """Vazio = desativado (válido). Aceita apenas webhooks do Discord em https."""
    text = "" if value is None else str(value).strip()
    if not text:
        return True, ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False, "Webhook: o URL é inválido."
    if parts.scheme != "https" or (parts.hostname or "").lower() not in WEBHOOK_HOSTS:
        return False, "Webhook: tem de ser um URL https de um webhook do Discord (https://discord.com/api/webhooks/...)."
    if not _WEBHOOK_PATH.match(parts.path):
        return False, "Webhook: o URL não tem o formato de um webhook do Discord (.../api/webhooks/<id>/<token>)."
    return True, ""


def redact_webhook(url: str) -> str:
    """Versão do URL segura para mostrar (o token do webhook é um segredo)."""
    m = re.search(r"/webhooks/(\d+)/", url or "")
    return f"https://discord.com/api/webhooks/{m.group(1)}/***" if m else "(webhook)"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_webhook_payload(title: str, description: str, color: int, fields: Optional[List[Tuple[str, str]]] = None) -> dict:
    embed = {
        "title": title[:256],
        "description": description[:2000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [{"name": n[:256], "value": (v or "-")[:1024], "inline": True} for n, v in (fields or [])][:25],
    }
    return {"username": WEBHOOK_USERNAME, "embeds": [embed]}


def post_webhook(url: str, payload: dict, timeout: float = WEBHOOK_TIMEOUT) -> None:
    """Envia o JSON ao Discord. Levanta WebhookError (sem expor o URL) em caso de falha."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        # O Discord rejeita o User-Agent por defeito do Python (erro 403/1010).
        headers={"Content-Type": "application/json", "User-Agent": "RobloxAutoRejoin/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry = None
            try:
                retry = float(json.loads(exc.read().decode("utf-8")).get("retry_after"))
            except Exception:
                pass
            raise WebhookError("O Discord limitou os pedidos (429).", retry_after=retry) from None
        if exc.code in (401, 404):
            raise WebhookError(f"O Discord rejeitou o webhook (HTTP {exc.code}): foi apagado ou o URL está errado.") from None
        raise WebhookError(f"O Discord respondeu com o erro HTTP {exc.code}.") from None
    except urllib.error.URLError as exc:
        raise WebhookError(f"Sem ligação ao Discord: {exc.reason}") from None
    except (OSError, ValueError) as exc:
        raise WebhookError(f"Falha ao enviar para o Discord: {exc}") from None


def send_test_webhook(url: str, sender: Optional[Callable[[str, dict], None]] = None) -> Tuple[bool, str]:
    """Envio síncrono de uma mensagem de teste. Devolve (ok, mensagem)."""
    ok, err = validate_webhook_url(url)
    if not ok:
        return False, err
    if not url.strip():
        return False, "Webhook: não há nenhum URL para testar."
    payload = build_webhook_payload(
        "🔔 Teste do webhook",
        "Se estás a ver isto, as notificações do Roblox Auto Rejoin estão a funcionar.",
        COLOR_BLUE,
        [("PC", platform.node() or "?")],
    )
    try:
        (sender or post_webhook)(url.strip(), payload)
    except WebhookError as exc:
        return False, str(exc)
    return True, "Mensagem de teste enviada."


class WebhookNotifier:
    """
    Envia mensagens por uma thread própria, pela ordem em que foram pedidas.
    send() nunca bloqueia nem levanta exceções: se a fila encher ou o envio
    falhar, regista um aviso (sem o URL) e a monitorização continua.
    """

    def __init__(
        self,
        url: str,
        *,
        sender: Optional[Callable[[str, dict], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._url = url.strip()
        self._sender = sender or post_webhook
        self._log = logger or logging.getLogger(LOGGER_NAME)
        self._queue: "queue.Queue" = queue.Queue(maxsize=WEBHOOK_MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def send(self, title: str, description: str, color: int, fields: Optional[List[Tuple[str, str]]] = None) -> bool:
        payload = build_webhook_payload(title, description, color, fields)
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="webhook-sender", daemon=True)
                self._thread.start()
        try:
            self._queue.put_nowait((title, payload))
        except queue.Full:
            self._log.warning("Webhook: fila cheia; mensagem descartada.")
            return False
        return True

    def _deliver(self, title: str, payload: dict) -> None:
        for attempt in (1, 2):
            try:
                self._sender(self._url, payload)
                self._log.info(f"Webhook enviado: {title}")
                return
            except WebhookError as exc:
                if attempt == 1 and exc.retry_after is not None and exc.retry_after <= 10:
                    time.sleep(exc.retry_after + 0.1)
                    continue
                self._log.warning(f"Webhook não enviado ({title}): {exc}")
                return
            except Exception as exc:  # um webhook nunca deve rebentar o programa
                self._log.warning(f"Webhook não enviado ({title}): {exc!r}")
                return

    def _worker(self) -> None:
        while True:
            title, payload = self._queue.get()
            try:
                self._deliver(title, payload)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Espera (até 'timeout' s) que as mensagens pendentes sejam enviadas."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._queue.unfinished_tasks


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------
class _PrefixAdapter(logging.LoggerAdapter):
    """Põe '[conta] ' à frente de cada linha de log (modo várias contas)."""

    def process(self, msg, kwargs):
        return f"{self.extra['prefix']}{msg}", kwargs


class State(str, Enum):
    WAITING = "A aguardar"
    DETECTED = "Roblox detetado"
    REJOINING = "A tentar reentrar"
    OFFLINE = "Sem internet"
    ERROR = "Erro"
    STOPPED = "Parado"


class Monitor:
    """
    Monitoriza o Roblox numa thread própria.

    Proteções contra aberturas duplicadas / ciclos infinitos:
      * só existe uma thread de monitorização (start() repetido é ignorado);
      * antes de cada abertura volta a verificar se o Roblox já está aberto;
      * após cada abertura espera 'launch_wait' segundos pelo processo antes
        de contar a tentativa como falhada;
      * há uma pausa 'retry_interval' entre tentativas;
      * ao atingir 'max_attempts' a monitorização termina em estado Erro;
      * o contador só volta a 0 após 'stable_session' segundos seguidos com o
        processo aberto;
      * sem internet, não se gastam tentativas: espera-se pela ligação.
    """

    def __init__(
        self,
        config: Config,
        *,
        process_checker: Optional[Callable[[], bool]] = None,
        opener: Optional[Callable[[str], None]] = None,
        exe_opener: Optional[Callable[[str, str], None]] = None,
        notifier: Optional[WebhookNotifier] = None,
        net_checker: Optional[Callable[[], bool]] = None,
        log_watcher: Optional[RobloxLogWatcher] = None,
        killer: Optional[Callable[[], int]] = None,
        label: str = "",
        launcher: Optional[Callable[[], str]] = None,
        launch_on_start: bool = False,
        on_state: Optional[Callable[[State], None]] = None,
        on_attempts: Optional[Callable[[int, int], None]] = None,
        logger: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._checker = process_checker or (lambda: is_process_running(PROCESS_NAME))
        self._opener = opener or default_opener
        self._exe_opener = exe_opener or default_exe_opener
        self._on_state = on_state
        self._on_attempts = on_attempts
        base_logger = logger or logging.getLogger(LOGGER_NAME)
        self.label = label
        self._log = _PrefixAdapter(base_logger, {"prefix": f"[{label}] "}) if label else base_logger
        self._launcher = launcher  # se existir, substitui os links web/roblox:// (modo RAM)
        self._launch_on_start = launch_on_start
        self._quiet_recovered = False
        self._clock = clock

        if notifier is None and config.webhook_url.strip() and validate_webhook_url(config.webhook_url)[0]:
            notifier = WebhookNotifier(config.webhook_url, logger=self._log)
        self._webhook = notifier
        self._last_method = ""
        self._net_checker = net_checker or check_internet
        self._killer = killer or kill_roblox
        codes = parse_disconnect_codes(config.disconnect_codes)
        if log_watcher is None and config.disconnect_rejoin and codes:
            log_watcher = RobloxLogWatcher(
                roblox_log_dirs(config),
                codes,
                on_file=lambda path: self._log.info(f"A ler o log do Roblox para detetar desligados: {path.name}"),
            )
        self._watcher = log_watcher
        self._net_fails = 0
        self._net_down_since: Optional[float] = None
        self._net_confirmed_down = False
        self._net_restored: Optional[float] = None  # segundos em baixo, quando acabou de voltar

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._last_check_error = ""

        self.state = State.STOPPED
        self.attempts = 0
        self.launch_count = 0  # quantos pedidos de abertura foram enviados ao Windows
        self.last_error = ""

    # ---- estado ---------------------------------------------------------
    @property
    def is_active(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _set_state(self, state: State) -> None:
        if state == self.state:
            return
        self.state = state
        if self._on_state:
            try:
                self._on_state(state)
            except Exception:
                pass

    def _set_attempts(self, used: int) -> None:
        self.attempts = used
        if self._on_attempts:
            try:
                self._on_attempts(used, self.config.max_attempts)
            except Exception:
                pass

    def _fail(self, message: str, notify: bool = False) -> None:
        self.last_error = message
        self._log.error(message)
        self._set_state(State.ERROR)
        if notify:
            self._notify("failed", reason=message)

    def _notify(self, event: str, **info) -> None:
        """Envia (sem bloquear) a notificação do webhook, se estiver configurado."""
        if event == "recovered" and self._quiet_recovered:
            self._quiet_recovered = False  # abertura inicial: sem mensagem (como no modo normal)
            return
        if self._webhook is None:
            return
        fields: List[Tuple[str, str]] = [("PC", platform.node() or "?"), ("Place ID", str(self.config.place_id))]
        if self.label:
            fields.insert(0, ("Conta", self.label))
        try:
            if event == "closed":
                title, color = "⚠️ O Roblox fechou ou crashou", COLOR_RED
                text = (
                    f"O processo {PROCESS_NAME} desapareceu. O programa não distingue um crash de um fecho manual. "
                    "Vai tentar reabrir a experiência."
                )
                if info.get("session") is not None:
                    fields.append(("Sessão durou", format_duration(info["session"])))
            elif event == "recovered":
                title, color = "✅ O Roblox voltou a abrir", COLOR_GREEN
                if info.get("attempts"):
                    text = "O processo do Roblox foi detetado outra vez. Isto não confirma que entraste no servidor."
                    fields.append(("Tentativas", f"{info['attempts']}/{self.config.max_attempts}"))
                    if info.get("method"):
                        fields.append(("Método", info["method"]))
                else:
                    text = "O Roblox voltou a abrir sozinho; não foi preciso reabrir."
            elif event == "disconnected":
                title, color = "🔌 O Roblox foi desligado do servidor", COLOR_ORANGE
                text = "O Roblox ficou aberto mas desligado. Vai ser fechado e reaberto."
                fields.append(("Erro", str(info.get("code", "?"))))
            elif event == "net_back":
                title, color = "🌐 A internet voltou", COLOR_BLUE
                text = (
                    "O Roblox provavelmente perdeu a ligação ao servidor. Vai ser fechado e reaberto."
                    if info.get("restart")
                    else "A internet voltou. A reabrir a experiência."
                )
                fields.append(("Internet em baixo", format_duration(info.get("down") or 0)))
            else:  # failed
                title, color = "❌ Não foi possível reabrir o Roblox", COLOR_ORANGE
                text = str(info.get("reason", "Erro desconhecido."))
            self._webhook.send(title, text, color, fields)
        except Exception as exc:  # a notificação nunca pode afetar a monitorização
            self._log.warning(f"Webhook: erro ao preparar a mensagem: {exc!r}")

    # ---- controlo -------------------------------------------------------
    def start(self) -> bool:
        """Inicia a monitorização. Devolve False se não foi possível iniciar."""
        with self._start_lock:
            if self.is_active:
                self._log.warning("A monitorização já está em curso; pedido ignorado.")
                return False
            errors = self.config.validate()
            if errors:
                self._fail("Configuração inválida: " + " ".join(errors))
                return False
            self._stop.clear()
            self.last_error = ""
            self._set_attempts(0)
            self._thread = threading.Thread(target=self._run, name="rejoin-monitor", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Pede à monitorização para parar e espera que a thread termine."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if not self.is_active and self.state != State.ERROR:
            self._set_state(State.STOPPED)
        if self._webhook is not None:
            self._webhook.flush(timeout=min(timeout, 5.0))

    # ---- verificação ----------------------------------------------------
    def _is_running(self) -> Optional[bool]:
        """True/False, ou None se não foi possível verificar (erro tratado)."""
        try:
            result = bool(self._checker())
        except Exception as exc:  # permissões, erros do psutil, etc.
            message = f"Não foi possível verificar os processos: {exc}"
            if message != self._last_check_error:
                self._log.warning(message)
                self._last_check_error = message
            return None
        self._last_check_error = ""
        return result

    # ---- internet -------------------------------------------------------
    def _net_raw(self) -> bool:
        """Uma verificação direta. Se não for possível verificar, assume que há internet."""
        try:
            return bool(self._net_checker())
        except Exception as exc:
            self._log.warning(f"Não foi possível verificar a internet: {exc!r}")
            return True

    def _net_reset(self) -> None:
        self._net_fails = 0
        self._net_down_since = None
        self._net_confirmed_down = False
        self._net_restored = None

    def _net_online(self) -> bool:
        """
        True se há internet. Só declara 'sem internet' após 2 verificações falhadas
        seguidas (evita falsos alarmes). Quando a ligação volta depois de uma
        falha confirmada, guarda em _net_restored quanto tempo esteve em baixo.
        """
        if not self.config.net_rejoin:
            return True
        ok = self._net_raw()
        now = self._clock()
        if ok:
            self._net_fails = 0
            if self._net_confirmed_down:
                down = now - (self._net_down_since if self._net_down_since is not None else now)
                self._net_restored = down
                self._log.info(f"A internet voltou (esteve em baixo {format_duration(down)}).")
            self._net_confirmed_down = False
            self._net_down_since = None
            return True
        if self._net_down_since is None:
            self._net_down_since = now
        self._net_fails += 1
        if self._net_fails >= 2 and not self._net_confirmed_down:
            self._net_confirmed_down = True
            self._log.warning("Sem ligação à internet.")
        return not self._net_confirmed_down

    def _wait_for_internet(self) -> Optional[bool]:
        """
        Antes de reabrir: espera que haja internet (sem gastar tentativas).
        True = há internet; None = foi parado; False = excedeu 'net_wait_max'.
        """
        cfg = self.config
        if not cfg.net_rejoin or self._net_raw():
            return True
        self._set_state(State.OFFLINE)
        self._log.warning("Sem internet: a aguardar pela ligação antes de reabrir (isto não gasta tentativas).")
        start = self._clock()
        step = max(0.01, min(cfg.poll_interval, 1.0))
        while True:
            if self._stop.wait(step):
                return None
            if self._net_raw():
                if self._stop.wait(cfg.net_restore_delay):
                    return None
                if self._net_raw():
                    break
                continue
            if self._clock() - start >= cfg.net_wait_max:
                self._fail(
                    f"Sem internet há mais de {format_duration(cfg.net_wait_max)}. Monitorização terminada. "
                    "Verifica a ligação e carrega em Iniciar para recomeçar.",
                    notify=True,
                )
                return False
        down = self._clock() - start
        self._net_reset()
        self._log.info(f"A internet voltou (esteve em baixo {format_duration(down)}). A reabrir a experiência.")
        self._set_state(State.REJOINING)
        self._notify("net_back", down=down, restart=False)
        return True

    # ---- desligado do servidor -----------------------------------------
    def _attach_watcher(self) -> None:
        if self._watcher is not None:
            try:
                self._watcher.attach()
            except Exception as exc:
                self._log.warning(f"Não foi possível preparar a leitura do log do Roblox: {exc!r}")

    def _poll_disconnect(self) -> Optional[Tuple[int, str]]:
        if self._watcher is None or not self.config.disconnect_rejoin:
            return None
        try:
            return self._watcher.poll()
        except Exception as exc:
            self._log.warning(f"Não foi possível ler o log do Roblox: {exc!r}")
            return None

    def _restart_roblox(self) -> str:
        """Fecha o Roblox (que ficou aberto mas desligado) e reentra. Devolve o resultado de _rejoin()."""
        try:
            self._killer()
        except Exception as exc:
            self._log.warning(f"Não foi possível fechar o Roblox: {exc!r}")
        return self._rejoin()

    # ---- thread principal ----------------------------------------------
    def _run(self) -> None:
        cfg = self.config
        self._log.info(
            f"Monitorização iniciada (processo {PROCESS_NAME}, Place ID {cfg.place_id}, "
            f"máx. {cfg.max_attempts} tentativas)."
        )
        if self._watcher is not None and cfg.disconnect_rejoin and not self._watcher.has_dirs():
            self._log.warning(
                "Não encontrei a pasta de logs do Roblox (…\\Roblox\\logs): não consigo detetar o ecrã "
                "'Disconnected'. Podes indicá-la em 'roblox_log_dir' no config.json."
            )
        try:
            was_running = False
            running_since: Optional[float] = None
            if not self._stop.is_set() and self._launch_on_start and self._is_running() is False:
                self._quiet_recovered = True
                outcome = self._rejoin()
                self._quiet_recovered = False
                if outcome != "recovered":
                    return  # "exhausted" ou "stopped"
                was_running = True
                running_since = self._clock()
                self._attach_watcher()
            while not self._stop.is_set():
                online = self._net_online()
                if not online and was_running:
                    self._set_state(State.OFFLINE)  # sem internet: não decidir nada até voltar
                    if self._stop.wait(cfg.poll_interval):
                        break
                    continue
                restored, self._net_restored = self._net_restored, None
                if restored is not None:
                    if self.state == State.OFFLINE:
                        self._set_state(State.DETECTED if was_running else State.WAITING)
                    if was_running and restored >= cfg.net_down_threshold:
                        if self._stop.wait(cfg.net_restore_delay):
                            break
                        if self._net_online() and self._is_running():
                            self._log.warning(
                                f"A internet esteve em baixo {format_duration(restored)}: o Roblox deve ter perdido a "
                                "ligação ao servidor. A fechá-lo para reentrar."
                            )
                            self._notify("net_back", down=restored, restart=True)
                            was_running = False
                            running_since = None
                            outcome = self._restart_roblox()
                            if outcome == "recovered":
                                was_running = True
                                running_since = self._clock()
                                self._attach_watcher()
                                continue
                            return  # "exhausted" ou "stopped"
                running = self._is_running()
                if running is None:
                    pass  # não sabemos; não tomar decisões com dados incertos
                elif running:
                    if not was_running:
                        was_running = True
                        running_since = self._clock()
                        self._log.info("Roblox detetado (processo aberto). A monitorizar.")
                        self._set_state(State.DETECTED)
                        self._attach_watcher()
                    elif (
                        self.attempts > 0
                        and running_since is not None
                        and self._clock() - running_since >= cfg.stable_session
                    ):
                        self._log.info(
                            f"Sessão estável há {cfg.stable_session:g}s: contador de tentativas reposto a 0."
                        )
                        self._set_attempts(0)
                    hit = self._poll_disconnect()
                    if hit is not None:
                        code, line = hit
                        self._log.warning(
                            f"O Roblox foi desligado do servidor (erro {code}) mas o processo continua aberto. "
                            f"Linha do log: {line[:200]}"
                        )
                        self._notify("disconnected", code=code)
                        was_running = False
                        running_since = None
                        outcome = self._restart_roblox()
                        if outcome == "recovered":
                            was_running = True
                            running_since = self._clock()
                            self._attach_watcher()
                            continue
                        return  # "exhausted" ou "stopped"
                elif was_running:
                    self._log.warning(f"O processo {PROCESS_NAME} desapareceu (o Roblox fechou).")
                    session = self._clock() - running_since if running_since is not None else None
                    was_running = False
                    running_since = None
                    self._notify("closed", session=session)
                    outcome = self._rejoin()
                    if outcome == "recovered":
                        was_running = True
                        running_since = self._clock()
                        self._attach_watcher()
                        continue
                    return  # "exhausted" ou "stopped"
                else:
                    if self.state != State.WAITING:
                        self._log.info("Roblox não está aberto. A aguardar (o programa não abre o jogo sozinho).")
                    self._set_state(State.WAITING)
                if self._stop.wait(cfg.poll_interval):
                    break
        except Exception as exc:  # nunca deixar a thread morrer em silêncio
            self._log.exception("Erro inesperado na monitorização")
            self._fail(f"Erro inesperado: {exc!r}", notify=True)
        finally:
            if self.state != State.ERROR:
                self._set_state(State.STOPPED)
            self._log.info("Monitorização terminada.")

    # ---- reentrada ------------------------------------------------------
    def _rejoin(self) -> str:
        """Devolve 'recovered', 'exhausted' ou 'stopped'."""
        cfg = self.config
        self.last_error = ""  # só contam os problemas deste ciclo de reentrada
        self._set_state(State.REJOINING)
        self._log.info(f"A aguardar {cfg.rejoin_delay:g}s antes de tentar reentrar...")
        if self._stop.wait(cfg.rejoin_delay):
            return "stopped"

        while True:
            running = self._is_running()
            if running:
                self._log.info("O Roblox voltou a abrir sozinho; não é preciso reabrir.")
                self._set_state(State.DETECTED)
                self._notify("recovered", attempts=self.attempts)
                return "recovered"

            net = self._wait_for_internet()
            if net is None:
                return "stopped"
            if net is False:
                return "exhausted"
            if self._is_running():  # voltou a abrir enquanto esperávamos pela internet
                self._set_state(State.DETECTED)
                self._notify("recovered", attempts=self.attempts)
                return "recovered"

            if self.attempts >= cfg.max_attempts:
                detail = f" Último problema: {self.last_error}" if self.last_error else ""
                self._fail(
                    f"Limite de {cfg.max_attempts} tentativas atingido sem uma sessão estável. "
                    f"Monitorização terminada.{detail} Verifica o Place ID, se o Roblox está instalado e se a conta "
                    "tem sessão iniciada, e carrega em Iniciar para recomeçar.",
                    notify=True,
                )
                return "exhausted"

            attempt = self.attempts + 1
            self._set_attempts(attempt)
            self._log.info(f"Tentativa {attempt}/{cfg.max_attempts}: a pedir ao Windows para abrir a experiência.")
            launched = self._launch(attempt)
            if self._stop.is_set():
                return "stopped"

            if launched:
                appeared = self._wait_for_process(cfg.launch_wait)
                if appeared is None:
                    return "stopped"
                if appeared:
                    self._log.info(
                        "Processo do Roblox detetado após a tentativa. Isto só confirma que o processo abriu; "
                        "não confirma que entraste no servidor."
                    )
                    self._set_state(State.DETECTED)
                    self._notify("recovered", attempts=attempt, method=self._last_method)
                    return "recovered"
                self._log.warning(
                    f"O processo {PROCESS_NAME} não apareceu em {cfg.launch_wait:g}s após a tentativa {attempt}."
                )
                self.last_error = f"O Roblox não abriu após a tentativa {attempt}/{cfg.max_attempts}."

            if self.attempts < cfg.max_attempts:
                self._log.info(f"Nova tentativa dentro de {cfg.retry_interval:g}s.")
                if self._stop.wait(cfg.retry_interval):
                    return "stopped"

    def _launch(self, attempt: int) -> bool:
        # A paragem pode ser pedida enquanto o monitor espera ou antes do launcher.
        # Nunca iniciar um novo pedido depois de o evento de paragem estar ativo.
        if self._stop.is_set():
            return False
        cfg = self.config
        if self._launcher is not None:
            try:
                if self._stop.is_set():
                    return False
                method = self._launcher() or "ram"
            except Exception as exc:
                if self._stop.is_set():
                    return False
                self.last_error = f"Não foi possível abrir o Roblox: {exc}"
                self._log.error(self.last_error)
                return False
            if self._stop.is_set():
                return False
            self.launch_count += 1
            self._last_method = method
            self._log.info(f"Pedido de abertura enviado ({method}).")
            return True
        urls = build_urls(cfg.place_id_int)
        problems: List[str] = []
        for method in methods_for_attempt(cfg.launch_method, attempt, bool(cfg.roblox_path)):
            if self._stop.is_set():
                return False
            try:
                if method == "exe":
                    if not cfg.roblox_path:
                        raise LaunchError("Método 'exe' escolhido mas não há caminho do Roblox configurado.")
                    target = urls["protocol"]
                    self._exe_opener(cfg.roblox_path, target)
                else:
                    target = urls[method]
                    self._opener(target)
            except Exception as exc:
                problems.append(f"{method}: {exc}")
                self._log.warning(f"Falha ao abrir via {method}: {exc}")
                continue
            self.launch_count += 1
            self._last_method = method
            self._log.info(f"Pedido de abertura enviado ({method}): {target}")
            return True
        self.last_error = "Não foi possível abrir o Roblox. " + " | ".join(problems)
        self._log.error(self.last_error)
        return False

    def _wait_for_process(self, timeout: float) -> Optional[bool]:
        """True se apareceu, False se esgotou o tempo, None se foi parado."""
        step = max(0.01, min(self.config.poll_interval, 1.0))
        deadline = self._clock() + timeout
        while True:
            if self._is_running():
                return True
            if self._clock() >= deadline:
                return False
            if self._stop.wait(step):
                return None


# --------------------------------------------------------------------------
# Auto-verificação (usada por `RobloxAutoRejoin.exe --selftest`)
# --------------------------------------------------------------------------
def self_check() -> List[Tuple[str, bool, str]]:
    """Verificações rápidas e sem efeitos secundários relevantes."""
    results: List[Tuple[str, bool, str]] = []

    def run(name: str, func: Callable[[], str]) -> None:
        try:
            results.append((name, True, func()))
        except Exception as exc:
            results.append((name, False, repr(exc)))

    def check_dir() -> str:
        return str(get_data_dir())

    def check_config() -> str:
        cfg, warns = load_config(get_data_dir() / CONFIG_FILE)
        return f"max_attempts={cfg.max_attempts}, poll_interval={cfg.poll_interval:g}"

    def check_log() -> str:
        logger = setup_logging(get_data_dir() / LOG_FILE)
        logger.info("Auto-verificação executada.")
        return "logs.txt gravável"

    def check_psutil() -> str:
        count = sum(1 for _ in psutil.process_iter(["pid"]))
        is_process_running(PROCESS_NAME)
        return f"{count} processos listados"

    def check_urls() -> str:
        return build_urls(123)["web"]

    run("pasta de dados gravável", check_dir)
    run("config.json", check_config)
    run("logs.txt", check_log)
    run("psutil / deteção de processos", check_psutil)
    run("formato dos links", check_urls)
    return results
