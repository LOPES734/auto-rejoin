"""
Testes do módulo ramapi (lista de contas + cliente da API local do RAM).

O RAM é simulado: nenhum pedido sai do computador. Prova que o pedido tem o
formato do wrapper oficial do RAM (RAMAccount.lua) e que a palavra-passe nunca
aparece em erros; NÃO prova que um RAM real aceita o pedido.
"""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor as m  # noqa: E402
import ramapi  # noqa: E402
from ramapi import AccountSpec, RamClient, RamError, parse_accounts  # noqa: E402

SECRET = "s3cr3t-pass"


class FakeResponse:
    def __init__(self, body="", status=200):
        self._body, self.status = body.encode(), status

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RecordingOpener:
    def __init__(self, response=None, error=None):
        self.urls, self.response, self.error = [], response or FakeResponse("Success"), error

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.response


class TestParseAccounts(unittest.TestCase):
    def test_basic_lines_defaults_and_comments(self):
        text = "alpha, 111\n\n# comentario\n  beta  ,222 , 0123abcd-4567\ngamma\n"
        accounts, errors = parse_accounts(text, default_place="999")
        self.assertEqual(errors, [])
        self.assertEqual(
            accounts,
            [AccountSpec("alpha", 111), AccountSpec("beta", 222, "0123abcd-4567"), AccountSpec("gamma", 999)],
        )

    def test_vip_link_detected(self):
        accounts, errors = parse_accounts("alpha,1,https://www.roblox.com/games/1?privateServerLinkCode=1", "")
        self.assertEqual(errors, [])
        self.assertTrue(accounts[0].vip)
        self.assertFalse(AccountSpec("a", 1, "0123abcd-4567").vip)

    def test_errors_are_specific(self):
        cases = {
            "al/pha,1": "nome de conta inválido",
            "alpha": "falta um Place ID válido",
            "alpha,abc": "falta um Place ID válido",
            "alpha,0": "falta um Place ID válido",
            "alpha,1,xx": "JobId inválido",
            "alpha,1,0123abcd-4567,extra": "campos a mais",
            "alpha,1\nALPHA,2": "repetida",
        }
        for text, expected in cases.items():
            _, errors = parse_accounts(text, "")
            self.assertTrue(any(expected in e for e in errors), (text, errors))

    def test_line_numbers_in_errors(self):
        _, errors = parse_accounts("ok1,1\n\nbad/name,1", "")
        self.assertIn("Linha 3", errors[0])

    def test_max_accounts(self):
        text = "\n".join(f"acc{i},1" for i in range(ramapi.MAX_ACCOUNTS + 1))
        _, errors = parse_accounts(text, "")
        self.assertTrue(any("Máximo" in e for e in errors))

    def test_validate_settings(self):
        ok = lambda **k: ramapi.validate_ram_settings(  # noqa: E731
            k.get("accounts", "a1,1"), k.get("place", ""), k.get("port", 7963), k.get("pw", "")
        )
        self.assertEqual(ok(), [])
        self.assertTrue(any("pelo menos uma conta" in e for e in ok(accounts="")))
        self.assertTrue(any("Porta" in e for e in ok(port=0)))
        self.assertTrue(any("Porta" in e for e in ok(port=70000)))
        self.assertTrue(any("Palavra-passe" in e for e in ok(pw="abc")))
        self.assertEqual(ok(pw="abcdef"), [])


