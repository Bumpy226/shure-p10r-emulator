"""Tk front panel for the emulated receiver."""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog

from .board import LCD_W, LCD_H

SCALE = 6
KEYMAP = {"Up": "up", "Down": "down", "Return": "enter", "KP_Enter": "enter",
          "Escape": "exit", "BackSpace": "exit", "s": "scan", "S": "scan"}
HELP = ("Keys (click the display first):  Up / Down   Enter = enter   "
        "Esc = exit   S = scan      hold a key to hold the button")


class EmuThread(threading.Thread):
    """Runs the machine at wall-clock speed.  Anything that touches the
    machine from the UI goes through `call()` so it happens between slices."""
    SLICE_MS = 4

    def __init__(self, machine):
        super().__init__(daemon=True)
        self.m = machine
        self.q = queue.Queue()
        self.console = queue.Queue()
        self.running = True
        self.paused = False
        self.speed = 0.0            # measured emulated/real ratio
        self.fault_reported = False

    def call(self, fn, *args):
        self.q.put((fn, args))

    def run(self):
        m = self.m
        start = time.perf_counter()
        emu_ms = 0.0
        mark_t, mark_ms = start, 0.0
        while self.running:
            try:
                while True:
                    fn, args = self.q.get_nowait()
                    fn(*args)
            except queue.Empty:
                pass
            if self.paused or m.fault:
                if m.fault and not self.fault_reported:
                    self.fault_reported = True
                    self.console.put("\n*** emulation stopped: %s ***\n" % m.fault)
                time.sleep(0.02)
                start = time.perf_counter() - emu_ms / 1000.0
                continue
            self.fault_reported = False
            m.run_ms(self.SLICE_MS)
            emu_ms += self.SLICE_MS
            if m.console_out:
                self.console.put(m.take_console())
            now = time.perf_counter()
            ahead = emu_ms / 1000.0 - (now - start)
            if ahead > 0:
                time.sleep(ahead)
            elif ahead < -0.25:
                start = now - emu_ms / 1000.0      # too slow: don't try to catch up
            if now - mark_t > 1.0:
                self.speed = (emu_ms - mark_ms) / 1000.0 / (now - mark_t)
                mark_t, mark_ms = now, emu_ms


