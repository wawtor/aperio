#![windows_subsystem = "windows"] // no console window (background daemon)
//! aperio -- Aperio camera daemon (Windows).
//!
//! Event-driven, ~0% idle CPU: blocks on a registry-change notification for the
//! Windows camera-consent store. When an app OPENS the camera -> aim the gimbal at
//! the saved start position and enable Follow (AI tracking). When all apps RELEASE
//! the camera -> park into Privacy (lens-down sleep). Nothing is sent to the device
//! while idle (vendor HID commands reset the camera's privacy timer).
//!
//! Wake/sleep are detected by the NEWEST LastUsedTimeStart / LastUsedTimeStop
//! advancing -- robust to stale/orphaned "in-use" entries (e.g. old Discord versions).
//!
//! Usage:
//!   aperio            run the daemon (event loop)
//!   aperio active     one-shot: aim to start + enable Follow (test)
//!   aperio inactive   one-shot: park to Privacy (test)
//!   aperio find       print the resolved camera HID device path (test)

use std::time::Duration;
use std::{thread, fs, io::Write, path::PathBuf};

use windows::core::{GUID, PCWSTR, PWSTR};
use windows::Win32::Foundation::{
    CloseHandle, BOOL, HANDLE, ERROR_SUCCESS, GENERIC_READ, GENERIC_WRITE,
};
use windows::Win32::System::Registry::{
    RegCloseKey, RegEnumKeyExW, RegNotifyChangeKeyValue, RegOpenKeyExW, RegQueryValueExW,
    HKEY, HKEY_CURRENT_USER, KEY_READ, REG_NOTIFY_CHANGE_LAST_SET, REG_NOTIFY_THREAD_AGNOSTIC,
    REG_VALUE_TYPE,
};
use windows::Win32::System::Threading::{CreateEventW, ResetEvent, WaitForSingleObject, INFINITE};
use windows::Win32::Devices::DeviceAndDriverInstallation::{
    CM_Get_Device_Interface_ListW, CM_Get_Device_Interface_List_SizeW,
    CM_GET_DEVICE_INTERFACE_LIST_PRESENT, CR_SUCCESS,
};
use windows::Win32::Devices::HumanInterfaceDevice::HidD_GetHidGuid;
use windows::Win32::Storage::FileSystem::{
    CreateFileW, WriteFile, FILE_FLAGS_AND_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE,
    OPEN_EXISTING,
};

const WEBCAM: &str =
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam";

// Defaults if start_pos.txt is missing (pan, tilt in degrees).
const DEF_PAN: f32 = 7.5;
const DEF_TILT: f32 = -15.9;

// ---- small helpers ----------------------------------------------------------

fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn log(msg: &str) {
    let line = format!(
        "[{}] {}\n",
        chrono_now(),
        msg
    );
    if let Ok(mut f) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(exe_dir().join("aperio.log"))
    {
        let _ = f.write_all(line.as_bytes());
    }
}

// minimal local timestamp without pulling in a crate
fn chrono_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("t+{}", secs)
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn read_start_pos() -> (f32, f32) {
    if let Ok(s) = fs::read_to_string(exe_dir().join("start_pos.txt")) {
        let parts: Vec<f32> = s.split_whitespace().filter_map(|x| x.parse().ok()).collect();
        if parts.len() >= 2 {
            return (parts[0], parts[1]);
        }
    }
    (DEF_PAN, DEF_TILT)
}

/// Read last_track.state: "1" = Follow (AI tracking), "0" = Standard (fixed).
/// Defaults to true (Follow) if file is missing or unparseable.
fn read_tracking() -> bool {
    if let Ok(s) = fs::read_to_string(exe_dir().join("last_track.state")) {
        return s.trim() != "0";
    }
    true
}

/// Read auto_privacy.state: "1" = park to Privacy on camera release, "0" = leave as-is.
/// Defaults to true if file is missing.
fn read_auto_privacy() -> bool {
    if let Ok(s) = fs::read_to_string(exe_dir().join("auto_privacy.state")) {
        return s.trim() != "0";
    }
    true
}

// ---- HID protocol -----------------------------------------------------------

