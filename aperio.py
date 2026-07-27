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
FLIPFILE     = os.path.join(HERE, "image_flip.state")
MIRRORFILE   = os.path.join(HERE, "image_mirror.state")
APIFILE      = os.path.join(HERE, "api_server.state")

# Camera HID identifiers (EMEET Pixy / Piko series)
CAM_VID = 0x328F
CAM_PID = 0x00C0
CAM_RID = 0x09   # vendor report ID

# Camera movement
IO_HZ   = 12
MAX_DEG = 14.0
JS_DEAD = 0.07


# ---- DPI awareness / scaling ----

def _init_scale():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
        return windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0

SCALE = _init_scale()

def S(v):
    return int(round(v * SCALE))


# ---- palette ----

BG_TOP   = "#0b0f20"
BG_BOT   = "#181231"
CARD     = "#151a30"
CARD_HI  = "#3a4370"
EDGE     = "#293054"
FROST    = "#93a9e0"
WELL_LO  = "#0a0e1e"
WELL_HI  = "#1a2140"
ACC      = "#4f9dff"
ACC_HI   = "#8ec4ff"
ACC_HOV  = "#6cb0ff"
FG       = "#e8ebfa"
DIM      = "#8b93b8"
FAINT    = "#575e86"
GOOD     = "#5fd39a"
BTN2     = "#242c4e"
BTN2_HOV = "#2e3763"

def _hx(c):
    return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))

def _blend(a, b, t):
    a = _hx(a); b = _hx(b)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _rr(c, x1, y1, x2, y2, r, **kw):
    pts = (x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2,
           x2-r,y2, x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1)
    return c.create_polygon(pts, smooth=True, **kw)


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

