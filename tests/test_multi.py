"""
Testes do modo várias contas (multi.py).

Tudo simulado: um "RAM" falso (lança processos e cria logs falsos), um "mundo" de
processos falso e logs numa pasta temporária. Prova a lógica (quem pertence a
quem, só a conta afetada é fechada/reaberta, lançamentos em fila, erros de uma
conta não param as outras). NÃO prova que o RAM nem o Roblox reais se comportam
assim: isso só se vê com o RAM a sério.
"""

import logging
import shutil
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import monitor as m  # noqa: E402
import multi  # noqa: E402
from monitor import State  # noqa: E402
from multi import AccountRunner, LaunchGate, MultiManager, SharedNet  # noqa: E402
from ramapi import AccountSpec, RamError  # noqa: E402
from test_monitor import FAST_LIMITS, fast_config, wait_for  # noqa: E402
from test_net import FakeNet  # noqa: E402
from test_webhook import FakeNotifier  # noqa: E402

KICK = "2026-10-29T19:20:01.277Z,500.277,1a2b [FLog::Network] Sending disconnect with reason: 277"
SECRET = "s3cr3t-pass"


class FakeWorld:
    """Processos do Roblox falsos (implementa a interface ProcessOps)."""

    def __init__(self):
        self.procs = {}
        self._next = 1000
        self.killed = []
        self._lock = threading.Lock()

    def spawn(self):
        with self._lock:
            self._next += 1
            self.procs[self._next] = time.time()
            return self._next

    def pids(self):
        return set(self.procs)

    def create_time(self, pid):
        return self.procs.get(pid)

    def kill(self, pid, wait=5.0):
        self.killed.append(pid)
        self.procs.pop(pid, None)

    def die(self, pid):  # o utilizador fecha a janela / crash
        self.procs.pop(pid, None)


class FakeRam:
    """Faz de RAM: ao 'lançar' cria um processo e um log novo."""

    def __init__(self, world, logdir):
        self.world, self.logdir = world, logdir
        self.calls = []  # (conta, início, fim)
        self.modes = {}  # conta -> ok | noproc | invalid
        self.logs = {}  # conta -> último log criado
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def launch(self, spec: AccountSpec):
        start = time.monotonic()
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            mode = self.modes.get(spec.name, "ok")
            if mode == "invalid":
                raise RamError(f"O RAM não conhece a conta '{spec.name}'.")
            if mode == "ok":
                pid = self.world.spawn()
                path = self.logdir / f"0.500_{spec.name}_{pid}_Player_last.log"
                path.write_text("[FLog::Output] joining\n", encoding="utf-8")
                self.logs[spec.name] = path
            return "Success"
        finally:
            with self._lock:
                self.active -= 1
                self.calls.append((spec.name, start, time.monotonic()))

    def names(self):
        return Counter(c[0] for c in self.calls)


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class MultiCase(unittest.TestCase):
    def setUp(self):
        self._old_limits = dict(m.LIMITS)
        m.LIMITS.update(FAST_LIMITS)
        self.dir = Path(tempfile.mkdtemp())
        self.world = FakeWorld()
        self.ram = FakeRam(self.world, self.dir)
        self.notifier = FakeNotifier()
        self.net = FakeNet()
        self.states, self.managers = [], []
        self.handler = ListHandler()
        self.logger = logging.getLogger(f"test_multi_{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        for mgr in self.managers:
            mgr.stop(timeout=2)
        m.LIMITS.clear()
        m.LIMITS.update(self._old_limits)
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, accounts="alpha,111\nbeta,222", **cfg):
        cfg.setdefault("ram_launch_gap", 0.1)
        cfg.setdefault("ram_launch_timeout", 2.0)
        cfg.setdefault("stable_session", 60.0)
        cfg.setdefault("disconnect_rejoin", True)
        mgr = MultiManager(
            fast_config(ram_enabled=True, ram_accounts=accounts, **cfg),
            client=self.ram,
            ops=self.world,
            notifier=self.notifier,
            net_checker=self.net.check,
            net_ttl=0.02,
            log_dirs=[self.dir],
            on_state=self.states.append,
            logger=self.logger,
        )
        self.managers.append(mgr)
        return mgr

    def up(self, mgr, names=None):
        names = names or list(mgr.monitors)
        return all(mgr.monitors[n].state == State.DETECTED for n in names)

    def start_ready(self, mgr):
        self.assertTrue(mgr.start())
        self.assertTrue(wait_for(lambda: self.up(mgr), timeout=8), [m_.state for m_ in mgr.monitors.values()])


