"""
Testes do sistema de webhook (Discord).

Não enviam nada para o Discord: usam um 'sender' falso e um servidor HTTP
local para provar o formato do pedido. Não provam que o teu webhook real funciona
(para isso usa o botão "Testar" da aplicação).
"""

import http.server
import json
import socketserver
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import monitor as m  # noqa: E402
from monitor import State, WebhookError, WebhookNotifier  # noqa: E402
from test_monitor import FakeSystem, MonitorTestCase, fast_config, wait_for  # noqa: E402

GOOD_URL = "https://discord.com/api/webhooks/123456789012345678/AbC-dEf_123token"


class FakeNotifier:
    """Guarda as mensagens em vez de as enviar."""

    def __init__(self):
        self.sent = []

    def send(self, title, description, color, fields=None):
        self.sent.append((title, description, color, dict(fields or [])))
        return True

    def flush(self, timeout=5.0):
        return True

    def titles(self):
        return [t for t, *_ in self.sent]


class TestUrlValidation(unittest.TestCase):
    def test_empty_is_valid_and_means_disabled(self):
        self.assertTrue(m.validate_webhook_url("")[0])
        self.assertTrue(m.validate_webhook_url(None)[0])
        self.assertTrue(m.validate_webhook_url("   ")[0])

    def test_valid_discord_urls(self):
        for url in (
            GOOD_URL,
            "https://discordapp.com/api/webhooks/1/abc",
            "https://ptb.discord.com/api/webhooks/1/abc",
            "https://discord.com/api/v10/webhooks/1/abc",
        ):
            self.assertTrue(m.validate_webhook_url(url)[0], url)

    def test_invalid_urls(self):
        for url in (
            "http://discord.com/api/webhooks/1/abc",  # sem https
            "https://evil.example.com/api/webhooks/1/abc",  # outro servidor
            "https://discord.com.evil.com/api/webhooks/1/abc",
            "https://discord.com/api/webhooks/abc/def",  # id não numérico
            "https://discord.com/",
            "não é um url",
        ):
            ok, err = m.validate_webhook_url(url)
            self.assertFalse(ok, url)
            self.assertIn("Webhook", err)

    def test_config_validate_reports_bad_webhook(self):
        bad = m.Config(place_id="123", webhook_url="https://example.com/x")
        self.assertTrue(any("Webhook" in e for e in bad.validate()))
        self.assertEqual(m.Config(place_id="123", webhook_url=GOOD_URL).validate(), [])
        self.assertEqual(m.Config(place_id="123").validate(), [])

    def test_redact_hides_token(self):
        shown = m.redact_webhook(GOOD_URL)
        self.assertNotIn("AbC-dEf_123token", shown)
        self.assertIn("123456789012345678", shown)

    def test_config_roundtrip_keeps_webhook(self):
        import tempfile

        path = Path(tempfile.mkdtemp()) / "config.json"
        m.save_config(fast_config(webhook_url=GOOD_URL), path)
        cfg, warns = m.load_config(path)
        self.assertEqual(cfg.webhook_url, GOOD_URL)
        self.assertEqual(warns, [])


