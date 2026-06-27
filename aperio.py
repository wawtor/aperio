#!/usr/bin/env python3
"""
aperio.py -- Aperio camera helper app.

Drag the on-screen joystick to aim the camera in real-time,
or type coordinates directly, then click Save.
The Aperio daemon (aperio.exe) picks up the saved config on the
next camera-open event and runs silently in the background.

Requirements: pip install hidapi
"""
import os, sys, math, struct, threading, time
import tkinter as tk
from tkinter import messagebox

try:
    import hid
except ImportError:
    import tkinter as _tk
    _r = _tk.Tk(); _r.withdraw()
    messagebox.showerror("Missing dependency",
        "hidapi is not installed.\n\nRun:  pip install hidapi\n\nthen relaunch Aperio.")
    raise SystemExit(1)

HERE         = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
STARTPOSFILE = os.path.join(HERE, "start_pos.txt")
STATEFILE    = os.path.join(HERE, "last_track.state")
PRIVACYFILE  = os.path.join(HERE, "auto_privacy.state")
INVERTFILE   = os.path.join(HERE, "joystick_invert.state")

# Camera HID identifiers (EMEET Pixy / Piko series)
CAM_VID = 0x328F
CAM_PID = 0x00C0
CAM_RID = 0x09   # vendor report ID

# Virtual joystick geometry (pixels)
JS_SIZE  = 210
JS_CX    = JS_SIZE // 2
JS_CY    = JS_SIZE // 2
JS_OUTER = 88
JS_PUCK  = 20
JS_DEAD  = 0.07

# Camera movement
IO_HZ   = 12
MAX_DEG = 14.0


# ---- HID communication (self-contained, no external module needed) ----

class _Dev:
    def __init__(self, path):
        if hasattr(hid, "Device"):
            self._d = hid.Device(path=path)
        else:
            self._d = hid.device()
            self._d.open_path(path)
    def write(self, b): self._d.write(bytes(b))
    def read(self, n, ms):
        r = self._d.read(n, ms)
        return bytes(r) if r else b""
    def close(self):
        try: self._d.close()
        except Exception: pass

def _open_cam():
    infos = hid.enumerate(CAM_VID, CAM_PID)
    if not infos:
        raise RuntimeError("Camera HID interface not found (VID %04X PID %04X)" % (CAM_VID, CAM_PID))
    path = next(
        (d["path"] for d in infos
         if d.get("usage_page") in (0x83, 131) or d.get("interface_number") == 4),
        infos[0]["path"]
    )
    return _Dev(path)

def _drain(h, win):
    end = time.time() + win
    out = []
    while time.time() < end:
        d = h.read(32, int(max(1, (end - time.time()) * 1000)))
        if d: out.append(bytes(d))
    return out

def _xfer(h, b1, b2, b3, payload=b"", wait=0.6):
    _drain(h, 0.03)
    frame = (bytes([CAM_RID, b1, b2, b3, 0x00, len(payload), 0x00, len(payload)]) + payload).ljust(32, b"\x00")
    h.write(frame)
    for d in _drain(h, wait):
        if len(d) >= 8 and d[1] == b1 and d[2] == b2 and d[3] == b3:
            return d[8:8 + d[5]], d
    return None, None

def _move_rel(h, axis, deg):
    pl = bytes([axis]) + struct.pack("<f", float(deg))
    h.write((bytes([CAM_RID, 0x63, 0x01, 0x19, 0x00, len(pl), 0x00, len(pl)]) + pl).ljust(32, b"\x00"))

def _move_abs(h, axis, deg):
    pl = bytes([axis]) + struct.pack("<f", float(deg))
    h.write((bytes([CAM_RID, 0x63, 0x01, 0x00, 0x00, len(pl), 0x00, len(pl)]) + pl).ljust(32, b"\x00"))


# ---- config helpers ----

def _load(path, default):
    try: return bool(int(open(path).read().strip()))
    except: return default

def _load_start_pos():
    try:
        p = open(STARTPOSFILE).read().split()
        return float(p[0]), float(p[1])
    except:
        return 0.0, 0.0

def _save_config(pan, tilt, tracking, auto_privacy, invert):
    with open(STARTPOSFILE, "w") as f: f.write("%.2f %.2f\n" % (pan, tilt))
    with open(STATEFILE,    "w") as f: f.write("1\n" if tracking    else "0\n")
    with open(PRIVACYFILE,  "w") as f: f.write("1\n" if auto_privacy else "0\n")
    with open(INVERTFILE,   "w") as f: f.write("1\n" if invert       else "0\n")