class TestStartAndMapping(MultiCase):
    def test_launches_each_account_once_and_maps_pid_and_log(self):
        mgr = self.make()
        self.start_ready(mgr)
        a, b = mgr.runners["alpha"], mgr.runners["beta"]
        self.assertEqual(self.ram.names(), Counter(alpha=1, beta=1))
        self.assertNotEqual(a.pid, b.pid)
        self.assertEqual({a.pid, b.pid}, self.world.pids())
        self.assertEqual(a.log_file, self.ram.logs["alpha"])
        self.assertEqual(b.log_file, self.ram.logs["beta"])
        self.assertIn(str(a.pid), a.log_file.name)
        self.assertTrue(a.is_running() and b.is_running())

    def test_launches_are_serialized_with_a_gap(self):
        mgr = self.make(ram_launch_gap=0.3)
        self.start_ready(mgr)
        self.assertEqual(self.ram.max_active, 1, "Nunca dois lançamentos ao mesmo tempo")
        first, second = sorted(self.ram.calls, key=lambda c: c[1])
        self.assertGreaterEqual(second[1] - first[2], 0.3 - 0.02)

    def test_no_webhook_on_initial_launch(self):
        mgr = self.make()
        self.start_ready(mgr)
        time.sleep(0.3)
        self.assertEqual(self.notifier.sent, [])

    def test_overall_state_and_account_lines(self):
        lines = []
        mgr = self.make()
        mgr._on_accounts = lines.append
        self.start_ready(mgr)
        self.assertEqual(mgr.state, State.DETECTED)
        self.assertEqual(sorted(lines[-1]), ["alpha: Roblox detetado", "beta: Roblox detetado"])
        self.assertTrue(mgr.is_active)

    def test_existing_roblox_processes_trigger_a_warning(self):
        self.world.spawn()
        mgr = self.make()
        self.start_ready(mgr)
        self.assertTrue(any("não gere" in line for line in self.handler.lines))

    def test_invalid_config_does_not_start(self):
        mgr = self.make(accounts="alpha")  # falta Place ID e o geral é "123456789" -> válido
        self.assertTrue(mgr.start())
        mgr2 = MultiManager(fast_config(ram_enabled=True, ram_accounts="", place_id=""), client=self.ram,
                            ops=self.world, log_dirs=[self.dir], logger=self.logger)
        self.assertFalse(mgr2.start())
        self.assertIn("Configuração inválida", mgr2.last_error)


