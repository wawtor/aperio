#!/usr/bin/env python3
"""
diag.py -- Aperio compatibility diagnostic (read-only)

Scans for an AI gimbal webcam using the vendor HID command channel,
then queries firmware versions, serial numbers, motor positions, and
device state without changing any settings or moving the camera.

Confirmed working:  EMEET Pixy  (VID 0x328F  PID 0x00C0)
Likely compatible:  EMEET Piko / Piko+ / Piko Dual (same chipset)
Untested:           OBSBOT Tiny/Meet series, Insta360 Link series, others

Usage:
    python diag.py                          # auto-detect EMEET Pixy defaults
    python diag.py --vid 0x328F --pid 0x00C0
    python diag.py --list                   # list ALL HID devices and exit

Requirements: pip install hidapi
"""
import sys, struct, time, argparse
try:
    import hid
except ImportError:
    print("ERROR: hidapi not installed.  Run:  pip install hidapi")
    sys.exit(1)

# ---- defaults (EMEET Pixy) ----
DEFAULT_VID = 0x328F
DEFAULT_PID = 0x00C0
RID         = 0x09     # Report ID for vendor HID channel

# Known VID/PID for other AI gimbal cameras (untested — use --list to discover yours)
# OBSBOT cameras:  VID 0x3554  (PIDs vary by model — run --list to find yours)
# Insta360 Link:   VID 0x2E1A  (PIDs vary by model — run --list to find yours)

# ---- low-level HID helpers ----

def open_device(vid, pid):
    """Return an open HID handle to the vendor command interface, or None."""
    infos = hid.enumerate(vid, pid)
    if not infos:
        return None, None
    # Prefer the vendor interface (usage_page 0x83 or interface 4)
    target = None
    for d in infos:
        if d.get("usage_page") in (0x83, 131) or d.get("interface_number") == 4:
            target = d
            break
    target = target or infos[0]
    if hasattr(hid, "Device"):
        dev = hid.Device(path=target["path"])
    else:
        dev = hid.device()
        dev.open_path(target["path"])
    return dev, target

def drain(dev, win):
    end = time.time() + win
    out = []
    while time.time() < end:
        d = dev.read(32, int(max(1, (end - time.time()) * 1000)))
        if d:
            out.append(bytes(d))
    return out

def xfer(dev, b1, b2, b3, payload=b"", wait=0.5):
    """Send a vendor command and return (payload_bytes, raw_reply), or (None, None)."""
    drain(dev, 0.03)
    frame = (bytes([RID, b1, b2, b3, 0x00, len(payload), 0x00, len(payload)]) + payload).ljust(32, b"\x00")
    dev.write(frame)
    for d in drain(dev, wait):
        if len(d) >= 8 and d[1] == b1 and d[2] == b2 and d[3] == b3:
            return d[8:8 + d[5]], d
    return None, None

def show(dev, tag, b1, b2, b3, payload=b""):
    p, _ = xfer(dev, b1, b2, b3, payload)
    status = p.hex(" ") if p else "NO REPLY"
    print("  %-36s %s" % (tag, status))
    return p

# ---- decoders ----

def decode_ver(p):
    if p and len(p) >= 2:
        return "v%d.%d.%d" % (p[1] >> 4, p[1] & 0xF, p[0])
    return None

def decode_sn(p):
    if p:
        return p.split(b"\x00")[0].decode("ascii", "replace") or "(empty)"
    return None

def decode_f32(p, offset=1):
    if p and len(p) >= offset + 4:
        return struct.unpack("<f", p[offset:offset + 4])[0]
    return None

# ---- main diagnostic ----

