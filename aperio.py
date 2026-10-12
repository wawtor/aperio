#!/usr/bin/env python3
"""
aperio.py -- Aperio camera helper app.

Drag the on-screen joystick to aim the camera in real-time, or type
coordinates directly, then save it as the startup position. Settings apply
as soon as they are toggled: the Aperio daemon (aperio.exe) watches the
config files and runs silently in the background.

Requirements: pip install hidapi pillow
"""
import os, sys, math, struct, threading, time, zlib, base64
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

try:
    import hid
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
except ImportError as _e:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("Missing dependency",
        "%s\n\nRun:  pip install hidapi pillow\n\nthen relaunch Aperio." % _e)
    raise SystemExit(1)

HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
# Settings live in a per-user folder shared with the daemon: under Program
# Files the install folder is read-only for normal users. Versions before
# 0.3.0 kept them in HERE, which is still read as a fallback.
CONFIG_DIR = (os.path.join(os.environ["LOCALAPPDATA"], "Aperio")
              if os.environ.get("LOCALAPPDATA") else HERE)

# attr -> (state file, label, default, description). Single source for loading,
# saving and the settings rows; the daemon reads the same files.
TOGGLES = {
    "_track_on":   ("last_track.state",      "AI tracking",      True,  "Follows you while the camera is in use"),
    "_privacy_on": ("auto_privacy.state",    "Auto privacy",     True,  "Parks the lens when no app is using it"),
    "_flip_on":    ("image_flip.state",      "Flip image",       False, "Rotates 180° for upside-down mounting"),
    "_mirror_on":  ("image_mirror.state",    "Mirror image",     False, "Mirrors the video horizontally"),
    "_invert_on":  ("joystick_invert.state", "Invert joystick",  False, "Reverses the drag direction"),
    "_api_on":     ("api_server.state",      "Local API server", False, "Control the camera from your apps"),
}

# Camera HID identifiers (EMEET Pixy / Piko series)
CAM_VID = 0x328F
CAM_PID = 0x00C0
CAM_RID = 0x09   # vendor report ID

# Local API server port (must match API_PORT in the daemon)
API_PORT = 4750

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


# ---- theme: macOS colours, following the system light/dark setting ----

class _Theme(dict):
    __getattr__ = dict.__getitem__

DARK = _Theme(
    dark=True, bg="#1e1e1e", card="#2a2a2c", card_edge="#353538", sep="#3a3a3d",
    text="#ececee", text2="#98989d", text3="#6c6c71", text_off="#5c5c60",
    accent="#0a84ff", accent_hover="#2a94ff", accent_press="#0070e0", on_accent="#ffffff",
    accent_off="#3a3a3d", on_accent_off="#7c7c80", link="#3d9bff",
    ctl="#4a4a4d", ctl_hover="#545458", ctl_press="#5f5f63", ctl_edge="#58585c",
    field="#1f1f21", field_edge="#434346", sw_off="#3d3d40", sw_knob="#ffffff",
    sw_knob_edge="#c8c8cc", ok="#30d158", bad="#ff6961",
    well_top="#141416", well_bot="#1d1d20", well_edge="#3a3a3e", well_line="#2a2a2e",
    well_mark="#4c4c51", well_shade=170,
    glass_top=(205, 225, 255, 51), glass_bot=(120, 170, 255, 18), glass_shadow=0.55,
    glass_edge=None,
)
LIGHT = _Theme(
    dark=False, bg="#ececec", card="#ffffff", card_edge="#dcdcdf", sep="#e3e3e6",
    text="#1d1d1f", text2="#6e6e73", text3="#8e8e93", text_off="#b0b0b5",
    accent="#007aff", accent_hover="#1a87ff", accent_press="#0062cc", on_accent="#ffffff",
    accent_off="#d1d1d6", on_accent_off="#ffffff", link="#0066d6",
    ctl="#ffffff", ctl_hover="#f7f7f7", ctl_press="#ececee", ctl_edge="#c8c8cc",
    field="#ffffff", field_edge="#c8c8cc", sw_off="#e3e3e6", sw_knob="#ffffff",
    sw_knob_edge="#d4d4d8", ok="#248a3d", bad="#d70015",
    well_top="#dcdce1", well_bot="#eaeaee", well_edge="#cfcfd4", well_line="#d6d6db",
    well_mark="#a8a8ae", well_shade=60,
    glass_top=(255, 255, 255, 150), glass_bot=(225, 236, 255, 80), glass_shadow=0.28,
    glass_edge=(0, 0, 0, 38),
)
T = DARK        # active theme, picked by _init_style()
BORDER = 1      # hairline width in px, scaled by _init_style()
SS = 4          # supersampling factor for the Pillow-rendered joystick

class F:
    """Fonts (sized in px), filled in by _init_style()."""

def _reg_dword(path, name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None

def _prefers_light():
    return _reg_dword(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                      "AppsUseLightTheme") == 1

def _init_style():
    global T, BORDER
    T = LIGHT if _prefers_light() else DARK
    T.focus = _mix(T.accent, T.bg, 0.45)        # focus ring, accent at ~50%
    T.bad_ring = _mix(T.bad, T.bg, 0.45)
    BORDER = max(1, int(SCALE))
    fams = set(tkfont.families())
    pick = lambda *names: next((n for n in names if n in fams), names[-1])
    text = pick("Segoe UI Variable Text", "Segoe UI")
    semi = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold")
    disp = pick("Segoe UI Variable Display Semib", "Segoe UI Variable Display Semibold",
                "Segoe UI Semibold")
    F.title   = (disp, -S(20))
    F.strong  = (semi, -S(13))
    F.body    = (text, -S(13))
    F.caption = (text, -S(11))
    F.mono    = (pick("Cascadia Mono", "Consolas"), -S(12))

def _rgb(c):
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)

def _mix(a, b, t):
    a, b = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _ease_out(u):
    return 1 - (1 - u) ** 3

def _ease_out_back(u, k=1.25):
    u -= 1
    return 1 + (k + 1) * u ** 3 + k * u ** 2

