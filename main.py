"""
main.py - Roblox Auto Rejoin: ponto de entrada e interface gráfica (customtkinter).

Uso:  py main.py
      RobloxAutoRejoin.exe            (versão compilada)
      RobloxAutoRejoin.exe --selftest (auto-verificação sem abrir a janela)
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

try:
    import customtkinter as ctk
except ImportError as _exc:  # dependência em falta: mensagem clara em vez de falhar em silêncio
    try:
        import tkinter
        from tkinter import messagebox

        _root = tkinter.Tk()
        _root.withdraw()
        messagebox.showerror(
            "Roblox Auto Rejoin",
            "Falta a biblioteca 'customtkinter'.\n\nAbre o PowerShell nesta pasta e executa:\n"
            "py -m pip install -r requirements.txt",
        )
    except Exception:
        pass
    sys.exit(1)

import monitor as rm
import multi
import ramapi

STATE_COLORS = {
    rm.State.WAITING: "#d4a017",
    rm.State.DETECTED: "#2fa84f",
    rm.State.REJOINING: "#3b8ed0",
    rm.State.OFFLINE: "#e67e22",
    rm.State.ERROR: "#d9534f",
    rm.State.STOPPED: "#8a8a8a",
}

NOTE_TEXT = (
    "Nota: o Place ID abre a experiência num servidor escolhido pelo Roblox, que pode ser diferente "
    "daquele em que estavas. O programa só confirma que o processo do Roblox foi detetado; não consegue "
    "confirmar que entraste no servidor."
)

MAX_LOG_LINES = 1000


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.title("Roblox Auto Rejoin")
        try:  # a janela é alta (muitas opções): ajusta-se a ecrãs pequenos
            screen_h = int(self.winfo_screenheight())
        except Exception:
            screen_h = 1080
        height = max(600, min(980, screen_h - 80))
        self.geometry(f"780x{height}")
        self.minsize(700, min(760, height))

        self.events: "queue.Queue" = queue.Queue()
        self.monitor: Optional[rm.Monitor] = None
        self.last_error = ""
        self._after_id: Optional[str] = None
        self._closing = False

        self.data_dir = rm.get_data_dir()
        self.config_path = self.data_dir / rm.CONFIG_FILE
        self.log_path = self.data_dir / rm.LOG_FILE
        self.logger = rm.setup_logging(self.log_path, callback=lambda line: self.events.put(("log", line)))
        self.config, warnings = rm.load_config(self.config_path)

        self._build_ui()
        for warning in warnings:
            self.logger.info(warning)
        self.logger.info(f"Pasta de dados: {self.data_dir}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.after(100, self._poll_events)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))
        ctk.CTkLabel(header, text="Roblox Auto Rejoin", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Reabre a experiência escolhida se o Roblox fechar. Não abre nada ao arrancar (exceto no modo várias contas).",
            text_color=("gray35", "gray70"),
        ).pack(anchor="w")

        # ---- configuração
        cfg_frame = ctk.CTkFrame(self)
        cfg_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=8)
        cfg_frame.grid_columnconfigure(1, weight=1)

        self.place_var = ctk.StringVar(value=self.config.place_id)
        self.attempts_var = ctk.StringVar(value=str(self.config.max_attempts))
        self.poll_var = ctk.StringVar(value=f"{self.config.poll_interval:g}")
        self.delay_var = ctk.StringVar(value=f"{self.config.rejoin_delay:g}")
        self.retry_var = ctk.StringVar(value=f"{self.config.retry_interval:g}")
        self.stable_var = ctk.StringVar(value=f"{self.config.stable_session:g}")
        self.path_var = ctk.StringVar(value=self.config.roblox_path)
        self.webhook_var = ctk.StringVar(value=self.config.webhook_url)
        self.net_var = ctk.BooleanVar(value=self.config.net_rejoin)
        self.disc_var = ctk.BooleanVar(value=self.config.disconnect_rejoin)
        self.ram_var = ctk.BooleanVar(value=self.config.ram_enabled)
        self.ram_port_var = ctk.StringVar(value=str(self.config.ram_port))
        self.ram_pw_var = ctk.StringVar(value=self.config.ram_password)
        self.net_threshold_var = ctk.StringVar(value=f"{self.config.net_down_threshold:g}")

        ctk.CTkLabel(cfg_frame, text="Place ID").grid(row=0, column=0, sticky="w", padx=(14, 8), pady=(14, 4))
        self.place_entry = ctk.CTkEntry(cfg_frame, textvariable=self.place_var, placeholder_text="ex.: 123456789")
        self.place_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(14, 4))

        ctk.CTkLabel(cfg_frame, text="Máx. de tentativas").grid(row=1, column=0, sticky="w", padx=(14, 8), pady=4)
        self.attempts_entry = ctk.CTkEntry(cfg_frame, textvariable=self.attempts_var, width=90)
        self.attempts_entry.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=4)

        ctk.CTkLabel(cfg_frame, text="Pasta do Roblox").grid(row=2, column=0, sticky="w", padx=(14, 8), pady=4)
        self.path_entry = ctk.CTkEntry(
            cfg_frame, textvariable=self.path_var, placeholder_text="opcional: ...\\Roblox\\Versions\\version-xxxx"
        )
        self.path_entry.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=4)

        ctk.CTkLabel(cfg_frame, text="Webhook Discord").grid(row=3, column=0, sticky="w", padx=(14, 8), pady=4)
        webhook_row = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        webhook_row.grid(row=3, column=1, sticky="ew", padx=(0, 14), pady=4)
        webhook_row.grid_columnconfigure(0, weight=1)
        # O URL do webhook é um segredo: fica escondido no campo (e nunca aparece nos logs).
        self.webhook_entry = ctk.CTkEntry(
            webhook_row, textvariable=self.webhook_var, show="•",
            placeholder_text="opcional: https://discord.com/api/webhooks/...",
        )
        self.webhook_entry.grid(row=0, column=0, sticky="ew")
        self.webhook_test_btn = ctk.CTkButton(webhook_row, text="Testar", width=70, command=self._on_test_webhook)
        self.webhook_test_btn.grid(row=0, column=1, padx=(8, 0))

        net_row = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        net_row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14, pady=4)
        self.net_check = ctk.CTkCheckBox(
            net_row, text="Reentrar se a internet cair", variable=self.net_var, onvalue=True, offvalue=False
        )
        self.net_check.pack(side="left")
        ctk.CTkLabel(net_row, text="e estiver em baixo há mais de (s)", font=ctk.CTkFont(size=11),
                     text_color=("gray35", "gray70")).pack(side="left", padx=(14, 6))
        self.net_threshold_entry = ctk.CTkEntry(net_row, textvariable=self.net_threshold_var, width=70)
        self.net_threshold_entry.pack(side="left")

        disc_row = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        disc_row.grid(row=5, column=0, columnspan=2, sticky="ew", padx=14, pady=4)
        self.disc_check = ctk.CTkCheckBox(
            disc_row, text="Reentrar se o Roblox for desligado do servidor (ecrã Disconnected, erro 277)",
            variable=self.disc_var, onvalue=True, offvalue=False,
        )
        self.disc_check.pack(side="left")

        ram_row = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        ram_row.grid(row=6, column=0, columnspan=2, sticky="ew", padx=14, pady=(8, 2))
        self.ram_check = ctk.CTkCheckBox(
            ram_row, text="Modo várias contas (Roblox Account Manager)", variable=self.ram_var,
            onvalue=True, offvalue=False,
        )
        self.ram_check.pack(side="left")
        ctk.CTkLabel(ram_row, text="Porta", font=ctk.CTkFont(size=11), text_color=("gray35", "gray70")).pack(
            side="left", padx=(14, 4)
        )
        self.ram_port_entry = ctk.CTkEntry(ram_row, textvariable=self.ram_port_var, width=60)
        self.ram_port_entry.pack(side="left")
        ctk.CTkLabel(ram_row, text="Palavra-passe", font=ctk.CTkFont(size=11), text_color=("gray35", "gray70")).pack(
            side="left", padx=(10, 4)
        )
        # a palavra-passe da API do RAM é um segredo: escondida no campo e nunca nos logs
        self.ram_pw_entry = ctk.CTkEntry(ram_row, textvariable=self.ram_pw_var, width=110, show="•")
        self.ram_pw_entry.pack(side="left")
        self.ram_test_btn = ctk.CTkButton(ram_row, text="Testar RAM", width=90, command=self._on_test_ram)
        self.ram_test_btn.pack(side="left", padx=(10, 0))

        ctk.CTkLabel(
            cfg_frame, text="Contas, uma por linha:  nome no RAM, PlaceId (opcional), JobId (opcional)",
            font=ctk.CTkFont(size=11), text_color=("gray35", "gray70"),
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=14)
        self.ram_box = ctk.CTkTextbox(cfg_frame, height=70, wrap="none", font=ctk.CTkFont(family="Consolas", size=12))
        self.ram_box.grid(row=8, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 4))
        if self.config.ram_accounts:
            self.ram_box.insert("1.0", self.config.ram_accounts)

        adv = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        adv.grid(row=9, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 12))
        self.advanced_entries = []
        for col, (text, var) in enumerate(
            [
                ("Verificar a cada (s)", self.poll_var),
                ("Esperar antes de reabrir (s)", self.delay_var),
                ("Pausa entre tentativas (s)", self.retry_var),
                ("Sessão estável (s)", self.stable_var),
            ]
        ):
            adv.grid_columnconfigure(col, weight=1)
            ctk.CTkLabel(adv, text=text, font=ctk.CTkFont(size=11), text_color=("gray35", "gray70")).grid(
                row=0, column=col, sticky="w", padx=4
            )
            entry = ctk.CTkEntry(adv, textvariable=var, width=90)
            entry.grid(row=1, column=col, sticky="w", padx=4)
            self.advanced_entries.append(entry)

        # ---- botões
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        self.start_btn = ctk.CTkButton(buttons, text="Iniciar", width=130, command=self._on_start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ctk.CTkButton(
            buttons, text="Parar", width=130, fg_color="#b03a3a", hover_color="#8f2e2e",
            command=self._on_stop, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            buttons, text="Abrir pasta de dados", width=150, fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), command=self._open_data_dir,
        ).pack(side="right")

        # ---- estado
        status = ctk.CTkFrame(self)
        status.grid(row=3, column=0, sticky="ew", padx=20, pady=8)
        status.grid_columnconfigure(1, weight=1)
        self.dot = ctk.CTkLabel(status, text="●", font=ctk.CTkFont(size=22), text_color=STATE_COLORS[rm.State.STOPPED])
        self.dot.grid(row=0, column=0, padx=(14, 6), pady=10)
        self.state_label = ctk.CTkLabel(status, text=rm.State.STOPPED.value, font=ctk.CTkFont(size=16, weight="bold"))
        self.state_label.grid(row=0, column=1, sticky="w")
        self.attempts_label = ctk.CTkLabel(status, text=f"Tentativas: 0/{self.config.max_attempts}")
        self.attempts_label.grid(row=0, column=2, padx=14)
        self.error_label = ctk.CTkLabel(status, text="", text_color="#d9534f", wraplength=640, justify="left")
        self.error_label.grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 8))
        self.error_label.grid_remove()
        self.accounts_label = ctk.CTkLabel(status, text="", justify="left", anchor="w", font=ctk.CTkFont(size=12))
        self.accounts_label.grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 8))
        self.accounts_label.grid_remove()

        ctk.CTkLabel(
            self, text=NOTE_TEXT, wraplength=700, justify="left", font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
        ).grid(row=4, column=0, sticky="w", padx=22, pady=(0, 4))

        # ---- logs
        self.log_box = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=5, column=0, sticky="nsew", padx=20, pady=(4, 4))
        self.log_box.configure(state="disabled")

        log_buttons = ctk.CTkFrame(self, fg_color="transparent")
        log_buttons.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 14))
        ctk.CTkButton(log_buttons, text="Copiar último erro", width=150, command=self._copy_error).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(log_buttons, text="Copiar logs", width=110, command=self._copy_logs).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            log_buttons, text="Limpar vista", width=110, fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"), command=self._clear_logs,
        ).pack(side="left")

    # ------------------------------------------------------------- ações
    def _inputs(self):
        return [
            self.place_entry, self.attempts_entry, self.path_entry, self.webhook_entry,
            self.net_check, self.net_threshold_entry, self.disc_check,
            self.ram_check, self.ram_port_entry, self.ram_pw_entry, self.ram_box, *self.advanced_entries,
        ]

    def _read_form(self):
        """Lê os campos. Devolve (Config, erros)."""
        errors = []
        cfg = rm.Config(
            launch_method=self.config.launch_method,
            net_restore_delay=self.config.net_restore_delay,  # só editáveis no config.json
            net_wait_max=self.config.net_wait_max,
            disconnect_codes=self.config.disconnect_codes,
            roblox_log_dir=self.config.roblox_log_dir,
            ram_launch_gap=self.config.ram_launch_gap,
            ram_launch_timeout=self.config.ram_launch_timeout,
        )
        cfg.ram_enabled = bool(self.ram_var.get())
        cfg.ram_accounts = self.ram_box.get("1.0", "end").strip()
        cfg.ram_password = self.ram_pw_var.get().strip()
        cfg.net_rejoin = bool(self.net_var.get())
        cfg.disconnect_rejoin = bool(self.disc_var.get())
        cfg.place_id = self.place_var.get().strip()
        cfg.roblox_path = self.path_var.get().strip().strip('"')
        cfg.webhook_url = self.webhook_var.get().strip()

        def number(var, label, convert):
            text = var.get().strip().replace(",", ".")
            try:
                return convert(text)
            except ValueError:
                errors.append(f"{label}: valor inválido ('{text}').")
                return None

        values = {
            "max_attempts": number(self.attempts_var, rm.FIELD_LABELS["max_attempts"], int),
            "poll_interval": number(self.poll_var, rm.FIELD_LABELS["poll_interval"], float),
            "rejoin_delay": number(self.delay_var, rm.FIELD_LABELS["rejoin_delay"], float),
            "retry_interval": number(self.retry_var, rm.FIELD_LABELS["retry_interval"], float),
            "stable_session": number(self.stable_var, rm.FIELD_LABELS["stable_session"], float),
            "net_down_threshold": number(self.net_threshold_var, rm.FIELD_LABELS["net_down_threshold"], float),
            "ram_port": number(self.ram_port_var, rm.FIELD_LABELS["ram_port"], int),
        }
        for name, value in values.items():
            if value is not None:
                setattr(cfg, name, value)
        parsed_ok = {name for name, value in values.items() if value is not None}
        for error in cfg.validate():
            # evita repetir erros de campos que já falharam a leitura
            if not any(rm.FIELD_LABELS[n] in error for n in values if n not in parsed_ok):
                errors.append(error)
        return cfg, errors

    def _on_start(self) -> None:
        if self.monitor is not None and self.monitor.is_active:
            return
        cfg, errors = self._read_form()
        if errors:
            self._show_error("Corrige os dados antes de iniciar: " + " ".join(errors))
            self._apply_state(rm.State.ERROR)
            return
        self.config = cfg
        try:
            rm.save_config(cfg, self.config_path)
        except OSError as exc:
            self.logger.warning(f"Não foi possível guardar {self.config_path}: {exc}")
        self._hide_error()
        self.attempts_label.configure(text=f"Tentativas: 0/{cfg.max_attempts}")
        common = dict(
            logger=self.logger,
            on_state=lambda s: self.events.put(("state", s)),
            on_attempts=lambda used, maximum: self.events.put(("attempts", (used, maximum))),
        )
        if cfg.ram_enabled:
            self.accounts_label.configure(text="")
            self.monitor = multi.MultiManager(
                cfg, on_accounts=lambda lines: self.events.put(("accounts", lines)), **common
            )
        else:
            self.accounts_label.grid_remove()
            self.monitor = rm.Monitor(cfg, **common)
        self.monitor.start()
        self._refresh_buttons()

    def _on_stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self._drain_events()
        self._apply_state(rm.State.STOPPED)
        self._refresh_buttons()

    def _on_test_webhook(self) -> None:
        url = self.webhook_var.get().strip()
        ok, err = rm.validate_webhook_url(url)
        if not ok:
            self._show_error(err)
            return
        if not url:
            self._show_error("Webhook: cola primeiro o URL do webhook do Discord.")
            return
        self.webhook_test_btn.configure(state="disabled")
        self.logger.info(f"A enviar mensagem de teste para {rm.redact_webhook(url)} ...")

        def work() -> None:
            ok, message = rm.send_test_webhook(url)
            (self.logger.info if ok else self.logger.error)(f"Webhook: {message}")
            self.events.put(("webhook_test_done", None))

        threading.Thread(target=work, name="webhook-test", daemon=True).start()

    def _on_test_ram(self) -> None:
        text = self.ram_port_var.get().strip()
        if not text.isascii() or not text.isdigit() or not (1 <= int(text) <= 65535):
            self._show_error("Porta do RAM: tem de ser um número entre 1 e 65535.")
            return
        port = int(text)
        self.ram_test_btn.configure(state="disabled")
        self.logger.info(f"A testar se o RAM responde na porta {port} ...")

        def work() -> None:
            ok, message = ramapi.RamClient(port, "").ping()
            (self.logger.info if ok else self.logger.error)(f"RAM: {message}")
            self.events.put(("ram_test_done", None))

        threading.Thread(target=work, name="ram-test", daemon=True).start()

    def _open_data_dir(self) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(self.data_dir))  # type: ignore[attr-defined]
            else:
                self.logger.info(f"Pasta de dados: {self.data_dir}")
        except OSError as exc:
            self._show_error(f"Não foi possível abrir a pasta: {exc}")

    def _copy_to_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()

    def _copy_error(self) -> None:
        text = self.last_error or (self.monitor.last_error if self.monitor else "")
        if not text:
            self.logger.info("Não há nenhum erro para copiar.")
            return
        self._copy_to_clipboard(text)
        self.logger.info("Último erro copiado para a área de transferência.")

    def _copy_logs(self) -> None:
        self._copy_to_clipboard(self.log_box.get("1.0", "end").strip())
        self.logger.info("Logs copiados para a área de transferência.")

    def _clear_logs(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # -------------------------------------------------------- atualizações
    def _show_error(self, text: str) -> None:
        self.last_error = text
        self.error_label.configure(text=text)
        self.error_label.grid()
        self.logger.error(text)

    def _hide_error(self) -> None:
        self.error_label.configure(text="")
        self.error_label.grid_remove()

    def _apply_state(self, state: rm.State) -> None:
        self.state_label.configure(text=state.value)
        self.dot.configure(text_color=STATE_COLORS[state])
        if state == rm.State.ERROR and self.monitor is not None and self.monitor.last_error:
            self.last_error = self.monitor.last_error
            self.error_label.configure(text=self.monitor.last_error)
            self.error_label.grid()

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        total = int(self.log_box.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.log_box.delete("1.0", f"{total - MAX_LOG_LINES}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _refresh_buttons(self) -> None:
        active = self.monitor is not None and self.monitor.is_active
        self.start_btn.configure(state="disabled" if active else "normal")
        self.stop_btn.configure(state="normal" if active else "disabled")
        for entry in self._inputs():
            entry.configure(state="disabled" if active else "normal")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "state":
                    self._apply_state(payload)
                elif kind == "ram_test_done":
                    self.ram_test_btn.configure(state="normal")
                elif kind == "accounts":
                    self.accounts_label.configure(text="\n".join(payload))
                    if payload:
                        self.accounts_label.grid()
                    else:
                        self.accounts_label.grid_remove()
                elif kind == "webhook_test_done":
                    self.webhook_test_btn.configure(state="normal")
                elif kind == "attempts":
                    used, maximum = payload
                    self.attempts_label.configure(text=f"Tentativas: {used}/{maximum}")
        except queue.Empty:
            pass

    def _poll_events(self) -> None:
        if self._closing:
            return
        self._drain_events()
        self._refresh_buttons()
        self._after_id = self.after(100, self._poll_events)

    # -------------------------------------------------------------- saída
    def _on_close(self) -> None:
        self._closing = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        if self.monitor is not None:
            self.monitor.stop(timeout=3)
        cfg, errors = self._read_form()
        if not errors:
            try:
                rm.save_config(cfg, self.config_path)
            except OSError:
                pass
        self.destroy()

    def report_callback_exception(self, exc, val, tb) -> None:  # erros da interface vão para o log
        self.logger.error("Erro na interface: " + "".join(traceback.format_exception(exc, val, tb)).strip())


def run_selftest() -> int:
    """Verifica o essencial sem intervenção: usado para testar o .exe compilado."""
    results = rm.self_check()
    try:
        app = App()
        app.update_idletasks()
        app.update()
        app._on_close()
        results.append(("interface gráfica (customtkinter)", True, "janela criada e fechada"))
    except Exception as exc:
        results.append(("interface gráfica (customtkinter)", False, repr(exc)))

    lines = [f"[{'OK' if ok else 'FALHA'}] {name}: {detail}" for name, ok, detail in results]
    try:
        (rm.get_data_dir() / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0 if all(ok for _, ok, _ in results) else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