class TestPayloadAndPost(unittest.TestCase):
    def test_payload_shape(self):
        p = m.build_webhook_payload("T", "D", m.COLOR_RED, [("PC", "x"), ("Vazio", "")])
        emb = p["embeds"][0]
        self.assertEqual((emb["title"], emb["description"], emb["color"]), ("T", "D", m.COLOR_RED))
        self.assertEqual(emb["fields"][1]["value"], "-")  # o Discord rejeita valores vazios
        self.assertIn("timestamp", emb)

    def test_payload_truncates_long_text(self):
        emb = m.build_webhook_payload("T" * 500, "D" * 5000, 0)["embeds"][0]
        self.assertLessEqual(len(emb["title"]), 256)
        self.assertLessEqual(len(emb["description"]), 2000)

    def test_post_sends_json_with_user_agent_to_local_server(self):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received["body"] = json.loads(body)
                received["ua"] = self.headers["User-Agent"]
                received["ct"] = self.headers["Content-Type"]
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            threading.Thread(target=srv.handle_request, daemon=True).start()
            payload = m.build_webhook_payload("Olá", "teste", m.COLOR_GREEN)
            m.post_webhook(f"http://127.0.0.1:{srv.server_address[1]}/x", payload, timeout=3)
        self.assertEqual(received["body"]["embeds"][0]["title"], "Olá")
        self.assertEqual(received["ct"], "application/json")
        self.assertTrue(received["ua"].startswith("RobloxAutoRejoin"))
        self.assertNotIn("Python-urllib", received["ua"])

    def test_http_errors_become_webhook_error_without_url(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            threading.Thread(target=srv.handle_request, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/segredo-token"
            with self.assertRaises(WebhookError) as ctx:
                m.post_webhook(url, {}, timeout=3)
        self.assertIn("404", str(ctx.exception))
        self.assertNotIn("segredo-token", str(ctx.exception))

    def test_connection_error_becomes_webhook_error(self):
        with self.assertRaises(WebhookError):
            m.post_webhook("http://127.0.0.1:9/x", {}, timeout=1)

    def test_send_test_webhook(self):
        calls = []
        ok, msg = m.send_test_webhook(GOOD_URL, sender=lambda u, p: calls.append((u, p)))
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], GOOD_URL)
        ok, msg = m.send_test_webhook("", sender=lambda u, p: None)
        self.assertFalse(ok)
        ok, msg = m.send_test_webhook("https://x.com/a", sender=lambda u, p: None)
        self.assertFalse(ok)

        def boom(u, p):
            raise WebhookError("falhou")

        self.assertEqual(m.send_test_webhook(GOOD_URL, sender=boom), (False, "falhou"))


class TestNotifier(unittest.TestCase):
    def test_sends_in_order_in_background(self):
        got = []
        n = WebhookNotifier(GOOD_URL, sender=lambda u, p: got.append(p["embeds"][0]["title"]))
        for i in range(5):
            n.send(f"m{i}", "d", 0)
        self.assertTrue(n.flush(3))
        self.assertEqual(got, [f"m{i}" for i in range(5)])

    def test_send_does_not_block_on_slow_sender(self):
        n = WebhookNotifier(GOOD_URL, sender=lambda u, p: time.sleep(0.5))
        start = time.monotonic()
        n.send("a", "d", 0)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_failure_is_logged_without_url_and_does_not_raise(self):
        import logging

        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        log = logging.getLogger("test_webhook_logger")
        log.addHandler(H())
        log.setLevel(logging.INFO)

        def boom(u, p):
            raise WebhookError("sem ligação")

        n = WebhookNotifier(GOOD_URL, sender=boom, logger=log)
        n.send("a", "d", 0)
        n.flush(2)
        self.assertTrue(any("não enviado" in r for r in records))
        self.assertFalse(any("AbC-dEf_123token" in r for r in records))

    def test_unexpected_exception_in_sender_is_contained(self):
        def boom(u, p):
            raise RuntimeError("bug")

        n = WebhookNotifier(GOOD_URL, sender=boom)
        n.send("a", "d", 0)
        self.assertTrue(n.flush(2))
        got = []
        n._sender = lambda u, p: got.append(1)
        n.send("b", "d", 0)  # a thread continua viva
        n.flush(2)
        self.assertEqual(got, [1])

    def test_rate_limit_retries_once(self):
        calls = []

        def sender(u, p):
            calls.append(1)
            if len(calls) == 1:
                raise WebhookError("429", retry_after=0.05)

        n = WebhookNotifier(GOOD_URL, sender=sender)
        n.send("a", "d", 0)
        n.flush(3)
        self.assertEqual(len(calls), 2)

    def test_full_queue_drops_instead_of_blocking(self):
        gate = threading.Event()
        n = WebhookNotifier(GOOD_URL, sender=lambda u, p: gate.wait(2))
        results = [n.send(f"m{i}", "d", 0) for i in range(m.WEBHOOK_MAX_QUEUE + 5)]
        gate.set()
        self.assertIn(False, results)


