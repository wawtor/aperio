# EMEET Pixy — Reverse-Engineering Notes (Control Protocol)

> Goal: control the EMEET Pixy AI PTZ webcam (pan / tilt / zoom, AI face‑tracking,
> gesture, presets, privacy, image params) **without** the vendor app, for
> integration into our own software.
>
> **Status:** protocol recovered by static analysis of the device's USB
> descriptors plus reverse‑engineering of the official EMEET control apps.
> **No firmware was flashed and no commands were sent to the device.** Everything
> below was obtained from (a) read‑only USB descriptor dumps and (b) static
> disassembly of the macOS application binaries.

---

## 0. TL;DR — how to control it

The Pixy exposes **three** control surfaces. You only need the first two:

| What you want | Use | Channel |
|---|---|---|
| **Point the camera** (pan/tilt/zoom/focus) | Standard **UVC PanTilt(Absolute)/Zoom/Focus** controls | Any UVC stack (v4l2 / libuvc / IOKit) — *no vendor secret needed* |
| **Pan/tilt by motor, presets, AI face‑tracking, gesture, privacy, fill light** | Vendor **HID command protocol**, Report ID **0x09**, 32‑byte reports | `hidraw` / hidapi (interrupt EP `0x84` IN, `0x01` OUT) |
| (vendor‑internal) image tuning / firmware | UVC **Extension Unit** GUID `{46394292‑0cd0‑4ae3‑8783‑3133f9eaaa3b}` | UVC XU control transfers — *mostly not needed, some entries are dangerous* |

The single most useful fact: **AI/PTZ features are driven by a simple, un‑checksummed
HID command frame on Report ID 9.** Full command table in §4.

---

## 1. Device identity

From `lsusb -v` (read‑only) of the connected unit:

```
idVendor           0x328f  EMEET
idProduct          0x00c0  EMEET PIXY
bcdDevice          20.04            (firmware/HW rev)
iManufacturer      EMEET
iProduct           EMEET PIXY
iSerial            <serial>
bDeviceClass       0xEF (Misc) / IAD  → composite device
```

Composite device, 5 interfaces:

| Intf | Class | Function |
|---|---|---|
| 0 | Video Control (UVC) | Camera controls (PTZ, focus, exposure, image) + Extension Unit |
| 1 | Video Streaming | MJPEG + YUY2, up to 3840×2160 (4K) |
| 2 | Audio Control | Built‑in microphone |
| 3 | Audio Streaming | 48 kHz / 16‑bit mono PCM |
| **4** | **HID (vendor)** | **Proprietary command channel — Report ID 9** |

The Pixy is a **dual‑camera AI PTZ** unit; the protocol has explicit
"master/partner" sub‑device addressing (see §4.2).

---

## 1A. Silicon / processors

Recovered authoritatively from the Pixy's own firmware manifest
`https://www.emeet.ai/device_software/EMEET_STUDIO/pixy/device_upgrade_pixy.json`
(`"device_pid":"0x00c0"`, `"type":"Pixy"`) plus the bundled `cskburn` flasher.
The Pixy is **not** a single‑chip webcam — it's a multi‑processor board:

| Block | Part | Role | Firmware component |
|---|---|---|---|
| **Image sensor** | **Sony IMX362** | ~12.2 MP, 1/2.55", dual‑pixel PDAF | (named in ISP fw) |
| **ISP / UVC bridge SoC** | **Fitipower FIC7608** | The "camera" chip — drives the IMX362, the 4K MJPEG/YUY2 USB video pipeline, standard UVC controls (pan/tilt/zoom/focus/exposure/image) and the UVC Extension Unit. This is what enumerates as `328f:00c0`. | `EMEET_PIXY_FIC7608_IMX362_V2.0.4…bin` (`fic760x`) |
| **AI SoC + NPU** ("the GPU") | **ListenAI CSK‑series (CSK6)** | The gimbal "brain". Runs on‑device neural inference (YOLO‑style face / subject / whiteboard‑keypoint detection) for auto‑framing & tracking. Dual‑firmware AP + CP. | `yuntai_ap.bin` (App Proc) + `yuntai_cp.bin` (Co‑Proc) + `yuntai_res.bin` (NPU model/resources), all v2.0.5 |
| **Motor‑control MCU** | small MCU (Intel‑HEX image) | Drives the pan/tilt stepper/servo motors of the gimbal | `project.hex` + `flash_info.ini`, v2.0.7 (`mcu`) |

Notes:
- **There is no traditional graphics GPU.** The closest equivalent is the **NPU
  inside the ListenAI CSK6 AI SoC** — that's where the face/subject‑tracking neural
  net runs on‑device (the binary carries YOLO whiteboard/face keypoint detection
  symbols, e.g. `parse_yolo_whiteboard_kpts_detect_data_interface`,
  `group4_adaptive_convolution`, `CDataBlob`).
- "**yuntai**" (云台) = pan‑tilt **gimbal**. So `yuntai_ap/cp/res` = the CSK AI SoC's
  application processor, co‑processor and model pack.