fn frame(g: u8, p: u8, i: u8, payload: &[u8]) -> [u8; 32] {
    let mut f = [0u8; 32];
    f[0] = 0x09; // Report ID
    f[1] = g;
    f[2] = p;
    f[3] = i;
    f[4] = 0x00;
    f[5] = payload.len() as u8;
    f[6] = 0x00;
    f[7] = payload.len() as u8;
    f[8..8 + payload.len()].copy_from_slice(payload);
    f
}

fn motor_pos(axis: u8, deg: f32) -> [u8; 32] {
    let mut pl = vec![axis];
    pl.extend_from_slice(&deg.to_le_bytes());
    frame(0x63, 0x01, 0x00, &pl) // SET_MOTOR_POS
}

fn device_mode(m: u8) -> [u8; 32] {
    frame(0x01, 0x01, 0x00, &[m]) // SET_DEVICE_MODE (1=Follow, 2=Privacy, 0=Standard)
}

/// Find the Pixy vendor HID interface path (VID 328F / PID 00C0 / MI_04), any USB port.
fn find_pixy() -> Option<Vec<u16>> {
    unsafe {
        let guid = HidD_GetHidGuid();

        let mut len: u32 = 0;
        if CM_Get_Device_Interface_List_SizeW(
            &mut len,
            &guid,
            PCWSTR::null(),
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
        ) != CR_SUCCESS
            || len < 2
        {
            return None;
        }
        let mut buf = vec![0u16; len as usize];
        if CM_Get_Device_Interface_ListW(
            &guid,
            PCWSTR::null(),
            &mut buf,
            CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
        ) != CR_SUCCESS
        {
            return None;
        }
        for s in buf.split(|&c| c == 0).filter(|s| !s.is_empty()) {
            let low = String::from_utf16_lossy(s).to_lowercase();
            if low.contains("vid_328f") && low.contains("pid_00c0") && low.contains("mi_04") {
                let mut v = s.to_vec();
                v.push(0); // NUL-terminate for PCWSTR
                return Some(v);
            }
        }
        None
    }
}

/// Open the device and write each 32-byte frame (with settle delays between).
fn send(frames: &[[u8; 32]]) -> bool {
    let path = match find_pixy() {
        Some(p) => p,
        None => {
            log("send: Pixy HID interface not found");
            return false;
        }
    };
    unsafe {
        let h = match CreateFileW(
            PCWSTR(path.as_ptr()),
            GENERIC_READ.0 | GENERIC_WRITE.0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAGS_AND_ATTRIBUTES(0),
            None,
        ) {
            Ok(h) if !h.is_invalid() => h,
            _ => {
                log("send: CreateFileW failed");
                return false;
            }
        };
        let mut ok = true;
        for (k, fr) in frames.iter().enumerate() {
            let mut written = 0u32;
            if WriteFile(h, Some(&fr[..]), Some(&mut written), None).is_err() {
                log("send: WriteFile failed");
                ok = false;
                break;
            }
            if k + 1 < frames.len() {
                thread::sleep(Duration::from_millis(400));
            }
        }
        let _ = CloseHandle(h);
        ok
    }
}

fn on_active() {
    let (pan, tilt) = read_start_pos();
    let track = read_tracking();
    let mode  = if track { 1 } else { 0 };  // 1=Follow, 0=Standard
    log(&format!(
        "camera ACTIVE -> goto {:.1}/{:.1} + mode={} ({})",
        pan, tilt, mode, if track { "Follow" } else { "Standard" }
    ));
    let frames = [motor_pos(1, pan), motor_pos(2, tilt), device_mode(mode)];
    let ok = send(&frames);
    log(if ok { "  -> sent" } else { "  -> send FAILED" });
}

fn on_inactive() {
    log("camera INACTIVE -> Privacy (park/sleep)");
    let ok = send(&[device_mode(2)]);
    log(if ok { "  -> sent (privacy)" } else { "  -> send FAILED" });
}

// ---- registry scan (newest open/close timestamps) ---------------------------

