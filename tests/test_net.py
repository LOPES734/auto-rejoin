"""
Testes da reentrada quando a internet cai.

Usam um verificador de internet falso e um "fechador" falso do Roblox: provam a
lógica, NÃO a rede nem o Windows reais (o Roblox real pode comportar-se de
forma diferente, p. ex. reconectar-se sozinho em quedas curtas).
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil  # noqa: E402

import monitor as m  # noqa: E402
from monitor import Monitor, State  # noqa: E402
from test_monitor import FakeSystem, MonitorTestCase, fast_config, wait_for  # noqa: E402
from test_webhook import FakeNotifier  # noqa: E402


class FakeNet:
    def __init__(self, online=True):
        self.online = online
        self.calls = 0
        self.script = []  # respostas pontuais, consumidas primeiro

    def check(self):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return self.online


class NetTestCase(MonitorTestCase):
    def make_net(self, system, net, **cfg):
        self.notifier = FakeNotifier()
        self.kills = 0

        def killer():
            self.kills += 1
            system.running = False
            return 1

        cfg.setdefault("stable_session", 60.0)  # senão o contador de tentativas repõe-se a meio do teste
        mon = Monitor(
            fast_config(net_rejoin=True, **cfg),
            process_checker=system.checker,
            opener=system.opener,
            net_checker=net.check,
            killer=killer,
            notifier=self.notifier,
            on_state=self.states.append,
        )
        self.monitors.append(mon)
        return mon


class TestOutageWhileRunning(NetTestCase):
    def test_long_outage_restarts_roblox_after_internet_returns(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))

        net.online = False
        self.assertTrue(wait_for(lambda: mon.state == State.OFFLINE))
        time.sleep(0.4)  # > net_down_threshold (0.2)
        self.assertEqual(self.kills, 0, "Não pode fechar o Roblox enquanto não há internet")
        self.assertEqual(system.urls, [])

        net.online = True
        self.assertTrue(wait_for(lambda: mon.launch_count == 1 and mon.state == State.DETECTED, timeout=5))
        time.sleep(0.3)
        self.assertEqual(self.kills, 1)
        self.assertEqual(mon.launch_count, 1, "Sem aberturas duplicadas")
        self.assertEqual(mon.attempts, 1)
        titles = self.notifier.titles()
        self.assertEqual(titles[0], "🌐 A internet voltou")
        self.assertIn("fechado e reaberto", self.notifier.sent[0][1])
        self.assertIn("Internet em baixo", self.notifier.sent[0][3])
        self.assertIn("✅ O Roblox voltou a abrir", titles)

    def test_short_outage_does_not_touch_roblox(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net, net_down_threshold=5.0)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.online = False
        self.assertTrue(wait_for(lambda: mon.state == State.OFFLINE))
        net.online = True
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.4)
        self.assertEqual(self.kills, 0)
        self.assertEqual(system.urls, [])
        self.assertEqual(self.notifier.sent, [])

    def test_single_failed_check_is_ignored(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.script = [False]  # uma única falha isolada
        time.sleep(0.4)
        self.assertNotIn(State.OFFLINE, self.states)
        self.assertEqual(self.kills, 0)

    def test_process_check_is_paused_while_offline(self):
        """Se o Roblox fechar durante a queda, só reabre quando a internet voltar, sem gastar tentativas."""
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.online = False
        wait_for(lambda: mon.state == State.OFFLINE)
        system.running = False  # o Roblox fechou sem internet
        time.sleep(0.4)
        self.assertEqual(mon.launch_count, 0)
        self.assertEqual(mon.attempts, 0)
        net.online = True
        self.assertTrue(wait_for(lambda: mon.launch_count == 1 and mon.state == State.DETECTED, timeout=5))
        self.assertEqual(self.kills, 0, "O Roblox já tinha fechado; não há nada para fechar")
        self.assertEqual(mon.attempts, 1)


class TestClosedWithoutInternet(NetTestCase):
    def test_waits_for_internet_without_spending_attempts(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net, rejoin_delay=0.3)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.online = False
        system.running = False  # fecha logo ao mesmo tempo que cai a internet
        self.assertTrue(wait_for(lambda: mon.state == State.OFFLINE, timeout=3))
        time.sleep(0.4)
        self.assertEqual(mon.launch_count, 0)
        self.assertEqual(mon.attempts, 0)
        net.online = True
        self.assertTrue(wait_for(lambda: mon.launch_count == 1 and mon.state == State.DETECTED, timeout=5))
        self.assertEqual(mon.attempts, 1)
        self.assertEqual(net.calls > 0, True)
        self.assertIn("🌐 A internet voltou", self.notifier.titles())
        self.assertIn("A reabrir", self.notifier.sent[1][1])

    def test_gives_up_after_net_wait_max(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net, rejoin_delay=0.05, net_wait_max=0.3)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.online = False
        system.running = False
        self.assertTrue(wait_for(lambda: mon.state == State.ERROR, timeout=5))
        self.assertIn("Sem internet há mais de", mon.last_error)
        self.assertEqual(mon.launch_count, 0)
        self.assertEqual(mon.attempts, 0)
        self.assertTrue(wait_for(lambda: any("Não foi possível reabrir" in t for t in self.notifier.titles())))

    def test_stop_while_waiting_for_internet(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net, rejoin_delay=0.05, net_wait_max=60)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.online = False
        system.running = False
        wait_for(lambda: mon.state == State.OFFLINE)
        start = time.monotonic()
        mon.stop()
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(mon.is_active)
        self.assertEqual(mon.state, State.STOPPED)

    def test_roblox_back_by_itself_while_waiting(self):
        system, net = FakeSystem(running=True), FakeNet()
        mon = self.make_net(system, net, rejoin_delay=0.05)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        net.online = False
        system.running = False
        wait_for(lambda: mon.state == State.OFFLINE)
        system.running = True  # o utilizador abriu o Roblox à mão
        net.online = True
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED, timeout=5))
        time.sleep(0.3)
        self.assertEqual(system.urls, [], "Não deve abrir outra vez se já está aberto")


class TestDisabled(NetTestCase):
    def test_disabled_never_checks_or_kills(self):
        system, net = FakeSystem(running=True), FakeNet(online=False)
        mon = Monitor(
            fast_config(net_rejoin=False),
            process_checker=system.checker,
            opener=system.opener,
            net_checker=net.check,
            killer=lambda: 1 / 0,
        )
        self.monitors.append(mon)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.5)
        self.assertEqual(net.calls, 0)
        self.assertEqual(mon.state, State.DETECTED)
        system.running = False  # fecha: reabre normalmente mesmo com a "internet em baixo"
        self.assertTrue(wait_for(lambda: mon.launch_count == 1, timeout=5))

    def test_checker_exception_is_treated_as_online(self):
        system = FakeSystem(running=True)
        mon = Monitor(
            fast_config(net_rejoin=True),
            process_checker=system.checker,
            opener=system.opener,
            net_checker=lambda: 1 / 0,
        )
        self.monitors.append(mon)
        mon.start()
        self.assertTrue(wait_for(lambda: mon.state == State.DETECTED))
        time.sleep(0.3)
        self.assertEqual(mon.state, State.DETECTED)


class TestHelpers(unittest.TestCase):
    def test_check_internet_true_if_any_host_answers(self):
        calls = []

        def fake_connect(addr, timeout=None):
            calls.append(addr[0])
            if addr[0] == "1.1.1.1":
                return mock.MagicMock()
            raise OSError("sem rota")

        with mock.patch("socket.create_connection", fake_connect):
            self.assertTrue(m.check_internet())
        self.assertEqual(calls, ["www.roblox.com", "1.1.1.1"])

    def test_check_internet_false_if_all_fail(self):
        with mock.patch("socket.create_connection", side_effect=OSError("sem rota")):
            self.assertFalse(m.check_internet())

    def test_kill_roblox_terminates_only_matching_processes(self):
        class FakeProc:
            def __init__(self, name, dies_on_terminate=True):
                self.info = {"pid": 1, "name": name}
                self.alive = True
                self.dies = dies_on_terminate
                self.terminated = self.killed = False

            def terminate(self):
                self.terminated = True
                if self.dies:
                    self.alive = False

            def kill(self):
                self.killed = True
                self.alive = False

            def is_running(self):
                return self.alive

        good, stubborn, other = FakeProc("RobloxPlayerBeta.exe"), FakeProc("robloxplayerbeta.EXE", False), FakeProc("chrome.exe")
        count = m.kill_roblox(wait=0.3, iterator=lambda attrs: [good, stubborn, other])
        self.assertEqual(count, 2)
        self.assertTrue(good.terminated and not good.killed)
        self.assertTrue(stubborn.terminated and stubborn.killed, "Se não termina, é forçado")
        self.assertFalse(other.terminated or other.killed, "Não pode tocar noutros programas")

    def test_kill_roblox_ignores_vanished_processes(self):
        class Gone:
            @property
            def info(self):
                raise psutil.NoSuchProcess(1)

        self.assertEqual(m.kill_roblox(wait=0.1, iterator=lambda attrs: [Gone()]), 0)


class TestNetConfig(unittest.TestCase):
    def test_defaults_valid_and_roundtrip(self):
        import tempfile

        cfg = m.Config(place_id="123")
        self.assertTrue(cfg.net_rejoin)
        self.assertEqual(cfg.validate(), [])
        path = Path(tempfile.mkdtemp()) / "config.json"
        cfg.net_rejoin = False
        cfg.net_down_threshold = 45.0
        m.save_config(cfg, path)
        loaded, warns = m.load_config(path)
        self.assertFalse(loaded.net_rejoin)
        self.assertEqual(loaded.net_down_threshold, 45.0)
        self.assertEqual(warns, [])

    def test_invalid_values(self):
        cfg = m.Config(place_id="123", net_down_threshold=1.0, net_wait_max=1.0)
        errors = " ".join(cfg.validate())
        self.assertIn("Internet em baixo há", errors)
        self.assertIn("Espera máxima por internet", errors)

    def test_invalid_net_rejoin_in_file_falls_back_to_default(self):
        import json
        import tempfile

        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({"net_rejoin": "sim"}), encoding="utf-8")
        cfg, warns = m.load_config(path)
        self.assertTrue(cfg.net_rejoin)
        self.assertTrue(any("net_rejoin" in w for w in warns))


if __name__ == "__main__":
    unittest.main(verbosity=2)