# ---- GUI ----

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Aperio")
        self.resizable(False, False)
        try:
            self.iconbitmap(os.path.join(HERE, "aperio.ico"))
        except Exception:
            pass

        self._jx = 0.0; self._jy = 0.0; self._dragging = False
        self._goto_target = None
        self._pan = 0.0; self._tilt = 0.0
        self._stop = False; self._hid = None

        self._build_ui()

        try:
            self._hid = _open_cam()
        except Exception as e:
            messagebox.showerror("Camera not found",
                "Camera not detected.\n\n"
                "Make sure the camera is plugged in, then reopen Aperio.\n\n%s" % e)
            self.destroy()
            return

        threading.Thread(target=self._io_worker, daemon=True).start()
        self.after(350, self._refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----

    def _build_ui(self):
        BG = "#0f0f1a"; BG2 = "#1a1a2e"; ACC = "#4a9eff"; FG = "#dde0f0"; DIM = "#666688"
        self.configure(bg=BG)

        def chk(parent, text, var):
            return tk.Checkbutton(parent, text=text, variable=var,
                                  font=("Segoe UI", 10), bg=BG2, fg=FG,
                                  selectcolor="#252540", activebackground=BG2,
                                  activeforeground=FG, anchor="w")

        root = tk.Frame(self, bg=BG, padx=18, pady=14)
        root.pack(fill="both", expand=True)

        tk.Label(root, text="Aperio  ·  Startup Setup",
                 font=("Segoe UI", 13, "bold"), bg=BG, fg=FG).pack(anchor="w")
        tk.Label(root, text="Aim the camera at your startup position, then save.",
                 font=("Segoe UI", 9), bg=BG, fg=DIM).pack(anchor="w", pady=(1, 10))

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True)

        # Left: joystick
        lf = tk.Frame(body, bg=BG)
        lf.pack(side="left", anchor="n", padx=(0, 18))

        self._cv = tk.Canvas(lf, width=JS_SIZE, height=JS_SIZE,
                              bg="#070710", highlightthickness=1,
                              highlightbackground="#2a2a44")
        self._cv.pack()
        self._draw_joystick_bg()
        self._puck = self._cv.create_oval(
            JS_CX - JS_PUCK, JS_CY - JS_PUCK, JS_CX + JS_PUCK, JS_CY + JS_PUCK,
            fill=ACC, outline="#7bbfff", width=2)
        self._cv.bind("<ButtonPress-1>",   self._jdown)
        self._cv.bind("<B1-Motion>",       self._jmove)
        self._cv.bind("<ButtonRelease-1>", self._jup)

        tk.Label(lf, text="← Pan →    ↑ Tilt ↓",
                 font=("Segoe UI", 8), bg=BG, fg=DIM).pack(pady=(4, 0))

        cf = tk.Frame(lf, bg=BG)
        cf.pack(pady=(8, 0))
        for col, text, attr in ((0, "Pan:",  "_pan_entry"), (2, "Tilt:", "_tilt_entry")):
            tk.Label(cf, text=text, font=("Segoe UI", 9), bg=BG, fg=DIM).grid(row=0, column=col, padx=(0,3))
            e = tk.Entry(cf, width=7, font=("Segoe UI", 10), bg="#252540", fg=FG,
                         insertbackground=FG, relief="flat",
                         highlightthickness=1, highlightbackground="#2a2a44")
            e.grid(row=0, column=col+1, padx=(0, 8 if col == 0 else 8))
            e.bind("<Return>", self._goto_coords)
            setattr(self, attr, e)
        tk.Button(cf, text="Go", command=self._goto_coords, font=("Segoe UI", 9),
                  bg="#2a2a44", fg=FG, activebackground="#3a3a55", activeforeground=FG,
                  relief="flat", padx=6, pady=2, cursor="hand2").grid(row=0, column=4)
        tk.Label(lf, text="±150 pan  /  ±90 tilt",
                 font=("Segoe UI", 7), bg=BG, fg=DIM).pack(pady=(2, 0))

        # Right: readout + settings
        rf = tk.Frame(body, bg=BG2, padx=14, pady=12,
                      highlightthickness=1, highlightbackground="#2a2a44")
        rf.pack(side="left", fill="y", anchor="n")

        def sep(r):
            tk.Frame(rf, height=1, bg="#2a2a44").grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        def hdr(r, text):
            tk.Label(rf, text=text, font=("Segoe UI", 10, "bold"), bg=BG2, fg=FG).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(0, 4))
        def sub(r, text):
            tk.Label(rf, text=text, font=("Segoe UI", 8), bg=BG2, fg=DIM).grid(
                row=r, column=0, columnspan=2, sticky="w")

        hdr(0, "Live Position")
        self._pan_var = tk.StringVar(value="—")
        self._tilt_var = tk.StringVar(value="—")
        for r, lbl, var in ((1, "Pan :", self._pan_var), (2, "Tilt:", self._tilt_var)):
            tk.Label(rf, text=lbl, font=("Segoe UI", 10), bg=BG2, fg=DIM, anchor="w").grid(
                row=r, column=0, sticky="w", pady=2)
            tk.Label(rf, textvariable=var, font=("Segoe UI", 10, "bold"), bg=BG2, fg=ACC,
                     width=14, anchor="e").grid(row=r, column=1, sticky="e")

        sep(3); hdr(4, "Settings")

        self._track_var = tk.BooleanVar(value=_load(STATEFILE, True))
        chk(rf, "AI Tracking  (Follow mode)", self._track_var).grid(
            row=5, column=0, columnspan=2, sticky="w")
        sub(6, "Camera follows you while in use")

        self._privacy_var = tk.BooleanVar(value=_load(PRIVACYFILE, True))
        chk(rf, "Auto Privacy  (lens-down sleep)", self._privacy_var).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        sub(8, "Lens parks down when no app uses camera")

        self._invert_var = tk.BooleanVar(value=_load(INVERTFILE, False))
        chk(rf, "Invert Joystick", self._invert_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))

        sep(10)
        sp = _load_start_pos()
        tk.Label(rf, text="Saved start:", font=("Segoe UI", 9), bg=BG2, fg=DIM, anchor="w").grid(
            row=11, column=0, sticky="w")
        self._saved_var = tk.StringVar(value="Pan %+.1f°  Tilt %+.1f°" % sp)
        tk.Label(rf, textvariable=self._saved_var, font=("Segoe UI", 9), bg=BG2, fg=DIM).grid(
            row=12, column=0, columnspan=2, sticky="w")

        # Buttons
        tk.Frame(root, height=1, bg="#2a2a44").pack(fill="x", pady=(12, 8))
        bf = tk.Frame(root, bg=BG)
        bf.pack(anchor="e")
        tk.Button(bf, text="  Save  ", command=self._save,
                  font=("Segoe UI", 10, "bold"), bg=ACC, fg="white",
                  activebackground="#3a8eef", activeforeground="white",
                  relief="flat", padx=8, pady=5, cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(bf, text="  Close  ", command=self._on_close,
                  font=("Segoe UI", 10), bg="#2a2a44", fg=FG,
                  activebackground="#3a3a55", activeforeground=FG,
                  relief="flat", padx=8, pady=5, cursor="hand2").pack(side="left")

    def _draw_joystick_bg(self):
        c = self._cv
        c.create_oval(JS_CX-JS_OUTER, JS_CY-JS_OUTER, JS_CX+JS_OUTER, JS_CY+JS_OUTER,
                      outline="#2a2a44", fill="#0d0d1e", width=2)
        ir = int(JS_OUTER * 0.55)
        c.create_oval(JS_CX-ir, JS_CY-ir, JS_CX+ir, JS_CY+ir, outline="#1a1a34", fill="", width=1)
        c.create_line(JS_CX-JS_OUTER, JS_CY, JS_CX+JS_OUTER, JS_CY, fill="#181830")
        c.create_line(JS_CX, JS_CY-JS_OUTER, JS_CX, JS_CY+JS_OUTER, fill="#181830")
        dim = "#444466"
        c.create_text(JS_CX,        12,           text="▲", fill=dim, font=("Segoe UI", 9))
        c.create_text(JS_CX,        JS_SIZE-12,   text="▼", fill=dim, font=("Segoe UI", 9))
        c.create_text(12,           JS_CY,         text="◄", fill=dim, font=("Segoe UI", 9))
        c.create_text(JS_SIZE-12,   JS_CY,         text="►", fill=dim, font=("Segoe UI", 9))
        c.create_oval(JS_CX-3, JS_CY-3, JS_CX+3, JS_CY+3, fill="#2a2a44", outline="")

    # ---- Joystick ----

    def _jdown(self, e): self._dragging = True;  self._jset(e.x, e.y)
    def _jmove(self, e):
        if self._dragging: self._jset(e.x, e.y)
    def _jup(self, _):
        self._dragging = False; self._jx = 0.0; self._jy = 0.0
        self._cv.coords(self._puck, JS_CX-JS_PUCK, JS_CY-JS_PUCK, JS_CX+JS_PUCK, JS_CY+JS_PUCK)
    def _jset(self, mx, my):
        dx = mx - JS_CX; dy = my - JS_CY; d = math.hypot(dx, dy)
        if d > JS_OUTER: dx = dx/d*JS_OUTER; dy = dy/d*JS_OUTER
        self._jx = dx/JS_OUTER; self._jy = dy/JS_OUTER
        self._cv.coords(self._puck, JS_CX+dx-JS_PUCK, JS_CY+dy-JS_PUCK,
                        JS_CX+dx+JS_PUCK, JS_CY+dy+JS_PUCK)

    # ---- Coordinate entry ----

    def _goto_coords(self, _=None):
        try:
            pan  = max(-150.0, min(150.0, float(self._pan_entry.get())))
            tilt = max(-90.0,  min(90.0,  float(self._tilt_entry.get())))
        except ValueError:
            messagebox.showwarning("Invalid", "Enter numeric degrees.\nPan: ±150  Tilt: ±90")
            return
        self._goto_target = (pan, tilt)

    # ---- Refresh ----

    def _refresh(self):
        if not self._stop:
            self._pan_var.set("%+.2f°" % self._pan)
            self._tilt_var.set("%+.2f°" % self._tilt)
            self.after(350, self._refresh)

    # ---- Save ----

    def _save(self):
        pan, tilt = self._pan, self._tilt
        tracking = self._track_var.get()
        auto_privacy = self._privacy_var.get()
        invert = self._invert_var.get()
        _save_config(pan, tilt, tracking, auto_privacy, invert)
        self._saved_var.set("Pan %+.1f°  Tilt %+.1f°" % (pan, tilt))
        messagebox.showinfo("Saved",
            "Startup position:\n  Pan  %+.2f°\n  Tilt %+.2f°\n\n"
            "AI Tracking:  %s\nAuto Privacy: %s\n\n"
            "Takes effect the next time an app opens the camera."
            % (pan, tilt, "ON" if tracking else "OFF", "ON" if auto_privacy else "OFF"))

    def _on_close(self):
        self._stop = True
        self.destroy()

    # ---- I/O thread ----

    def _query_pos(self, h):
        p, _ = _xfer(h, 0x63, 0x01, 0x01, bytes([1]), wait=0.15)
        t, _ = _xfer(h, 0x63, 0x01, 0x01, bytes([2]), wait=0.15)
        if p and len(p) >= 5: self._pan  = struct.unpack("<f", p[1:5])[0]
        if t and len(t) >= 5: self._tilt = struct.unpack("<f", t[1:5])[0]

    def _io_worker(self):
        h = self._hid
        interval = 1.0 / IO_HZ
        last_query = 0.0
        try:
            self._query_pos(h); last_query = time.time()
        except Exception: pass

        while not self._stop:
            goto = self._goto_target
            if goto is not None:
                self._goto_target = None
                try:
                    _move_abs(h, 1, goto[0]); time.sleep(0.5)
                    _move_abs(h, 2, goto[1]); time.sleep(0.5)
                    self._query_pos(h); last_query = time.time()
                except Exception: pass
                continue

            jx = self._jx; jy = self._jy; now = time.time()
            if self._dragging and math.hypot(jx, jy) > JS_DEAD:
                inv = -1.0 if self._invert_var.get() else 1.0
                try:
                    dpan = inv * jx * MAX_DEG; dtilt = inv * -jy * MAX_DEG
                    if abs(dpan)  > 0.05: _move_rel(h, 1, dpan)
                    if abs(dtilt) > 0.05: _move_rel(h, 2, dtilt)
                except Exception: pass
                time.sleep(interval)
            elif not self._dragging and now - last_query >= 0.8:
                try: self._query_pos(h)
                except Exception: pass
                last_query = time.time()
            else:
                time.sleep(0.04)

        try: h.close()
        except Exception: pass


if __name__ == "__main__":
    App().mainloop()