- The CSK SoC is flashed by **`cskburn`** (ListenAI's open‑source serial flasher,
  bundled in eMeetLink's Resources) over a serial bootloader — layout
  `flashboot.bin @0x0, master.bin @0x10000, respack.bin @0x100000`. The
  `E_CSK_AP_FIRMWARE` / `CSK DEBUG: … command %02X` paths tunnel this serial
  protocol. ⚠️ **Do not** trigger the CSK bootloader / `cskburn` / `read_chip_id`.
- The HID `GET_VER` / `GET_VER_MASTER_1/2/3` and `GET_SN_MASTER_1/2` commands
  (§4.3) read the per‑processor firmware versions / serials (FIC ISP, AP, CP, MCU),
  which is how the app distinguishes the sub‑processors — and how *we* can confirm
  the chip set live, read‑only, without touching any bootloader.

**✔ Confirmed live** (read‑only `GET_VER*` against the connected unit — versions
match the manifest exactly, proving the sub‑device → chip mapping):

| Query (`b1`) | Reply version | Chip |
|---|---|---|
| `GET_VER` (`0x01`) | **v2.0.4** (raw `…04 20`) | Fitipower **FIC7608** ISP (matches `fic760x 2.0.4`) |
| `GET_VER_MASTER_2` (`0x41`) | **v2.0.5** | **ListenAI CSK** gimbal AI SoC (matches `ap/cp 2.0.5`) |
| `GET_VER_MASTER_3` (`0x61`) | **v2.0.7** | motor‑control **MCU** (matches `mcu 2.0.7`) |
| `GET_SN` (`0x01`) | ASCII `<serial>` | main‑board serial |

Version byte layout (2‑byte payload): `vMAJOR.MINOR.PATCH` =
`(p[1]>>4).(p[1]&0xF).(p[0])`.

---

## 2. Control surface from the USB descriptors

### 2.1 UVC Camera Terminal (Entity ID 1) — standard PTZ

`bmControls = 0x00020e6a` advertises these standard UVC controls:

- Auto‑Exposure Mode
- Exposure Time (Absolute)
- **Focus (Absolute)**, Focus (Relative), Focus Auto
- **Zoom (Absolute)**, Zoom (Relative)
- **PanTilt (Absolute)**  ← hardware pan/tilt via the standard UVC mechanism

### 2.2 UVC Processing Unit (Entity ID 3) — image params

`bmControls = 0x0000177f`: Brightness, Contrast, Hue, Saturation, Sharpness,
Gamma, White‑Balance‑Temp (+Auto), Backlight Compensation, Gain, Power‑Line‑Freq.

### 2.3 UVC Extension Unit (Entity ID 2) — vendor

```
guidExtensionCode  {46394292-0cd0-4ae3-8783-3133f9eaaa3b}
bNumControls       10        (control selectors 1..10)
bControlSize       4         (bmControls = ff 03 00 00 → CS 1..10 enabled)
bSourceID          1         (chained off the Camera Terminal)
```

The vendor app uses this XU for a few internal operations (`XU_SET_CUR`,
`XU_GET`, and — **dangerous** — `XU_ISP_FW_DOWNLOAD`,
`XU_SERIAL_FLASH_ACCESS_CONTROL`). For normal control you do **not** need the XU;
prefer standard UVC + the HID channel. **Do not poke XU firmware/flash selectors.**

### 2.4 HID interface (Interface 4) — the command channel

Decoded HID Report Descriptor (35 bytes, read from
`/sys/.../0003:328F:00C0.*/report_descriptor`):

```
Usage Page 0xFF83 (vendor), Usage 0x83, Collection(Application)
  Report ID = 0x09
  Input  report (device→host): 31 bytes, logical -128..127   → EP 0x84 IN  (interrupt)
  Output report (host→device): 31 bytes, logical -128..127   → EP 0x01 OUT (interrupt)
End Collection
```

So every HID transfer is **Report ID 0x09 + 31 payload bytes = 32 bytes total**,
matching the 32‑byte interrupt endpoints. The host sends commands on the Output
report and the device answers on the Input report.

On Linux this device is `/dev/hidraw2`
(`HID_NAME=EMEET EMEET PIXY`, `HID_ID=0003:0000328F:000000C0`).

### 2.5 No infrared sensor — Windows Hello is not possible

Windows Hello face auth requires the camera to expose an **infrared frame source**
(a grayscale/IR `FrameSourceType` Windows enumerates as a biometric sensor), almost
always paired with an 850/940 nm IR illuminator. **The Pixy has neither**, and the
descriptors prove it:

* The VideoStreaming interface advertises exactly **two formats, both colour**:
  MJPEG (10 frame sizes, 3840×2160 → 640×360) and one **uncompressed YUY2**
  (`guidFormat {32595559-…}` → FourCC `YUY2`, packed YCbCr 4:2:2 — *colour*, not
  `L8` grayscale / `D16` depth / any IR FourCC). YUY2 frames: 640×480, 640×360.
* **Both** formats carry a `COLORFORMAT` descriptor declaring `bColorPrimaries =
  BT.709/sRGB`. An IR stream has no colour primaries.
* There is **one** camera input terminal (`wTerminalType 0x0201`) and one video
  function. A Hello camera presents the IR feed as a second, IR-tagged frame source —
  absent here.
* String search of both apps (eMeetLink, EMEET STUDIO) for `hello`/`infrared`/
  `biometric`/`faceauth`/`illuminator`/`winbio`/`depth` → **nothing**.

The "dual camera" is for **AI framing**: a wide sensor the internal CSK6 NPU uses to
see the room and decide the crop, plus the main IMX362 (RGB) that's streamed. The
wide feed is consumed on-device and **never exposed to the host**; it's RGB too. So
there is no IR path anywhere — Hello cannot be enabled, and there is no firmware/
protocol route to add it (see §9 for why a firmware patch can't substitute for the
missing IR optics). Same reason every camera in this class (Insta360 Link, Obsbot
Tiny, …) lacks Hello.

---

## 3. How the protocol was recovered (provenance)

Both official EMEET apps were downloaded from EMEET's own S3 buckets and verified
by MD5 (no device interaction):

| App | File | Notes |
|---|---|---|
| **eMeetLink** v5.3.0 (macOS) | `eMeetLink/EMEETLINK_macOS_V5.3.0_Release_20260605.dmg` (md5 `cc450bbb…`) | Native Cocoa/Obj‑C. Generic EMEET camera framework. Does **not** list the Pixy, but shares the camera command architecture. |
| **EMEET STUDIO** v1.15.6 | `EMEET_STUDIO_V1.15.6_macOS.pkg` (md5 `26430e08…`) | Native **Qt/C++**, **non‑stripped**, bundles **libusb** + **hidapi**. Explicitly supports `EMEET PIXY`. **This is the authoritative source.** |

EMEET STUDIO's `EMEET STUDIO` Mach‑O (universal x86_64+arm64, ~150 MB/slice) keeps
full C++ symbols. The protocol classes:

- **`EMHidCmdHelper`** — the camera HID command set (61 `CMD_*` constants + builders).
- **`EMHidCmdHead`** — the 4‑byte command header struct.
- **`UsbHidCtrl`** — hidapi transport (`write`/`read`/`push` over `hid_device*`).
- **`UsbUvcCtrlMac`** — standard UVC getters/setters (`setPan/setTilt/setZoom/setFocus`…).
- **`EMDeviceControl` / `EMDeviceViewController`** — UI → command glue.

The 61 command header bytes were recovered by disassembling (capstone, arm64
slice) the static initializers that construct each `EMHidCmdHead(a,b,c,d)` object,
resolving the chained‑fixup GOT binds back to their `EMHidCmdHelper::CMD_*` symbol.
Frame layout and payloads came from disassembling the `hidCmdSend*` builders and
the generic `EMHidCmdHelper::hidCmdSend()`.

---

## 4. The vendor HID command protocol (Report ID 9)

### 4.1 Frame format

`EMHidCmdHelper::hidCmdSend()` builds the outgoing report like this
(`EMHidCmdHead` is literally `struct{ u8 b0,b1,b2,b3; }`):

```
Offset  Size  Field
  0      1    0x09            HID Report ID  (== EMHidCmdHead.b0, constant)
  1      1    group           command group / category   (EMHidCmdHead.b1)
  2      1    page            command page               (EMHidCmdHead.b2)
  3      1    index           command index              (EMHidCmdHead.b3)
  4      1    0x00
  5      1    LEN             payload length (bytes)
  6      1    0x00
  7      1    LEN             payload length (duplicate)
  8..  LEN    payload         command data (see §4.4)
```

- **⚠ Write the full 32‑byte report.** Although the macOS app's builder can emit a
  short buffer, the device on Linux/`hidraw` only answered when the OUT report was
  padded to the full **32 bytes** (`[09 b1 b2 b3 00 LEN 00 LEN <payload> <zero‑pad>]`).
  Short writes (4/8 bytes) were accepted but produced **no reply**. Pad with zeros.
- **No checksum / no sequence number.** Replies arrive on EP `0x84` IN and **echo
  the same frame format**: `[09 b1 b2 b3 00 LEN 00 LEN <payload>]`, where `LEN` =
  reply byte 5 and `payload = reply[8 : 8+LEN]`.

**✔ Confirmed live** (read‑only, against the connected unit):

```
GET_VER       send 09 01 00 04 + zero-pad(32)
              recv 09 01 00 04 00 02 00 02  04 20            → v2.0.4
GET_SN        recv 09 01 00 03 00 0c 00 0c  <hex bytes>  → "<serial>"
GET_MOTOR_POS recv 09 63 01 01 00 09 00 09  00 00000000 00000000   → MotorType=0, pan=0.0°, tilt=0.0° (home)
```

The `00 LEN 00 LEN` block and every payload layout below were verified byte‑for‑byte.

### 4.2 Header byte 1 (`group`) encodes the target sub‑camera

`b1 = (subDevice << 5) | category`. Bits [6:5] select which physical camera in
the dual‑cam unit; bits [4:0] are the command category:

| subDevice | b1 high bits | Meaning |
|---|---|---|
| 0 | `0x00` | main / agent camera (default) |
| 1 | `0x20` | master 1 |
| 2 | `0x40` | master 2 |
| 3 | `0x60` | master 3 / partner |

Evidence: `GET_SN` = `09 01 …`, `GET_SN_MASTER_1` = `09 21 …`,
`GET_SN_MASTER_2` = `09 41 …`; firmware `UPGRADE_DEV_1/2/3` = `09 29/49/69 …`.

### 4.3 Complete command table (recovered, authoritative)

Header bytes are `b0 b1 b2 b3` (b0 is always `09`). GET/SET pairs usually differ
only in `b3`. ⚠️ = do not send (firmware / destructive).

**Pan / Tilt / Zoom motor — live motion (group 0x63, page 0x01)**

| Command | Header | Payload (SET) |
|---|---|---|
| `SET_MOTOR_POS` | `09 63 01 00` | `[MotorType:u8][angle:f32]` (LEN 5) |
| `GET_MOTOR_POS` | `09 63 01 01` | → `MotorType, pan:f32, tilt:f32` |
| `SET_MOTOR_SPEED` | `09 63 01 02` | `[MotorType:u8][speed:f32]` |
| `GET_MOTOR_SPEED` | `09 63 01 03` | → `MotorType, f32, f32` |
| `SET_MOTOR_RELATIVE_POS` | `09 63 01 19` | `[MotorType:u8][delta:f32]` (LEN 5) |
| `SET_MOTOR_RUNNING` | `09 63 01 20` | start/stop continuous slew (`[MotorType][on?]`) |

**Pan / Tilt presets & home (group 0x03, page 0x01)**

| Command | Header |
|---|---|
| `SET_MOTOR_POWER_ON_DEFAULT_POS_MODE` | `09 03 01 13` |
| `GET_MOTOR_POWER_ON_DEFAULT_POS_MODE` | `09 03 01 14` |
| `SET_MOTOR_PRESET_POS_MODE` | `09 03 01 15` |
| `GET_MOTOR_PRESET_POS_MODE` | `09 03 01 16` |
| `SET_MOTOR_DEFAULT_POS` (set "home") | `09 03 01 17` |
| `SET_MOTOR_PRESET_POS` (save/goto preset) | `09 03 01 18` |

**AI tracking / framing / focus / exposure region (group 0x04)**

| Command | Header | Payload |
|---|---|---|
| `SET_TARGET_TRACK` (AI face/subject tracking) | `09 04 01 00` | `[TrackMode:u8][f32][f32][f32]` (LEN 13) |
| `GET_TARGET_TRACK` | `09 04 01 01` | → `TrackMode, f32, f32, f32` |
| `SET_GESTURE_RECOG_STA` (gesture control) | `09 04 02 00` | `[GestureType…][on:u8]` |
| `GET_GESTURE_RECOG_STA` | `09 04 02 01` | → `GestureType, u8` |
| `SET_FOCUS_MODE` (AF mode / focus ROI) | `09 04 00 01` | `[FocusMode][SquareInfo]` |
| `GET_FOCUS_MODE` | `09 04 00 02` | → `FocusMode, SquareInfo` |
| `SET_METER_MODE` (AE metering region) | `09 04 00 03` | `[mode][SquareInfo]` |
| `GET_METER_MODE` | `09 04 00 04` | |
| `SET_REVERSE_STA` (image flip H/V) | `09 04 00 08` | `[ReverseType][on:u8]` |
| `GET_REVERSE_STA` | `09 04 00 07` | |
| `SET_WB_LOCK_STA` | `09 04 00 09` | `[u8]` |
| `GET_WB_LOCK_STA` | `09 04 00 0A` | |
| `SET_EV_LOCK_STA` | `09 04 00 0B` | `[u8][u32]` |
| `GET_EV_LOCK_STA` | `09 04 00 0C` | |
| `SET_FOCUS_LOCK_STA` | `09 04 00 0D` | `[u8]` |
| `GET_FOCUS_LOCK_STA` | `09 04 00 0E` | |
| `GET_UVCARGS_STATUS` | `09 04 00 11` | |

**Device mode & identity (group 0x01)**

| Command | Header | Payload |
|---|---|---|
| `SET_DEVICE_MODE` (framing/whiteboard/desk mode) | `09 01 01 00` | `[DeviceMode:u8]` |
| `GET_DEVICE_MODE` | `09 01 01 01` | → `DeviceMode` |
| `GET_SN` | `09 01 00 03` | → serial string |
| `GET_VER` | `09 01 00 04` | → firmware version (u16) |
| `SET_FACTORY_RESET` ⚠️ | `09 01 00 05` | resets the device |

**Privacy & fill light (group 0x02)**

| Command | Header |
|---|---|
| `SET_PRIVACY_TRIGGER_TIME` | `09 02 01 00` |
| `GET_PRIVACY_TRIGGER_TIME` | `09 02 01 01` |
| `SET_LIGHT_RGB_SWITCH` | `09 02 02 00` |
| `GET_LIGHT_RGB_SWITCH` | `09 02 02 01` |
| `SET_LIGHT_RGB_COLOR` | `09 02 02 02` |
| `GET_LIGHT_RGB_COLOR` | `09 02 02 03` |
| `SET_LIGHT_RGB_BRIGHTNESS` | `09 02 02 04` |
| `GET_LIGHT_RGB_BRIGHTNESS` | `09 02 02 05` |
| `SET_LIGHT_MODE` | `09 02 02 06` |
| `GET_LIGHT_MODE` | `09 02 02 07` |
| `GET_LIGHT_ADJUST_ALLOW_STA` | `09 02 02 08` |

*(Note: privacy on the Pixy is mechanical/auto — "Auto Privacy Protection".
`SET_PRIVACY_TRIGGER_TIME` sets the auto‑privacy timeout. There is also a
`PtzPrivacyMode` / `EMDeviceControl::setPrivacyModeEnabled(bool)` path in
EMEET STUDIO that drives privacy via the device‑mode/PTZ logic.)*

**Misc**

| Command | Header |
|---|---|
| `SET_MUSIC_MODE` | `09 05 00 03` |
| `GET_MUSIC_MODE` | `09 05 00 04` |
| `SET_DENOISE_STA` | `09 45 00 00` |
| `GET_DENOISE_STA` | `09 45 00 01` |
| `SET_REMOTE_PAIRING_STA` (IR/RF remote pair) | `09 03 04 03` |
| `GET_REMOTE_PAIRING_STA` | `09 03 04 04` |
| `GET_SN_MASTER_1 / _2` | `09 21 00 03` / `09 41 00 03` |
| `GET_VER_MASTER_1 / _2 / _3` | `09 21/41/61 00 04` |

**⚠️ Firmware / DO NOT SEND**

| Command | Header |
|---|---|
| `SET_UPGRADE_DEV_1_STA` ⚠️ | `09 29 00 01` |
| `SET_UPGRADE_DEV_2_STA` ⚠️ | `09 49 00 01` |
| `SET_UPGRADE_DEV_3_STA` ⚠️ | `09 69 00 01` |
| `SET_END_UPGRADE_DEV_2_STA` ⚠️ | `09 49 00 04` |
| `SET_END_UPGRADE_DEV_STA` ⚠️ | `09 49 00 05` |

### 4.4 Payload encodings & enums

- **Floats are IEEE‑754 single precision, little‑endian** (stored via `str s0`).
- **✔ Pan/tilt motion confirmed live & reversible.** `SET_MOTOR_RELATIVE_POS`
  pan `+5.0` moved the gimbal from `+0.033°` to `+5.043°` (Δ +5.01°), and `-5.0`
  returned it to `+0.060°`. **Units are degrees**; the ack payload `[MotorType, 0x20]`
  — `status 0x20` = **OK/done** (not an error). Pan speed read back as **60.0°/s**.
- **⚠ `GET_MOTOR_POS` requires an axis byte in its payload** (`[MotorType]`, e.g.
  `01`=pan, `02`=tilt). Sent with no payload it replies all‑zeros. Reply layout is
  `[MotorType:1][currentAngle:f32][targetAngle:f32]` (the two floats are equal once
  motion settles).
- **`MotorType`** (axis selector, 1 byte): **`1` = Pan / Yaw (horizontal)**,
  **`2` = Tilt / Pitch (vertical)**. (Recovered from
  `EMDeviceControl::onUpdateRelativePos(CloudPlatformDirect, float)`: Left/Right →
  MotorType 1, Up/Down → MotorType 2, with the sign of the float giving direction;
  e.g. Down and Right negate the value.) Value `3` is used for a third axis
  (roll/zoom on some SKUs). The angle/delta float is in **degrees**.
- **`SET_MOTOR_RELATIVE_POS`** = nudge the given axis by `delta` degrees (used by the
  on‑screen joystick / arrow buttons). **`SET_MOTOR_POS`** = absolute angle.
- **`TrackMode`** (1 byte) for `SET_TARGET_TRACK`: `0` = tracking off, non‑zero =
  on / tracking mode. The three trailing floats are the normalized target
  ROI/anchor (send `0,0,0` for a plain on/off toggle). The vendor UI calls this
  from `EMDeviceControl::setFollowModeEnabled(bool)` and `onAiFollowDeviceMode()`.
- **`GestureType` / `DeviceMode` / `ReverseType`** are small C++ enums. Live
  reads (read‑only) decode the payloads as:
  - `GET_TARGET_TRACK` → `[TrackMode:1][f32][f32][f32]` (LEN 13). `TrackMode 0` = off.
  - `GET_DEVICE_MODE` → `[DeviceMode:1]` (LEN 1). Observed current value **`2`**
    (= the standard framing mode).
  - `GET_GESTURE_RECOG_STA` → `[GestureType:1][enabled:1]` (LEN 2). Observed
    `GestureType=0, enabled=1` (gesture recognition was on).
  - `GET_REVERSE_STA` → `[ReverseType:1][on:1]`. Observed `0,0` (no flip).
  - `GET_PRIVACY_TRIGGER_TIME` → `u32 LE` **seconds**. Observed `0x00000384` = **900 s
    (15 min)** auto‑privacy timeout.
  Enumerate the rest by reading the `GET_*` reply for the current UI state and
  toggling in the vendor app, rather than guessing.

### 4.6 Gesture control & autonomous state changes (important for integration)

The Pixy recognizes **hand gestures on‑device** (CSK NPU). When gesture recognition
is enabled, **holding up an open palm / "high‑five" toggles AI tracking on and off**
— this is done entirely by the camera firmware; the host is *not* in the loop.

- Enable/disable is controlled by `SET_GESTURE_RECOG_STA` (`09 04 02 00`,
  payload `[GestureType][enabled]`) and `setAllGestureRecogSta(bool)` (all gestures
  at once). `GestureType` is an enum (multiple gestures exist; the open‑palm =
  tracking‑toggle mapping is fixed in firmware). On the test unit gesture
  recognition read back **enabled** (`GET_GESTURE_RECOG_STA → GestureType=0,
  enabled=1`).
- **Consequence for our software:** because the gesture toggles `TARGET_TRACK`
  *autonomously*, our app cannot assume it owns the tracking on/off state. The
  vendor app handles this by listening for **unsolicited HID input reports** on
  EP `0x84` and routing them through `EMDeviceControl::onHIDDataArrival(...)`, which
  re‑parses the changed state (e.g. a `TARGET_TRACK` update) and refreshes the UI.
  We must do the same: keep a read thread on the HID input endpoint and update our
  cached tracking state when the device pushes a change (or poll `GET_TARGET_TRACK`
  / `GET_GESTURE_RECOG_STA` periodically). Do **not** treat tracking on/off as
  host‑authoritative.
- Net effect: a user can flip tracking with a high‑five at any time; our UI/state
  has to follow the device, not the other way around.

### 4.5 Worked examples (host → device, hidraw write incl. report‑ID byte)

```
# Pan to absolute 0° (MotorType=1=pan, angle=0.0f):
09 63 01 00  00 05 00 05  01  00 00 00 00

# Tilt up by +5.0° relative (MotorType=2=tilt, delta=+5.0f = 0x40A00000 LE):
09 63 01 19  00 05 00 05  02  00 00 A0 40

# Pan left by 5.0° relative (MotorType=1, delta=-5.0f = 0xC0A00000 LE):
09 63 01 19  00 05 00 05  01  00 00 A0 C0

# Enable AI face/subject tracking (TrackMode=1, ROI 0,0,0):
09 04 01 00  00 0D 00 0D  01  00000000 00000000 00000000

# Disable tracking:
09 04 01 00  00 0D 00 0D  00  00000000 00000000 00000000

# Query current pan/tilt position (no payload → header only):
09 63 01 01
# → reply on EP 0x84: 09 63 01 01 <status> <len> <MotorType> <pan f32> <tilt f32>

# Read serial number (header only):
09 01 00 03
```

> Float bytes above are little‑endian. `+5.0f`=`00 00 A0 40`, `-5.0f`=`00 00 A0 C0`,
> `0.0f`=`00 00 00 00`.

---

## 5. The standard UVC path (recommended for pan/tilt/zoom/focus)

EMEET STUDIO's `UsbUvcCtrlMac` proves the Pixy honors the **standard UVC**
Camera‑Terminal controls (`setPan/getPan`, `setTilt`, `setZoom`, `setFocus`,
`setExposure`, plus the Processing‑Unit image controls). This is the cleanest,
cross‑platform way to "point the camera" and needs no vendor code.

### 5.0 Travel limits / stop angles (✔ read live from the device)

The angle stop‑limits are **not clamped in the app's command path**
(`EMDeviceViewController::setMotorPos` / `onSetRelativePos` call the HID sender
directly, no min/max check) — they are **enforced by the device** (motor MCU) and
exposed through the standard UVC PanTilt MIN/MAX. Read read‑only from the unit
(`VIDIOC_QUERYCTRL` on `/dev/video0`):

| Control | Min | Max | Step | Default | Range |
|---|---|---|---|---|---|
| `pan_absolute`  | −540000 | +540000 | 3600 | 0 | **±150°** |
| `tilt_absolute` | −324000 | +324000 | 3600 | 0 | **±90°** |
| `zoom_absolute` | 100 | 150 | 1 | 100 | **1.0×–1.5×** (digital) |
| `focus_absolute`| 0 | 1023 | 1 | 192 | — |

(UVC pan/tilt units are **arcseconds**; 3600 arcsec = 1°. So pan = ±540000 arcsec =
±150°, tilt = ±324000 arcsec = ±90°.) The vendor app builds its PTZ sliders from
these device‑reported ranges rather than hard‑coding them. **For our software:**
clamp absolute targets to pan ∈ [−150°, +150°] and tilt ∈ [−90°, +90°]. Commanding
past these via either UVC or the HID `MOTOR` commands is clamped by the MCU (it
won't drive into the end‑stops), but we should clamp on our side anyway.

### 5.1 Raw UVC control transfers

Camera Terminal = **Entity ID 1**, VideoControl interface = **0**.

**PanTilt(Absolute)** — `CT_PANTILT_ABSOLUTE_CONTROL` selector `0x0D`, 8‑byte data:

```
SET_CUR:  bmRequestType=0x21  bRequest=0x01(SET_CUR)
          wValue=0x0D00  wIndex=0x0100 (entity 1 << 8 | iface 0)  wLength=8
          data = int32 dwPanAbsolute  || int32 dwTiltAbsolute   (LE, arcseconds)

GET_CUR/MIN/MAX/RES/DEF: bmRequestType=0xA1  bRequest=0x81/0x82/0x83/0x84/0x87
```

Units are **arcseconds** (1° = 3600). Read MIN/MAX/RES first to learn the range —
all read‑only and safe.

- **Zoom(Absolute)**: `CT_ZOOM_ABSOLUTE_CONTROL` `0x0B`, `wValue=0x0B00`, 2‑byte u16.
- **Focus(Absolute)**: `CT_FOCUS_ABSOLUTE_CONTROL` `0x06`, `wValue=0x0600`, 2‑byte u16.
- **Focus Auto**: `CT_FOCUS_AUTO_CONTROL` `0x08`, 1‑byte bool.

### 5.2 On Linux (easiest)

The kernel `uvcvideo` driver maps these to V4L2 automatically:

```bash
# install: sudo apt install v4l-utils
v4l2-ctl -d /dev/video0 --list-ctrls            # read-only, shows ranges
v4l2-ctl -d /dev/video0 --set-ctrl=pan_absolute=0
v4l2-ctl -d /dev/video0 --set-ctrl=tilt_absolute=18000     # +5° (arcsec)
v4l2-ctl -d /dev/video0 --set-ctrl=zoom_absolute=<n>
```

(`/dev/video0` is the Pixy: `EMEET PIXY` per `/sys/class/video4linux/video0/name`.)

> Pan/tilt over standard UVC and over the HID `MOTOR` commands drive the **same**
> physical gimbal. UVC absolute uses arcseconds; the HID motor commands use float
> degrees and additionally expose presets, continuous slew, speed, and AI tracking
> that standard UVC cannot.

---

## 6. Integration plan for our software

1. **Open the camera stream** normally as a UVC device (V4L2 / libuvc / OS camera API).
2. **Pan / tilt / zoom**: prefer **standard UVC** (`pan_absolute`, `tilt_absolute`,
   `zoom_absolute`, `focus_absolute`). Cross‑platform, no reversing required.
3. **AI features** (face tracking on/off, gesture, presets, framing/device modes,
   fill light, privacy timeout, flip): open `/dev/hidraw*` for VID `0x328F` /
   PID `0x00C0` (or hidapi `hid_open(0x328F,0x00C0,NULL)`) and send the §4 frames
   on **Report ID 0x09**. Read replies from the interrupt IN endpoint and match the
   echoed `b0..b3` header.
4. **Discovery on Linux**: match `/sys/class/hidraw/*/device/uevent` for
   `HID_ID=0003:0000328F:000000C0`.
5. **Probe safely**: start with `GET_*` commands (`GET_VER`, `GET_SN`,
   `GET_MOTOR_POS`, `GET_TARGET_TRACK`) — they're read‑only and let you confirm
   framing and decode the enum values from live state before issuing any `SET_*`.

### 6.1 Minimal Linux sender (hidapi, illustrative)

```python
import hid, struct
d = hid.device(); d.open(0x328F, 0x00C0)          # EMEET PIXY HID

def cmd(b1,b2,b3,payload=b""):
    L = len(payload)
    frame = bytes([0x09,b1,b2,b3, 0x00,L,0x00,L]) + payload
    frame = frame.ljust(32, b"\x00")              # MUST pad to 32 bytes (verified)
    d.write(frame)                                # byte[0] = report ID 0x09
    r = d.read(32, timeout_ms=500)                # reply: [09 b1 b2 b3 00 LEN 00 LEN <payload>]
    return bytes(r[8:8+r[5]]) if r else None      # LEN at byte 5, payload at byte 8

print(cmd(0x01,0x00,0x03).decode())               # GET_SN  -> "<serial>"
print(cmd(0x01,0x00,0x04))                        # GET_VER -> b'\x04\x20' = v2.0.4
mt,pan,tilt = struct.unpack('<Bff', cmd(0x63,0x01,0x01))   # GET_MOTOR_POS (read-only)
cmd(0x63,0x01,0x19, bytes([0x02]) + struct.pack('<f',  5.0))   # tilt +5° relative
cmd(0x04,0x01,0x00, bytes([0x01]) + struct.pack('<fff',0,0,0)) # tracking ON
```

> All `GET_*` calls above were exercised read‑only on the real device and returned
> the values shown. The two `SET_*` lines are documented but were **not** sent.

### 6.2 Privacy mode, wake, and persisting AI tracking

**Privacy/sleep:** after `PRIVACY_TRIGGER_TIME` (default **900 s** idle) the camera
auto‑enters a privacy/sleep state (a PTZ park, `PtzPrivacyMode`) and self‑wakes when
an app opens the video stream. The protocol exposes **only the timeout** (get/set);
there is **no "privacy state" or "wake" command** to query or be notified by. We
**keep** this timeout as‑is.

**The persistence problem:** the firmware boots/​wakes tracking **off** and has **no
power‑on default** for it (only motor *position* persists, via
`SET_MOTOR_POWER_ON_DEFAULT_POS_MODE`). The vendor app only *appears* to remember
settings — on launch `onDeviceConnected()` → `restoreLastDeviceMode()` re‑sends them.
So to keep tracking "on" we must re‑issue it whenever the camera (re)activates.

**Open question (verify on device):** whether tracking actually resets across a
*privacy* sleep, or only across a full power‑cycle. The CSK SoC stays powered during
a privacy park, so tracking may survive a privacy wake by itself. Quick test:
`pixy.py priv-set 15` → wait for it to park → open the camera in an app to wake it →
`pixy.py info` (check `target_track`) → `pixy.py priv-set 900` to restore.

**Wake detection on Windows:** camera "in use" is observable via
`HKCU/HKLM\…\CapabilityAccessManager\ConsentStore\webcam` (`LastUsedTimeStop == 0`
while an app holds the camera). An app going in‑use (e.g. Discord opening the cam) is
the privacy‑wake signal. (Per‑app, not per‑device — fine with a single webcam.)

**Tooling in `<repo-root>\emeet-pixy-RE\`:**

- **`pixy.py`** — protocol helper. Read‑only: `info`, `listen`, `priv`. Setters:
  `pan`/`tilt` (relative), `goto <pan> <tilt>` (absolute), `mode [n]`,
  `follow <0|1>` (= `SET_DEVICE_MODE(Follow/Standard)`), `priv-set`.
- **`pixy_autotrack.py`** — persistence watcher. Triggers on **USB arrival**
  (power‑cycle) *and* **camera‑call** (privacy wake, via the registry above). On each
  trigger it waits `settle`, then runs the wake routine: **(1) `goto` the saved start
  position, then (2) enable Follow** (`SET_DEVICE_MODE(Follow)`) — restoring the **last**
  tracking state (default ON) and verifying by read‑back. While in use (after a `grace`
  window) it captures deliberate user changes (DeviceMode ≠ Follow) as the new last
  state, so the next wake restores what you actually left it on. Config: start position
  in **`start_pos.txt`** (`"pan tilt"` degrees; capture by aiming the camera then reading
  `pixy.py info`). Modes: `--check`, `--dry-run`, `--once` (single wake‑restore + exit).
  Logs to `autotrack.log`; remembers state in `last_track.state`.
- **`install_autotrack.bat` / `uninstall_autotrack.bat`** — register/remove the
  per‑user **logon** Scheduled Task `EmeetPixyAutoTrack` (runs windowless via
  `pythonw`). Start now: `schtasks /Run /TN EmeetPixyAutoTrack`.

It opens the HID (MI_04) only briefly and never holds the camera (MI_00) stream, so
it does not interfere with Discord/OBS/etc.

### 6.3 Enabling AI tracking — ✅ SOLVED (2026‑06‑23, confirmed live)

**The entire recipe is one command: `SET_DEVICE_MODE(Follow)`** — `09 01 01 00`,
1‑byte payload `01`. Confirmed working end‑to‑end on the unit with our own tool
(`pixy.py follow 1`): the camera tracks the subject just like EMEET STUDIO. No held
session, no `TargetTrack`, no UVC control needed.

**`DeviceMode` enum** (decoded from the EMEET STUDIO 2 `.dSYM`):

```
DeviceMode { Standard = 0,  Follow = 1,  Privacy = 2,  Standby = 3 }
```

`Follow = 1` is the AI subject‑tracking mode (LED turns **orange**). This explains
every value we saw: `3`=Standby (idle), `2`=Privacy, `0`=Standard (also what a manual
`SET_MOTOR_*` drops it to), `1`=Follow.

**Key facts:**

1. **Tracking is on‑device.** Static RE of STUDIO 2 (45k symbols) shows its only
   host‑side vision model is a *whiteboard* detector (`_whiteboard_detect_yolov8_*`,
   `YOLOHead`) — **no host‑side person/face tracker**. The subject‑follow AI runs on the
   camera's CSK6. Disassembly of STUDIO's Follow toggle (`DeviceControlManage::
   sendSetDeviceMode` → `HidConfigManage::sendSetDeviceMode`) confirms it sends **only
   `SET_DEVICE_MODE`** — nothing else.
2. **`TargetTrack` is a red herring.** STUDIO has *no* `sendSetTargetTrack` and never
   writes it. Entering Follow makes the firmware set `target_track = 1` **internally**;
   we don't touch it. (Earlier failures chasing this flag were a dead end.)
3. **`DeviceMode` persists** across separate HID open/close cycles — so a single
   one‑shot `SET_DEVICE_MODE(Follow)` is enough; **no held‑open handle is required.**
4. **Why it looked broken at first: lighting / subject detection.** With Follow set but
   the room too dim (or the subject too close/out of the wide FOV), the on‑device AI has
   nothing to detect, so the gimbal sits still. In good light it tracks immediately. The
   blocker was never the protocol — it was whether the camera could *see* a subject.
5. **Wake resets `DeviceMode → Standard(0)`** and re‑homes the gimbal to the motor
   power‑on default (**−16° tilt**, via `SET_MOTOR_POWER_ON_DEFAULT_POS_MODE 09 03 01 13`
   / `SET_MOTOR_DEFAULT_POS 09 03 01 17`). So to keep tracking persistent, re‑send
   `SET_DEVICE_MODE(Follow)` **after** each wake / stream start — this is exactly what
   `pixy_autotrack.py` now does.
6. `GET_MOTOR_POS` returns a **cached/target** value while the AI is driving the motors,
   so it can't be used to observe live tracking movement (eyes‑on only).

**Tooling:** `pixy.py follow <0|1>` now sends only `SET_DEVICE_MODE(Follow/Standard)`;
`mode [n]` gets/sets DeviceMode directly. `pixy_autotrack.py` restores tracking on wake
via `SET_DEVICE_MODE(Follow)`.

---

## 7. Safety / do‑not‑touch list

- ❌ `SET_UPGRADE_DEV_*`, `SET_END_UPGRADE_*` (HID firmware update path).
- ❌ UVC XU selectors `XU_ISP_FW_DOWNLOAD`, `XU_SERIAL_FLASH_ACCESS_CONTROL`
  (firmware/flash; on the `{46394292‑…}` extension unit).
- ❌ The bundled `cskburn` tool / CSK serial bootloader path — the AI SoC is a
  ListenAI/ChipSky **CSK** chip; `cskburn` flashes it. Never invoke.
- ⚠️ `SET_FACTORY_RESET` (`09 01 00 05`) — wipes settings.
- ✅ All `GET_*` commands and all UVC `GET_*`/`--list-ctrls` queries are read‑only
  and safe to use for discovery.
- ❌ The archived OTA firmware images (`firmware/`, see §9) are for **static analysis
  only**. Do not flash them or any modified version — bricking risk, and the on‑device
  bootloader's signature policy is unverified.

---

## 8. Open items / TODO

- ✅ *Done — protocol + reply framing confirmed live* (read‑only `GET_*`): the
  `[09 b1 b2 b3 00 LEN 00 LEN payload]` frame, the 32‑byte write requirement, and
  the FIC/CSK/MCU version mapping were all verified against the connected unit.
- ✅ **SOLVED — "enable AI tracking" = `SET_DEVICE_MODE(Follow=1)`** (§6.3). Confirmed
  live with our own tool; tracks just like STUDIO. No held session / no TargetTrack /
  no UVC control. Resolved by static RE of EMEET STUDIO 2 (`.dSYM`, on the MacBook Air):
  the Follow toggle sends only `SET_DEVICE_MODE`, tracking is on‑device (CSK6), and the
  earlier "doesn't track" was just **dim‑room subject detection**.
- Enumerate the **full** `DeviceMode` / `GestureType` / `FocusMode` value sets
  (only current states observed so far: DeviceMode=2, GestureType=0/enabled).
  Easiest: read `GET_*` while toggling each option in the vendor app once, or
  finish decompiling `EMDeviceViewController::onSetActiveDeviceMode` /
  `setGestureRecogSta`.
- Verify the standard UVC PanTilt arcsecond range with a read‑only
  `v4l2-ctl --list-ctrls` (or UVC GET_MIN/MAX) on the actual unit.
- `GET_VER_MASTER_1` (`0x21`) returned no reply; `0x41`/`0x61` map to the CSK SoC
  and motor MCU. Determine whether `0x21` is the CSK co‑processor (silent unless
  active) or an unpopulated second‑camera slot.
- The Pixy ships its own neural net (YOLO‑style whiteboard/face keypoint detection
  symbols seen in the binary) — AI runs on‑device; the host only toggles
  modes/tracking via the commands above.
- ✅ *Done — IR / Windows Hello question settled (§2.5):* no IR sensor or illuminator;
  both UVC formats are sRGB colour; Hello is impossible and unfixable in software.
- ✅ *Done — OTA firmware located, archived, and statically analysed (§9):* full
  MD5‑verified image set for PID `0x00C0`; FIC7608 image is unencrypted with the USB
  descriptor table in the clear.

---

## 9. Firmware images (provenance, analysis, modifiability)

The official OTA images were located, downloaded (read‑only), and MD5‑verified on
2026‑06‑23. They live in **`firmware/`** (`firmware/README.md` has the full table,
URLs, and analysis). **No image was written to the device.**

### 9.1 Where they come from

EMEET STUDIO builds a per‑model update URL:

```
https://www.emeet.ai/device_software/EMEET_STUDIO/<model>/device_upgrade_<model>.json
```

Our unit is the **`pixy`** model — its manifest self‑identifies as
`"device_pid":"0x00c0"`, `"type":"Pixy"`, with versions matching the live HID readout
(FIC7608 2.0.4, gimbal/"yuntai" 2.0.5, MCU 2.0.7). The manifest lists each component's
URL + version + MD5. (`piko`/`piko_dual`/`piko_plus` are *different* products, e.g.
`piko_dual` = FIC7605 + Samsung S5K2L9, PID `0x0101`.)

### 9.2 Component map

| Image | Chip / role | Ver | Flash path |
|---|---|---|---|
| `…FIC7608_IMX362_…update.bin` | **FIC7608 ISP** — USB bridge / UVC pipeline / **descriptors** | 2.0.4 | HID DFU (`IMAGE+` container) |
| `yuntai_ap.bin` / `yuntai_cp.bin` / `yuntai_res.bin` | **CSK6** gimbal+AI (app core / co‑proc / AI models) | 2.0.5 | open `cskburn`, per‑block MD5 |
| `project.hex` | **Motor MCU = CW32F030K8U7** (Cortex‑M0+), Intel HEX | 2.0.7 | MCU loader |

Flash maps: CSK6 `ap@0x0 cp@0x100000 res@0x300000 block=0x1000`;
MCU `app 0x0..0xEFFF page=0x200`.

### 9.3 FIC7608 image — is it patchable?

* **Unencrypted.** Entropy 3.24 bits/byte; plaintext strings (`Fic760x`, `IMX362`,
  `YUY2`, `Version: USB->%X,ISP->%X,CTRL->%X`).
* **`IMAGE+` container** (sub‑headers at `0x0`/`0xf3d`/`0x1e09c`); header carries a
  CRC‑class word `0xA8758218`, chip id `0x7608`, version `0x2004` — **no RSA/ECDSA
  signature block visible**.
* **USB descriptor table is in the clear and editable:** device descriptor @ `0x20088`
  (`12 01 … 8f 32 c0 00`), config descriptor @ `0x2104c` (`09 02 74 03 05 01 …`,
  byte‑for‑byte the live `lsusb`), VS format/frame descriptors following.

→ A **descriptor/behaviour binary patch is plausible** (edit bytes, fix CRC, reflash)
*if* the bootloader accepts a CRC‑only image — unverified, and untested by design.

### 9.4 Verdict by goal

* **Windows Hello — no.** Patchability is irrelevant: a descriptor edit can only make
  the device *claim* an IR stream the hardware can't fill (no IR sensor/illuminator).
  Result is a dead stream, or grayscale‑RGB = an insecure spoof that fails liveness and
  is rejected by Enhanced Sign‑in Security. The blocker is hardware, not firmware.
* **Binary patch (descriptors / UVC defaults / strings) — plausible** (see §9.3).
* **Compile FIC7608 from scratch — no** (Fitipower ISA/SDK closed). Patch, don't rebuild.
* **Compile CSK6 — the realistic route** for AI/tracking customization (RISC‑V,
  ListenAI Zephyr SDK, open `cskburn`). Not a Hello path.

---

*Generated from read‑only USB descriptor inspection, static reverse‑engineering of
EMEET STUDIO v1.15.6 and eMeetLink v5.3.0, and read‑only analysis of EMEET's public
OTA firmware images (archived under `firmware/`, MD5‑verified). No firmware flashing
or device writes were performed.*
