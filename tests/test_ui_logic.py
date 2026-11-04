"""
Teste da LÓGICA da interface (main.py) com um 'customtkinter' falso.

Não abre nenhuma janela e NÃO valida o aspeto visual nem a compatibilidade
com a versão real do customtkinter: só confirma que o código de main.py corre,
valida os campos e liga/desliga o Monitor corretamente.
"""

import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.kw = dict(kwargs)
        self.text = ""

    def configure(self, **kwargs):
        self.kw.update(kwargs)

    def cget(self, key):
        return self.kw.get(key)

    # CTkTextbox
    def insert(self, index, text):
        self.text += text

    def delete(self, start, end):
        self.text = "" if start == "1.0" and end == "end" else self.text

    def get(self, start=None, end=None):
        return self.text

    def index(self, idx):
        return f"{self.text.count(chr(10)) + 1}.0"

    def __getattr__(self, name):  # grid, pack, grid_remove, see, ...
        return lambda *a, **k: None


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class FakeCTk(FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.clipboard = ""
        self.destroyed = False

    def after(self, ms, func=None, *args):
        return "after#1"

    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, text):
        self.clipboard += text

    def destroy(self):
        self.destroyed = True


def install_fake_ctk():
    fake = types.ModuleType("customtkinter")
    fake.CTk = FakeCTk
    for name in ["CTkFrame", "CTkLabel", "CTkEntry", "CTkButton", "CTkTextbox", "CTkFont", "CTkCheckBox"]:
        setattr(fake, name, FakeWidget)
    fake.StringVar = FakeVar
    fake.BooleanVar = FakeVar
    fake.set_appearance_mode = lambda *a, **k: None
    fake.set_default_color_theme = lambda *a, **k: None
    sys.modules["customtkinter"] = fake