unsafe fn read_qword(key: HKEY, name: &str) -> Option<u64> {
    let n = wide(name);
    let mut ty = REG_VALUE_TYPE(0);
    let mut data = [0u8; 8];
    let mut sz = 8u32;
    let r = RegQueryValueExW(
        key,
        PCWSTR(n.as_ptr()),
        None,
        Some(&mut ty),
        Some(data.as_mut_ptr()),
        Some(&mut sz),
    );
    if r == ERROR_SUCCESS && sz == 8 {
        Some(u64::from_le_bytes(data))
    } else {
        None
    }
}

unsafe fn walk(key: HKEY, depth: i32, max_start: &mut u64, max_stop: &mut u64) {
    if let Some(v) = read_qword(key, "LastUsedTimeStart") {
        if v > *max_start {
            *max_start = v;
        }
    }
    if let Some(v) = read_qword(key, "LastUsedTimeStop") {
        if v > *max_stop {
            *max_stop = v;
        }
    }
    if depth <= 0 {
        return;
    }
    let mut i = 0u32;
    loop {
        let mut name = [0u16; 256];
        let mut nlen = name.len() as u32;
        let r = RegEnumKeyExW(
            key,
            i,
            PWSTR(name.as_mut_ptr()),
            &mut nlen,
            None,
            PWSTR::null(),
            None,
            None,
        );
        if r != ERROR_SUCCESS {
            break;
        }
        i += 1;
        let mut sub = HKEY::default();
        if RegOpenKeyExW(key, PCWSTR(name.as_ptr()), 0, KEY_READ, &mut sub) == ERROR_SUCCESS {
            walk(sub, depth - 1, max_start, max_stop);
            let _ = RegCloseKey(sub);
        }
    }
}

fn scan() -> (u64, u64) {
    let mut ms = 0u64;
    let mut mc = 0u64;
    unsafe {
        let mut key = HKEY::default();
        let wp = wide(WEBCAM);
        if RegOpenKeyExW(HKEY_CURRENT_USER, PCWSTR(wp.as_ptr()), 0, KEY_READ, &mut key)
            == ERROR_SUCCESS
        {
            walk(key, 2, &mut ms, &mut mc);
            let _ = RegCloseKey(key);
        }
    }
    (ms, mc)
}

// ---- daemon -----------------------------------------------------------------

fn run_daemon() {
    log("aperio started (event-driven; idle = no device I/O)");
    unsafe {
        let wp = wide(WEBCAM);
        let mut key = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, PCWSTR(wp.as_ptr()), 0, KEY_READ, &mut key)
            != ERROR_SUCCESS
        {
            log("FATAL: cannot open webcam ConsentStore key");
            return;
        }
        let event = match CreateEventW(None, BOOL(1), BOOL(0), PCWSTR::null()) {
            Ok(e) => e,
            Err(_) => {
                log("FATAL: CreateEventW failed");
                return;
            }
        };
        let (mut last_open, mut last_close) = scan();
        log(&format!("baseline open_ts={} close_ts={}", last_open, last_close));

        loop {
            let _ = ResetEvent(event);
            let r = RegNotifyChangeKeyValue(
                key,
                BOOL(1), // watch subtree
                REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_THREAD_AGNOSTIC,
                event,
                BOOL(1), // asynchronous (signal the event)
            );
            if r != ERROR_SUCCESS {
                log("FATAL: RegNotifyChangeKeyValue failed");
                break;
            }
            WaitForSingleObject(event, INFINITE);
            thread::sleep(Duration::from_millis(300)); // debounce burst of writes

            let (o, c) = scan();
            let opened = o > last_open;
            let closed = c > last_close;
            if o > last_open {
                last_open = o;
            }
            if c > last_close {
                last_close = c;
            }
            if opened {
                on_active();
            } else if closed && read_auto_privacy() {
                on_inactive();
            }
        }
        let _ = RegCloseKey(key);
    }
}

fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("active") => on_active(),
        Some("inactive") => on_inactive(),
        Some("find") => match find_pixy() {
            Some(p) => log(&format!("found: {}", String::from_utf16_lossy(&p))),
            None => log("not found"),
        },
        _ => run_daemon(),
    }
}