class TestRamClient(unittest.TestCase):
    def client(self, opener, password=""):
        return RamClient(7963, password, opener=opener)

    def test_launch_url_matches_official_wrapper(self):
        op = RecordingOpener()
        self.client(op).launch(AccountSpec("alpha", 123))
        self.assertEqual(op.urls, ["http://localhost:7963/LaunchAccount?Account=alpha&PlaceId=123"])

    def test_launch_with_job_vip_and_password(self):
        op = RecordingOpener()
        self.client(op, SECRET).launch(AccountSpec("al pha", 5, "0123abcd-4567"))
        url = op.urls[0]
        self.assertTrue(url.startswith("http://localhost:7963/LaunchAccount?Account=al%20pha&PlaceId=5&JobId=0123abcd-4567"))
        self.assertTrue(url.endswith(f"&Password={SECRET}"))
        self.assertNotIn("JoinVIP", url)
        op = RecordingOpener()
        self.client(op).launch(AccountSpec("a1", 5, "https://www.roblox.com/games/5?x=1&y=2"))
        self.assertIn("JobId=https%3A%2F%2Fwww.roblox.com%2Fgames%2F5%3Fx%3D1%26y%3D2", op.urls[0])
        self.assertIn("&JoinVIP=true", op.urls[0])

    def test_short_password_is_not_sent_like_the_official_wrapper(self):
        op = RecordingOpener()
        self.client(op, "abc").launch(AccountSpec("alpha", 1))
        self.assertNotIn("Password", op.urls[0])

    def test_only_launchaccount_is_allowed(self):
        c = self.client(RecordingOpener())
        for method in ("GetCookie", "ImportCookie", "GetAccounts", "SetField"):
            with self.assertRaises(RamError):
                c._url(method, [])

    def test_always_talks_to_localhost(self):
        op = RecordingOpener()
        self.client(op, SECRET).launch(AccountSpec("alpha", 1))
        self.assertTrue(op.urls[0].startswith("http://localhost:"))

    def test_errors_never_contain_url_or_password(self):
        cases = [
            RecordingOpener(error=urllib.error.URLError(ConnectionRefusedError("recusado"))),
            RecordingOpener(error=urllib.error.HTTPError("http://localhost:7963/x", 403, "Forbidden", {}, io.BytesIO(b"no"))),
            RecordingOpener(error=urllib.error.HTTPError("http://localhost:7963/x", 500, "Err", {}, io.BytesIO(b"boom"))),
            RecordingOpener(response=FakeResponse("Invalid Account")),
            RecordingOpener(error=OSError("rede avariada")),
        ]
        for op in cases:
            with self.assertRaises(RamError) as ctx:
                self.client(op, SECRET).launch(AccountSpec("alpha", 1))
            text = str(ctx.exception)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("http://", text)
            self.assertNotIn("Password", text)

    def test_error_messages_are_helpful(self):
        refused = RecordingOpener(error=urllib.error.URLError(ConnectionRefusedError("recusado")))
        with self.assertRaises(RamError) as ctx:
            self.client(refused).launch(AccountSpec("alpha", 1))
        self.assertIn("7963", str(ctx.exception))
        forbidden = RecordingOpener(error=urllib.error.HTTPError("u", 403, "F", {}, io.BytesIO(b"")))
        with self.assertRaises(RamError) as ctx:
            self.client(forbidden).launch(AccountSpec("alpha", 1))
        self.assertIn("palavra-passe", str(ctx.exception))
        invalid = RecordingOpener(response=FakeResponse("Invalid Account"))
        with self.assertRaises(RamError) as ctx:
            self.client(invalid).launch(AccountSpec("alpha", 1))
        self.assertIn("alpha", str(ctx.exception))

    def test_ping(self):
        ok, msg = self.client(RecordingOpener(response=FakeResponse("x"))).ping()
        self.assertTrue(ok)
        # um 404 também prova que o servidor está lá
        ok, _ = self.client(RecordingOpener(error=urllib.error.HTTPError("u", 404, "NF", {}, io.BytesIO(b"")))).ping()
        self.assertTrue(ok)
        ok, msg = self.client(RecordingOpener(error=urllib.error.URLError("recusado"))).ping()
        self.assertFalse(ok)
        # o ping nunca envia a palavra-passe
        op = RecordingOpener()
        self.client(op, SECRET).ping()
        self.assertNotIn("Password", op.urls[0])
        self.assertEqual(op.urls[0], "http://localhost:7963/")


