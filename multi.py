"""
Modo várias contas (Roblox Account Manager).

Como funciona:
  * O programa lança cada conta pela API local do RAM (LaunchAccount), UMA DE CADA
    VEZ, com uma pausa entre lançamentos.
  * Antes e depois de cada lançamento compara a lista de processos
    RobloxPlayerBeta.exe e a lista de ficheiros .log do Roblox: o processo e o log
    "novos" pertencem a essa conta. É assim que sabe qual processo é de qual conta
    (o Roblox não diz).
  * Cada conta tem o seu próprio Monitor (a mesma lógica de uma conta: reabrir se
    fechar, internet, "Disconnected" no log), que só fecha e reabre o processo DELA.

Limites conhecidos:
  * Só gere as contas que ele próprio lançou. Contas que já estavam abertas quando
    carregas em Iniciar não são adotadas (há um aviso no log).
  * Não foi testado com um RAM nem com Roblox reais.
"""

import contextlib
import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import psutil

import monitor as rm
import ramapi
from ramapi import AccountSpec, RamClient, RamError

LOG_WAIT = 20.0  # quanto esperar pelo ficheiro de log novo de uma conta depois de o processo abrir
START_TOLERANCE = 1.5  # segundos de diferença aceites ao comparar a hora de arranque de um processo

# Quanto "pior" é um estado, mais ele manda no estado geral mostrado na janela.
_PRIORITY = [rm.State.STOPPED, rm.State.DETECTED, rm.State.WAITING, rm.State.REJOINING, rm.State.OFFLINE, rm.State.ERROR]


# --------------------------------------------------------------------------
# Operações sobre processos (substituíveis nos testes)
# --------------------------------------------------------------------------
class ProcessOps:
    def pids(self) -> Set[int]:
        return set(rm.find_process_pids())

    def create_time(self, pid: int) -> Optional[float]:
        """Hora de arranque se o PID é (ainda) um RobloxPlayerBeta.exe vivo; senão None."""
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() != rm.PROCESS_NAME.lower() or proc.status() == psutil.STATUS_ZOMBIE:
                return None
            return proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    def kill(self, pid: int, wait: float = 5.0) -> None:
        """Fecha SÓ este processo (pede para terminar; se não terminar, força)."""
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(wait)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


# --------------------------------------------------------------------------
# Um lançamento de cada vez
# --------------------------------------------------------------------------
class LaunchGate:
    """Serializa os lançamentos e impõe uma pausa mínima entre eles."""

    def __init__(self, gap: float, stop: threading.Event, clock: Callable[[], float] = time.monotonic):
        self._gap = gap
        self._stop = stop
        self._clock = clock
        self._lock = threading.Lock()
        self._last_end: Optional[float] = None

    @contextlib.contextmanager
    def slot(self):
        """Dá True se é a nossa vez; False se o programa foi parado entretanto."""
        acquired = False
        while not self._stop.is_set():
            if self._lock.acquire(timeout=0.2):
                acquired = True
                break
        if not acquired:
            yield False
            return
        try:
            if self._last_end is not None:
                wait = self._last_end + self._gap - self._clock()
                if wait > 0 and self._stop.wait(wait):
                    yield False
                    return
            yield True
        finally:
            self._last_end = self._clock()
            self._lock.release()


