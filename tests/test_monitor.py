"""
Testes automatizados do Roblox Auto Rejoin.

IMPORTANTE: estes testes usam um "Roblox falso" (simulação) e um abridor
falso. Provam a lógica do programa, NÃO testam o Roblox real nem o Windows.

Executar:  py -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil  # noqa: E402

import monitor as m  # noqa: E402
from monitor import Config, LaunchError, Monitor, State  # noqa: E402

FAST_LIMITS = {
    "max_attempts": (1, 50),
    "poll_interval": (0.01, 60.0),
    "rejoin_delay": (0.01, 300.0),
    "retry_interval": (0.01, 600.0),
    "launch_wait": (0.05, 600.0),
    "stable_session": (0.05, 86400.0),
    "net_down_threshold": (0.01, 3600.0),
    "net_restore_delay": (0.01, 300.0),
    "net_wait_max": (0.05, 86400.0),
    "ram_port": (1, 65535),
    "ram_launch_gap": (0.0, 300.0),
    "ram_launch_timeout": (0.1, 600.0),
}


def fast_config(**overrides) -> Config:
    base = dict(
        place_id="123456789",
        max_attempts=3,
        poll_interval=0.02,
        rejoin_delay=0.05,
        retry_interval=0.05,
        launch_wait=0.3,
        stable_session=0.3,
        roblox_path="",
        net_rejoin=False,  # os testes de internet (test_net.py) ligam-no com um verificador falso
        disconnect_rejoin=False,  # os testes do log (test_disconnect.py) ligam-no com uma pasta temporária
        net_down_threshold=0.2,
        net_restore_delay=0.05,
        net_wait_max=5.0,
    )
    base.update(overrides)
    return Config(**base)


def wait_for(predicate, timeout=3.0, step=0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class FakeSystem:
    """Roblox falso: 'running' liga/desliga; o abridor simula o arranque."""

    def __init__(self, running=False, behaviours=None):
        self.running = running
        self.urls = []
        self.behaviours = list(behaviours or [])  # 'start' | 'ignore' | 'raise' | 'raise_first'
        self.default_behaviour = "start"
        self._lock = threading.Lock()

    def checker(self):
        return self.running

    def opener(self, url):
        with self._lock:
            self.urls.append(url)
            behaviour = self.behaviours.pop(0) if self.behaviours else self.default_behaviour
        if behaviour == "raise":
            raise LaunchError("sem programa associado")
        if behaviour == "start":
            threading.Timer(0.05, lambda: setattr(self, "running", True)).start()
        # 'ignore': não faz nada (o processo nunca aparece)


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self._old_limits = dict(m.LIMITS)
        m.LIMITS.update(FAST_LIMITS)
        self.states = []
        self.monitors = []

    def tearDown(self):
        for mon in self.monitors:
            mon.stop(timeout=2)
        m.LIMITS.clear()
        m.LIMITS.update(self._old_limits)

    def make(self, system, config=None) -> Monitor:
        mon = Monitor(
            config or fast_config(),
            process_checker=system.checker,
            opener=system.opener,
            on_state=self.states.append,
        )
        self.monitors.append(mon)
        return mon


# ---------------------------------------------------------------- cenário 1
class TestRobloxOpenNormally(MonitorTestCase):
    def test_detects_running_roblox_and_does_not_launch(self):
        system = FakeSystem(running=True)
        mon = self.make(system)
        self.assertTrue(mon.start())
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.3)
        self.assertEqual(system.urls, [])
        self.assertEqual(mon.launch_count, 0)
        mon.stop()
        self.assertEqual(mon.state, State.STOPPED)

    def test_does_not_open_game_when_started_with_roblox_closed(self):
        system = FakeSystem(running=False)
        mon = self.make(system)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.WAITING))
        time.sleep(0.4)
        self.assertEqual(system.urls, [], "Não deve abrir o jogo ao arrancar")
        self.assertEqual(mon.state, State.WAITING)
        # Quando o utilizador abre o Roblox à mão, passa a Detetado.
        system.running = True
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        self.assertEqual(system.urls, [])

    def test_second_start_is_ignored(self):
        system = FakeSystem(running=True)
        mon = self.make(system)
        self.assertTrue(mon.start())
        self.assertFalse(mon.start())
        threads = [t for t in threading.enumerate() if t.name == "rejoin-monitor"]
        self.assertEqual(len(threads), 1)


# ---------------------------------------------------------------- cenário 2
class TestRobloxClosedManually(MonitorTestCase):
    def test_rejoins_once_after_close(self):
        system = FakeSystem(running=True)
        # stable_session longo: senão o contador repõe-se a 0 durante o sleep do teste (corrida)
        mon = self.make(system, fast_config(stable_session=60.0))
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))

        system.running = False  # o utilizador fechou o Roblox
        self.assertTrue(wait_for(lambda: mon.state == State.REJOINING))
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED and mon.launch_count == 1))
        time.sleep(0.3)
        self.assertEqual(mon.launch_count, 1, "Não pode haver aberturas duplicadas")
        self.assertEqual(mon.attempts, 1)
        self.assertEqual(system.urls, ["https://www.roblox.com/games/start?placeId=123456789"])
        self.assertIn(State.REJOINING, self.states)

    def test_no_launch_if_roblox_reappears_during_delay(self):
        system = FakeSystem(running=True)
        cfg = fast_config(rejoin_delay=0.4)
        mon = self.make(system, cfg)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        wait_for(lambda: mon.state == State.REJOINING)
        system.running = True  # voltou a abrir sozinho
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.2)
        self.assertEqual(system.urls, [])
        self.assertEqual(mon.attempts, 0)


# ---------------------------------------------------------------- cenário 3
class TestProcessDoesNotAppear(MonitorTestCase):
    def test_second_attempt_alternates_link_and_succeeds(self):
        system = FakeSystem(running=True, behaviours=["ignore", "start"])
        mon = self.make(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.launch_count == 2, timeout=5))
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        self.assertEqual(mon.attempts, 2)
        self.assertEqual(
            system.urls,
            [
                "https://www.roblox.com/games/start?placeId=123456789",
                "roblox://placeId=123456789",
            ],
        )

    def test_opener_failure_uses_alternative_method_in_same_attempt(self):
        system = FakeSystem(running=True, behaviours=["raise", "start"])
        mon = self.make(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.launch_count == 1, timeout=5))
        self.assertEqual(mon.attempts, 1)
        self.assertEqual(len(system.urls), 2)
        self.assertTrue(system.urls[1].startswith("roblox://"))

    def test_opener_always_fails_gives_clear_error(self):
        system = FakeSystem(running=True)
        system.default_behaviour = "raise"
        mon = self.make(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.state == State.ERROR, timeout=5))
        self.assertIn("Limite de 3 tentativas", mon.last_error)
        self.assertEqual(mon.launch_count, 0)
        self.assertEqual(mon.attempts, 3)


# ---------------------------------------------------------------- cenário 4
class TestInvalidPlaceId(MonitorTestCase):
    def test_invalid_values_rejected(self):
        for bad in ["", "   ", "abc", "12a", "0", "-5", "12.5", "1 2", "https://www.roblox.com/games/123",
                    "123&x=1", "١٢٣", str(2**63), None]:
            ok, number, err = m.validate_place_id(bad)
            self.assertFalse(ok, f"{bad!r} devia ser inválido")
            self.assertIsNone(number)
            self.assertTrue(err)

    def test_valid_values_accepted(self):
        for good, expected in [("123456789", 123456789), ("  42  ", 42), (str(2**63 - 1), 2**63 - 1), (7, 7)]:
            ok, number, err = m.validate_place_id(good)
            self.assertTrue(ok, good)
            self.assertEqual(number, expected)

    def test_monitor_refuses_to_start_with_invalid_place_id(self):
        system = FakeSystem(running=True)
        mon = self.make(system, fast_config(place_id="abc"))
        self.assertFalse(mon.start())
        self.assertFalse(mon.is_active)
        self.assertEqual(mon.state, State.ERROR)
        self.assertIn("Place ID", mon.last_error)
        self.assertEqual(system.urls, [])

    def test_numeric_limits_validated(self):
        self.assertTrue(fast_config(max_attempts=0).validate())
        self.assertTrue(fast_config(max_attempts=2.5).validate())
        self.assertTrue(fast_config(poll_interval="x").validate())
        self.assertTrue(fast_config(launch_method="foo").validate())
        self.assertEqual(fast_config().validate(), [])

    def test_urls_cannot_be_injected(self):
        urls = m.build_urls(123)
        self.assertEqual(urls["web"], "https://www.roblox.com/games/start?placeId=123")
        self.assertEqual(urls["protocol"], "roblox://placeId=123")
        with self.assertRaises(ValueError):
            m.build_urls("123&x=1")  # type: ignore[arg-type]


# ---------------------------------------------------------------- cenário 5
class TestMaxAttempts(MonitorTestCase):
    def test_stops_after_max_attempts_when_process_never_appears(self):
        system = FakeSystem(running=True)
        system.default_behaviour = "ignore"
        mon = self.make(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.state == State.ERROR, timeout=8))
        self.assertTrue(wait_for(lambda: not mon.is_active))
        self.assertEqual(mon.launch_count, 3)
        self.assertEqual(mon.attempts, 3)
        self.assertIn("Limite de 3 tentativas", mon.last_error)
        time.sleep(0.4)
        self.assertEqual(mon.launch_count, 3, "Não pode haver ciclo infinito")

    def test_consecutive_crashes_without_stable_session_exhaust_attempts(self):
        system = FakeSystem(running=True)  # cada tentativa 'abre' o Roblox...
        cfg = fast_config(stable_session=30.0)  # ...mas nunca fica estável
        mon = self.make(system, cfg)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)

        def crash_when_running():
            while mon.state != State.ERROR and mon.is_active:
                if mon.state == State.DETECTED:
                    system.running = False
                time.sleep(0.01)

        crasher = threading.Thread(target=crash_when_running, daemon=True)
        crasher.start()
        self.assertTrue(wait_for(lambda: mon.state == State.ERROR, timeout=8))
        self.assertEqual(mon.launch_count, 3)
        self.assertEqual(mon.attempts, 3, "O contador não pode repor sem sessão estável")


# ---------------------------------------------------------------- cenário 6
class TestStopButton(MonitorTestCase):
    def test_stop_while_monitoring(self):
        system = FakeSystem(running=True)
        mon = self.make(system, fast_config(poll_interval=30.0))
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        started = time.monotonic()
        mon.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(mon.is_active)
        self.assertEqual(mon.state, State.STOPPED)

    def test_stop_during_rejoin_delay(self):
        system = FakeSystem(running=True)
        mon = self.make(system, fast_config(rejoin_delay=30.0))
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.state == State.REJOINING))
        started = time.monotonic()
        mon.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(mon.state, State.STOPPED)
        self.assertEqual(system.urls, [], "Parar antes da abertura não deve abrir nada")

    def test_stop_while_waiting_for_process_and_between_attempts(self):
        system = FakeSystem(running=True)
        system.default_behaviour = "ignore"
        mon = self.make(system, fast_config(launch_wait=30.0))
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: len(system.urls) == 1))
        mon.stop()
        self.assertFalse(mon.is_active)
        self.assertEqual(mon.state, State.STOPPED)
        time.sleep(0.2)
        self.assertEqual(len(system.urls), 1)

    def test_can_start_again_after_stop(self):
        system = FakeSystem(running=True)
        mon = self.make(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        mon.stop()
        self.assertTrue(mon.start())
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))


# ---------------------------------------------------------------- cenário 7
class TestStableSessionReset(MonitorTestCase):
    def test_counter_resets_only_after_stable_session(self):
        system = FakeSystem(running=True)
        cfg = fast_config(stable_session=0.4)
        mon = self.make(system, cfg)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.attempts == 1 and mon.state == State.DETECTED, timeout=5))
        # Logo a seguir à reentrada o contador ainda não foi reposto...
        self.assertEqual(mon.attempts, 1)
        # ...só depois da sessão estável (0.4 s) é que volta a 0.
        self.assertTrue(wait_for(lambda: mon.attempts == 0, timeout=3))


# ------------------------------------------------- erros na deteção (psutil)
class FakeProc:
    def __init__(self, name=None, pid=1, error=None, running=True):
        self._name, self._pid, self._error, self._running = name, pid, error, running

    @property
    def info(self):
        if self._error:
            raise self._error
        return {"name": self._name, "pid": self._pid}

    def is_running(self):
        if self._error:
            raise self._error
        return self._running


class TestDetectionRobustness(MonitorTestCase):
    def test_find_process_ignores_vanishing_and_denied_processes(self):
        procs = [
            FakeProc(error=psutil.NoSuchProcess(10)),
            FakeProc(error=psutil.AccessDenied(11)),
            FakeProc(name=None, pid=12),
            FakeProc(name="notepad.exe", pid=13),
            FakeProc(name="robloxplayerbeta.EXE", pid=14),
            FakeProc(name="RobloxPlayerBeta.exe", pid=15, running=False),
        ]
        pids = m.find_process_pids(iterator=lambda attrs: iter(procs))
        self.assertEqual(pids, [14])
        self.assertTrue(m.is_process_running(iterator=lambda attrs: iter(procs)))
        self.assertFalse(m.is_process_running(iterator=lambda attrs: iter([FakeProc(name="x.exe")])))

    def test_monitor_survives_checker_errors(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                raise psutil.AccessDenied(1)
            return True

        mon = Monitor(fast_config(), process_checker=flaky, opener=lambda url: None)
        self.monitors.append(mon)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.3)
        self.assertTrue(mon.is_active)
        self.assertEqual(mon.launch_count, 0, "Um erro de verificação não deve contar como 'Roblox fechou'")

    @unittest.skipUnless(os.name == "posix" and shutil.which("sleep"), "teste com processo real só em Linux/macOS")
    def test_real_psutil_detects_real_process(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            fake = tmp / "fakeroblox"
            shutil.copy(shutil.which("sleep"), fake)
            proc = subprocess.Popen([str(fake), "30"])
            try:
                self.assertTrue(wait_for(lambda: m.is_process_running("fakeroblox"), timeout=3))
            finally:
                proc.terminate()
                proc.wait(timeout=5)
            self.assertTrue(wait_for(lambda: not m.is_process_running("fakeroblox"), timeout=3))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- abrir a partir da pasta
class TestExeLaunch(MonitorTestCase):
    def test_auto_with_path_tries_exe_first_then_links(self):
        self.assertEqual(m.methods_for_attempt("auto", 1, True), ["exe", "web", "protocol"])
        self.assertEqual(m.methods_for_attempt("auto", 2, True), ["exe", "protocol", "web"])
        self.assertEqual(m.methods_for_attempt("auto", 1, False), ["web", "protocol"])
        self.assertEqual(m.methods_for_attempt("exe", 1, True), ["exe"])

    def test_exe_used_with_protocol_url(self):
        calls = []
        system = FakeSystem(running=True)
        mon = Monitor(
            fast_config(roblox_path="C:\\Roblox\\Versions\\version-x"),
            process_checker=system.checker, opener=system.opener,
            exe_opener=lambda path, url: (calls.append((path, url)), setattr(system, "running", True))[0],
        )
        self.monitors.append(mon)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.launch_count == 1, timeout=5))
        self.assertEqual(calls, [("C:\\Roblox\\Versions\\version-x", "roblox://placeId=123456789")])
        self.assertEqual(system.urls, [])

    def test_missing_path_falls_back_to_links(self):
        system = FakeSystem(running=True)
        mon = Monitor(
            fast_config(roblox_path="/nao/existe/version-zzz"),
            process_checker=system.checker, opener=system.opener,
        )
        self.monitors.append(mon)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.launch_count == 1, timeout=5))
        self.assertEqual(system.urls, ["https://www.roblox.com/games/start?placeId=123456789"])

    def test_resolve_folder_and_file(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            self.assertRaises(LaunchError, m.resolve_roblox_exe, str(tmp))
            beta = tmp / "RobloxPlayerBeta.exe"
            beta.write_text("x")
            self.assertEqual(m.resolve_roblox_exe(str(tmp)), beta)
            self.assertEqual(m.resolve_roblox_exe(str(beta)), beta)
            launcher = tmp / "RobloxPlayerLauncher.exe"
            launcher.write_text("x")
            self.assertEqual(m.resolve_roblox_exe('"%s"' % tmp), launcher)
            self.assertRaises(LaunchError, m.resolve_roblox_exe, str(tmp / "nada"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ configuração e logs
class TestConfigAndLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_match_specification_and_file_is_created(self):
        path = self.tmp / "config.json"
        cfg, warnings = m.load_config(path)
        self.assertTrue(path.exists())
        self.assertEqual(cfg.max_attempts, 5)
        self.assertEqual(cfg.poll_interval, 3)
        self.assertEqual(cfg.rejoin_delay, 5)
        self.assertEqual(cfg.place_id, "")
        self.assertTrue(warnings)

    def test_roundtrip(self):
        path = self.tmp / "config.json"
        cfg = Config(place_id="999", max_attempts=7, poll_interval=2.5)
        m.save_config(cfg, path)
        loaded, warnings = m.load_config(path)
        self.assertEqual(loaded, cfg)
        self.assertEqual(warnings, [])

    def test_corrupt_file_is_backed_up_and_replaced(self):
        path = self.tmp / "config.json"
        path.write_text("{isto não é json", encoding="utf-8")
        cfg, warnings = m.load_config(path)
        self.assertEqual(cfg, Config())
        self.assertTrue((self.tmp / "config.json.bad").exists())
        self.assertTrue(warnings)
        json.loads(path.read_text(encoding="utf-8"))  # o novo ficheiro é JSON válido

    def test_bad_types_fall_back_to_defaults(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"place_id": 555, "max_attempts": "abc", "poll_interval": True,
                                    "launch_method": "xyz", "password": "segredo"}), encoding="utf-8")
        cfg, warnings = m.load_config(path)
        self.assertEqual(cfg.place_id, "555")
        self.assertEqual(cfg.max_attempts, 5)
        self.assertEqual(cfg.poll_interval, 3.0)
        self.assertEqual(cfg.launch_method, "auto")
        self.assertEqual(len(warnings), 3)

    def test_config_never_stores_unknown_or_secret_keys(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"place_id": "1", "cookie": "x", "token": "y"}), encoding="utf-8")
        cfg, _ = m.load_config(path)
        m.save_config(cfg, path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {f for f in Config.__dataclass_fields__})

    def test_data_dir_override_and_writable(self):
        os.environ[m.ENV_HOME] = str(self.tmp / "dados")
        try:
            self.assertEqual(m.get_data_dir(), self.tmp / "dados")
        finally:
            del os.environ[m.ENV_HOME]

    def test_frozen_exe_uses_appdata_or_portable_folder(self):
        exe_dir = self.tmp / "app"
        exe_dir.mkdir()
        appdata = self.tmp / "AppData"
        old_frozen = getattr(sys, "frozen", None)
        old_exe = sys.executable
        old_appdata = os.environ.get("APPDATA")
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = str(exe_dir / "RobloxAutoRejoin.exe")
        os.environ["APPDATA"] = str(appdata)
        os.environ.pop(m.ENV_HOME, None)
        try:
            self.assertEqual(m.get_data_dir(), appdata / "RobloxAutoRejoin")
            (exe_dir / m.PORTABLE_FLAG).write_text("", encoding="utf-8")
            self.assertEqual(m.get_data_dir(), exe_dir / "data")
        finally:
            if old_frozen is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = old_frozen  # type: ignore[attr-defined]
            sys.executable = old_exe
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata

    def test_logs_written_to_file_and_callback(self):
        received = []
        logger = m.setup_logging(self.tmp / "logs.txt", callback=received.append)
        logger.info("olá logs")
        for h in logger.handlers:
            h.flush()
        text = (self.tmp / "logs.txt").read_text(encoding="utf-8")
        self.assertIn("olá logs", text)
        self.assertTrue(any("olá logs" in line for line in received))
        # Chamar de novo não duplica handlers
        m.setup_logging(self.tmp / "logs.txt", callback=received.append)
        self.assertEqual(len(logger.handlers), 2)
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()

    def test_self_check_passes(self):
        os.environ[m.ENV_HOME] = str(self.tmp / "sc")
        try:
            results = m.self_check()
        finally:
            del os.environ[m.ENV_HOME]
            logger = m.logging.getLogger(m.LOGGER_NAME)
            for h in list(logger.handlers):
                logger.removeHandler(h)
                h.close()
        failed = [r for r in results if not r[1]]
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
