# Aperio

Free your AI gimbal webcam from its vendor software.

Aperio is a lightweight background daemon and GUI helper that replaces the
required vendor companion app for AI motorized-gimbal webcams. It automatically
aims your camera at a saved startup position and enables AI tracking whenever
an application opens the camera — with no vendor software running, no
telemetry, and no background bloat.

---

## Philosophy

AI gimbal webcams (EMEET, OBSBOT, Insta360 Link, etc.) require the
manufacturer's companion app to be installed and running before the camera's
pan/tilt/tracking features work. These apps are resource-heavy, auto-update
without asking, and run background processes at all times.

Aperio talks directly to the camera hardware over the vendor HID command
channel, replacing all of that with a single silent background process and
a simple one-time setup GUI.

---

## Compatibility

### Confirmed working

| Camera | Notes |
|---|---|
| EMEET Pixy | Full support — AI tracking, auto privacy, startup position |

### Untested — likely similar protocol

These cameras use motorized gimbals and companion apps with a similar design.
They may respond to the same HID command set but have not been tested.
Run `research/diag.py` to check compatibility on your hardware.

| Camera | |
|---|---|
| OBSBOT Tiny 2 | |
| OBSBOT Tiny 2 Lite | |
| OBSBOT Tiny 4K | |
| OBSBOT Meet 4K | |
| Insta360 Link | |
| Insta360 Link 2 | |

> If you get it working on any of the above, open a PR with the output of
> `research/diag.py` and the camera model.

---

## Requirements

- Windows 10/11
- Python 3.10+ — only needed for `aperio.py` (the setup GUI)
- `hidapi` Python package: `pip install hidapi`
- Rust toolchain — only needed if building `aperio.exe` from source

---

## Setup

### 1. Get the daemon

**Option A — download a release (recommended)**

Grab the latest `aperio-vX.X.X-windows-x64.zip` from the
[Releases](https://github.com/wawtor/aperio/releases) page and extract
everything into a folder (e.g. `C:\aperio\`).

**Option B — build from source**

```
cd daemon
cargo build --release
copy target\release\aperio.exe ..\
```

### 2. Configure your startup position

Run `aperio.py` once to aim the camera and save your settings:

```
python aperio.py
```

- **Drag the joystick** to pan/tilt the camera to your preferred position
- Or **type coordinates** directly (Pan ±150°, Tilt ±90°)
- Toggle **AI Tracking** — camera follows your face when in use
- Toggle **Auto Privacy** — lens parks down when no app is using the camera
- Toggle **Local API server** — control the camera from your own apps (off by default)
- Click **Save**, then close

### 3. Install the daemon

```
install.bat
```

This registers `aperio.exe` as a Windows scheduled task that starts at logon
and runs silently in the background. It also puts the `aperio` command on your
PATH, so from any new PowerShell or cmd window you can reopen the setup GUI
with just:

```
aperio
```

(The Windows installer from the Releases page does the same.)
To remove everything run `uninstall.bat`.

---

## How It Works

The daemon watches the Windows `CapabilityAccessManager` webcam consent
registry for changes using zero-overhead event notification (no polling).

- **App opens camera** → daemon aims gimbal to saved position, enables Follow mode
- **All apps close camera** → daemon parks lens to Privacy (if Auto Privacy is on)

Nothing is sent to the camera while idle — vendor HID commands reset the
camera's privacy timer, so any keepalive would prevent it from ever sleeping.

---

## Local API

Aperio can expose a small HTTP API so your own applications and scripts can
drive the camera. It is **off by default** — enable the *Local API server*
toggle in the setup GUI (`aperio`) and save. The daemon then listens on
`127.0.0.1:4750` (loopback only; nothing is reachable from the network).

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Endpoint index (JSON) |
| `/status` | GET | Daemon info, camera presence, saved settings (JSON) |
| `/move?pan=X&tilt=Y` | POST | Absolute move in degrees (either param optional) |
| `/move_rel?pan=X&tilt=Y` | POST | Relative move in degrees |
| `/mode?value=follow\|standard\|privacy` | POST | Set device mode |
| `/home` | POST | Return to the saved startup position |
| `/shutdown` | POST | Turn the API server off (persists until re-enabled) |

Pan is clamped to ±150°, tilt to ±90°. Examples:

```
curl http://127.0.0.1:4750/status
curl -X POST "http://127.0.0.1:4750/move?pan=30&tilt=-10"
curl -X POST "http://127.0.0.1:4750/mode?value=follow"
curl -X POST http://127.0.0.1:4750/home
```

All settings toggles apply immediately — the daemon watches its config
directory, so flipping the API server on or off (in the GUI or via
`POST /shutdown`) takes effect within a second, no restart or camera event
needed. Click the **?** next to the toggle in the GUI for a quick reference
of the endpoints — handy when wiring up your own joystick, stream deck
buttons, or OBS scripts.

---

## Files

| File | Purpose |
|---|---|
| `aperio.py` | Setup GUI — set startup position, toggle settings, save |
| `aperio.exe` | Background daemon (build from `daemon/`) |
| `daemon/` | Rust source for the daemon |
| `install.bat` | Register daemon as a logon task + add the `aperio` command to PATH |
| `uninstall.bat` | Remove the scheduled task, `aperio` command, and PATH entry |
| `research/` | Protocol research, diagnostic tool, per-camera data |

Config files are created by `aperio.py` on first save and live alongside the exe:

| File | Content |
|---|---|
| `start_pos.txt` | Startup pan/tilt in degrees |
| `last_track.state` | AI tracking on/off (1/0) |
| `auto_privacy.state` | Auto privacy on/off (1/0) |
| `joystick_invert.state` | Invert joystick on/off (1/0) |
| `api_server.state` | Local API server on/off (1/0, default off) |

---

## Testing Another Camera

```
pip install hidapi

# List all HID devices to find your camera's VID and PID
python research/diag.py --list

# Run the read-only diagnostic against your camera
python research/diag.py --vid 0xXXXX --pid 0xXXXX
```

`diag.py` is completely read-only. It queries firmware versions, serial
numbers, motor positions, and device state without moving the camera or
changing any settings. If your camera responds to the command sweep it is
a strong indicator that full Aperio support is achievable.

---

## Research

`research/` contains the reverse-engineering work behind Aperio, organized
per camera. See `research/README.md` for how to test your own camera.

```
research/
├── README.md            how to test your camera + how to contribute
├── diag.py              read-only diagnostic for any AI gimbal camera
└── EMEET Pixy/          confirmed working — full protocol reference
    ├── REpixy.md
    ├── usb_descriptors.txt
    ├── cmd_headers.txt
    ├── emeetlink_strings.txt
    └── studio_symbols.txt
```

---

## Disclaimer

This software communicates directly with camera hardware over undocumented vendor
protocols recovered through reverse engineering. It is provided **as-is, with no
warranty of any kind**.

By using Aperio you accept full responsibility for any outcome, including but not
limited to camera malfunction, firmware corruption, or permanent hardware damage.
The author(s) are not responsible for bricked devices, voided warranties, or any
other damage resulting from the use of this software.

Read the safety notes in `research/EMEET Pixy/REpixy.md` before sending any
commands beyond the documented safe set.

---

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Any derivative work or integration that builds on the protocol research in
this repository must retain attribution to the original author.
