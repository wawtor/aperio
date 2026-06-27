# Aperio Research

> **Disclaimer:** The tools and documentation in this folder interact directly
> with camera hardware over undocumented vendor protocols. They are provided
> as-is with no warranty. The author(s) accept no responsibility for bricked
> devices, firmware corruption, voided warranties, or any other damage. Use
> read-only commands (`GET_*`, `diag.py`) first and do not send any command
> marked ⚠️ in `EMEET Pixy/REpixy.md`.

This folder contains the reverse-engineering work behind Aperio and the tools
needed to expand support to cameras beyond the EMEET Pixy.

---

## Folder Layout

```
research/
├── diag.py           <- run this first on any untested camera
└── EMEET Pixy/       <- full research data for the confirmed camera
    ├── REpixy.md          protocol notes and command reference
    ├── usb_descriptors.txt USB descriptor dump
    ├── cmd_headers.txt     decoded HID command headers
    ├── emeetlink_strings.txt strings extracted from EMEET Link companion app
    └── studio_symbols.txt  symbol table from EMEET Studio 2
```

When a new camera is confirmed working, its data goes in its own folder here
alongside `EMEET Pixy/`. For example: `OBSBOT Tiny 2/`, `Insta360 Link/`.

---

## Testing Your Camera with diag.py

`diag.py` is a read-only diagnostic script — it never moves the camera or
changes any settings. It sends vendor HID queries and reports back what the
camera responds to.

**Requirements:**

```
pip install hidapi
```

**Step 1 — Find your camera's VID and PID:**

```
python diag.py --list
```

Look for your camera by name in the output. Note the `VID` and `PID` columns.

**Step 2 — Run the diagnostic:**

```
python diag.py --vid 0xXXXX --pid 0xXXXX
```

The output will show:
- Firmware versions (ISP, gimbal, motor MCU)
- Serial numbers
- Current motor position (pan/tilt)
- Device mode and AI tracking state
- A raw command sweep showing which queries the camera acknowledges

**Step 3 — Share your results:**

If your camera responds to any of the commands in the sweep, there is a strong
chance Aperio can be made to work with it. Open a PR or issue on GitHub with:

1. The full output of `diag.py`
2. Your camera model and USB descriptor info (`--list` output)
3. Any other information about the companion app (name, version)

Add your research data under a new folder named after your camera, following
the same structure as `EMEET Pixy/`.

---

## Cameras We Want Data For

These all have motorized gimbals and companion apps. None have been tested yet.
If you own one, running `diag.py` takes 30 seconds and is completely safe.

| Camera | Manufacturer VID (if known) |
|---|---|
| OBSBOT Tiny 2 | 0x3554 |
| OBSBOT Tiny 2 Lite | 0x3554 |
| OBSBOT Tiny 4K | 0x3554 |
| OBSBOT Meet 4K | 0x3554 |
| Insta360 Link | 0x2E1A |
| Insta360 Link 2 | 0x2E1A |

Use `--list` to confirm the PID for your specific model, then pass both to `diag.py`.

---

## Protocol Overview

All confirmed commands use a vendor HID channel on a dedicated interface
(usage_page 0x83). Frames are 32 bytes with Report ID 0x09:

```
Byte 0    Report ID (0x09)
Byte 1    Group (0x01=ISP, 0x41=Gimbal, 0x61=Motor, 0x63=Motor+, etc.)
Byte 2    Page
Byte 3    Index (0x00=set, 0x01=get, 0x04=getver, 0x19=move_rel, etc.)
Byte 4    0x00
Byte 5    Payload length
Byte 6    0x00
Byte 7    Payload length (repeated)
Bytes 8+  Payload, padded to 32 bytes total with 0x00
```

Motor positions are IEEE-754 float32 LE. Axis 1 = Pan (±150°), Axis 2 = Tilt (±90°).

See `EMEET Pixy/REpixy.md` for the full command reference.