class TestUiLogic(unittest.TestCase):
    def setUp(self):
        import os

        self._saved = sys.modules.get("customtkinter")
        install_fake_ctk()
        sys.modules.pop("main", None)
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["ROBLOX_AUTO_REJOIN_HOME"] = str(self.tmp)
        import main

        self.main = main
        self.app = main.App()

    def tearDown(self):
        import os

        import monitor as rm

        if self.app.monitor is not None:
            self.app.monitor.stop()
        logger = rm.logging.getLogger(rm.LOGGER_NAME)
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        os.environ.pop("ROBLOX_AUTO_REJOIN_HOME", None)
        sys.modules.pop("main", None)
        if self._saved is not None:
            sys.modules["customtkinter"] = self._saved
        else:
            sys.modules.pop("customtkinter", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_and_logs_created_automatically(self):
        self.assertTrue((self.tmp / "config.json").exists())
        self.assertTrue((self.tmp / "logs.txt").exists())

    def test_invalid_place_id_blocks_start_and_shows_error(self):
        self.app.place_var.set("abc")
        self.app._on_start()
        self.assertIsNone(self.app.monitor)
        self.assertIn("Place ID", self.app.last_error)
        self.assertIn("Corrige", self.app.error_label.kw["text"])

    def test_invalid_number_blocks_start(self):
        self.app.place_var.set("123456789")
        self.app.attempts_var.set("muitas")
        self.app._on_start()
        self.assertIsNone(self.app.monitor)
        self.assertIn("inválido", self.app.last_error)

    def test_start_and_stop_flow(self):
        self.app.place_var.set("123456789")
        self.app.attempts_var.set("4")
        self.app._on_start()
        self.assertIsNotNone(self.app.monitor)
        self.assertTrue(self.app.monitor.is_active)
        self.assertEqual(self.app.start_btn.kw["state"], "disabled")
        self.assertEqual(self.app.stop_btn.kw["state"], "normal")
        time.sleep(0.3)
        self.app._drain_events()
        self.assertIn("Monitorização iniciada", self.app.log_box.text)

        self.app._on_stop()
        self.assertFalse(self.app.monitor.is_active)
        self.assertEqual(self.app.state_label.kw["text"], "Parado")
        self.assertEqual(self.app.start_btn.kw["state"], "normal")
        self.assertEqual(self.app.stop_btn.kw["state"], "disabled")

        import json

        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["place_id"], "123456789")
        self.assertEqual(saved["max_attempts"], 4)

    def test_copy_error_and_logs(self):
        self.app.place_var.set("x")
        self.app._on_start()
        self.app._copy_error()
        self.assertIn("Place ID", self.app.clipboard)
        self.app._drain_events()
        self.app._copy_logs()
        self.assertTrue(self.app.clipboard)

    def test_invalid_webhook_blocks_start(self):
        self.app.place_var.set("123456789")
        self.app.webhook_var.set("https://example.com/x")
        self.app._on_start()
        self.assertIsNone(self.app.monitor)
        self.assertIn("Webhook", self.app.last_error)

    def test_webhook_saved_but_never_logged(self):
        import json

        url = "https://discord.com/api/webhooks/123456789012345678/SecretTok_en-1"
        self.app.place_var.set("123456789")
        self.app.webhook_var.set(url)
        self.app._on_start()
        self.assertIsNotNone(self.app.monitor)
        self.app._on_stop()
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["webhook_url"], url)
        self.app._drain_events()
        self.assertNotIn("SecretTok_en-1", self.app.log_box.text)
        self.assertNotIn("SecretTok_en-1", (self.tmp / "logs.txt").read_text(encoding="utf-8"))

    def test_test_webhook_button_validates_and_runs_in_background(self):
        import monitor as rm

        self.app.webhook_var.set("")
        self.app._on_test_webhook()
        self.assertIn("Webhook", self.app.last_error)

        calls = []
        original = rm.send_test_webhook
        rm.send_test_webhook = lambda url: calls.append(url) or (True, "Mensagem de teste enviada.")
        try:
            url = "https://discord.com/api/webhooks/123456789012345678/SecretTok_en-1"
            self.app.webhook_var.set(url)
            self.app._on_test_webhook()
            deadline = time.time() + 2
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            self.app._drain_events()
        finally:
            rm.send_test_webhook = original
        self.assertEqual(calls, [url])
        self.assertEqual(self.app.webhook_test_btn.kw["state"], "normal")
        self.assertIn("Mensagem de teste enviada", self.app.log_box.text)
        self.assertNotIn("SecretTok_en-1", self.app.log_box.text)

    def test_net_settings_saved_and_validated(self):
        import json

        self.app.place_var.set("123456789")
        self.app.net_var.set(False)
        self.app.net_threshold_var.set("45")
        self.app._on_start()
        self.assertIsNotNone(self.app.monitor)
        self.app._on_stop()
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertIs(saved["net_rejoin"], False)
        self.assertEqual(saved["net_down_threshold"], 45)
        self.assertEqual(saved["net_wait_max"], 3600)  # valores só do config.json são preservados

    def test_invalid_net_threshold_blocks_start(self):
        self.app.place_var.set("123456789")
        self.app.net_threshold_var.set("1")
        self.app._on_start()
        self.assertIsNone(self.app.monitor)
        self.assertIn("Internet em baixo há", self.app.last_error)

    def test_offline_state_has_a_color(self):
        import monitor as rm

        self.assertIn(rm.State.OFFLINE, self.main.STATE_COLORS)
        self.app._apply_state(rm.State.OFFLINE)
        self.assertEqual(self.app.state_label.kw["text"], "Sem internet")

    def test_disconnect_setting_saved_and_extra_fields_preserved(self):
        import json

        (self.tmp / "config.json").write_text(
            json.dumps({"disconnect_codes": "277,279", "roblox_log_dir": "D:\\logs"}), encoding="utf-8"
        )
        import monitor as rm

        self.app.config, _ = rm.load_config(self.tmp / "config.json")
        self.app.place_var.set("123456789")
        self.app.disc_var.set(False)
        self.app._on_start()
        self.assertIsNotNone(self.app.monitor)
        self.app._on_stop()
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertIs(saved["disconnect_rejoin"], False)
        self.assertEqual(saved["disconnect_codes"], "277,279")  # só editável no config.json: não se perde
        self.assertEqual(saved["roblox_log_dir"], "D:\\logs")

    def test_ram_mode_starts_multi_manager_and_saves_settings(self):
        import json

        import multi

        self.app.place_var.set("")
        self.app.ram_var.set(True)
        self.app.ram_box.text = "alpha,111\nbeta,222"
        self.app.ram_port_var.set("7963")
        self.app.ram_pw_var.set("s3cr3t-pass")
        self.app._on_start()
        self.assertIsInstance(self.app.monitor, multi.MultiManager)
        self.app._on_stop()
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertIs(saved["ram_enabled"], True)
        self.assertEqual(saved["ram_accounts"], "alpha,111\nbeta,222")
        self.assertEqual(saved["ram_password"], "s3cr3t-pass")
        self.assertEqual(saved["ram_launch_gap"], 8.0)  # só do config.json: preservado
        self.app._drain_events()
        self.assertNotIn("s3cr3t-pass", self.app.log_box.text)
        self.assertNotIn("s3cr3t-pass", (self.tmp / "logs.txt").read_text(encoding="utf-8"))

    def test_ram_mode_validates_accounts_port_and_password(self):
        self.app.ram_var.set(True)
        for accounts, port, pw, expected in [
            ("", "7963", "", "pelo menos uma conta"),
            ("al/pha,1", "7963", "", "nome de conta inválido"),
            ("alpha,1", "0", "", "Porta do RAM"),
            ("alpha,1", "7963", "abc", "Palavra-passe do RAM"),
        ]:
            self.app.ram_box.text = accounts
            self.app.ram_port_var.set(port)
            self.app.ram_pw_var.set(pw)
            self.app._on_start()
            self.assertIsNone(self.app.monitor, (accounts, port, pw))
            self.assertIn(expected, self.app.last_error)

    def test_normal_mode_ignores_ram_fields_and_requires_place_id(self):
        import monitor as rm

        self.app.ram_var.set(False)
        self.app.ram_box.text = "isto não é válido!!"
        self.app.place_var.set("")
        self.app._on_start()
        self.assertIsNone(self.app.monitor)
        self.assertIn("Place ID", self.app.last_error)
        self.app.place_var.set("123456789")
        self.app._on_start()
        self.assertIsInstance(self.app.monitor, rm.Monitor)
        self.app._on_stop()

    def test_ram_test_button_rejects_bad_port_and_pings_in_background(self):
        import ramapi

        self.app.ram_port_var.set("abc")
        self.app._on_test_ram()
        self.assertIn("Porta do RAM", self.app.last_error)

        calls = []
        original = ramapi.RamClient.ping
        ramapi.RamClient.ping = lambda self_: calls.append(self_._port) or (True, "O RAM respondeu.")
        try:
            self.app.ram_port_var.set("8000")
            self.app._on_test_ram()
            deadline = time.time() + 2
            while not calls and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            self.app._drain_events()
        finally:
            ramapi.RamClient.ping = original
        self.assertEqual(calls, [8000])
        self.assertEqual(self.app.ram_test_btn.kw["state"], "normal")
        self.assertIn("O RAM respondeu", self.app.log_box.text)

    def test_account_lines_event_updates_label(self):
        self.app.events.put(("accounts", ["alpha: Roblox detetado", "beta: A tentar reentrar (1/5)"]))
        self.app._drain_events()
        self.assertEqual(self.app.accounts_label.kw["text"], "alpha: Roblox detetado\nbeta: A tentar reentrar (1/5)")

    def test_close_stops_monitor(self):
        self.app.place_var.set("123456789")
        self.app._on_start()
        self.app._on_close()
        self.assertFalse(self.app.monitor.is_active)
        self.assertTrue(self.app.destroyed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