class App:
    def __init__(self, machine, title=None):
        self.m = machine
        self.emu = EmuThread(machine)
        self.root = root = tk.Tk()
        root.title(title or "%s emulator" % machine.name)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.held = set()

        left = ttk.Frame(root, padding=8)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.Frame(root, padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # --- display + LEDs ------------------------------------------
        self.img = tk.PhotoImage(width=LCD_W, height=LCD_H)
        self.zoomed = self.img.zoom(SCALE)
        self.lcd = tk.Label(left, image=self.zoomed, bd=6, relief="sunken",
                            bg="#202020", takefocus=1, highlightthickness=2)
        self.lcd.grid(row=0, column=0, columnspan=5)
        self.lcd.bind("<Button-1>", lambda e: self.lcd.focus_set())
        ledrow = ttk.Frame(left)
        ledrow.grid(row=1, column=0, columnspan=5, pady=(6, 2))
        self.leds = tk.Canvas(ledrow, width=250, height=22, highlightthickness=0)
        self.leds.pack()
        self.led_items = []
        for i, name in enumerate(("red", "green", "RF")):
            x = 14 + i * 84
            self.led_items.append(self.leds.create_oval(x, 4, x + 14, 18, fill="#222"))
            self.leds.create_text(x + 20, 11, text=name, anchor="w")
        # --- buttons ----------------------------------------------------
        for col, (name, label) in enumerate((("exit", "exit"), ("up", "▲"),
                                             ("down", "▼"), ("enter", "enter"),
                                             ("scan", "scan"))):
            b = tk.Button(left, text=label, width=7, takefocus=0)
            b.grid(row=2, column=col, padx=2, pady=4)
            b.bind("<ButtonPress-1>", lambda e, n=name: self.press(n, True))
            b.bind("<ButtonRelease-1>", lambda e, n=name: self.press(n, False))
        ttk.Label(left, text=HELP, wraplength=LCD_W * SCALE, foreground="#555").grid(
            row=3, column=0, columnspan=5, sticky="w")
        self.status = tk.StringVar()
        ttk.Label(left, textvariable=self.status, font=("Consolas", 9),
                  justify="left").grid(row=4, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # --- unit controls ----------------------------------------------
        unit = ttk.LabelFrame(left, text="Unit", padding=6)
        unit.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.batt = tk.DoubleVar(value=machine.board.battery_mv / 1000.0)
        self.vol = tk.IntVar(value=machine.get_volume())
        self.phones = tk.BooleanVar(value=machine.get_phones())
        ttk.Label(unit, text="Battery (V)").grid(row=0, column=0, sticky="w")
        tk.Scale(unit, variable=self.batt, from_=1.6, to=3.4, resolution=0.05,
                 orient="horizontal", length=200, command=self._set_batt).grid(row=0, column=1)
        ttk.Label(unit, text="Volume knob").grid(row=1, column=0, sticky="w")
        tk.Scale(unit, variable=self.vol, from_=0, to=255, orient="horizontal",
                 length=200, command=self._set_vol).grid(row=1, column=1)
        if machine.has_phones:
            ttk.Checkbutton(unit, text="Earphones plugged in", variable=self.phones,
                            command=self._set_phones).grid(row=2, column=0, sticky="w")
        if hasattr(machine, "set_power"):
            self.power = tk.BooleanVar(value=machine.board.power)
            ttk.Checkbutton(unit, text="Power switch on", variable=self.power,
                            command=lambda: self.emu.call(self.m.set_power, self.power.get())
                            ).grid(row=2, column=1, sticky="w")
        row = ttk.Frame(unit)
        row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(row, text="Reset", command=lambda: self.emu.call(self.m.reset)).pack(side="left")
        self.pause_btn = ttk.Button(row, text="Pause", command=self._pause)
        self.pause_btn.pack(side="left", padx=4)
        ttk.Button(row, text="Screenshot...", command=self._shot).pack(side="left")

        # --- mock RF ----------------------------------------------------
        rf = ttk.LabelFrame(right, text="Mock RF environment", padding=6)
        rf.grid(row=0, column=0, sticky="ew")
        self.txlist = tk.Listbox(rf, height=6, width=58, font=("Consolas", 9),
                                 exportselection=False)
        self.txlist.grid(row=0, column=0, columnspan=10, sticky="ew")
        self.txlist.bind("<<ListboxSelect>>", self._tx_select)
        self.f_freq = tk.StringVar(value="470.125")
        self.f_lvl = tk.StringVar(value="-45")
        self.f_l = tk.StringVar(value="50")
        self.f_r = tk.StringVar(value="50")
        self.f_pilot = tk.BooleanVar(value=True)
        self.f_follow = tk.BooleanVar(value=False)
        for col, (lab, var, w) in enumerate((("MHz", self.f_freq, 9), ("dBm", self.f_lvl, 5),
                                             ("L %", self.f_l, 4), ("R %", self.f_r, 4))):
            ttk.Label(rf, text=lab).grid(row=1, column=col * 2, sticky="e", pady=4)
            ttk.Entry(rf, textvariable=var, width=w).grid(row=1, column=col * 2 + 1, sticky="w")
        ttk.Checkbutton(rf, text="pilot", variable=self.f_pilot).grid(row=1, column=8)
        ttk.Checkbutton(rf, text="follow tuned freq", variable=self.f_follow).grid(row=1, column=9)
        btns = ttk.Frame(rf)
        btns.grid(row=2, column=0, columnspan=10, sticky="w")
        ttk.Button(btns, text="Add", command=self._tx_add).pack(side="left")
        ttk.Button(btns, text="Update", command=self._tx_update).pack(side="left", padx=4)
        ttk.Button(btns, text="On/Off", command=self._tx_toggle).pack(side="left")
        ttk.Button(btns, text="Remove", command=self._tx_remove).pack(side="left", padx=4)
        ttk.Label(btns, text="  noise floor dBm").pack(side="left")
        self.f_noise = tk.StringVar(value="%g" % machine.board.rf.noise_dbm)
        e = ttk.Entry(btns, textvariable=self.f_noise, width=6)
        e.pack(side="left")
        e.bind("<Return>", self._set_noise)
        e.bind("<FocusOut>", self._set_noise)

        # --- console ----------------------------------------------------
        con = ttk.LabelFrame(right, text="Serial console (firmware CLI - try \"help all\")",
                             padding=6)
        con.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self.text = tk.Text(con, width=80, height=22, font=("Consolas", 9),
                            bg="#111", fg="#ddd", insertbackground="#ddd", wrap="char")
        sb = ttk.Scrollbar(con, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set, state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        con.rowconfigure(0, weight=1)
        con.columnconfigure(0, weight=1)
        self.line = tk.StringVar()
        entry = ttk.Entry(con, textvariable=self.line, font=("Consolas", 9))
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        entry.bind("<Return>", self._send_line)
        self.history, self.hist_pos = [], 0
        entry.bind("<Up>", lambda e: self._hist(-1))
        entry.bind("<Down>", lambda e: self._hist(1))

        root.bind_all("<KeyPress>", self._key_down)
        root.bind_all("<KeyRelease>", self._key_up)
        self._tx_refresh()
        self.lcd.focus_set()
        self.emu.start()
        self._tick()

    # -- helpers --------------------------------------------------------
    def press(self, name, down):
        self.emu.call(self.m.button, name, down)

    def _is_typing(self, event):
        return isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text))

    def _key_down(self, event):
        name = KEYMAP.get(event.keysym)
        if name and not self._is_typing(event) and name not in self.held:
            self.held.add(name)
            self.press(name, True)

    def _key_up(self, event):
        name = KEYMAP.get(event.keysym)
        if name in self.held:
            self.held.discard(name)
            self.press(name, False)

    def _set_batt(self, _=None):
        self.m.board.battery_mv = self.batt.get() * 1000.0

    def _set_vol(self, _=None):
        self.emu.call(self.m.set_volume, int(self.vol.get()))

    def _set_phones(self):
        self.emu.call(self.m.set_phones, self.phones.get())

    def _set_noise(self, _=None):
        try:
            self.m.board.rf.noise_dbm = float(self.f_noise.get())
        except ValueError:
            pass

    def _pause(self):
        self.emu.paused = not self.emu.paused
        self.pause_btn.configure(text="Resume" if self.emu.paused else "Pause")

    def _shot(self):
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG image", "*.png")])
        if path:
            self.zoomed.write(path, format="png")

    # -- transmitters ---------------------------------------------------
    def _tx_refresh(self, keep=None):
        self.txlist.delete(0, "end")
        for t in self.m.board.rf.transmitters:
            self.txlist.insert("end", t.describe())
        if keep is not None and keep < self.txlist.size():
            self.txlist.selection_set(keep)

    def _tx_fields(self):
        try:
            return dict(freq_mhz=float(self.f_freq.get()), level_dbm=float(self.f_lvl.get()),
                        left=float(self.f_l.get()) / 100.0, right=float(self.f_r.get()) / 100.0,
                        pilot=self.f_pilot.get(), follow=self.f_follow.get())
        except ValueError:
            return None

    def _tx_sel(self):
        sel = self.txlist.curselection()
        return sel[0] if sel else None

    def _tx_select(self, _=None):
        i = self._tx_sel()
        if i is None:
            return
        t = self.m.board.rf.transmitters[i]
        self.f_freq.set("%.3f" % t.freq_mhz)
        self.f_lvl.set("%g" % t.level_dbm)
        self.f_l.set("%g" % (t.left * 100))
        self.f_r.set("%g" % (t.right * 100))
        self.f_pilot.set(t.pilot)
        self.f_follow.set(t.follow)

    def _tx_add(self):
        f = self._tx_fields()
        if f:
            self.m.board.rf.add(**f)
            self._tx_refresh(len(self.m.board.rf.transmitters) - 1)

    def _tx_update(self):
        i, f = self._tx_sel(), self._tx_fields()
        if i is not None and f:
            t = self.m.board.rf.transmitters[i]
            for k, v in f.items():
                setattr(t, k, v)
            self._tx_refresh(i)

    def _tx_toggle(self):
        i = self._tx_sel()
        if i is not None:
            t = self.m.board.rf.transmitters[i]
            t.enabled = not t.enabled
            self._tx_refresh(i)

    def _tx_remove(self):
        i = self._tx_sel()
        if i is not None:
            del self.m.board.rf.transmitters[i]
            self._tx_refresh()

    # -- console --------------------------------------------------------
    def _send_line(self, _=None):
        line = self.line.get()
        self.line.set("")
        if line:
            self.history.append(line)
        self.hist_pos = len(self.history)
        self.emu.call(self.m.type, line + "\r")

    def _hist(self, step):
        if not self.history:
            return "break"
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        self.line.set(self.history[self.hist_pos] if self.hist_pos < len(self.history) else "")
        return "break"

    def _append(self, s):
        s = s.replace("\r", "")
        self.text.configure(state="normal")
        self.text.insert("end", s)
        if float(self.text.index("end-1c").split(".")[0]) > 3000:
            self.text.delete("1.0", "500.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    # -- periodic refresh -------------------------------------------------
    def _draw_lcd(self):
        b = self.m.board
        red, green, rf, back = self.m.leds()
        lit = int(0x70 + 0x8F * back)
        bg = "#%02x%02x%02x" % (int(lit * 0.80), int(lit * 0.92), lit) if back > 0 else "#6f7b68"
        fg = "#0b1410"
        rows = self.m.lcd_rows()
        self.img.put(" ".join("{" + " ".join(fg if c == "#" else bg for c in r) + "}"
                              for r in rows), to=(0, 0))
        self.zoomed = self.img.zoom(SCALE)
        self.lcd.configure(image=self.zoomed)
        for item, (lvl, rgb) in zip(self.led_items, ((red, (255, 40, 40)),
                                                     (green, (40, 230, 60)),
                                                     (rf, (60, 140, 255)))):
            k = 0.15 + 0.85 * min(1.0, lvl * 3) if lvl > 0 else 0.12
            self.leds.itemconfigure(item, fill="#%02x%02x%02x" % tuple(int(c * k) for c in rgb))

    def _tick(self):
        try:
            while True:
                self._append(self.emu.console.get_nowait())
        except queue.Empty:
            pass
        self._draw_lcd()
        b = self.m.board
        lvl, tx = b.rf.level_at(b.synth.rf_khz)
        self.status.set("tuned %8.3f MHz   LO %8.3f MHz   antenna %s\n"
                        "RF at antenna %6.1f dBm %s\n"
                        "uptime %7.1f s   speed %3.0f%%%s" % (
                            b.synth.rf_khz / 1000.0, b.synth.lo_khz / 1000.0, self.m.antenna(),
                            lvl, "(carrier)" if tx else "(noise) ",
                            self.m.time_ms / 1000.0, self.emu.speed * 100,
                            "   FAULT" if self.m.fault else ""))
        self.root.after(50, self._tick)

    def close(self):
        self.emu.running = False
        self.m.save()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