def _save_config(pan, tilt, tracking, auto_privacy, invert, flip, mirror, api):
    with open(STARTPOSFILE, "w") as f: f.write("%.2f %.2f\n" % (pan, tilt))
    with open(STATEFILE,    "w") as f: f.write("1\n" if tracking    else "0\n")
    with open(PRIVACYFILE,  "w") as f: f.write("1\n" if auto_privacy else "0\n")
    with open(INVERTFILE,   "w") as f: f.write("1\n" if invert       else "0\n")
    with open(FLIPFILE,     "w") as f: f.write("1\n" if flip         else "0\n")
    with open(MIRRORFILE,   "w") as f: f.write("1\n" if mirror       else "0\n")
    with open(APIFILE,      "w") as f: f.write("1\n" if api          else "0\n")


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
        self._reverse_target = False
        self._home_target = None
        self._pan = 0.0; self._tilt = 0.0
        self._stop = False; self._hid = None

        self._track_on   = _load(STATEFILE,   True)
        self._privacy_on = _load(PRIVACYFILE, True)
        self._invert_on  = _load(INVERTFILE,  False)
        self._flip_on    = _load(FLIPFILE,    False)
        self._mirror_on  = _load(MIRRORFILE,  False)
        self._api_on     = _load(APIFILE,     False)

        self._toggles = {}
        self._anim_seq = 0
        self._flash_job = None

        self._build_ui()
        self._apply_window_chrome()

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

    # ---- window chrome ----

    def _apply_window_chrome(self):
        try:
            self.attributes("-alpha", 0.97)
        except Exception:
            pass
        try:
            from ctypes import windll, byref, c_int, sizeof
            self.update_idletasks()
            hwnd = windll.user32.GetParent(self.winfo_id())
            windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(c_int(1)), sizeof(c_int))
        except Exception:
            pass

    # ---- UI ----

    def _build_ui(self):
        W, H = S(640), S(592)
        self.configure(bg=BG_TOP)
        cv = tk.Canvas(self, width=W, height=H, highlightthickness=0, bd=0, bg=BG_TOP)
        cv.pack()
        self._cv = cv

        self._paint_backdrop(W, H)

        # Header: aperture mark + title
        mx, my = S(42), S(40)
        cv.create_oval(mx-S(11), my-S(11), mx+S(11), my+S(11), outline=ACC, width=S(2))
        for a in (25, 145, 265):
            cv.create_arc(mx-S(6), my-S(6), mx+S(6), my+S(6),
                          start=a, extent=75, style="arc", outline=ACC_HI, width=S(2))
        cv.create_text(S(64), S(31), anchor="w", text="Aperio",
                       font=("Segoe UI Semibold", 15), fill=FG)
        cv.create_text(S(64), S(53), anchor="w",
                       text="Aim the camera at its startup position, then save.",
                       font=("Segoe UI", 9), fill=DIM)

        # Cards
        self._card(S(26),  S(80), S(312), S(518))
        self._card(S(326), S(80), S(614), S(518))

        # Left card: joystick
        self._jcx, self._jcy = S(169), S(206)
        self._jr   = S(88)
        self._jpk  = S(23)
        self._jtr  = self._jr - self._jpk - S(6)
        self._draw_joystick(self._jcx, self._jcy)

        cv.create_text(self._jcx, S(318), text="Drag to aim — release to stop",
                       font=("Segoe UI", 8), fill=FAINT)

        # Coordinate entry row
        ey = S(354)
        def entry(x):
            e = tk.Entry(cv, width=6, font=("Segoe UI", 10), bg="#1b2242", fg=FG,
                         insertbackground=FG, relief="flat", justify="center",
                         highlightthickness=1, highlightbackground=EDGE,
                         highlightcolor=ACC)
            cv.create_window(x, ey, window=e, height=S(26))
            e.bind("<Return>", self._goto_coords)
            return e
        cv.create_text(S(62),  ey, text="Pan",  font=("Segoe UI", 9), fill=DIM, anchor="e")
        self._pan_entry  = entry(S(96))
        cv.create_text(S(160), ey, text="Tilt", font=("Segoe UI", 9), fill=DIM, anchor="e")
        self._tilt_entry = entry(S(194))
        self._button(S(232), ey-S(13), S(282), ey+S(13), "Go", False, self._goto_coords, rad=8, fsize=9)
        cv.create_text(self._jcx, S(384), text="±150° pan   ·   ±90° tilt",
                       font=("Segoe UI", 8), fill=FAINT)

        # Right card
        lx, rx = S(344), S(596)
        cv.create_text(lx, S(102), anchor="w", text="L I V E   P O S I T I O N",
                       font=("Segoe UI", 8, "bold"), fill=FAINT)
        cv.create_text(lx, S(130), anchor="w", text="Pan",  font=("Segoe UI", 10), fill=DIM)
        cv.create_text(lx, S(158), anchor="w", text="Tilt", font=("Segoe UI", 10), fill=DIM)
        self._pan_item  = cv.create_text(rx, S(130), anchor="e", text="—",
                                         font=("Segoe UI Semibold", 13), fill=ACC_HI)
        self._tilt_item = cv.create_text(rx, S(158), anchor="e", text="—",
                                         font=("Segoe UI Semibold", 13), fill=ACC_HI)

        cv.create_line(lx, S(182), rx, S(182), fill=EDGE)
        cv.create_text(lx, S(198), anchor="w", text="S E T T I N G S",
                       font=("Segoe UI", 8, "bold"), fill=FAINT)

        rows = (
            ("AI tracking",     "Camera follows you while in use",       S(222), "_track_on"),
            ("Auto privacy",    "Lens parks down when no app uses it",   S(266), "_privacy_on"),
            ("Flip image",      "180° for cameras mounted upside-down",  S(310), "_flip_on"),
            ("Mirror image",    "Horizontally mirror the video",         S(354), "_mirror_on"),
            ("Invert joystick", "Reverse drag direction",                S(398), "_invert_on"),
            ("Local API server","Control the camera from your own apps", S(442), "_api_on"),
        )
        for label, caption, y, attr in rows:
            li = cv.create_text(lx, y, anchor="w", text=label, font=("Segoe UI", 10), fill=FG)
            cv.create_text(lx, y+S(18), anchor="w", text=caption, font=("Segoe UI", 8),  fill=FAINT)
            self._make_toggle(rx-S(44), y-S(2), attr)
            if attr == "_api_on":
                hx = cv.bbox(li)[2] + S(13)
                cv.create_oval(hx-S(8), y-S(8), hx+S(8), y+S(8),
                               outline=FAINT, width=1, tags="apihelp")
                cv.create_text(hx, y, text="?", font=("Segoe UI", 8, "bold"),
                               fill=DIM, tags="apihelp")
                cv.tag_bind("apihelp", "<Button-1>", lambda _e: self._show_api_help())
                cv.tag_bind("apihelp", "<Enter>", lambda _e: cv.config(cursor="hand2"))
                cv.tag_bind("apihelp", "<Leave>", lambda _e: cv.config(cursor=""))

        cv.create_line(lx, S(476), rx, S(476), fill=EDGE)
        cv.create_text(lx, S(496), anchor="w", text="Startup position",
                       font=("Segoe UI", 9), fill=DIM)
        self._saved_item = cv.create_text(rx, S(496), anchor="e",
                                          text="Pan %+.1f°  ·  Tilt %+.1f°" % _load_start_pos(),
                                          font=("Segoe UI", 9), fill=FG)

        # Footer
        self._status_item = cv.create_text(S(30), S(557), anchor="w", text="",
                                           font=("Segoe UI", 9), fill=GOOD)
        self._button(S(428), S(539), S(514), S(575), "Close", False, self._on_close)
        self._button(S(528), S(539), S(614), S(575), "Save",  True,  self._save)

        # Joystick interaction
        cv.bind("<ButtonPress-1>",   self._jdown)
        cv.bind("<B1-Motion>",       self._jmove)
        cv.bind("<ButtonRelease-1>", self._jup)

    # ---- painting ----

    def _paint_backdrop(self, W, H):
        cv = self._cv
        step = max(1, S(2))
        for y in range(0, H + step, step):
            cv.create_rectangle(0, y, W, y + step, width=0,
                                fill=_blend(BG_TOP, BG_BOT, min(1.0, y / H)))
        self._orb(W - S(60), S(20),  S(200), "#3b6fd4", 0.20, H)
        self._orb(S(30),  H - S(30), S(240), "#6c4fd8", 0.17, H)
        self._orb(S(330), S(-70),    S(150), "#2a8fd8", 0.12, H)

    def _orb(self, x, y, R, col, strength, H):
        cv = self._cv
        base = _blend(BG_TOP, BG_BOT, min(1.0, max(0.0, y / H)))
        n = 20
        for i in range(n):
            t = (i + 1) / n
            r = int(R * (1.0 - i / n))
            if r < 2: break
            cv.create_oval(x - r, y - r, x + r, y + r, width=0,
                           fill=_blend(base, col, strength * t))

    def _card(self, x1, y1, x2, y2):
        cv = self._cv
        r = S(16)
        _rr(cv, x1 + S(2), y1 + S(5), x2 + S(2), y2 + S(5), r, fill="#070a16", outline="")
        _rr(cv, x1, y1, x2, y2, r, fill=CARD, outline=EDGE)
        _rr(cv, x1 + S(4), y1 + S(2), x2 - S(4), y1 + S(24), r,
            fill=_blend(CARD, FROST, 0.035), outline="")
        cv.create_line(x1 + r, y1 + 1, x2 - r, y1 + 1, fill=CARD_HI)

    def _draw_joystick(self, cx, cy):
        cv = self._cv
        R = self._jr

        # active glow ring (hidden until drag)
        self._glow_ids = []
        for i in range(5):
            rr = R + S(3) + S(3) * i
            self._glow_ids.append(cv.create_oval(
                cx - rr, cy - rr, cx + rr, cy + rr, state="hidden",
                outline=_blend(ACC, CARD, 0.45 + i * 0.12), width=S(2)))

        # bezel rim
        cv.create_oval(cx - R - S(6), cy - R - S(6), cx + R + S(6), cy + R + S(6),
                       fill="#0b0f20", outline="#060912", width=1)
        # radial-gradient well, lit slightly from the top-left
        n = 30
        for i in range(n, 0, -1):
            t = i / n
            r = int(R * t)
            off = int(S(5) * (1.0 - t))
            cv.create_oval(cx - r - off, cy - r - off, cx + r - off, cy + r - off,
                           width=0, fill=_blend(WELL_HI, WELL_LO, t), tags="well")
        cv.create_oval(cx - R, cy - R, cx + R, cy + R, outline="#323a63", width=S(2), tags="well")
        cv.create_arc(cx - R, cy - R, cx + R, cy + R, start=60, extent=120,
                      style="arc", outline=CARD_HI, width=S(2))

        # ticks + cardinal chevrons
        for a in range(0, 360, 30):
            rad = math.radians(a)
            cardinal = a % 90 == 0
            r1 = R - S(4); r2 = R - (S(11) if cardinal else S(8))
            cv.create_line(cx + r1 * math.cos(rad), cy + r1 * math.sin(rad),
                           cx + r2 * math.cos(rad), cy + r2 * math.sin(rad),
                           fill="#4a527e" if cardinal else "#252c4e",
                           width=S(2) if cardinal else 1)
        ch = R - S(20)
        w = S(5)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            tx, ty = cx + dx * ch, cy + dy * ch
            px, py = -dy, dx
            cv.create_polygon(tx + dx * w, ty + dy * w,
                              tx + px * w - dx * S(2), ty + py * w - dy * S(2),
                              tx - px * w - dx * S(2), ty - py * w - dy * S(2),
                              fill="#3d4570", outline="")

        # guides
        g = int(R * 0.52)
        cv.create_oval(cx - g, cy - g, cx + g, cy + g, outline="#20274a", width=1)
        cv.create_line(cx - R + S(10), cy, cx + R - S(10), cy, fill="#1c2344")
        cv.create_line(cx, cy - R + S(10), cx, cy + R - S(10), fill="#1c2344")
        cv.create_oval(cx - S(3), cy - S(3), cx + S(3), cy + S(3), fill="#2c3456", width=0)

        # rubber band
        self._band = cv.create_line(cx, cy, cx, cy, state="hidden",
                                    fill=_blend(ACC, WELL_LO, 0.25),
                                    width=S(2), capstyle="round")

        # puck (all items tagged "puck", moved together)
        P = self._jpk
        cv.create_oval(cx - P - S(2), cy - P + S(2), cx + P + S(2), cy + P + S(6),
                       fill="#060912", width=0, tags="puck")
        layers = 9
        for i in range(layers):
            t = i / (layers - 1)
            r = int(P * (1.0 - 0.55 * t))
            off = int(P * 0.30 * t)
            cv.create_oval(cx - r - off, cy - r - off, cx + r - off, cy + r - off,
                           width=0, fill=_blend("#2b66c4", "#a9d4ff", t * 0.85), tags="puck")
        cv.create_oval(cx - P, cy - P, cx + P, cy + P,
                       outline=_blend(ACC_HI, WELL_LO, 0.25), width=S(2), fill="", tags="puck")
        sr = int(P * 0.16)
        sx, sy = cx - int(P * 0.42), cy - int(P * 0.46)
        cv.create_oval(sx - sr, sy - sr, sx + sr, sy + sr, fill="#eef6ff", width=0, tags="puck")

        self._px, self._py = cx, cy
        cv.tag_bind("puck", "<Enter>", lambda _: cv.config(cursor="fleur"))
        cv.tag_bind("puck", "<Leave>", lambda _: cv.config(cursor=""))

    # ---- canvas widgets ----

    def _make_toggle(self, x, y, attr):
        cv = self._cv
        w, h = S(44), S(22)
        tag = "tgl_" + attr
        on = getattr(self, attr)
        col = ACC if on else "#262e50"
        items = [
            cv.create_oval(x, y, x + h, y + h, fill=col, width=0, tags=tag),
            cv.create_oval(x + w - h, y, x + w, y + h, fill=col, width=0, tags=tag),
            cv.create_rectangle(x + h // 2, y, x + w - h // 2, y + h, fill=col, width=0, tags=tag),
        ]
        kr = (h - S(6)) // 2
        kx = x + w - h // 2 if on else x + h // 2
        knob = cv.create_oval(kx - kr, y + h // 2 - kr, kx + kr, y + h // 2 + kr,
                              fill="#eef1fb", outline="#0e1226", tags=tag)
        self._toggles[attr] = {"items": items, "knob": knob, "x": x, "w": w, "h": h,
                               "y": y, "kr": kr, "anim": None}
        cv.tag_bind(tag, "<Button-1>", lambda _e, a=attr: self._flip_toggle(a))
        cv.tag_bind(tag, "<Enter>", lambda _: cv.config(cursor="hand2"))
        cv.tag_bind(tag, "<Leave>", lambda _: cv.config(cursor=""))

    def _flip_toggle(self, attr):
        on = not getattr(self, attr)
        setattr(self, attr, on)
        # persist immediately -- the daemon watches the config dir
        path, label = {
            "_track_on":   (STATEFILE,   "AI tracking"),
            "_privacy_on": (PRIVACYFILE, "Auto privacy"),
            "_invert_on":  (INVERTFILE,  "Invert joystick"),
            "_flip_on":    (FLIPFILE,    "Flip image"),
            "_mirror_on":  (MIRRORFILE,  "Mirror image"),
            "_api_on":     (APIFILE,     "Local API server"),
        }[attr]
        try:
            with open(path, "w") as f:
                f.write("1\n" if on else "0\n")
            self._flash("%s %s — applied" % (label, "on" if on else "off"))
        except Exception:
            pass
        if attr in ("_flip_on", "_mirror_on"):
            self._reverse_target = True
        t = self._toggles[attr]
        cv = self._cv
        col = ACC if on else "#262e50"
        for it in t["items"]:
            cv.itemconfigure(it, fill=col)
        x0 = t["x"] + t["h"] // 2
        x1 = t["x"] + t["w"] - t["h"] // 2
        src, dst = (x0, x1) if on else (x1, x0)
        if t["anim"]:
            self.after_cancel(t["anim"])
        self._slide_knob(t, src, dst, 0)

    def _slide_knob(self, t, src, dst, step):
        steps = 5
        u = (step + 1) / steps
        u = 1 - (1 - u) ** 2
        kx = src + (dst - src) * u
        ky = t["y"] + t["h"] // 2
        self._cv.coords(t["knob"], kx - t["kr"], ky - t["kr"], kx + t["kr"], ky + t["kr"])
        if step + 1 < steps:
            t["anim"] = self.after(14, lambda: self._slide_knob(t, src, dst, step + 1))
        else:
            t["anim"] = None

    def _button(self, x1, y1, x2, y2, text, primary, cmd, rad=10, fsize=10):
        cv = self._cv
        tag = "btn_" + text.lower()
        fill, hov = (ACC, ACC_HOV) if primary else (BTN2, BTN2_HOV)
        rect = _rr(cv, x1, y1, x2, y2, S(rad), fill=fill, outline="", tags=tag)
        cv.create_text((x1 + x2) // 2, (y1 + y2) // 2, text=text,
                       font=("Segoe UI Semibold", fsize),
                       fill="#ffffff" if primary else FG, tags=tag)
        cv.tag_bind(tag, "<Button-1>", lambda _e: cmd())
        cv.tag_bind(tag, "<Enter>", lambda _e: (cv.itemconfigure(rect, fill=hov),
                                                cv.config(cursor="hand2")))
        cv.tag_bind(tag, "<Leave>", lambda _e: (cv.itemconfigure(rect, fill=fill),
                                                cv.config(cursor="")))

    # ---- Joystick ----

    def _move_puck_to(self, nx, ny):
        self._cv.move("puck", nx - self._px, ny - self._py)
        self._px, self._py = nx, ny

    def _jdown(self, e):
        cx, cy = self._jcx, self._jcy
        if math.hypot(e.x - cx, e.y - cy) > self._jr:
            return
        self._anim_seq += 1
        self._dragging = True
        for g in self._glow_ids:
            self._cv.itemconfigure(g, state="normal")
        self._jset(e.x, e.y)

    def _jmove(self, e):
        if self._dragging:
            self._jset(e.x, e.y)

    def _jup(self, _):
        if not self._dragging:
            return
        self._dragging = False
        self._jx = 0.0; self._jy = 0.0
        for g in self._glow_ids:
            self._cv.itemconfigure(g, state="hidden")
        self._cv.itemconfigure(self._band, state="hidden")
        self._anim_seq += 1
        self._spring(self._anim_seq, self._px, self._py, 0)

    def _spring(self, seq, sx, sy, step):
        if seq != self._anim_seq:
            return
        steps = 9
        u = (step + 1) / steps
        u = 1 - (1 - u) ** 3
        nx = sx + (self._jcx - sx) * u
        ny = sy + (self._jcy - sy) * u
        self._move_puck_to(nx, ny)
        if step + 1 < steps:
            self.after(12, lambda: self._spring(seq, sx, sy, step + 1))

    def _jset(self, mx, my):
        cx, cy = self._jcx, self._jcy
        dx = mx - cx; dy = my - cy; d = math.hypot(dx, dy)
        if d > self._jtr:
            dx = dx / d * self._jtr; dy = dy / d * self._jtr
        self._jx = dx / self._jtr; self._jy = dy / self._jtr
        self._move_puck_to(cx + dx, cy + dy)
        if math.hypot(dx, dy) > S(8):
            self._cv.coords(self._band, cx, cy, cx + dx, cy + dy)
            self._cv.itemconfigure(self._band, state="normal")
        else:
            self._cv.itemconfigure(self._band, state="hidden")
        self._cv.tag_raise("puck")

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
            self._cv.itemconfigure(self._pan_item,  text="%+.2f°" % self._pan)
            self._cv.itemconfigure(self._tilt_item, text="%+.2f°" % self._tilt)
            self.after(350, self._refresh)

    # ---- Save ----

    def _flash(self, msg):
        self._cv.itemconfigure(self._status_item, text=msg)
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(3200,
            lambda: self._cv.itemconfigure(self._status_item, text=""))

    def _save(self):
        pan, tilt = self._pan, self._tilt
        _save_config(pan, tilt, self._track_on, self._privacy_on, self._invert_on,
                     self._flip_on, self._mirror_on, self._api_on)
        self._home_target = (pan, tilt)
        self._cv.itemconfigure(self._saved_item,
                               text="Pan %+.1f°  ·  Tilt %+.1f°" % (pan, tilt))
        self._flash("Saved — the camera will now wake up aimed here")

    # ---- API help ----

    def _show_api_help(self):
        if getattr(self, "_help_win", None) is not None:
            try:
                if self._help_win.winfo_exists():
                    self._help_win.lift()
                    return
            except Exception:
                pass
        w = tk.Toplevel(self)
        self._help_win = w
        w.title("Aperio — Local API")
        w.configure(bg=BG_TOP)
        w.resizable(False, False)
        w.transient(self)
        try:
            w.iconbitmap(os.path.join(HERE, "aperio.ico"))
        except Exception:
            pass
        try:
            from ctypes import windll, byref, c_int, sizeof
            w.update_idletasks()
            hwnd = windll.user32.GetParent(w.winfo_id())
            windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(c_int(1)), sizeof(c_int))
        except Exception:
            pass

        pad = S(20)
        f = tk.Frame(w, bg=BG_TOP, padx=pad, pady=pad)
        f.pack(fill="both", expand=True)
        tk.Label(f, text="Local API server", font=("Segoe UI Semibold", 12),
                 bg=BG_TOP, fg=FG, anchor="w").pack(fill="x")
        tk.Label(f, text="While the toggle is on, the Aperio daemon listens on\n"
                         "http://127.0.0.1:4750  (this PC only — not reachable from the network)",
                 font=("Segoe UI", 9), bg=BG_TOP, fg=DIM, anchor="w",
                 justify="left").pack(fill="x", pady=(S(4), S(12)))

        endpoints = (
            ("GET  /",                        "endpoint index"),
            ("GET  /status",                  "daemon + camera info"),
            ("POST /move?pan=X&tilt=Y",       "absolute move (degrees)"),
            ("POST /move_rel?pan=X&tilt=Y",   "relative move (degrees)"),
            ("POST /mode?value=follow|standard|privacy", "set device mode"),
            ("POST /flip?value=on|off",       "flip image 180° (upside-down mount)"),
            ("POST /mirror?value=on|off",     "mirror image horizontally"),
            ("POST /home",                    "go to saved startup position"),
            ("POST /shutdown",                "turn this API server off"),
        )
        grid = tk.Frame(f, bg=CARD, padx=S(12), pady=S(10),
                        highlightthickness=1, highlightbackground=EDGE)
        grid.pack(fill="x")
        for r, (ep, desc) in enumerate(endpoints):
            tk.Label(grid, text=ep, font=("Consolas", 9), bg=CARD, fg=ACC_HI,
                     anchor="w").grid(row=r, column=0, sticky="w", pady=1)
            tk.Label(grid, text=desc, font=("Segoe UI", 9), bg=CARD, fg=DIM,
                     anchor="w").grid(row=r, column=1, sticky="w", padx=(S(16), 0))

        tk.Label(f, text="Pan is clamped to ±150°, tilt to ±90°.  Example:",
                 font=("Segoe UI", 9), bg=BG_TOP, fg=DIM, anchor="w",
                 justify="left").pack(fill="x", pady=(S(12), S(2)))
        tk.Label(f, text='curl -X POST "http://127.0.0.1:4750/move?pan=30&tilt=-10"',
                 font=("Consolas", 9), bg=BG_TOP, fg=FG, anchor="w").pack(fill="x")
        tk.Label(f, text="Toggle changes apply immediately — build your own joystick,\n"
                         "stream deck buttons, OBS scripts, anything that can speak HTTP.",
                 font=("Segoe UI", 9), bg=BG_TOP, fg=DIM, anchor="w",
                 justify="left").pack(fill="x", pady=(S(10), S(12)))
        tk.Button(f, text="  Close  ", command=w.destroy, font=("Segoe UI", 9),
                  bg=BTN2, fg=FG, activebackground=BTN2_HOV, activeforeground=FG,
                  relief="flat", cursor="hand2").pack(anchor="e")

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
            # wake to Standard so the joystick works and readouts are real even
            # if the camera was parked in Privacy when the GUI was opened
            _xfer(h, 0x01, 0x01, 0x00, bytes([0]), wait=0.8)
            time.sleep(1.0)
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

            home = self._home_target
            if home is not None:
                self._home_target = None
                try:
                    # wake to Standard first: while parked, motor moves are
                    # discarded but the capture below is NOT -- it would store
                    # the parked pose (tilt -90) as the wake default
                    _xfer(h, 0x01, 0x01, 0x00, bytes([0]), wait=0.8)
                    time.sleep(1.2)
                    _move_abs(h, 1, home[0]); time.sleep(0.4)
                    _move_abs(h, 2, home[1])
                    end = time.time() + 5.0
                    while time.time() < end:
                        self._query_pos(h)
                        if abs(self._pan - home[0]) < 0.8 and abs(self._tilt - home[1]) < 0.8:
                            break
                        time.sleep(0.3)
                    # SET_MOTOR_POWER_ON_DEFAULT_POS_MODE(1): capture the current
                    # position as the camera's power-on/wake default
                    _xfer(h, 0x03, 0x01, 0x13, bytes([1]), wait=0.6)
                    last_query = time.time()
                except Exception: pass
                continue

            if self._reverse_target:
                self._reverse_target = False
                f = 1 if self._flip_on else 0
                m = 1 if self._mirror_on else 0
                try:
                    # SET_REVERSE_STA: ReverseType 1 = horizontal, 2 = vertical
                    # flip (180°) reverses both axes, mirror reverses horizontal
                    _xfer(h, 0x04, 0x00, 0x08, bytes([1, f ^ m]), wait=0.4)
                    _xfer(h, 0x04, 0x00, 0x08, bytes([2, f]), wait=0.4)
                except Exception: pass
                continue

            jx = self._jx; jy = self._jy; now = time.time()
            if self._dragging and math.hypot(jx, jy) > JS_DEAD:
                inv = -1.0 if self._invert_on else 1.0
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