class TestMonitorSendsWebhook(MonitorTestCase):
    def make_hooked(self, system, config=None):
        self.notifier = FakeNotifier()
        mon = m.Monitor(
            config or fast_config(),
            process_checker=system.checker,
            opener=system.opener,
            notifier=self.notifier,
            on_state=self.states.append,
        )
        self.monitors.append(mon)
        return mon

    def test_close_then_reopen_sends_two_messages(self):
        system = FakeSystem(running=True)
        mon = self.make_hooked(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: len(self.notifier.sent) >= 2, timeout=5))
        time.sleep(0.2)
        self.assertEqual(len(self.notifier.sent), 2)
        closed, recovered = self.notifier.sent
        self.assertIn("fechou", closed[0])
        self.assertEqual(closed[3]["Place ID"], "123456789")
        self.assertIn("Sessão durou", closed[3])
        self.assertIn("voltou a abrir", recovered[0])
        self.assertEqual(recovered[3]["Tentativas"], "1/3")
        self.assertEqual(recovered[3]["Método"], "web")

    def test_no_message_when_nothing_happens(self):
        system = FakeSystem(running=True)
        mon = self.make_hooked(system)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        time.sleep(0.3)
        self.assertEqual(self.notifier.sent, [])

    def test_no_message_when_started_with_roblox_closed(self):
        system = FakeSystem(running=False)
        mon = self.make_hooked(system)
        mon.start()
        time.sleep(0.3)
        self.assertEqual(self.notifier.sent, [])

    def test_roblox_reappears_by_itself_notifies_closed_and_recovered(self):
        system = FakeSystem(running=True)
        mon = self.make_hooked(system, fast_config(rejoin_delay=0.3))
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        wait_for(lambda: mon.state == State.REJOINING)
        system.running = True
        self.assertTrue(wait_for(lambda: len(self.notifier.sent) == 2))
        self.assertIn("sozinho", self.notifier.sent[1][1])
        self.assertEqual(system.urls, [])

    def test_exhausted_attempts_send_failure(self):
        system = FakeSystem(running=True)
        system.default_behaviour = "ignore"
        mon = self.make_hooked(system, fast_config(max_attempts=2))
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.state == State.ERROR, timeout=6))
        self.assertTrue(wait_for(lambda: len(self.notifier.sent) == 2))
        self.assertIn("fechou", self.notifier.sent[0][0])
        self.assertIn("Não foi possível reabrir", self.notifier.sent[1][0])
        self.assertIn("Limite de 2 tentativas", self.notifier.sent[1][1])

    def test_manual_stop_during_rejoin_sends_no_failure(self):
        system = FakeSystem(running=True)
        mon = self.make_hooked(system, fast_config(rejoin_delay=1.0))
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        wait_for(lambda: mon.state == State.REJOINING)
        mon.stop()
        self.assertEqual(self.notifier.titles().count("❌ Não foi possível reabrir o Roblox"), 0)

    def test_broken_notifier_does_not_break_monitor(self):
        class Broken:
            def send(self, *a, **k):
                raise RuntimeError("webhook avariado")

            def flush(self, timeout=5.0):
                return True

        system = FakeSystem(running=True)
        mon = m.Monitor(
            fast_config(), process_checker=system.checker, opener=system.opener, notifier=Broken()
        )
        self.monitors.append(mon)
        mon.start()
        wait_for(lambda: mon.state == State.DETECTED)
        system.running = False
        self.assertTrue(wait_for(lambda: mon.launch_count == 1 and mon.state == State.DETECTED, timeout=5))

    def test_monitor_builds_notifier_from_config_only_when_valid(self):
        system = FakeSystem(running=False)
        on = m.Monitor(fast_config(webhook_url=GOOD_URL), process_checker=system.checker)
        off = m.Monitor(fast_config(webhook_url=""), process_checker=system.checker)
        self.assertIsInstance(on._webhook, WebhookNotifier)
        self.assertIsNone(off._webhook)

    def test_invalid_webhook_blocks_start(self):
        system = FakeSystem(running=False)
        mon = self.make(system, fast_config(webhook_url="https://example.com/x"))
        self.assertFalse(mon.start())
        self.assertIn("Webhook", mon.last_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
