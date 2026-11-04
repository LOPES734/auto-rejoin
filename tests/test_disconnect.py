"""
Testes da deteção de "desligado do servidor" através do log do Roblox.

IMPORTANTE: as linhas de log usadas aqui são EXEMPLOS plausíveis, escritos por
mim; não foram copiadas de um log real do Roblox. Provam a lógica (ler só o que é
novo, ignorar sessões antigas, reagir só aos códigos pedidos), NÃO que o formato
real do log do Roblox coincide. Isso só se confirma com o Roblox a sério.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import monitor as m  # noqa: E402
from monitor import Monitor, RobloxLogWatcher, State  # noqa: E402
from test_monitor import FakeSystem, MonitorTestCase, fast_config, wait_for  # noqa: E402
from test_webhook import FakeNotifier  # noqa: E402

KICK = "2026-10-29T19:20:01.277Z,500.277,1a2b [FLog::Network] Sending disconnect with reason: 277"
KICK_UI = "2026-10-29T19:20:01.300Z,500.300,1a2b [FLog::Network] Disconnected (Error Code: 277)"
NOISE = "2026-10-29T19:20:00.277Z,499.277,1a2b [FLog::Network] ping 277 ms to server"


class LogCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "0.500_Player_X_last.log"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, *lines, mode="a", path=None):
        with open(path or self.log, mode, encoding="utf-8", newline="") as fh:
            for line in lines:
                fh.write(line + "\n")

    def watcher(self, codes=(277,), since=None):
        w = RobloxLogWatcher([self.dir], set(codes), start_time_fn=lambda: since)
        w.attach()
        return w


class TestWatcher(LogCase):
    def test_detects_disconnect_lines(self):
        for line in (KICK, KICK_UI, "Connection lost - Id: 277"):
            self.log.write_text("", encoding="utf-8")
            w = self.watcher()
            self.write("normal line", line)
            hit = w.poll()
            self.assertIsNotNone(hit, line)
            self.assertEqual(hit[0], 277)

    def test_only_reads_new_data_and_reports_once(self):
        w = self.watcher()
        self.write("a", "b")
        self.assertIsNone(w.poll())
        self.write(KICK)
        self.assertIsNotNone(w.poll())
        self.assertIsNone(w.poll(), "A mesma linha não pode disparar duas vezes")
        self.write("c")
        self.assertIsNone(w.poll())

    def test_ignores_other_codes_and_timestamp_noise(self):
        w = self.watcher(codes=(277,))
        self.write(
            "Sending disconnect with reason: 273",  # conta aberta noutro lado: não reentrar
            "Disconnected (Error Code: 267)",  # expulso
            NOISE,  # 277 só em milissegundos/ping
            "[FLog::Network] Disconnect handler registered",  # sem código
        )
        self.assertIsNone(w.poll())

    def test_codes_are_configurable(self):
        w = self.watcher(codes=(273,))
        self.write("Sending disconnect with reason: 273")
        self.assertEqual(w.poll()[0], 273)

    def test_incomplete_line_waits_for_newline(self):
        w = self.watcher()
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("Sending disconnect with reason: 2")
        self.assertIsNone(w.poll())
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("77\n")
        self.assertEqual(w.poll()[0], 277)

    def test_old_session_log_is_ignored(self):
        self.write(KICK)
        os.utime(self.log, (time.time() - 100, time.time() - 100))
        w = self.watcher(since=time.time() - 10)  # o processo atual arrancou depois desse log
        self.assertIsNone(w.poll())
        # um log novo (da sessão atual) é lido desde o início
        new = self.dir / "0.501_Player_Y_last.log"
        self.write("x", KICK, path=new)
        self.assertEqual(w.poll()[0], 277)

    def test_already_disconnected_log_from_current_session_is_found(self):
        self.write("x", KICK)  # já estava desligado quando a monitorização começou
        w = self.watcher(since=time.time() - 60)
        self.assertEqual(w.poll()[0], 277)

    def test_newest_file_wins_and_restarts_at_zero(self):
        old = self.dir / "old.log"
        self.write("x", path=old)
        os.utime(old, (time.time() - 50, time.time() - 50))
        w = self.watcher()
        self.write("y")
        os.utime(self.log, (time.time() - 5, time.time() - 5))  # datas bem distintas (o Windows tem pouca resolução)
        self.assertIsNone(w.poll())
        new = self.dir / "new.log"
        self.write(KICK, path=new)
        self.assertEqual(w.poll()[0], 277)

    def test_equal_mtime_tie_breaks_by_name(self):
        """Simula a baixa resolução de datas do Windows: dois logs com a MESMA data."""
        a, b = self.dir / "0.500_Player_A_last.log", self.dir / "0.500_Player_B_last.log"
        w = self.watcher()
        self.write("x", path=a)
        self.write(KICK, path=b)
        t = time.time()
        os.utime(a, (t, t))
        os.utime(b, (t, t))
        self.assertEqual(w.poll()[0], 277)

    def test_missing_dir_and_bad_bytes_are_safe(self):
        w = RobloxLogWatcher([self.dir / "nao_existe"], {277}, start_time_fn=lambda: None)
        w.attach()
        self.assertIsNone(w.poll())
        self.assertFalse(w.has_dirs())
        w2 = self.watcher()
        with open(self.log, "ab") as fh:
            fh.write(b"\xff\xfe lixo\n" + KICK.encode() + b"\n")
        self.assertEqual(w2.poll()[0], 277)

    def test_log_that_triggered_is_never_read_again(self):
        w = self.watcher()
        self.write(KICK)
        self.assertIsNotNone(w.poll())
        w.attach()  # novo processo, mas o log velho acabou de ser modificado (dentro da margem)
        self.assertIsNone(w.poll(), "O mesmo log não pode provocar uma segunda reentrada")
        new = self.dir / "0.502_Player_Z_last.log"
        self.write(KICK, path=new)
        self.assertIsNotNone(w.poll(), "Um log novo continua a ser vigiado")

    def test_non_log_files_are_ignored(self):
        w = self.watcher()
        self.write(KICK, path=self.dir / "notes.txt")
        self.assertIsNone(w.poll())

    def test_truncated_file_restarts(self):
        w = self.watcher()
        self.write("a" * 200)
        w.poll()
        self.log.write_text(KICK + "\n", encoding="utf-8")  # recriado, mais pequeno
        self.assertEqual(w.poll()[0], 277)


class TestMonitorReactsToDisconnect(LogCase, MonitorTestCase):
    def setUp(self):
        LogCase.setUp(self)
        MonitorTestCase.setUp(self)
        self.kills = 0
        self.since = None

    def tearDown(self):
        MonitorTestCase.tearDown(self)
        LogCase.tearDown(self)

    def make_disc(self, system, **cfg):
        self.notifier = FakeNotifier()

        def killer():
            self.kills += 1
            self.since = time.time()  # o processo novo arranca agora: logs anteriores deixam de contar
            system.running = False
            return 1

        watcher = RobloxLogWatcher([self.dir], {277, 278}, start_time_fn=lambda: self.since)
        cfg.setdefault("stable_session", 60.0)
        cfg.setdefault("disconnect_rejoin", True)
        mon = Monitor(
            fast_config(**cfg),
            process_checker=system.checker,
            opener=system.opener,
            log_watcher=watcher,
            killer=killer,
            notifier=self.notifier,
            on_state=self.states.append,
        )
        self.monitors.append(mon)
        return mon

    def test_disconnect_while_process_alive_restarts_roblox(self):
        system = FakeSystem(running=True)
        mon = self.make_disc(system)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        self.write("jogo a correr", KICK_UI)
        self.assertTrue(wait_for(lambda: mon.launch_count == 1 and mon.state == State.DETECTED, timeout=5))
        time.sleep(0.5)
        self.assertEqual(self.kills, 1)
        self.assertEqual(mon.launch_count, 1, "O log antigo não pode voltar a disparar depois de reabrir")
        self.assertEqual(mon.attempts, 1)
        first = self.notifier.sent[0]
        self.assertEqual(first[0], "🔌 O Roblox foi desligado do servidor")
        self.assertEqual(first[3]["Erro"], "277")
        self.assertIn("✅ O Roblox voltou a abrir", self.notifier.titles())

    def test_already_disconnected_when_monitoring_starts(self):
        self.write(KICK_UI)
        system = FakeSystem(running=True)
        mon = self.make_disc(system)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.launch_count == 1, timeout=5))
        self.assertEqual(self.kills, 1)

    def test_other_codes_do_nothing(self):
        system = FakeSystem(running=True)
        mon = self.make_disc(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        self.write("Sending disconnect with reason: 273", "Disconnected (Error Code: 267)", NOISE)
        time.sleep(0.5)
        self.assertEqual(self.kills, 0)
        self.assertEqual(system.urls, [])
        self.assertEqual(self.notifier.sent, [])

    def test_disabled_in_config_does_nothing(self):
        system = FakeSystem(running=True)
        mon = self.make_disc(system, disconnect_rejoin=False)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        self.write(KICK)
        time.sleep(0.5)
        self.assertEqual(self.kills, 0)

    def test_repeated_disconnects_respect_max_attempts(self):
        """Se cada sessão nova volta a ser desligada, para ao atingir o limite (sem ciclo infinito)."""
        system = FakeSystem(running=True)
        mon = self.make_disc(system, max_attempts=2)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        n = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and mon.state != State.ERROR:
            n += 1
            self.write(KICK, path=self.dir / f"0.6{n:02d}_Player_S{n}_last.log")  # um log por sessão
            time.sleep(0.15)
        self.assertEqual(mon.state, State.ERROR)
        self.assertIn("Limite de 2 tentativas", mon.last_error)
        self.assertEqual(mon.launch_count, 2)

    def test_watcher_errors_do_not_break_monitor(self):
        class Broken:
            def attach(self):
                raise RuntimeError("x")

            def poll(self):
                raise RuntimeError("y")

            def has_dirs(self):
                return True

        system = FakeSystem(running=True)
        mon = Monitor(
            fast_config(disconnect_rejoin=True),
            process_checker=system.checker,
            opener=system.opener,
            log_watcher=Broken(),
        )
        self.monitors.append(mon)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.3)
        self.assertEqual(mon.state, State.DETECTED)


class TestDisconnectConfig(unittest.TestCase):
    def test_parse_codes(self):
        self.assertEqual(m.parse_disconnect_codes("277, 278,279"), {277, 278, 279})
        for bad in ("", "abc", "277,x", "0", "-1", None):
            self.assertIsNone(m.parse_disconnect_codes(bad), bad)

    def test_defaults_exclude_account_and_kick_codes(self):
        codes = m.parse_disconnect_codes(m.Config().disconnect_codes)
        self.assertIn(277, codes)
        self.assertNotIn(273, codes)
        self.assertNotIn(267, codes)

    def test_validate_and_roundtrip(self):
        self.assertTrue(any("Códigos" in e for e in m.Config(place_id="1", disconnect_codes="abc").validate()))
        self.assertEqual(m.Config(place_id="1", disconnect_rejoin=False, disconnect_codes="abc").validate(), [])
        path = Path(tempfile.mkdtemp()) / "config.json"
        cfg = m.Config(place_id="1", disconnect_rejoin=False, disconnect_codes="277", roblox_log_dir="D:\\logs")
        m.save_config(cfg, path)
        loaded, warns = m.load_config(path)
        self.assertEqual((loaded.disconnect_rejoin, loaded.disconnect_codes, loaded.roblox_log_dir), (False, "277", "D:\\logs"))
        self.assertEqual(warns, [])
        path.write_text(json.dumps({"disconnect_rejoin": "sim"}), encoding="utf-8")
        loaded, warns = m.load_config(path)
        self.assertTrue(loaded.disconnect_rejoin)
        self.assertTrue(any("disconnect_rejoin" in w for w in warns))

    def test_log_dirs_include_defaults_and_versions_sibling(self):
        old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = r"C:\Users\PC\AppData\Local"
        try:
            cfg = m.Config(roblox_path=r"C:\Users\PC\AppData\Local\Roblox\Versions\version-abc", roblox_log_dir="D:\\meus")
            dirs = [str(d) for d in m.roblox_log_dirs(cfg)]
        finally:
            if old is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old
        self.assertEqual(dirs[0], "D:\\meus")
        self.assertEqual(len(dirs), len(set(dirs)), "sem duplicados")
        self.assertTrue(any(d.replace("\\", "/").endswith("Local/Roblox/logs") for d in dirs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