class TestRealHttpAgainstLocalFakeRam(unittest.TestCase):
    """urllib a sério contra um servidor local que faz de RAM (nada sai do computador)."""

    def run_server(self, status, body):
        import http.server
        import socketserver
        import threading

        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"] = self.path
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.addCleanup(srv.server_close)
        return srv, seen

    def test_launch_reaches_server_with_expected_path_and_query(self):
        from urllib.parse import parse_qs, urlsplit

        srv, seen = self.run_server(200, "Success")
        reply = RamClient(srv.server_address[1], SECRET, timeout=3).launch(AccountSpec("al pha", 42, "0123abcd-4567"))
        self.assertEqual(reply, "Success")
        parts = urlsplit(seen["path"])
        self.assertEqual(parts.path, "/LaunchAccount")
        self.assertEqual(
            parse_qs(parts.query),
            {"Account": ["al pha"], "PlaceId": ["42"], "JobId": ["0123abcd-4567"], "Password": [SECRET]},
        )

    def test_real_403_and_invalid_account(self):
        srv, _ = self.run_server(403, "no")
        with self.assertRaises(RamError) as ctx:
            RamClient(srv.server_address[1], SECRET, timeout=3).launch(AccountSpec("alpha", 1))
        self.assertIn("palavra-passe", str(ctx.exception))
        srv, _ = self.run_server(200, "Invalid Account")
        with self.assertRaises(RamError) as ctx:
            RamClient(srv.server_address[1], "", timeout=3).launch(AccountSpec("alpha", 1))
        self.assertIn("alpha", str(ctx.exception))

    def test_real_connection_refused(self):
        with self.assertRaises(RamError) as ctx:
            RamClient(9, SECRET, timeout=2).launch(AccountSpec("alpha", 1))
        self.assertNotIn(SECRET, str(ctx.exception))
        ok, msg = RamClient(9, "", timeout=2).ping()
        self.assertFalse(ok)


class TestConfigRam(unittest.TestCase):
    def test_ram_mode_does_not_require_global_place_id(self):
        cfg = m.Config(ram_enabled=True, ram_accounts="alpha,111\nbeta,222")
        self.assertEqual(cfg.validate(), [])
        self.assertTrue(any("Place ID" in e for e in m.Config(place_id="").validate()))

    def test_ram_mode_validation_and_default_place(self):
        errors = m.Config(ram_enabled=True, ram_accounts="alpha").validate()
        self.assertTrue(any("falta um Place ID" in e for e in errors))
        self.assertEqual(m.Config(ram_enabled=True, ram_accounts="alpha", place_id="5").validate(), [])
        self.assertTrue(any("Porta" in e for e in m.Config(ram_enabled=True, ram_accounts="a1,1", ram_port=0).validate()))
        # com o modo desligado, os campos do RAM são ignorados
        self.assertEqual(m.Config(place_id="5", ram_accounts="isto não é válido!!").validate(), [])

    def test_roundtrip_and_bad_values(self):
        path = Path(tempfile.mkdtemp()) / "config.json"
        cfg = m.Config(place_id="5", ram_enabled=True, ram_port=8000, ram_password="abcdef",
                       ram_accounts="alpha,1\nbeta,2", ram_launch_gap=3.0)
        m.save_config(cfg, path)
        loaded, warns = m.load_config(path)
        self.assertEqual(warns, [])
        self.assertEqual((loaded.ram_enabled, loaded.ram_port, loaded.ram_password), (True, 8000, "abcdef"))
        self.assertEqual(loaded.ram_accounts, "alpha,1\nbeta,2")
        path.write_text(json.dumps({"ram_enabled": "sim", "ram_port": "abc"}), encoding="utf-8")
        loaded, warns = m.load_config(path)
        self.assertFalse(loaded.ram_enabled)
        self.assertEqual(loaded.ram_port, 7963)
        self.assertEqual(len(warns), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