def run_diag(dev, info):
    print()
    print("=" * 60)
    print("  Aperio  --  Camera Diagnostic (read-only)")
    print("=" * 60)
    print()
    print("  Device  : %s" % info.get("product_string", "(unknown)"))
    print("  VID/PID : %04X / %04X" % (info.get("vendor_id", 0), info.get("product_id", 0)))
    print("  Mfr     : %s" % info.get("manufacturer_string", "(unknown)"))
    if info.get("serial_number"):
        print("  USB SN  : %s" % info["serial_number"])
    print()

    # Firmware versions
    print("  -- Firmware --")
    for label, b1 in [("ISP (FIC7608)",       0x01),
                      ("Gimbal (M2)",          0x41),
                      ("Motor MCU (M3)",       0x61)]:
        p, _ = xfer(dev, b1, 0x00, 0x04)
        ver  = decode_ver(p)
        print("  %-20s %s" % (label + ":", ver if ver else "no reply"))

    print()
    print("  -- Serial Numbers --")
    for label, b1 in [("ISP", 0x01), ("Gimbal", 0x41), ("Motor MCU", 0x61)]:
        p, _ = xfer(dev, b1, 0x00, 0x03)
        sn   = decode_sn(p)
        print("  %-20s %s" % (label + ":", sn if sn else "no reply"))

    print()
    print("  -- Motor State --")
    pan  = decode_f32(xfer(dev, 0x63, 0x01, 0x01, bytes([1]))[0])
    tilt = decode_f32(xfer(dev, 0x63, 0x01, 0x01, bytes([2]))[0])
    print("  Pan  : %s deg" % ("%.3f" % pan  if pan  is not None else "no reply"))
    print("  Tilt : %s deg" % ("%.3f" % tilt if tilt is not None else "no reply"))

    print()
    print("  -- Device State --")
    p, _ = xfer(dev, 0x01, 0x01, 0x01)
    if p:
        modes = {0: "Standard", 1: "Follow (AI track)", 2: "Privacy", 3: "Standby"}
        print("  Device mode  : %d  (%s)" % (p[0], modes.get(p[0], "unknown")))
    else:
        print("  Device mode  : no reply")

    p, _ = xfer(dev, 0x04, 0x01, 0x01)
    if p:
        print("  Target track : %d  (0=off)" % p[0])
    else:
        print("  Target track : no reply")

    p, _ = xfer(dev, 0x04, 0x02, 0x01)
    if p and len(p) >= 2:
        print("  Gesture recog: type=%d  enabled=%d" % (p[0], p[1]))
    else:
        print("  Gesture recog: no reply")

    p, _ = xfer(dev, 0x02, 0x01, 0x01)
    if p and len(p) >= 4:
        secs = struct.unpack("<I", p[:4])[0]
        print("  Privacy timer: %d s" % secs)
    else:
        print("  Privacy timer: no reply")

    print()
    print("  -- Raw command sweep (read-only getters) --")
    print("  (These show what your camera responds to. 'NO REPLY' = not supported.)")
    show(dev, "VER (ISP)        0x01 0x00 0x04", 0x01, 0x00, 0x04)
    show(dev, "VER (M2)         0x41 0x00 0x04", 0x41, 0x00, 0x04)
    show(dev, "VER (M3)         0x61 0x00 0x04", 0x61, 0x00, 0x04)
    show(dev, "DEVICE_MODE get  0x01 0x01 0x01", 0x01, 0x01, 0x01)
    show(dev, "MOTOR_POS pan    0x63 0x01 0x01", 0x63, 0x01, 0x01, bytes([1]))
    show(dev, "MOTOR_POS tilt   0x63 0x01 0x01", 0x63, 0x01, 0x01, bytes([2]))
    show(dev, "TARGET_TRACK get 0x04 0x01 0x01", 0x04, 0x01, 0x01)
    show(dev, "GESTURE get      0x04 0x02 0x01", 0x04, 0x02, 0x01)
    show(dev, "PRIVACY_TIME get 0x02 0x01 0x01", 0x02, 0x01, 0x01)

    print()
    print("  DONE — camera was not moved and no settings were changed.")
    print()

def list_all():
    print("All HID devices on this system:")
    print()
    for d in hid.enumerate():
        print("  VID %04X  PID %04X  iface %2s  usage_pg 0x%02X  '%s'  '%s'" % (
            d.get("vendor_id", 0),
            d.get("product_id", 0),
            d.get("interface_number", "?"),
            d.get("usage_page", 0),
            d.get("manufacturer_string", ""),
            d.get("product_string", ""),
        ))

def main():
    ap = argparse.ArgumentParser(description="Aperio camera diagnostic (read-only)")
    ap.add_argument("--vid",  default=None, help="Vendor ID  hex (default: 0x328F EMEET)")
    ap.add_argument("--pid",  default=None, help="Product ID hex (default: 0x00C0 Pixy)")
    ap.add_argument("--list", action="store_true", help="List all HID devices and exit")
    args = ap.parse_args()

    if args.list:
        list_all()
        return

    vid = int(args.vid, 16) if args.vid else DEFAULT_VID
    pid = int(args.pid, 16) if args.pid else DEFAULT_PID

    print("Searching for VID %04X  PID %04X ..." % (vid, pid))
    dev, info = open_device(vid, pid)
    if dev is None:
        print("Device not found.")
        print("Try --list to see all HID devices, then pass --vid / --pid.")
        sys.exit(1)

    try:
        run_diag(dev, info)
    finally:
        try: dev.close()
        except Exception: pass

if __name__ == "__main__":
    main()
