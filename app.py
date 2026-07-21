"""
Balfund - Kotak Neo Heikin Ashi Strategy
=======================================
CustomTkinter front-end for the first-candle HA reference,
stop-and-reverse paper strategy.

    python app.py
"""

import io
import sys
import time
import queue
import threading
from datetime import datetime, timedelta

import customtkinter as ctk

# ---------------------------------------------------------------------------
# Theme (Balfund light white/blue)
# ---------------------------------------------------------------------------
CLR_BG      = "#f7f8fc"
CLR_HEADER  = "#1a56db"
CLR_PANEL   = "#ffffff"
CLR_CARD    = "#f0f4ff"
CLR_TEXT    = "#1f2937"
CLR_MUTED   = "#6b7280"
CLR_GREEN   = "#059669"
CLR_RED     = "#dc2626"
CLR_AMBER   = "#d97706"
CLR_BORDER  = "#dbe3f5"

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

LOG_Q = queue.Queue()


class StdoutRedirect(io.TextIOBase):
    """Route every print() from the strategy modules into the GUI log."""
    def __init__(self, original):
        self.original = original

    def write(self, s):
        if s and s.strip():
            LOG_Q.put(s.rstrip("\n"))
        try:
            self.original.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Balfund  |  Kotak Neo  -  Heikin Ashi Strategy")
        self.geometry("1280x820")
        self.configure(fg_color=CLR_BG)

        self.client = None
        self.runner = None
        self.stop_evt = threading.Event()
        self.running = False
        self.started_at = None
        self._rows = {}

        self._build_header()
        self._build_controls()
        self._build_tabs()

        self.after(300, self._pump)

    # ------------------------------------------------------------------
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color=CLR_HEADER, height=74, corner_radius=0)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", padx=22, pady=12)
        ctk.CTkLabel(left, text="BALFUND", font=("Helvetica", 22, "bold"),
                     text_color="#ffffff").pack(anchor="w")
        ctk.CTkLabel(left, text="Kotak Neo  ·  Heikin Ashi  ·  1 Minute",
                     font=("Helvetica", 12), text_color="#c7d7f7").pack(anchor="w")

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=22)
        self.lbl_mode = ctk.CTkLabel(right, text="PAPER TRADING",
                                     font=("Helvetica", 13, "bold"),
                                     text_color="#ffffff", fg_color="#0f3fa8",
                                     corner_radius=6, width=140, height=30)
        self.lbl_mode.pack(pady=(6, 2))
        self.lbl_clock = ctk.CTkLabel(right, text="--:--:--",
                                      font=("Helvetica", 12),
                                      text_color="#c7d7f7")
        self.lbl_clock.pack()

    def _build_controls(self):
        f = ctk.CTkFrame(self, fg_color=CLR_PANEL, corner_radius=10,
                         border_width=1, border_color=CLR_BORDER)
        f.pack(fill="x", padx=16, pady=(14, 8))

        self.btn_start = ctk.CTkButton(f, text="START", width=130, height=38,
                                       font=("Helvetica", 14, "bold"),
                                       fg_color=CLR_HEADER, hover_color="#1541a8",
                                       command=self.start)
        self.btn_start.pack(side="left", padx=(16, 8), pady=14)

        self.btn_stop = ctk.CTkButton(f, text="STOP", width=110, height=38,
                                      font=("Helvetica", 14, "bold"),
                                      fg_color="#9ca3af", hover_color="#6b7280",
                                      state="disabled", command=self.stop)
        self.btn_stop.pack(side="left", padx=4, pady=14)

        self.stats = {}
        for key, label in [("conn", "Connection"), ("legs", "Legs"),
                           ("ticks", "Ticks"), ("uptime", "Uptime")]:
            box = ctk.CTkFrame(f, fg_color=CLR_CARD, corner_radius=8,
                               width=150, height=52)
            box.pack(side="left", padx=6, pady=12)
            box.pack_propagate(False)
            ctk.CTkLabel(box, text=label.upper(), font=("Helvetica", 9, "bold"),
                         text_color=CLR_MUTED).pack(pady=(7, 0))
            v = ctk.CTkLabel(box, text="-", font=("Helvetica", 14, "bold"),
                             text_color=CLR_TEXT)
            v.pack()
            self.stats[key] = v

        pnl = ctk.CTkFrame(f, fg_color=CLR_CARD, corner_radius=8, height=52)
        pnl.pack(side="right", padx=16, pady=12)
        ctk.CTkLabel(pnl, text="NET P&L", font=("Helvetica", 9, "bold"),
                     text_color=CLR_MUTED).pack(padx=22, pady=(7, 0))
        self.lbl_pnl = ctk.CTkLabel(pnl, text="0.00",
                                    font=("Helvetica", 19, "bold"),
                                    text_color=CLR_TEXT)
        self.lbl_pnl.pack(padx=22)

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, fg_color=CLR_PANEL,
                                   segmented_button_selected_color=CLR_HEADER,
                                   corner_radius=10)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(6, 16))
        for t in ("Credentials", "Positions", "Log", "Settings"):
            self.tabs.add(t)

        self._build_credentials(self.tabs.tab("Credentials"))
        self._build_positions(self.tabs.tab("Positions"))
        self._build_log(self.tabs.tab("Log"))
        self._build_settings(self.tabs.tab("Settings"))

    # ------------------------------------------------------------------
    def _build_credentials(self, parent):
        from config_loader import CONFIG, FIELDS, config_path, is_complete

        box = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=10)
        box.pack(fill="x", padx=16, pady=16)

        ctk.CTkLabel(box, text="Kotak Neo credentials",
                     font=("Helvetica", 16, "bold"),
                     text_color=CLR_TEXT).grid(row=0, column=0, columnspan=3,
                                               sticky="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(box, text="Saved to config.json beside the application. "
                              "Nothing is stored inside the EXE.",
                     font=("Helvetica", 11),
                     text_color=CLR_MUTED).grid(row=1, column=0, columnspan=3,
                                                sticky="w", padx=18, pady=(0, 12))

        self.cred_entries = {}
        self.cred_show = {}
        for i, (key, label, secret) in enumerate(FIELDS):
            ctk.CTkLabel(box, text=label, font=("Helvetica", 12),
                         text_color=CLR_TEXT, width=230, anchor="w").grid(
                row=2 + i, column=0, sticky="w", padx=(18, 8), pady=7)

            ent = ctk.CTkEntry(box, width=340, height=34,
                               show="*" if secret else "")
            ent.insert(0, str(CONFIG.get(key, "")))
            ent.grid(row=2 + i, column=1, sticky="w", pady=7)
            self.cred_entries[key] = ent

            if secret:
                self.cred_show[key] = False

                def _toggle(k=key):
                    self.cred_show[k] = not self.cred_show[k]
                    self.cred_entries[k].configure(
                        show="" if self.cred_show[k] else "*")

                ctk.CTkButton(box, text="show", width=58, height=30,
                              fg_color="#9ca3af", hover_color="#6b7280",
                              command=_toggle).grid(row=2 + i, column=2,
                                                    padx=8, pady=7)

        n = len(FIELDS) + 2
        btns = ctk.CTkFrame(box, fg_color="transparent")
        btns.grid(row=n, column=0, columnspan=3, sticky="w", padx=18, pady=(14, 18))

        ctk.CTkButton(btns, text="SAVE CREDENTIALS", width=190, height=38,
                      font=("Helvetica", 13, "bold"),
                      fg_color=CLR_HEADER, hover_color="#1541a8",
                      command=self.save_credentials).pack(side="left")

        ctk.CTkButton(btns, text="TEST TOTP", width=130, height=38,
                      font=("Helvetica", 13),
                      fg_color="#6b7280", hover_color="#4b5563",
                      command=self.test_totp).pack(side="left", padx=10)

        self.lbl_cred = ctk.CTkLabel(btns, text="", font=("Helvetica", 12))
        self.lbl_cred.pack(side="left", padx=14)

        ctk.CTkLabel(parent, text=f"config.json location:\n{config_path()}",
                     font=("Menlo", 10), text_color=CLR_MUTED,
                     justify="left").pack(anchor="w", padx=24)

        if not is_complete(CONFIG):
            self.after(400, lambda: self.tabs.set("Credentials"))

    def save_credentials(self):
        from config_loader import save_config, is_complete, missing_fields
        vals = {k: e.get().strip() for k, e in self.cred_entries.items()}

        mpin = vals.get("mpin", "")
        if mpin and not (mpin.isdigit() and len(mpin) == 6):
            self.lbl_cred.configure(text="MPIN must be exactly 6 digits",
                                    text_color=CLR_RED)
            return

        path = save_config(vals)
        if is_complete():
            self.lbl_cred.configure(text="Saved. Ready to start.",
                                    text_color=CLR_GREEN)
        else:
            self.lbl_cred.configure(
                text="Saved, still missing: " + ", ".join(missing_fields()),
                text_color=CLR_AMBER)
        self.log(f"[CRED] saved to {path}")

    def test_totp(self):
        seed = self.cred_entries["totp_secret"].get().strip()
        if not seed:
            self.lbl_cred.configure(text="Enter the TOTP secret first",
                                    text_color=CLR_AMBER)
            return
        try:
            import pyotp
            code = pyotp.TOTP(seed).now()
            self.lbl_cred.configure(
                text=f"TOTP now: {code}  (must match your authenticator)",
                text_color=CLR_GREEN)
        except Exception as e:
            self.lbl_cred.configure(text=f"Invalid TOTP secret: {e}",
                                    text_color=CLR_RED)

    COLS = [("instrument", 250), ("side", 60), ("reference", 105), ("ltp", 105),
            ("state", 85), ("entry", 105), ("qty", 70),
            ("realized", 120), ("unrealized", 120), ("trades", 75)]

    def _build_positions(self, parent):
        head = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=8, height=38)
        head.pack(fill="x", padx=10, pady=(10, 2))
        head.pack_propagate(False)
        for name, w in self.COLS:
            ctk.CTkLabel(head, text=name.upper(), width=w,
                         font=("Helvetica", 10, "bold"),
                         text_color=CLR_MUTED,
                         anchor="w" if name == "instrument" else "e").pack(
                side="left", padx=4)

        self.rows_frame = ctk.CTkScrollableFrame(parent, fg_color=CLR_PANEL)
        self.rows_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.lbl_empty = ctk.CTkLabel(
            self.rows_frame,
            text="\n\nNot started.\n\n"
                 "Press START before 09:00 so the MCX session is captured.\n"
                 "Strikes lock automatically at 09:00 (MCX) and 09:15 (NSE/BSE).",
            font=("Helvetica", 13), text_color=CLR_MUTED, justify="center")
        self.lbl_empty.pack(pady=40)

    def _build_log(self, parent):
        self.txt = ctk.CTkTextbox(parent, fg_color="#0f172a",
                                  text_color="#e2e8f0",
                                  font=("Menlo", 11), corner_radius=8)
        self.txt.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt.insert("end", "Balfund Kotak Neo strategy - log\n")
        self.txt.configure(state="disabled")

    def _build_settings(self, parent):
        import strategy_config as sc

        box = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=10)
        box.pack(fill="x", padx=14, pady=14)

        ctk.CTkLabel(box, text="Strategy parameters",
                     font=("Helvetica", 15, "bold"),
                     text_color=CLR_TEXT).grid(row=0, column=0, columnspan=2,
                                               sticky="w", padx=16, pady=(14, 8))

        ctk.CTkLabel(box, text="Strike band (each side of ATM)",
                     font=("Helvetica", 12),
                     text_color=CLR_TEXT).grid(row=1, column=0, sticky="w",
                                               padx=16, pady=6)
        self.ent_band = ctk.CTkEntry(box, width=90)
        self.ent_band.insert(0, str(sc.BAND))
        self.ent_band.grid(row=1, column=1, sticky="w", pady=6)

        ctk.CTkLabel(box, text="Fill price", font=("Helvetica", 12),
                     text_color=CLR_TEXT).grid(row=2, column=0, sticky="w",
                                               padx=16, pady=6)
        self.opt_fill = ctk.CTkOptionMenu(box, values=["close", "ha_close"],
                                          width=140, fg_color=CLR_HEADER)
        self.opt_fill.set(sc.FILL_PRICE)
        self.opt_fill.grid(row=2, column=1, sticky="w", pady=6)

        rules = (
            "Rules\n"
            "  • Reference = Heikin Ashi CLOSE of the first 1-minute candle\n"
            "      09:15  NIFTY (nse_fo) and SENSEX (bse_fo)\n"
            "      09:00  CRUDEOILM (mcx_fo)\n"
            "  • ATM strike locked once that first candle completes, fixed for the day\n"
            "  • Every completed candle after that:\n"
            "        HA close ABOVE reference  ->  BUY that strike\n"
            "        HA close BELOW reference  ->  SELL that strike\n"
            "        HA close EQUAL reference  ->  no action\n"
            "  • Stop and reverse: 1 lot on first entry, 2 lots on every reversal\n"
            "  • CE and PE trade independently, no cap on trades\n"
            "  • Signals use the Heikin Ashi close, fills use the raw candle close"
        )
        ctk.CTkLabel(parent, text=rules, font=("Menlo", 11),
                     text_color=CLR_TEXT, justify="left").pack(
            anchor="w", padx=22, pady=6)

    # ------------------------------------------------------------------
    def log(self, msg):
        self.txt.configure(state="normal")
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def start(self):
        if self.running:
            return

        from config_loader import is_complete, missing_fields
        if not is_complete():
            self.tabs.set("Credentials")
            self.lbl_cred.configure(
                text="Missing: " + ", ".join(missing_fields()),
                text_color=CLR_RED)
            return

        self.running = True
        self.stop_evt.clear()
        self.started_at = time.time()
        self.btn_start.configure(state="disabled", fg_color="#9ca3af")
        self.btn_stop.configure(state="normal", fg_color=CLR_RED,
                                hover_color="#b91c1c")
        self.stats["conn"].configure(text="starting", text_color=CLR_AMBER)
        self.tabs.set("Log")

        import strategy_config as sc
        try:
            sc.BAND = int(self.ent_band.get())
        except ValueError:
            pass
        sc.FILL_PRICE = self.opt_fill.get()

        self.runner = threading.Thread(target=self._run, daemon=True)
        self.runner.start()

    def stop(self):
        self.stop_evt.set()
        self.running = False
        self.btn_stop.configure(state="disabled", fg_color="#9ca3af")
        self.btn_start.configure(state="normal", fg_color=CLR_HEADER)
        self.stats["conn"].configure(text="stopped", text_color=CLR_MUTED)
        self.log("[GUI ] stop requested")

    # ------------------------------------------------------------------
    def _run(self):
        """Background worker - drives the existing strategy modules."""
        sys.stdout = StdoutRedirect(sys.__stdout__)
        try:
            import paper_strategy as ps
            from kotak_ws_base import login, _ALIAS, _lock
            from option_chain import (UNDERLYINGS, load_scrip_master,
                                      near_month_future, MAX_TOTAL_SCRIPS)
            from candle_engine import IST
            from strategy_config import SESSION_OPEN, LOCK_DELAY_SEC, \
                UNSUBSCRIBE_BAND_AFTER_LOCK

            print("[GUI ] logging in...")
            client = login()
            self.client = client
            client.on_message = ps.on_message
            ps.POOL.on_candle_close(ps.on_candle)
            print("[GUI ] authenticated")

            masters = {}
            for seg in sorted({u["fo_segment"] for u in UNDERLYINGS.values()}):
                masters[seg] = load_scrip_master(client, seg)

            idx_subs, fut_subs, spot_names = [], [], {}
            for name, cfg in UNDERLYINGS.items():
                if cfg["spot_type"] == "index":
                    idx_subs.append({"instrument_token": cfg["spot_name"],
                                     "exchange_segment": cfg["spot_segment"]})
                    spot_names[name] = cfg["spot_name"]
                else:
                    fut = near_month_future(masters[cfg["fo_segment"]],
                                            cfg["fo_segment"], name)
                    if not fut:
                        continue
                    with _lock:
                        _ALIAS[fut["instrument_token"]] = fut["trd"]
                    fut_subs.append({"instrument_token": fut["instrument_token"],
                                     "exchange_segment": fut["exchange_segment"]})
                    spot_names[name] = fut["trd"]
                    print(f"[FUT ] {name}: {fut['trd']} exp {fut['expiry']}")

            if idx_subs:
                client.subscribe(instrument_tokens=idx_subs, isIndex=True)
            if fut_subs:
                client.subscribe(instrument_tokens=fut_subs)

            print("[PREV] waiting for previous close...")
            deadline = time.time() + 30
            while time.time() < deadline and not self.stop_evt.is_set():
                if all(s in ps.PREV_CLOSE or s in ps.POOL.engines
                       for s in spot_names.values()):
                    break
                time.sleep(0.5)

            bands, band_subs = {}, []
            for name, cfg in UNDERLYINGS.items():
                sname = spot_names.get(name)
                est = ps.PREV_CLOSE.get(sname)
                if est is None:
                    est = (ps.POOL.snapshot().get(sname) or {}).get("ltp")
                if est is None:
                    print(f"[WARN] {name}: no reference price, skipped")
                    continue
                rows, _ = ps.build_band(masters, name, cfg, est)
                bands[name] = rows
                band_subs += [{"instrument_token": r["token"],
                               "exchange_segment": r["segment"]} for r in rows]

            total = len(idx_subs) + len(fut_subs) + len(band_subs)
            if total > MAX_TOTAL_SCRIPS:
                print(f"[ERR ] {total} subscriptions exceeds {MAX_TOTAL_SCRIPS}")
                return
            print(f"[SUB ] {total}/{MAX_TOTAL_SCRIPS} subscriptions")
            for i in range(0, len(band_subs), 100):
                client.subscribe(instrument_tokens=band_subs[i:i + 100])
                time.sleep(1)

            ps.POOL.start_roller(interval=1.0)

            today = datetime.now(IST).date()
            pending = {}
            for name, cfg in UNDERLYINGS.items():
                if name not in bands:
                    continue
                ref_b = datetime.combine(today, SESSION_OPEN[cfg["fo_segment"]],
                                         tzinfo=IST)
                pending[name] = {
                    "cfg": cfg, "ref_bucket": ref_b,
                    "lock_at": ref_b + timedelta(minutes=1,
                                                 seconds=LOCK_DELAY_SEC)}
                print(f"[PLAN] {name}: reference {ref_b:%H:%M}, "
                      f"lock {pending[name]['lock_at']:%H:%M:%S}")

            print("[RUN ] live")
            while not self.stop_evt.is_set():
                now = datetime.now(IST)
                for name in list(pending):
                    if now >= pending[name]["lock_at"]:
                        info = pending.pop(name)
                        kept, dropped = ps.lock_underlying(
                            name, info["cfg"], bands[name],
                            spot_names[name], info["ref_bucket"])
                        if dropped and UNSUBSCRIBE_BAND_AFTER_LOCK:
                            for i in range(0, len(dropped), 100):
                                try:
                                    client.un_subscribe(
                                        instrument_tokens=dropped[i:i + 100])
                                except Exception as e:
                                    print(f"[LOCK] unsubscribe: {e}")
                            print(f"       trading {len(kept)} legs, "
                                  f"dropped {len(dropped)}")
                time.sleep(1)

        except Exception as e:
            import traceback
            print(f"[ERR ] {e}")
            print(traceback.format_exc())
        finally:
            try:
                import paper_strategy as ps
                ps.POOL.stop()
            except Exception:
                pass
            sys.stdout = sys.__stdout__

    # ------------------------------------------------------------------
    def _pump(self):
        self.lbl_clock.configure(
            text=datetime.now().strftime("%d-%b-%Y  %H:%M:%S"))

        drained = 0
        while drained < 200:
            try:
                self.log(LOG_Q.get_nowait())
                drained += 1
            except queue.Empty:
                break

        try:
            import paper_strategy as ps
            legs = list(ps.LEGS.values())
            ticks = sum(e.snapshot()["closed"] for e in ps.POOL.engines.values())
            self.stats["legs"].configure(text=str(len(legs)))
            self.stats["ticks"].configure(text=f"{len(ps.POOL.engines)} inst")
            if self.running:
                self.stats["conn"].configure(
                    text="live" if ps.POOL.engines else "connecting",
                    text_color=CLR_GREEN if ps.POOL.engines else CLR_AMBER)
            self._refresh_rows(legs)
        except Exception:
            pass

        if self.started_at and self.running:
            s = int(time.time() - self.started_at)
            self.stats["uptime"].configure(
                text=f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}")

        self.after(500, self._pump)

    def _refresh_rows(self, legs):
        if not legs:
            return
        if self.lbl_empty.winfo_exists():
            self.lbl_empty.destroy()

        tot_r = tot_u = 0.0
        for lg in sorted(legs, key=lambda x: (x.underlying, x.side)):
            u = lg.unrealized()
            tot_r += lg.realized
            tot_u += u

            if lg.symbol not in self._rows:
                row = ctk.CTkFrame(self.rows_frame, fg_color=CLR_CARD,
                                   corner_radius=8, height=44)
                row.pack(fill="x", pady=3)
                row.pack_propagate(False)
                cells = {}
                for name, w in self.COLS:
                    lab = ctk.CTkLabel(row, text="-", width=w,
                                       font=("Helvetica", 12),
                                       text_color=CLR_TEXT,
                                       anchor="w" if name == "instrument" else "e")
                    lab.pack(side="left", padx=4)
                    cells[name] = lab
                self._rows[lg.symbol] = cells

            c = self._rows[lg.symbol]
            colour = (CLR_GREEN if lg.state == "LONG"
                      else CLR_RED if lg.state == "SHORT" else CLR_MUTED)
            c["instrument"].configure(text=lg.symbol)
            c["side"].configure(text=lg.side)
            c["reference"].configure(
                text=f"{lg.reference:,.2f}" if lg.reference else "-")
            c["ltp"].configure(
                text=f"{lg.last_price:,.2f}" if lg.last_price else "-")
            c["state"].configure(text=lg.state, text_color=colour)
            c["entry"].configure(
                text=f"{lg.entry_price:,.2f}" if lg.entry_price else "-")
            c["qty"].configure(text=str(lg.lot) if lg.state != "FLAT" else "0")
            c["realized"].configure(
                text=f"{lg.realized:+,.2f}",
                text_color=CLR_GREEN if lg.realized >= 0 else CLR_RED)
            c["unrealized"].configure(
                text=f"{u:+,.2f}",
                text_color=CLR_GREEN if u >= 0 else CLR_RED)
            c["trades"].configure(text=str(len(lg.trades)))

        net = tot_r + tot_u
        self.lbl_pnl.configure(
            text=f"{net:+,.2f}",
            text_color=CLR_GREEN if net >= 0 else CLR_RED)


if __name__ == "__main__":
    App().mainloop()