def _deg(v):
    if abs(v) < 0.05:
        return "0.0°"
    return ("%+.1f°" % v).replace("-", "−")


# ---- anti-aliased shapes ----
# Tk's canvas draws jagged circles and arcs on Windows, so every rounded
# element is rasterised here into a small RGBA PNG instead.

_IMG = {}

def _shape(w, h, r, layers, band=None):
    """PhotoImage of a w x h rounded rect with corner radius r (a circle when
    w == h == 2r). layers: ((inset, colour, ring_width), ...) painted back to
    front; ring_width 0 fills. band: (rows, colour) repaints the bottom rows of
    the outline, like a text box underline."""
    key = (w, h, r, layers, band)
    img = _IMG.get(key)
    if img is not None:
        return img
    deep = max(k + rw for k, _c, rw in layers) + 1.0

    # Every layer is an inset of the same outline, so a pixel's colour depends
    # only on its signed distance to it: tabulate that every 1/16 px.
    def table(layers):
        out = []
        for i in range(int((deep + 0.5) * 16) + 2):
            s = i / 16.0 - deep
            a = 0.0; rgb = (0.0, 0.0, 0.0)
            for k, col, rw in layers:
                cov = min(1.0, max(0.0, 0.5 - s - k))
                if rw:
                    cov -= min(1.0, max(0.0, 0.5 - s - k - rw))
                if cov > 0:
                    na = cov + a * (1 - cov)
                    rgb = tuple((c * cov + p * a * (1 - cov)) / na for c, p in zip(_rgb(col), rgb))
                    a = na
            out.append(bytes((round(rgb[0]), round(rgb[1]), round(rgb[2]), round(a * 255))))
        return out

    clear = bytes(4)
    def row(y, cols):
        # top-left quadrant only; the shape is mirrored into the other three
        py = y + 0.5
        px_ = []
        for x in range((w + 1) // 2):
            px = x + 0.5
            s = math.hypot(r - px, r - py) - r if (px < r and py < r) else -min(px, py)
            px_.append(clear if s >= 0.5 else cols[max(0, int((s + deep) * 16 + 0.5))])
        return b"".join(px_ + px_[:w // 2][::-1])

    cols = table(layers)
    top = [row(y, cols) for y in range((h + 1) // 2)]
    rows = top + top[:h // 2][::-1]
    if band:
        bcols = table(((0, band[1], 0),))
        for y in range(band[0]):
            rows[h - 1 - y] = row(y, bcols)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + r_ for r_ in rows)))
           + chunk(b"IEND", b""))
    img = _IMG[key] = tk.PhotoImage(data=base64.b64encode(png))
    return img


# ---- joystick rendering (Pillow: blur and translucency for the glass puck) ----

def _ss(v):
    """Logical px -> supersampled device px."""
    return int(round(v * SCALE * SS))

def _pmask(size, box, radius, fill=255):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([int(v) for v in box], int(radius), fill=fill)
    return m

def _circ(c, r, dx=0, dy=0):
    return (c - r + dx, c - r + dy, c + r + dx, c + r + dy)

def _player(im, color, mask):
    """Composite a flat colour through `mask` onto `im`."""
    if len(color) == 4 and color[3] != 255:
        a = color[3]
        mask = mask.point(lambda v: v * a // 255)
    c = Image.new("RGBA", im.size, color[:3] + (0,))
    c.putalpha(mask)
    return Image.alpha_composite(im, c)

def _pgradient(im, box, radius, top, bot):
    """Composite a vertical-gradient rounded rect onto `im`."""
    x0, y0, x1, y1 = [int(v) for v in box]
    h = max(2, y1 - y0)
    g = Image.new("RGBA", (1, h))
    g.putdata([tuple(int(top[i] + (bot[i] - top[i]) * y / (h - 1)) for i in range(4))
               for y in range(h)])
    full = Image.new("RGBA", im.size, top[:3] + (0,))
    full.paste(g.resize((max(1, x1 - x0), h)), (x0, y0))
    full.putalpha(ImageChops.multiply(full.getchannel("A"), _pmask(im.size, box, radius)))
    return Image.alpha_composite(im, full)

def _well_img(R, M, active):
    """Recessed circular joystick well, R = radius, M = margin for the glow."""
    D = 2 * (R + M)
    im = Image.new("RGBA", (D * SS, D * SS), (0, 0, 0, 0))
    c, Rs = (R + M) * SS, R * SS
    if active:
        glow = _pmask(im.size, _circ(c, Rs + _ss(1)), Rs).filter(ImageFilter.GaussianBlur(_ss(5)))
        im = _player(im, _rgb(T.accent) + (140,), glow)
    b = _ss(1.5) if active else BORDER * SS
    im = _player(im, _rgb(T.accent if active else T.well_edge) + (255,),
                 _pmask(im.size, _circ(c, Rs), Rs))
    ri = Rs - b
    im = _pgradient(im, _circ(c, ri), ri, _rgb(T.well_top) + (255,), _rgb(T.well_bot) + (255,))

    # inner shadow along the top edge
    inner = _pmask(im.size, _circ(c, ri), ri)
    hole = _pmask(im.size, _circ(c, ri, dy=_ss(6)), ri)
    sh = ImageChops.subtract(inner, hole).filter(ImageFilter.GaussianBlur(_ss(7)))
    im = _player(im, (0, 0, 0, T.well_shade), ImageChops.multiply(sh, inner))

    # guides
    g = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    lw = BORDER * SS
    line = _rgb(T.well_line) + (255,)
    d.ellipse(_circ(c, int(Rs * 0.55)), outline=line, width=lw)
    arm = Rs - _ss(14)
    d.line([(c - arm, c), (c + arm, c)], fill=line, width=lw)
    d.line([(c, c - arm), (c, c + arm)], fill=line, width=lw)
    ch, w = Rs - _ss(14), _ss(4)
    mark = _rgb(_mix(T.well_mark, T.accent, 0.7) if active else T.well_mark) + (255,)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        tx, ty = c + dx * ch, c + dy * ch
        px, py = -dy, dx
        d.polygon([(tx + dx * w, ty + dy * w),
                   (tx + px * w - dx * _ss(1), ty + py * w - dy * _ss(1)),
                   (tx - px * w - dx * _ss(1), ty - py * w - dy * _ss(1))], fill=mark)
    d.ellipse(_circ(c, _ss(2.5)), fill=line)
    im = Image.alpha_composite(im, g)
    return im.resize((D, D), Image.LANCZOS)

def _puck_img(P, M, k):
    """Liquid-glass puck. P = radius, M = margin, k = press amount 0..1."""
    D = 2 * (P + M)
    im = Image.new("RGBA", (D * SS, D * SS), (0, 0, 0, 0))
    c = (P + M) * SS
    r = P * SS * (1 + 0.08 * k)
    disc = _pmask(im.size, _circ(c, r), r)

    # soft drop shadow, kept outside the glass so the lens stays clear
    sh = _pmask(im.size, _circ(c, r, dy=_ss(3 + 2 * k)), r, int(255 * T.glass_shadow))
    sh = ImageChops.subtract(sh.filter(ImageFilter.GaussianBlur(_ss(6 + 2 * k))), disc)
    im = _player(im, (0, 0, 0), sh)
    if k > 0:
        glow = _pmask(im.size, _circ(c, r + _ss(2)), r).filter(ImageFilter.GaussianBlur(_ss(7)))
        im = _player(im, _rgb(T.accent) + (int(128 * k),), ImageChops.subtract(glow, disc))

    # glass body, tinting toward the accent while pressed
    top, bot = T.glass_top, T.glass_bot
    acc = _rgb(T.accent)
    bot = tuple(int(bot[i] + (acc[i] - bot[i]) * 0.25 * k) for i in range(3)) + (int(bot[3] + 30 * k),)
    im = _pgradient(im, _circ(c, r), r, top, bot)

    # refraction band brightening toward the rim
    band = ImageChops.subtract(disc, _pmask(im.size, _circ(c, r * 0.70), r * 0.70))
    band = ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(_ss(4))), disc)
    im = _player(im, (255, 255, 255, 46), band)

    # top-left specular highlight
    spec = Image.new("L", im.size, 0)
    ImageDraw.Draw(spec).ellipse([int(c - 0.62 * r), int(c - 0.80 * r),
                                  int(c + 0.18 * r), int(c - 0.30 * r)], fill=255)
    spec = ImageChops.multiply(spec.filter(ImageFilter.GaussianBlur(r * 0.10)), disc)
    im = _player(im, (255, 255, 255, 120), spec)

    # bottom caustic
    cr = r * 0.86
    caus = ImageChops.subtract(_pmask(im.size, _circ(c, cr, dy=r * 0.08), cr),
                               _pmask(im.size, _circ(c, cr, dy=-r * 0.02), cr))
    caus = ImageChops.multiply(caus.filter(ImageFilter.GaussianBlur(_ss(2))), disc)
    im = _player(im, (200, 225, 255, 90), caus)

    # rim: specular stroke, brightest at top-left with a weaker bounce at bottom-right
    rw = max(SS, _ss(1.4))
    ring = ImageChops.subtract(disc, _pmask(im.size, _circ(c, r - rw), r - rw))
    if T.glass_edge:
        im = _player(im, T.glass_edge, ring)
    ang = Image.new("L", im.size, 0)
    d = ImageDraw.Draw(ang)
    tl, br = math.radians(225), math.radians(45)
    for a in range(0, 360, 2):
        th = math.radians(a + 1)
        v = (0.22 + 0.78 * max(0.0, math.cos(th - tl)) ** 2
                  + 0.40 * max(0.0, math.cos(th - br)) ** 3)
        d.pieslice([int(v_) for v_ in _circ(c, r + SS)], a, a + 3, fill=int(255 * min(1.0, v)))
    im = _player(im, (255, 255, 255), ImageChops.multiply(ang, ring))
    return im.resize((D, D), Image.LANCZOS)


# ---- window chrome ----

def _set_icon(win):
    try:
        win.iconbitmap(os.path.join(HERE, "aperio.ico"))
    except Exception:
        pass

def _style_titlebar(win):
    """Match the title bar to the theme. On Windows 11 the caption is also
    painted in the window colour, unless the user wants accent title bars."""
    try:
        from ctypes import windll, byref, c_int, sizeof
        win.update_idletasks()
        hwnd = windll.user32.GetParent(win.winfo_id())
        dwm = windll.dwmapi.DwmSetWindowAttribute
        dwm(hwnd, 20, byref(c_int(T.dark)), sizeof(c_int))       # DWMWA_USE_IMMERSIVE_DARK_MODE
        if _reg_dword(r"Software\Microsoft\Windows\DWM", "ColorPrevalence") != 1:
            r, g, b = _rgb(T.bg)
            dwm(hwnd, 35, byref(c_int(r | g << 8 | b << 16)), sizeof(c_int))  # DWMWA_CAPTION_COLOR
    except Exception:
        pass


# ---- widgets ----

def _panel(parent, bg):
    return tk.Frame(parent, bg=bg, bd=0, highlightthickness=0)

def _label(parent, text, font, fg, bg, **kw):
    return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, bd=0, padx=0, pady=0,
                    highlightthickness=0, **kw)