class TestIsolationPerAccount(MultiCase):
    def test_closing_one_account_only_relaunches_that_one(self):
        mgr = self.make()
        self.start_ready(mgr)
        a_pid, b_pid = mgr.runners["alpha"].pid, mgr.runners["beta"].pid
        self.world.die(a_pid)
        self.assertTrue(wait_for(lambda: self.ram.names()["alpha"] == 2 and self.up(mgr), timeout=8))
        time.sleep(0.3)
        self.assertEqual(self.ram.names(), Counter(alpha=2, beta=1))
        self.assertEqual(mgr.runners["beta"].pid, b_pid, "A outra conta não pode ser tocada")
        self.assertNotEqual(mgr.runners["alpha"].pid, a_pid)
        self.assertEqual(self.world.killed, [])
        titles = self.notifier.titles()
        self.assertEqual(titles[0], "⚠️ O Roblox fechou ou crashou")
        self.assertEqual(self.notifier.sent[0][3]["Conta"], "alpha")
        self.assertIn("✅ O Roblox voltou a abrir", titles)
        self.assertEqual(self.notifier.sent[1][3]["Conta"], "alpha")

    def test_disconnect_in_one_log_kills_and_relaunches_only_that_account(self):
        mgr = self.make()
        self.start_ready(mgr)
        a_pid, b_pid = mgr.runners["alpha"].pid, mgr.runners["beta"].pid
        with open(mgr.runners["alpha"].log_file, "a", encoding="utf-8") as fh:
            fh.write(KICK + "\n")
        self.assertTrue(wait_for(lambda: self.ram.names()["alpha"] == 2 and self.up(mgr), timeout=8))
        time.sleep(0.3)
        self.assertEqual(self.world.killed, [a_pid])
        self.assertEqual(self.ram.names(), Counter(alpha=2, beta=1))
        self.assertEqual(mgr.runners["beta"].pid, b_pid)
        self.assertEqual(self.notifier.sent[0][0], "🔌 O Roblox foi desligado do servidor")
        self.assertEqual(self.notifier.sent[0][3]["Conta"], "alpha")
        self.assertEqual(self.notifier.sent[0][3]["Erro"], "277")

    def test_disconnect_line_in_other_accounts_log_is_not_mixed_up(self):
        mgr = self.make()
        self.start_ready(mgr)
        with open(mgr.runners["beta"].log_file, "a", encoding="utf-8") as fh:
            fh.write(KICK + "\n")
        self.assertTrue(wait_for(lambda: self.ram.names()["beta"] == 2 and self.up(mgr), timeout=8))
        self.assertEqual(self.ram.names()["alpha"], 1)

    def test_old_log_of_previous_session_does_not_retrigger(self):
        mgr = self.make()
        self.start_ready(mgr)
        old_log = mgr.runners["alpha"].log_file
        with open(old_log, "a", encoding="utf-8") as fh:
            fh.write(KICK + "\n")
        self.assertTrue(wait_for(lambda: self.ram.names()["alpha"] == 2 and self.up(mgr), timeout=8))
        self.assertNotEqual(mgr.runners["alpha"].log_file, old_log)
        time.sleep(0.5)
        self.assertEqual(self.ram.names()["alpha"], 2)

    def test_launch_failure_of_one_account_does_not_stop_the_others(self):
        self.ram.modes["beta"] = "invalid"
        mgr = self.make(max_attempts=2)
        self.assertTrue(mgr.start())
        self.assertTrue(wait_for(lambda: mgr.monitors["beta"].state == State.ERROR, timeout=8))
        self.assertTrue(wait_for(lambda: mgr.monitors["alpha"].state == State.DETECTED, timeout=8))
        self.assertEqual(mgr.state, State.ERROR)
        self.assertIn("beta", mgr.last_error)
        self.assertNotIn("alpha", mgr.last_error)
        self.assertTrue(mgr.runners["alpha"].is_running())
        self.assertTrue(any("beta" in t and "reabrir" in t for t in
                            [f"{c[3].get('Conta', '')} {c[0]}" for c in self.notifier.sent]))

    def test_ram_accepts_but_no_process_appears(self):
        self.ram.modes["alpha"] = "noproc"
        mgr = self.make(accounts="alpha,111", max_attempts=1, ram_launch_timeout=0.4)
        self.assertTrue(mgr.start())
        self.assertTrue(wait_for(lambda: mgr.monitors["alpha"].state == State.ERROR, timeout=6))
        self.assertIn("Multi Roblox", mgr.monitors["alpha"].last_error + mgr.last_error)