class SharedNet:
    """Uma única verificação de internet por segundo, partilhada por todas as contas."""

    def __init__(self, fn: Callable[[], bool], ttl: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self._fn, self._ttl, self._clock = fn, ttl, clock
        self._lock = threading.Lock()
        self._at: Optional[float] = None
        self._value = True

    def __call__(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._ttl:
                self._value = bool(self._fn())
                self._at = self._clock()
            return self._value


# --------------------------------------------------------------------------
# Uma conta
# --------------------------------------------------------------------------
class AccountRunner:
    def __init__(
        self,
        spec: AccountSpec,
        client: RamClient,
        ops: ProcessOps,
        gate: LaunchGate,
        log_dirs: List[Path],
        stop: threading.Event,
        *,
        launch_timeout: float,
        log_wait: float = LOG_WAIT,
        logger: Optional[logging.Logger] = None,
    ):
        self.spec = spec
        self._client, self._ops, self._gate, self._stop = client, ops, gate, stop
        self._log_dirs = list(log_dirs)
        self._launch_timeout = launch_timeout
        self._log_wait = log_wait
        self._log = logger or logging.getLogger(rm.LOGGER_NAME)
        self.pid: Optional[int] = None
        self.create_time: Optional[float] = None
        self.log_file: Optional[Path] = None

    # -- processo desta conta
    def is_running(self) -> bool:
        pid, started = self.pid, self.create_time
        if pid is None or started is None:
            return False
        current = self._ops.create_time(pid)
        # a hora de arranque tem de coincidir: protege contra o Windows reutilizar o PID
        return current is not None and abs(current - started) <= START_TOLERANCE

    def log_file_getter(self) -> Optional[Path]:
        return self.log_file

    def kill(self) -> int:
        pid, started = self.pid, self.create_time
        if pid is None or not self.is_running():
            return 0
        self._ops.kill(pid)
        return 1

    # -- abrir esta conta
    def _logs(self) -> Set[Path]:
        found: Set[Path] = set()
        for directory in self._log_dirs:
            try:
                found.update(directory.glob("*.log"))
            except OSError:
                continue
        return found

    def launch(self) -> str:
        with self._gate.slot() as ok:
            if not ok:
                raise RamError("Monitorização parada.")
            before_pids = self._ops.pids()
            before_logs = self._logs()
            started = time.time()
            self.pid = self.create_time = self.log_file = None

            reply = self._client.launch(self.spec)
            self._log.info("RAM: pedido de lançamento aceite" + (f" ({reply})." if reply else "."))

            deadline = time.monotonic() + self._launch_timeout
            while True:
                fresh = self._ops.pids() - before_pids
                if fresh:
                    break
                if time.monotonic() >= deadline:
                    raise RamError(
                        f"O RAM aceitou o pedido mas não apareceu nenhum {rm.PROCESS_NAME} novo em "
                        f"{self._launch_timeout:g}s. O Multi Roblox está ligado no RAM? A conta tem sessão iniciada?"
                    )
                if self._stop.wait(0.5):
                    raise RamError("Monitorização parada.")
            # se houver mais do que um (invulgar), fica o mais antigo
            timed = [(self._ops.create_time(p), p) for p in fresh]
            timed = [(t, p) for t, p in timed if t is not None]
            if not timed:
                raise RamError(f"O {rm.PROCESS_NAME} novo fechou logo a seguir a abrir.")
            created, pid = min(timed)
            self.pid, self.create_time = pid, created
            self._log.info(f"Processo desta conta: PID {pid}.")

            # o log desta sessão é o ficheiro .log que não existia antes do lançamento
            wait_until = time.monotonic() + self._log_wait
            while time.monotonic() < wait_until and not self._stop.is_set():
                new = [f for f in self._logs() - before_logs if _mtime(f) >= started - 1]
                if new:
                    self.log_file = max(new, key=_mtime)
                    break
                time.sleep(0.25)
            if self.log_file is None:
                self._log.warning(
                    "Não encontrei o log desta conta: só vou detetar que o Roblox fechou, "
                    "não o ecrã 'Disconnected'."
                )
        return "ram"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# Gestor
# --------------------------------------------------------------------------
class MultiManager:
    """Faz o papel do Monitor para a janela, mas para várias contas. Mesma interface:
    start(), stop(), is_active, last_error."""

    def __init__(
        self,
        config: rm.Config,
        *,
        client: Optional[RamClient] = None,
        ops: Optional[ProcessOps] = None,
        notifier: Optional[rm.WebhookNotifier] = None,
        net_checker: Optional[Callable[[], bool]] = None,
        log_dirs: Optional[List[Path]] = None,
        net_ttl: float = 1.0,
        on_state: Optional[Callable[[rm.State], None]] = None,
        on_attempts: Optional[Callable[[int, int], None]] = None,
        on_accounts: Optional[Callable[[List[str]], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self._client = client or RamClient(config.ram_port, config.ram_password)
        self._ops = ops or ProcessOps()
        self._log = logger or logging.getLogger(rm.LOGGER_NAME)
        self._log_dirs = log_dirs if log_dirs is not None else rm.roblox_log_dirs(config)
        self._on_state, self._on_attempts, self._on_accounts = on_state, on_attempts, on_accounts
        self._notifier = notifier
        if self._notifier is None and config.webhook_url.strip() and rm.validate_webhook_url(config.webhook_url)[0]:
            self._notifier = rm.WebhookNotifier(config.webhook_url, logger=self._log)
        self._net = SharedNet(net_checker or rm.check_internet, ttl=net_ttl)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._monitors: Dict[str, rm.Monitor] = {}
        self._runners: Dict[str, AccountRunner] = {}
        self._states: Dict[str, rm.State] = {}
        self._attempts: Dict[str, int] = {}
        self._overall = rm.State.STOPPED
        self.state = rm.State.STOPPED
        self.last_error = ""

    # -- estado
    @property
    def is_active(self) -> bool:
        return any(m.is_active for m in list(self._monitors.values()))

    @property
    def runners(self) -> Dict[str, AccountRunner]:
        return self._runners

    @property
    def monitors(self) -> Dict[str, rm.Monitor]:
        return self._monitors

    def _lines(self) -> List[str]:
        out = []
        for name, mon in self._monitors.items():
            state = self._states.get(name, rm.State.STOPPED)
            extra = f" ({self._attempts.get(name, 0)}/{self.config.max_attempts})" if state == rm.State.REJOINING else ""
            out.append(f"{name}: {state.value}{extra}")
        return out

    def _recompute(self) -> None:
        states = list(self._states.values()) or [rm.State.STOPPED]
        overall = max(states, key=_PRIORITY.index)
        errors = [f"{n}: {m.last_error}" for n, m in self._monitors.items() if self._states.get(n) == rm.State.ERROR and m.last_error]
        self.last_error = " | ".join(errors)
        changed = overall != self._overall
        self._overall = self.state = overall
        if self._on_accounts:
            try:
                self._on_accounts(self._lines())
            except Exception:
                pass
        if changed and self._on_state:
            try:
                self._on_state(overall)
            except Exception:
                pass

    def _state_cb(self, name: str):
        def cb(state: rm.State) -> None:
            with self._lock:
                self._states[name] = state
                self._recompute()
        return cb

    def _attempts_cb(self, name: str):
        def cb(used: int, maximum: int) -> None:
            with self._lock:
                self._attempts[name] = used
                if self._on_attempts:
                    try:
                        self._on_attempts(max(self._attempts.values()), maximum)
                    except Exception:
                        pass
                self._recompute()
        return cb

    # -- controlo
    def start(self) -> bool:
        with self._start_lock:
            if self.is_active:
                self._log.warning("A monitorização já está em curso; pedido ignorado.")
                return False
            cfg = self.config
            errors = cfg.validate()
            if errors:
                self.last_error = "Configuração inválida: " + " ".join(errors)
                self._log.error(self.last_error)
                self.state = rm.State.ERROR
                return False
            specs, _ = ramapi.parse_accounts(cfg.ram_accounts, cfg.place_id)
            self._stop.clear()
            self.last_error = ""
            self._monitors, self._runners, self._states, self._attempts = {}, {}, {}, {}
            gate = LaunchGate(cfg.ram_launch_gap, self._stop)
            codes = rm.parse_disconnect_codes(cfg.disconnect_codes)

            existing = self._ops.pids()
            if existing:
                self._log.warning(
                    f"Já há {len(existing)} processo(s) {rm.PROCESS_NAME} abertos que este programa não gere. "
                    "Fecha-os antes de iniciar o modo RAM, senão podes ficar com contas duplicadas."
                )
            self._log.info(f"Modo RAM: {len(specs)} conta(s): " + ", ".join(s.name for s in specs) + ".")

            for spec in specs:
                runner = AccountRunner(
                    spec, self._client, self._ops, gate, self._log_dirs, self._stop,
                    launch_timeout=cfg.ram_launch_timeout, logger=_Labeled(self._log, spec.name),
                )
                watcher = None
                if cfg.disconnect_rejoin and codes:
                    watcher = rm.RobloxLogWatcher(
                        self._log_dirs, codes, file_fn=runner.log_file_getter,
                        on_file=lambda p, n=spec.name: self._log.info(f"[{n}] A ler o log desta conta: {p.name}"),
                    )
                monitor = rm.Monitor(
                    dataclasses.replace(cfg, place_id=str(spec.place_id)),
                    process_checker=runner.is_running,
                    killer=runner.kill,
                    launcher=runner.launch,
                    launch_on_start=True,
                    label=spec.name,
                    notifier=self._notifier,
                    net_checker=self._net,
                    log_watcher=watcher,
                    on_state=self._state_cb(spec.name),
                    on_attempts=self._attempts_cb(spec.name),
                    logger=self._log,
                )
                self._runners[spec.name] = runner
                self._monitors[spec.name] = monitor
                self._states[spec.name] = rm.State.STOPPED
            with self._lock:
                self._recompute()
            started = [m.start() for m in self._monitors.values()]
            return all(started)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for monitor in list(self._monitors.values()):
            monitor.stop(timeout=timeout)
        with self._lock:
            for name in self._states:
                if self._states[name] != rm.State.ERROR:
                    self._states[name] = rm.State.STOPPED
            self._recompute()
        if self._notifier is not None:
            self._notifier.flush(timeout=min(timeout, 5.0))


class _Labeled(logging.LoggerAdapter):
    def __init__(self, logger, label):
        super().__init__(logger, {"prefix": f"[{label}] "})

    def process(self, msg, kwargs):
        return f"{self.extra['prefix']}{msg}", kwargs