def _font(spec, **kw):
    return tkfont.Font(family=spec[0], size=spec[1], **kw)

def _set_text(label, text):
    if label.cget("text") != text:
        label.configure(text=text)

def _pointer_in(w):
    x, y = w.winfo_pointerxy()
    return (w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width()
            and w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height())


class _Card(tk.Frame):
    """Rounded card. Tk frames are square, so the outline is the outer frame's
    colour showing around `body`, and each corner is capped with an
    anti-aliased arc."""

    def __init__(self, parent, outside, radius=10):
        super().__init__(parent, bg=T.card_edge, bd=0, highlightthickness=0)
        self.body = _panel(self, T.card)
        self.body.pack(fill="both", expand=True, padx=BORDER, pady=BORDER)
        self._r = r = S(radius)
        self._caps = []
        for rx, ry, anchor in ((0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se")):
            c = tk.Canvas(self, width=r, height=r, bg=outside, bd=0, highlightthickness=0)
            c.place(relx=rx, rely=ry, anchor=anchor)
            c.create_image(-r * rx, -r * ry, anchor="nw", tags="cap")
            self._caps.append(c)
        self.fill(T.card)

    def fill(self, colour):
        self.body.configure(bg=colour)
        cap = _shape(2 * self._r, 2 * self._r, self._r,
                     ((0, T.card_edge, 0), (BORDER, colour, 0)))
        for c in self._caps:
            c.itemconfigure("cap", image=cap)


class _Switch(tk.Canvas):
    """macOS-style switch. Click or Space flips it; command(on) returns False
    to refuse the change."""

    def __init__(self, parent, on, command, bg):
        self._m = m = S(4)                                 # room for the focus ring
        self._size = w, h = S(38), S(22)
        super().__init__(parent, width=w + 2 * m, height=h + 2 * m, bg=bg, bd=0,
                         highlightthickness=0, takefocus=1)
        self._on, self._cmd, self._job = on, command, None
        self._pos = 1.0 if on else 0.0
        self._ring = self.create_image(0, 0, anchor="nw", state="hidden",
            image=_shape(w + 2 * m, h + 2 * m, h / 2 + m, ((0, T.focus, S(3)),)))
        self._track = self.create_image(m, m, anchor="nw")
        d = h - 2 * S(2)
        self._knob = self.create_image(0, 0, image=_shape(d, d, d / 2,
                                       ((0, T.sw_knob_edge, 0), (BORDER, T.sw_knob, 0))))
        self._paint()
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<space>", lambda _e: self.toggle())
        self.bind("<FocusIn>", lambda _e: self.itemconfigure(self._ring, state="normal"))
        self.bind("<FocusOut>", lambda _e: self.itemconfigure(self._ring, state="hidden"))

    def toggle(self):
        on = not self._on
        if self._cmd(on) is False:
            return
        self._on = on
        if self._job:
            self.after_cancel(self._job)
        self._slide(self._pos, 1.0 if on else 0.0, time.perf_counter())

    def _release(self, e):
        if 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self.toggle()

    def _slide(self, src, dst, t0, ms=240):
        u = min(1.0, (time.perf_counter() - t0) * 1000 / ms)
        self._pos = src + (dst - src) * _ease_out_back(u)
        self._paint()
        self._job = self.after(12, self._slide, src, dst, t0) if u < 1.0 else None

    def _paint(self):
        (w, h), m = self._size, self._m
        p = round(max(0.0, min(1.0, self._pos)) * 10) / 10     # colour steps, for the cache
        self.itemconfigure(self._track, image=_shape(w, h, h / 2,
                           ((0, _mix(T.sw_off, T.accent, p), 0),)))
        self.coords(self._knob, m + h / 2 + (w - h) * self._pos, m + h / 2)


class _Button(tk.Canvas):
    """Capsule push button; accent=True for the primary action."""

    def __init__(self, parent, text, command, bg, accent=False, width=0):
        self._m = m = S(4)                                 # room for the focus ring
        w = width or _font(F.body).measure(text) + S(24)
        super().__init__(parent, width=w + 2 * m, height=S(30) + 2 * m, bg=bg, bd=0,
                         highlightthickness=0, takefocus=1)
        self._text, self._cmd, self._accent = text, command, accent
        self._state, self._enabled, self._flash_job = "rest", True, None
        self._bg = self.create_image(m, m, anchor="nw")
        self._ring = self.create_image(0, 0, anchor="nw", state="hidden")
        self._label = self.create_text(0, 0, text=text, font=F.body)
        self.bind("<Configure>", lambda _e: self._paint())
        self.bind("<Enter>", lambda _e: self._set("hover"))
        self.bind("<Leave>", lambda _e: self._set("rest"))
        self.bind("<ButtonPress-1>", lambda _e: self._set("press"))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<FocusIn>", lambda _e: self.itemconfigure(self._ring, state="normal"))
        self.bind("<FocusOut>", lambda _e: self.itemconfigure(self._ring, state="hidden"))
        self._paint()

    def invoke(self):
        if self._enabled:
            self._cmd()

    def set_enabled(self, on):
        if on != self._enabled:
            self._enabled = on
            self.configure(takefocus=int(on))
            self._paint()

    def flash(self, text, ms=1600):
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self.itemconfigure(self._label, text=text)
        self._flash_job = self.after(ms, lambda: self.itemconfigure(self._label, text=self._text))

    def _set(self, state):
        self._state = state
        self._paint()

    def _release(self, e):
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        self._set("hover" if inside else "rest")
        if inside:
            self.invoke()

    def _paint(self):
        m = self._m
        W = self.winfo_width() if self.winfo_width() > 1 else self.winfo_reqwidth()
        H = self.winfo_height() if self.winfo_height() > 1 else self.winfo_reqheight()
        w, h, st = W - 2 * m, H - 2 * m, self._state if self._enabled else "off"
        if self._accent:
            fill = {"rest": T.accent, "hover": T.accent_hover, "press": T.accent_press,
                    "off": T.accent_off}[st]
            layers = ((0, fill, 0),)
            fg = T.on_accent if self._enabled else T.on_accent_off
        else:
            fill = {"rest": T.ctl, "hover": T.ctl_hover, "press": T.ctl_press, "off": T.ctl}[st]
            layers = ((0, T.ctl_edge, 0), (BORDER, fill, 0))
            fg = T.text if self._enabled else T.text_off
        self.itemconfigure(self._bg, image=_shape(w, h, h / 2, layers))
        self.itemconfigure(self._ring, image=_shape(W, H, H / 2, ((0, T.focus, S(3)),)))
        self.itemconfigure(self._label, fill=fg)
        self.coords(self._label, W / 2, H / 2)


class _Field(tk.Canvas):
    """macOS text field: a flat tk.Entry on a rounded backdrop with a focus ring."""

    def __init__(self, parent, width, on_enter, bg):
        self._m = m = S(4)                                 # room for the focus ring
        self._size = w, h = S(width), S(28)
        super().__init__(parent, width=w + 2 * m, height=h + 2 * m, bg=bg, bd=0,
                         highlightthickness=0, cursor="xterm")
        self._state, self._err_job = "rest", None
        self._img = self.create_image(0, 0, anchor="nw")
        self.entry = e = tk.Entry(self, font=F.body, bd=0, relief="flat", highlightthickness=0,
                                  justify="right", bg=T.field, fg=T.text, insertbackground=T.text,
                                  insertwidth=max(1, S(1)), selectbackground=T.accent,
                                  selectforeground=T.on_accent)
        self.create_window(m + S(6), m + h // 2, window=e, anchor="w", width=w - S(12))
        e.bind("<Return>", on_enter)
        e.bind("<FocusIn>", lambda _e: self._set("focus"))
        e.bind("<FocusOut>", lambda _e: self._set("rest"))
        self.bind("<Button-1>", lambda _e: e.focus_set())
        self._set("rest")

    def get(self):
        return self.entry.get()

    def set(self, text):
        self.entry.delete(0, "end")
        self.entry.insert(0, text)

    def flash_error(self):
        if self._err_job:
            self.after_cancel(self._err_job)
        self._err_job = self.after(1500, self._clear_error)
        self._set(self._state)
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _clear_error(self):
        self._err_job = None
        self._set(self._state)

    def _set(self, state):
        self._state = state
        (w, h), m = self._size, self._m
        if self._err_job:
            ring, edge = T.bad_ring, T.bad
        elif state == "focus":
            ring, edge = T.focus, T.accent
        else:
            ring, edge = None, T.field_edge
        layers = ((m, edge, 0), (m + BORDER, T.field, 0))
        if ring:
            layers = ((m - S(3), ring, 0),) + layers
        self.itemconfigure(self._img, image=_shape(w + 2 * m, h + 2 * m, S(6) + m, layers))


class _Link(tk.Label):
    """Hyperlink-style text button."""

    def __init__(self, parent, text, command, bg):
        self._fonts = (_font(F.caption), _font(F.caption, underline=True))
        super().__init__(parent, text=text, font=self._fonts[0], fg=T.link, bg=bg, bd=0,
                         padx=0, pady=0, highlightthickness=0, takefocus=1)
        self._focused = False
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<space>", lambda _e: command())
        self.bind("<Return>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self.configure(font=self._fonts[1]))
        self.bind("<Leave>", lambda _e: self.configure(font=self._fonts[self._focused]))
        self.bind("<FocusIn>", lambda _e: self._focus(True))
        self.bind("<FocusOut>", lambda _e: self._focus(False))

    def _focus(self, on):
        self._focused = on
        self.configure(font=self._fonts[on])


class _SettingRow(tk.Frame):
    """Grouped-list row: title, description and a switch. Clicking anywhere
    on the row flips the switch."""

    def __init__(self, parent, title, desc, on, command, link=None):
        super().__init__(parent, bg=T.card, bd=0, highlightthickness=0)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        text = _panel(self, T.card)
        text.grid(row=0, column=0, sticky="w", padx=(S(14), S(12)), pady=S(8))
        t = _label(text, title, F.body, T.text, T.card)
        t.pack(anchor="w")
        line = _panel(text, T.card)
        line.pack(anchor="w", pady=(S(1), 0))
        d = _label(line, desc, F.caption, T.text2, T.card)
        d.pack(side="left")
        self.switch = _Switch(self, on, command, T.card)
        self.switch.grid(row=0, column=1, padx=(0, S(10)))
        for wdg in (self, text, line, t, d):
            wdg.bind("<ButtonRelease-1>", self._release)
        if link:
            _Link(line, link[0], link[1], T.card).pack(side="left", padx=(S(6), 0))

    def _release(self, _e):
        if _pointer_in(self):
            self.switch.toggle()


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

def _frame(b1, b2, b3, payload=b""):
    return (bytes([CAM_RID, b1, b2, b3, 0x00, len(payload), 0x00, len(payload)])
            + payload).ljust(32, b"\x00")

def _xfer(h, b1, b2, b3, payload=b"", wait=0.6):
    _drain(h, 0.03)
    h.write(_frame(b1, b2, b3, payload))
    for d in _drain(h, wait):
        if len(d) >= 8 and d[1] == b1 and d[2] == b2 and d[3] == b3:
            return d[8:8 + d[5]], d
    return None, None

def _move(h, axis, deg, index):
    h.write(_frame(0x63, 0x01, index, bytes([axis]) + struct.pack("<f", float(deg))))

def _move_rel(h, axis, deg):
    _move(h, axis, deg, 0x19)   # MOVE_MOTOR_REL

def _move_abs(h, axis, deg):
    _move(h, axis, deg, 0x00)   # SET_MOTOR_POS


# ---- config helpers ----

def _read_config(name):
    """A config file's contents, or None. The per-user folder wins over the
    install folder, where versions before 0.3.0 kept it."""
    for d in (CONFIG_DIR, HERE):
        try:
            with open(os.path.join(d, name)) as f:
                return f.read()
        except OSError:
            pass
    return None

def _write_config(name, text):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(os.path.join(CONFIG_DIR, name), "w") as f:
        f.write(text)

def _load_toggle(attr):
    try:
        return bool(int(_read_config(TOGGLES[attr][0]).strip()))
    except Exception:
        return TOGGLES[attr][2]

def _write_toggle(attr, on):
    _write_config(TOGGLES[attr][0], "1\n" if on else "0\n")

def _load_start_pos():
    """Saved (pan, tilt), or None if no startup position has been saved."""
    try:
        p = _read_config("start_pos.txt").split()
        return float(p[0]), float(p[1])
    except Exception:
        return None

def _save_config(pan, tilt, values):
    _write_config("start_pos.txt", "%.2f %.2f\n" % (pan, tilt))
    for attr, on in values.items():
        _write_toggle(attr, on)


# ---- GUI ----

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Aperio")
        self.resizable(False, False)
        _set_icon(self)
        _init_style()

        self._jx = 0.0; self._jy = 0.0; self._dragging = False
        self._goto_target = None
        self._reverse_target = False
        self._home_target = None
        self._pan = 0.0; self._tilt = 0.0
        self._stop = False; self._hid = None
        # set by the I/O thread: camera answering, and _pan/_tilt are real readings
        self._online = True; self._pos_known = False

        for attr in TOGGLES:
            setattr(self, attr, _load_toggle(attr))

        self._jobs = {}
        self._knob_hover = False
        self._status_key = None

        self._build_ui()
        _style_titlebar(self)

        try:
            self._hid = _open_cam()
        except Exception as e:
            messagebox.showerror("Camera not found",
                "Aperio couldn't find the camera.\n\n"
                "Make sure it's plugged in, then open Aperio again.\n\n%s" % e)
            self.destroy()
            return

        threading.Thread(target=self._io_worker, daemon=True).start()
        self.bind_all("<Button-1>", self._drop_focus, add="+")
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----

    def _build_ui(self):
        self.configure(bg=T.bg)
        pad = S(20)

        head = _panel(self, T.bg)
        head.pack(fill="x", padx=pad, pady=(S(16), 0))
        _label(head, "EMEET Pixy", F.title, T.text, T.bg).pack(side="left")
        self._status = _label(head, "", F.caption, T.text2, T.bg)
        self._status.pack(side="right")
        self._dot = tk.Canvas(head, width=S(8), height=S(8), bg=T.bg, bd=0, highlightthickness=0)
        self._dot.pack(side="right", padx=(0, S(8)))
        self._dot_img = self._dot.create_image(0, 0, anchor="nw")

        body = _panel(self, T.bg)
        body.pack(fill="both", expand=True, padx=pad, pady=(S(18), pad))
        body.grid_columnconfigure(0, minsize=S(300))
        body.grid_columnconfigure(1, minsize=S(364))
        self._section(body, 0, "Startup position", "Where the camera points when it wakes up")
        self._section(body, 1, "Settings", "Changes apply immediately")
        self._build_position_card(body).grid(row=1, column=0, sticky="nsew")
        self._build_settings(body).grid(row=1, column=1, sticky="nsew", padx=(S(16), 0))

    def _section(self, parent, col, title, caption):
        f = _panel(parent, T.bg)
        f.grid(row=0, column=col, sticky="w", padx=(S(16) if col else 0, 0), pady=(0, S(10)))
        _label(f, title, F.strong, T.text, T.bg).pack(anchor="w")
        _label(f, caption, F.caption, T.text2, T.bg).pack(anchor="w")

    def _build_position_card(self, parent):
        card = _Card(parent, T.bg)
        b = card.body
        p, m = S(16), S(4)
        b.grid_columnconfigure(0, weight=1)
        b.grid_rowconfigure(0, weight=1)
        self._build_pad(b).grid(row=0, column=0, pady=(S(20), 0))
        _label(b, "Drag to aim", F.caption, T.text3, T.card).grid(row=1, column=0, pady=(S(8), 0))
        self._build_readouts(b).grid(row=2, column=0, sticky="ew", padx=(p, p - m), pady=(S(18), 0))
        self._save_btn = _Button(b, "Save current position", self._save, T.card, accent=True)
        self._save_btn.grid(row=3, column=0, sticky="ew", padx=p - m, pady=(S(14) - m, p - m))
        return card

    def _build_readouts(self, parent):
        t = _panel(parent, T.card)
        t.grid_columnconfigure(0, weight=1)
        num = dict(sticky="e", padx=(S(8), S(10)))
        for col, text in ((1, "Pan"), (2, "Tilt")):
            _label(t, text, F.caption, T.text3, T.card).grid(row=0, column=col, pady=(0, S(2)), **num)

        def row(r, name):
            _label(t, name, F.body, T.text2, T.card).grid(row=r, column=0, sticky="w")
            cells = []
            for col in (1, 2):
                c = _label(t, "—", F.body, T.text, T.card)
                c.grid(row=r, column=col, pady=S(3), **num)
                cells.append(c)
            return cells
        self._cur_cells = row(1, "Current")
        self._saved_cells = row(2, "Saved")
        self._show_saved(_load_start_pos())

        m = S(4)                    # control margin, room for focus rings
        _label(t, "Go to", F.body, T.text2, T.card).grid(row=3, column=0, sticky="w", pady=(S(8), 0))
        self._pan_field = _Field(t, 72, self._goto_coords, T.card)
        self._pan_field.grid(row=3, column=1, sticky="e", padx=(S(8) - m, 0), pady=(S(8) - m, 0))
        self._tilt_field = _Field(t, 72, self._goto_coords, T.card)
        self._tilt_field.grid(row=3, column=2, sticky="e", padx=(S(8) - m, 0), pady=(S(8) - m, 0))
        self._go_btn = _Button(t, "Go", self._goto_coords, T.card)
        self._go_btn.grid(row=3, column=3, padx=(S(8) - m, 0), pady=(S(8) - m, 0))
        return t

    def _build_settings(self, parent):
        card = _Card(parent, T.bg)
        b = card.body
        b.grid_columnconfigure(0, weight=1)
        last = len(TOGGLES) - 1
        for i, attr in enumerate(TOGGLES):
            _file, label, _default, desc = TOGGLES[attr]
            link = ("View endpoints", self._show_api_help) if attr == "_api_on" else None
            if i:
                tk.Frame(b, bg=T.sep, height=BORDER, bd=0).grid(row=2 * i - 1, column=0,
                                                               sticky="ew", padx=(S(14), 0))
            row = _SettingRow(b, label, desc, getattr(self, attr),
                              lambda on, a=attr: self._set_toggle(a, on), link)
            row.grid(row=2 * i, column=0, sticky="nsew",
                     pady=(S(2) if i == 0 else 0, S(2) if i == last else 0))
            b.grid_rowconfigure(2 * i, weight=1, uniform="rows")
        return card

    def _build_pad(self, parent):
        self._jr  = S(84)                       # well radius
        self._jpk = S(22)                       # puck radius
        self._jtr = self._jr - self._jpk - S(6)
        wm, pm = S(10), S(12)                   # image margins for glow and shadow
        D = 2 * (self._jr + wm)
        c = D // 2
        cv = tk.Canvas(parent, width=D, height=D, bg=T.card, bd=0, highlightthickness=0)
        self._cv = cv
        self._jcx = self._jcy = c
        self._well_imgs = [ImageTk.PhotoImage(_well_img(self._jr, wm, a), master=cv)
                           for a in (False, True)]
        self._puck_imgs = [ImageTk.PhotoImage(_puck_img(self._jpk, pm, i / 4), master=cv)
                           for i in range(5)]
        self._well = cv.create_image(c, c, image=self._well_imgs[0])
        self._knob = cv.create_image(c, c, image=self._puck_imgs[0])
        self._px, self._py = c, c
        self._puck_k = 0.0
        cv.bind("<ButtonPress-1>",   self._jdown)
        cv.bind("<B1-Motion>",       self._jmove)
        cv.bind("<ButtonRelease-1>", self._jup)
        cv.bind("<Motion>",          self._jhover)
        cv.bind("<Leave>",           lambda _e: self._set_knob_hover(False))
        return cv

    def _paint_knob(self):
        # the glass puck swells slightly on hover and more while dragging
        target = 1.0 if self._dragging else 0.35 if self._knob_hover else 0.0
        k0 = self._puck_k
        def step(u):
            self._puck_k = k0 + (target - k0) * u
            self._cv.itemconfigure(self._knob, image=self._puck_imgs[round(self._puck_k * 4)])
        self._animate("puck", 160, step)

    def _set_knob_hover(self, on):
        if on != self._knob_hover:
            self._knob_hover = on
            self._paint_knob()

    def _animate(self, key, ms, step, ease=_ease_out):
        job = self._jobs.pop(key, None)
        if job:
            self.after_cancel(job)
        t0 = time.perf_counter()
        def tick():
            u = min(1.0, (time.perf_counter() - t0) * 1000.0 / ms)
            try:
                step(ease(u))
            except tk.TclError:
                return
            if u < 1.0:
                self._jobs[key] = self.after(12, tick)
            else:
                self._jobs.pop(key, None)
        tick()

    def _show_saved(self, pos):
        for cell, v in zip(self._saved_cells, pos or (None, None)):
            _set_text(cell, "—" if v is None else _deg(v))

    def _drop_focus(self, e):
        # clicking outside a text box takes the focus (and caret) off it
        w = e.widget
        if not isinstance(w, tk.Misc) or isinstance(w, (tk.Entry, _Field)):
            return
        try:
            f = self.focus_get()
        except (KeyError, tk.TclError):
            return
        if isinstance(f, tk.Entry) and f.winfo_toplevel() is w.winfo_toplevel():
            w.winfo_toplevel().focus_set()

    # ---- Joystick ----

    def _move_puck_to(self, nx, ny):
        self._cv.coords(self._knob, nx, ny)
        self._px, self._py = nx, ny

    def _jdown(self, e):
        cx, cy = self._jcx, self._jcy
        if math.hypot(e.x - cx, e.y - cy) > self._jr:
            return
        job = self._jobs.pop("spring", None)
        if job:
            self.after_cancel(job)
        self._dragging = True
        self._cv.itemconfigure(self._well, image=self._well_imgs[1])
        self._paint_knob()
        self._jset(e.x, e.y)

    def _jmove(self, e):
        if self._dragging:
            self._jset(e.x, e.y)

    def _jup(self, _):
        if not self._dragging:
            return
        self._dragging = False
        self._jx = 0.0; self._jy = 0.0
        self._cv.itemconfigure(self._well, image=self._well_imgs[0])
        self._paint_knob()
        sx, sy, cx, cy = self._px, self._py, self._jcx, self._jcy
        self._animate("spring", 420,
                      lambda u: self._move_puck_to(sx + (cx - sx) * u, sy + (cy - sy) * u),
                      _ease_out_back)

    def _jhover(self, e):
        if self._dragging:
            return
        self._set_knob_hover(math.hypot(e.x - self._px, e.y - self._py) <= self._jpk)

    def _jset(self, mx, my):
        cx, cy = self._jcx, self._jcy
        dx = mx - cx; dy = my - cy; d = math.hypot(dx, dy)
        if d > self._jtr:
            dx = dx / d * self._jtr; dy = dy / d * self._jtr
        self._jx = dx / self._jtr; self._jy = dy / self._jtr
        self._move_puck_to(cx + dx, cy + dy)

    # ---- Coordinate entry ----

    def _goto_coords(self, _=None):
        if not (self._online and self._pos_known):
            return
        target, bad = [], []
        for field, cur, lim in ((self._pan_field, self._pan, 150.0),
                                (self._tilt_field, self._tilt, 90.0)):
            s = field.get().strip().replace("−", "-").rstrip("°").strip()
            if not s:                       # blank keeps that axis where it is
                target.append(cur)
                continue
            try:
                v = max(-lim, min(lim, float(s)))
            except ValueError:
                bad.append(field)
                continue
            field.set("%g" % v)
            target.append(v)
        for field in reversed(bad):
            field.flash_error()
        if not bad:
            self._goto_target = tuple(target)

    # ---- Refresh ----

    def _refresh(self):
        if self._stop:
            return
        live = self._online and self._pos_known
        for cell, v in zip(self._cur_cells, (self._pan, self._tilt)):
            _set_text(cell, _deg(v) if live else "—")
        self._set_status("ok" if live else "wait" if self._online else "off")
        self._save_btn.set_enabled(live)
        self._go_btn.set_enabled(live)
        self.after(250, self._refresh)

    def _set_status(self, key):
        if key == self._status_key:
            return
        self._status_key = key
        text, colour = {"wait": ("Connecting…", T.text3),
                        "ok":   ("Connected", T.ok),
                        "off":  ("Disconnected — reconnect the camera and reopen Aperio", T.bad)}[key]
        self._status.configure(text=text)
        self._dot.itemconfigure(self._dot_img, image=_shape(S(8), S(8), S(4), ((0, colour, 0),)))

    # ---- Save ----

    def _write_failed(self, e):
        messagebox.showerror("Couldn't save settings",
            "Aperio couldn't write its settings to:\n%s\n\n%s" % (CONFIG_DIR, getattr(e, "strerror", None) or e),
            parent=self)

    def _set_toggle(self, attr, on):
        # persist immediately -- the daemon watches the config dir
        try:
            _write_toggle(attr, on)
        except OSError as e:
            self._write_failed(e)
            return False
        setattr(self, attr, on)
        if attr in ("_flip_on", "_mirror_on"):
            self._reverse_target = True
        return True

    def _save(self):
        pan, tilt = self._pan, self._tilt
        try:
            _save_config(pan, tilt, {attr: getattr(self, attr) for attr in TOGGLES})
        except OSError as e:
            self._write_failed(e)
            return
        self._home_target = (pan, tilt)
        self._show_saved((pan, tilt))
        self._save_btn.flash("Saved")

    # ---- API help ----

    def _show_api_help(self):
        w = getattr(self, "_help_win", None)
        if w is not None and w.winfo_exists():
            w.lift(); w.focus_set()
            return
        self._help_win = w = tk.Toplevel(self)
        w.withdraw()
        w.title("Aperio — Local API")
        w.configure(bg=T.bg)
        w.resizable(False, False)
        w.transient(self)
        _set_icon(w)

        pad, m = S(24), S(4)
        f = _panel(w, T.bg)
        f.pack(fill="both", expand=True, padx=(pad, pad - m), pady=(S(20), pad - m))
        _label(f, "Local API", F.title, T.text, T.bg).pack(anchor="w")
        _label(f, "While the Local API server setting is on, the Aperio daemon accepts HTTP "
                  "requests on http://127.0.0.1:%d. Only apps on this PC can reach it." % API_PORT,
               F.body, T.text2, T.bg, justify="left", anchor="w", wraplength=S(540)).pack(
               fill="x", pady=(S(6), S(16)))

        endpoints = (
            ("GET",  "/",                                      "Endpoint index"),
            ("GET",  "/status",                                "Daemon and camera info"),
            ("POST", "/move?pan=X&tilt=Y",                     "Absolute move, in degrees"),
            ("POST", "/move_rel?pan=X&tilt=Y",                 "Relative move, in degrees"),
            ("POST", "/mode?value=follow|standard|privacy",    "Set the device mode"),
            ("POST", "/flip?value=on|off",                     "Flip the image 180°"),
            ("POST", "/mirror?value=on|off",                   "Mirror the image"),
            ("POST", "/home",                                  "Go to the startup position"),
            ("POST", "/shutdown",                              "Turn the API server off"),
        )
        card = _Card(f, T.bg)
        card.pack(fill="x", padx=(0, m))
        g = card.body
        g.grid_columnconfigure(2, weight=1)
        last = len(endpoints) - 1
        for r, (method, path, desc) in enumerate(endpoints):
            py = (S(12) if r == 0 else S(3), S(12) if r == last else S(3))
            _label(g, method, F.mono, T.text3, T.card).grid(row=r, column=0, sticky="w",
                                                            padx=(S(16), S(12)), pady=py)
            _label(g, path, F.mono, T.text, T.card).grid(row=r, column=1, sticky="w", pady=py)
            _label(g, desc, F.caption, T.text2, T.card).grid(row=r, column=2, sticky="w",
                                                             padx=(S(24), S(16)), pady=py)
        _label(f, "Pan is clamped to ±150°, tilt to ±90°.", F.caption, T.text2,
               T.bg).pack(anchor="w", pady=(S(8), 0))

        _label(f, "Example", F.strong, T.text, T.bg).pack(anchor="w", pady=(S(16), S(6)))
        ex = _panel(f, T.bg)
        ex.pack(fill="x")
        cmd = 'curl -X POST "http://127.0.0.1:%d/move?pan=30&tilt=-10"' % API_PORT
        code = _Card(ex, T.bg)
        code.pack(side="left", fill="x", expand=True)
        _label(code.body, cmd, F.mono, T.text, T.card).pack(anchor="w", padx=S(12), pady=S(8))
        def copy():
            w.clipboard_clear(); w.clipboard_append(cmd)
            copy_btn.flash("Copied")
        copy_btn = _Button(ex, "Copy", copy, T.bg, width=S(72))
        copy_btn.pack(side="left", padx=(S(8) - m, 0))

        foot = _panel(f, T.bg)
        foot.pack(fill="x", pady=(S(20) - m, 0))
        _Button(foot, "Close", w.destroy, T.bg, width=S(96)).pack(side="right")
        w.bind("<Escape>", lambda _e: w.destroy())

        # centre over the main window, then show
        w.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - w.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + S(48)
        w.geometry("+%d+%d" % (max(0, x), max(0, y)))
        w.deiconify()
        _style_titlebar(w)
        w.focus_set()

    def _on_close(self):
        self._stop = True
        self.destroy()

    # ---- I/O thread ----

    def _query_pos(self, h):
        try:
            p, _ = _xfer(h, 0x63, 0x01, 0x01, bytes([1]), wait=0.15)
            t, _ = _xfer(h, 0x63, 0x01, 0x01, bytes([2]), wait=0.15)
        except Exception:
            self._online = False      # read/write errors: the camera went away
            raise
        if p and len(p) >= 5: self._pan  = struct.unpack("<f", p[1:5])[0]
        if t and len(t) >= 5: self._tilt = struct.unpack("<f", t[1:5])[0]
        if p and t:
            self._online = self._pos_known = True

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