class TestNetAndStop(MultiCase):
    def test_net_outage_restarts_every_account_one_at_a_time(self):
        mgr = self.make(net_rejoin=True, net_down_threshold=0.2, net_restore_delay=0.05)
        self.start_ready(mgr)
        old = {n: r.pid for n, r in mgr.runners.items()}
        self.net.online = False
        self.assertTrue(wait_for(lambda: mgr.state == State.OFFLINE, timeout=3))
        time.sleep(0.4)
        self.assertEqual(self.world.killed, [])
        self.net.online = True
        self.assertTrue(wait_for(lambda: self.ram.names() == Counter(alpha=2, beta=2) and self.up(mgr), timeout=10))
        self.assertEqual(sorted(self.world.killed), sorted(old.values()))
        self.assertEqual(self.ram.max_active, 1)
        self.assertTrue(all(mgr.runners[n].pid != old[n] for n in old))

    def test_stop_stops_all_monitors_and_launches(self):
        mgr = self.make()
        self.start_ready(mgr)
        mgr.stop()
        self.assertFalse(mgr.is_active)
        self.assertEqual(mgr.state, State.STOPPED)
        calls = len(self.ram.calls)
        self.world.die(mgr.runners["alpha"].pid)
        time.sleep(0.4)
        self.assertEqual(len(self.ram.calls), calls, "Parado: não pode reabrir nada")

    def test_stop_while_launching(self):
        mgr = self.make(accounts="a1,1\na2,2\na3,3", ram_launch_gap=5.0)
        self.assertTrue(mgr.start())
        wait_for(lambda: len(self.ram.calls) >= 1)
        start = time.monotonic()
        mgr.stop()
        self.assertLess(time.monotonic() - start, 4)
        self.assertFalse(mgr.is_active)
        self.assertLess(len(self.ram.calls), 3)

    def test_password_never_in_logs(self):
        mgr = MultiManager(
            fast_config(ram_enabled=True, ram_accounts="alpha,111", ram_password=SECRET, ram_launch_gap=0.05,
                        ram_launch_timeout=0.3, max_attempts=1),
            ops=self.world, log_dirs=[self.dir], logger=self.logger,
            net_checker=self.net.check,
            client=multi.RamClient(1, SECRET, opener=lambda url, timeout=None: (_ for _ in ()).throw(
                OSError("sem ligação"))),
        )
        self.managers.append(mgr)
        mgr.start()
        wait_for(lambda: mgr.monitors["alpha"].state == State.ERROR, timeout=6)
        joined = "\n".join(self.handler.lines) + mgr.last_error
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("Password", joined)


class TestHelpers(unittest.TestCase):
    def test_runner_rejects_reused_pid(self):
        world = FakeWorld()
        stop = threading.Event()
        runner = AccountRunner(AccountSpec("alpha", 1), None, world, LaunchGate(0, stop), [], stop, launch_timeout=1)
        pid = world.spawn()
        runner.pid, runner.create_time = pid, world.procs[pid]
        self.assertTrue(runner.is_running())
        world.procs[pid] = world.procs[pid] + 100  # o Windows reutilizou o PID para outro processo
        self.assertFalse(runner.is_running())
        self.assertEqual(runner.kill(), 0)
        self.assertEqual(world.killed, [])

    def test_gate_returns_false_when_stopped_while_waiting(self):
        stop = threading.Event()
        gate = LaunchGate(5.0, stop)
        with gate.slot() as ok:
            self.assertTrue(ok)
        results = []

        def second():
            with gate.slot() as ok:
                results.append(ok)

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.2)
        stop.set()
        t.join(2)
        self.assertEqual(results, [False])

    def test_shared_net_caches(self):
        calls = []
        now = [0.0]
        net = SharedNet(lambda: calls.append(1) or True, ttl=1.0, clock=lambda: now[0])
        net(); net(); net()
        self.assertEqual(len(calls), 1)
        now[0] = 1.5
        net()
        self.assertEqual(len(calls), 2)

    def test_labeled_monitor_logs_and_label_field(self):
        notifier = FakeNotifier()
        mon = m.Monitor(fast_config(), label="alpha", notifier=notifier, process_checker=lambda: True)
        mon._notify("closed", session=5)
        self.assertEqual(notifier.sent[0][3]["Conta"], "alpha")
        plain = m.Monitor(fast_config(), notifier=notifier, process_checker=lambda: True)
        plain._notify("closed", session=5)
        self.assertNotIn("Conta", notifier.sent[1][3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
