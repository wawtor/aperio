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
//! Optional local API server (off by default, toggled from the setup GUI via
//! api_server.state): HTTP on 127.0.0.1:4750 so user applications can drive
//! the camera. See the "local API server" section for endpoints.
//!
//! Usage:
//!   aperio            run the daemon (event loop)
//!   aperio active     one-shot: aim to start + enable Follow (test)
//!   aperio inactive   one-shot: park to Privacy (test)
//!   aperio find       print the resolved camera HID device path (test)

use std::io::{BufRead, BufReader};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use std::{thread, fs, io::Write, path::PathBuf};

use windows::core::{GUID, PCWSTR, PWSTR};
use windows::Win32::Foundation::{
    CloseHandle, BOOL, HANDLE, ERROR_SUCCESS, GENERIC_READ, GENERIC_WRITE, WAIT_OBJECT_0,
    WAIT_TIMEOUT, WAIT_FAILED,
};
use windows::Win32::System::Registry::{
    RegCloseKey, RegEnumKeyExW, RegNotifyChangeKeyValue, RegOpenKeyExW, RegQueryValueExW,
    HKEY, HKEY_CURRENT_USER, KEY_READ, REG_NOTIFY_CHANGE_LAST_SET, REG_NOTIFY_THREAD_AGNOSTIC,
    REG_VALUE_TYPE,
};
use windows::Win32::System::Threading::{
    CreateEventW, ResetEvent, WaitForMultipleObjects, WaitForSingleObject, INFINITE,
};
use windows::Win32::Devices::DeviceAndDriverInstallation::{
    CM_Get_Device_Interface_ListW, CM_Get_Device_Interface_List_SizeW,
    CM_GET_DEVICE_INTERFACE_LIST_PRESENT, CR_SUCCESS,
};
use windows::Win32::Devices::HumanInterfaceDevice::HidD_GetHidGuid;
use windows::Win32::Storage::FileSystem::{
    CreateFileW, FindFirstChangeNotificationW, FindNextChangeNotification, WriteFile,
    FILE_FLAGS_AND_ATTRIBUTES, FILE_NOTIFY_CHANGE_LAST_WRITE, FILE_SHARE_READ,
    FILE_SHARE_WRITE, OPEN_EXISTING,
};

const WEBCAM: &str =
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam";

// Defaults if start_pos.txt is missing (pan, tilt in degrees).
const DEF_PAN: f32 = 7.5;
const DEF_TILT: f32 = -15.9;

// Local API server (opt-in via api_server.state, loopback only).
const API_PORT: u16 = 4750;

// Grace period before parking to Privacy after the last app releases the camera.
// Apps renegotiating the stream (e.g. Windows Camera switching photo/video mode)
// release and reopen within a second or two; parking immediately makes the
// reopen land on a mid-park camera that discards commands and fails the stream.
const PARK_DELAY: Duration = Duration::from_secs(5);

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

/// Read image_flip.state: Some(true) = image flipped 180° (upside-down mount),
/// Some(false) = normal. None if the file is missing -> never touch the flip state.
fn read_flip() -> Option<bool> {
    fs::read_to_string(exe_dir().join("image_flip.state"))
        .ok()
        .map(|s| s.trim() == "1")
}

/// Read image_mirror.state: Some(true) = horizontal mirror on. None if the file
/// is missing -> never touch the reverse state on mirror's account.
fn read_mirror() -> Option<bool> {
    fs::read_to_string(exe_dir().join("image_mirror.state"))
        .ok()
        .map(|s| s.trim() == "1")
}

/// Read api_server.state: "1" = run the local API server. Defaults to false (off).
fn read_api_enabled() -> bool {
    if let Ok(s) = fs::read_to_string(exe_dir().join("api_server.state")) {
        return s.trim() == "1";
    }
    false
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

fn motor_rel(axis: u8, deg: f32) -> [u8; 32] {
    let mut pl = vec![axis];
    pl.extend_from_slice(&deg.to_le_bytes());
    frame(0x63, 0x01, 0x19, &pl) // MOVE_MOTOR_REL
}

fn device_mode(m: u8) -> [u8; 32] {
    frame(0x01, 0x01, 0x00, &[m]) // SET_DEVICE_MODE (1=Follow, 2=Privacy, 0=Standard)
}

fn reverse_sta(rtype: u8, on: bool) -> [u8; 32] {
    frame(0x04, 0x00, 0x08, &[rtype, on as u8]) // SET_REVERSE_STA (1=horizontal, 2=vertical)
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

// Serializes device access between the event loop and the API server thread.
static SEND_LOCK: Mutex<()> = Mutex::new(());

/// Open the device and write each 32-byte frame (with settle delays between).
fn send(frames: &[[u8; 32]]) -> bool {
    let _guard = SEND_LOCK.lock().unwrap_or_else(|e| e.into_inner());
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
        "camera ACTIVE -> wake + goto {:.1}/{:.1} + mode={} ({})",
        pan, tilt, mode, if track { "Follow" } else { "Standard" }
    ));
    // The stream-open self-wake raises the lens toward factory 0/0 -- the
    // stored power-on default is honored only for HID wakes and power-on.
    // Aim immediately to redirect the rise mid-wake, then aim again once the
    // unpark has settled: the first send can still land inside the parked
    // window where motor commands are silently discarded.
    let mut ok = send(&[device_mode(0), motor_pos(1, pan), motor_pos(2, tilt)]);
    thread::sleep(Duration::from_millis(1200));
    ok &= send(&[motor_pos(1, pan), motor_pos(2, tilt)]);
    // Reapply image orientation each wake: toggles made while the camera was
    // parked in Privacy are discarded by the device, so the saved state is
    // authoritative. Flip (180°) reverses both axes, mirror reverses horizontal.
    let flip = read_flip();
    let mirror = read_mirror();
    if flip.is_some() || mirror.is_some() {
        let f = flip.unwrap_or(false);
        let m = mirror.unwrap_or(false);
        thread::sleep(Duration::from_millis(400));
        ok &= send(&[reverse_sta(1, f != m), reverse_sta(2, f)]);
    }
    if mode != 0 {
        thread::sleep(Duration::from_millis(400));
        ok &= send(&[device_mode(mode)]);
    }
    log(if ok { "  -> sent" } else { "  -> send FAILED" });
}

fn on_inactive() {
    log("camera INACTIVE -> Standard, then Privacy (park/sleep)");
    // Drop to Standard before parking: a camera parked while in Follow wakes
    // with the AI already hunting for a subject (fast pan sweep) before the
    // daemon can re-aim it. Parked from Standard it wakes still.
    let ok = send(&[device_mode(0), device_mode(2)]);
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

// ---- local API server (opt-in) -----------------------------------------------
//
// Minimal HTTP/1.1 endpoint on 127.0.0.1:API_PORT so user applications can
// drive the camera. Enabled only while api_server.state contains "1".
//
//   GET  /                                endpoint index (JSON)
//   GET  /status                          daemon + config info (JSON)
//   POST /move?pan=X&tilt=Y               absolute move (deg, either optional)
//   POST /move_rel?pan=X&tilt=Y           relative move (deg, either optional)
//   POST /mode?value=follow|standard|privacy
//   POST /flip?value=on|off               image flip 180° (upside-down mount)
//   POST /mirror?value=on|off             horizontal mirror
//   POST /home                            go to the saved startup position
//   POST /shutdown                        turn the API server off (persists)

struct ApiServer {
    stop: Arc<AtomicBool>,
    handle: thread::JoinHandle<()>,
}

fn start_api_server() -> Option<ApiServer> {
    let listener = match TcpListener::bind(("127.0.0.1", API_PORT)) {
        Ok(l) => l,
        Err(e) => {
            log(&format!("api: bind 127.0.0.1:{} failed: {}", API_PORT, e));
            return None;
        }
    };
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    let handle = thread::spawn(move || {
        log(&format!("api: listening on 127.0.0.1:{}", API_PORT));
        for conn in listener.incoming() {
            if flag.load(Ordering::SeqCst) {
                break;
            }
            if let Ok(mut c) = conn {
                let _ = c.set_read_timeout(Some(Duration::from_secs(5)));
                handle_client(&mut c);
            }
        }
        log("api: stopped");
    });
    Some(ApiServer { stop, handle })
}

fn stop_api_server(s: ApiServer) {
    s.stop.store(true, Ordering::SeqCst);
    let _ = TcpStream::connect(("127.0.0.1", API_PORT)); // unblock accept()
    let _ = s.handle.join();
}

fn respond(c: &mut TcpStream, status: &str, body: &str) {
    let _ = c.write_all(
        format!(
            "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            status,
            body.len(),
            body
        )
        .as_bytes(),
    );
}

fn query_f32(query: &str, key: &str) -> Option<f32> {
    query
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .find(|(k, _)| *k == key)
        .and_then(|(_, v)| v.parse().ok())
}

fn query_str<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .find(|(k, _)| *k == key)
        .map(|(_, v)| v)
}

fn handle_client(c: &mut TcpStream) {
    let mut line = String::new();
    if BufReader::new(&mut *c).read_line(&mut line).is_err() {
        return;
    }
    let mut parts = line.split_whitespace();
    let (method, target) = match (parts.next(), parts.next()) {
        (Some(m), Some(t)) => (m, t),
        _ => return,
    };
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p, q),
        None => (target, ""),
    };

    match (method, path) {
        ("GET", "/") => {
            respond(c, "200 OK",
                "{\"name\":\"aperio\",\"port\":4750,\"endpoints\":[\"GET /\",\"GET /status\",\"POST /move?pan=X&tilt=Y\",\"POST /move_rel?pan=X&tilt=Y\",\"POST /mode?value=follow|standard|privacy\",\"POST /flip?value=on|off\",\"POST /mirror?value=on|off\",\"POST /home\",\"POST /shutdown\"]}");
        }
        ("POST", "/shutdown") => {
            log("api: /shutdown -> disabling api server");
            let _ = fs::write(exe_dir().join("api_server.state"), "0\n");
            respond(c, "200 OK", "{\"ok\":true,\"note\":\"api server disabled\"}");
        }
        ("GET", "/status") => {
            let (pan, tilt) = read_start_pos();
            let opt = |v: Option<bool>| match v {
                Some(true) => "true",
                Some(false) => "false",
                None => "null",
            };
            let body = format!(
                "{{\"name\":\"aperio\",\"camera_found\":{},\"tracking\":{},\"auto_privacy\":{},\"flip\":{},\"mirror\":{},\"start_pos\":{{\"pan\":{:.2},\"tilt\":{:.2}}}}}",
                find_pixy().is_some(),
                read_tracking(),
                read_auto_privacy(),
                opt(read_flip()),
                opt(read_mirror()),
                pan,
                tilt
            );
            respond(c, "200 OK", &body);
        }
        ("POST", "/move") | ("POST", "/move_rel") => {
            let rel = path == "/move_rel";
            let pan = query_f32(query, "pan");
            let tilt = query_f32(query, "tilt");
            if pan.is_none() && tilt.is_none() {
                respond(c, "400 Bad Request", "{\"ok\":false,\"error\":\"pan and/or tilt required\"}");
                return;
            }
            let mut frames: Vec<[u8; 32]> = Vec::new();
            if let Some(p) = pan {
                let p = p.clamp(-150.0, 150.0);
                frames.push(if rel { motor_rel(1, p) } else { motor_pos(1, p) });
            }
            if let Some(t) = tilt {
                let t = t.clamp(-90.0, 90.0);
                frames.push(if rel { motor_rel(2, t) } else { motor_pos(2, t) });
            }
            log(&format!("api: {} pan={:?} tilt={:?}", path, pan, tilt));
            if send(&frames) {
                respond(c, "200 OK", "{\"ok\":true}");
            } else {
                respond(c, "502 Bad Gateway", "{\"ok\":false,\"error\":\"camera not reachable\"}");
            }
        }
        ("POST", "/mode") => {
            let m = match query_str(query, "value") {
                Some("follow") => 1u8,
                Some("standard") => 0u8,
                Some("privacy") => 2u8,
                _ => {
                    respond(c, "400 Bad Request", "{\"ok\":false,\"error\":\"value must be follow|standard|privacy\"}");
                    return;
                }
            };
            log(&format!("api: /mode value={}", m));
            if send(&[device_mode(m)]) {
                respond(c, "200 OK", "{\"ok\":true}");
            } else {
                respond(c, "502 Bad Gateway", "{\"ok\":false,\"error\":\"camera not reachable\"}");
            }
        }
        ("POST", "/flip") | ("POST", "/mirror") => {
            let on = match query_str(query, "value") {
                Some("on") | Some("1") | Some("true") => true,
                Some("off") | Some("0") | Some("false") => false,
                _ => {
                    respond(c, "400 Bad Request", "{\"ok\":false,\"error\":\"value must be on|off\"}");
                    return;
                }
            };
            log(&format!("api: {} value={}", path, on));
            let state = if path == "/flip" { "image_flip.state" } else { "image_mirror.state" };
            let _ = fs::write(exe_dir().join(state), if on { "1\n" } else { "0\n" });
            let f = read_flip().unwrap_or(false);
            let m = read_mirror().unwrap_or(false);
            if send(&[reverse_sta(1, f != m), reverse_sta(2, f)]) {
                respond(c, "200 OK", "{\"ok\":true}");
            } else {
                respond(c, "502 Bad Gateway", "{\"ok\":false,\"error\":\"camera not reachable\"}");
            }
        }
        ("POST", "/home") => {
            let (pan, tilt) = read_start_pos();
            log("api: /home");
            if send(&[motor_pos(1, pan), motor_pos(2, tilt)]) {
                respond(c, "200 OK", "{\"ok\":true}");
            } else {
                respond(c, "502 Bad Gateway", "{\"ok\":false,\"error\":\"camera not reachable\"}");
            }
        }
        _ => respond(c, "404 Not Found", "{\"ok\":false,\"error\":\"unknown endpoint\"}"),
    }
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
        // watch the config dir so GUI toggles / api shutdown apply immediately
        let dir_w = wide(&exe_dir().to_string_lossy());
        let dir_notif = FindFirstChangeNotificationW(
            PCWSTR(dir_w.as_ptr()),
            BOOL(0),
            FILE_NOTIFY_CHANGE_LAST_WRITE,
        )
        .ok();
        if dir_notif.is_none() {
            log("WARN: config-dir watch unavailable; api toggle applies on camera events only");
        }

        let (mut last_open, mut last_close) = scan();
        log(&format!("baseline open_ts={} close_ts={}", last_open, last_close));

        let mut api: Option<ApiServer> = if read_api_enabled() {
            start_api_server()
        } else {
            None
        };
        let mut reg_armed = false;
        // Pending Privacy park: set when all apps release the camera, executed
        // PARK_DELAY later unless an app reopens the camera in the meantime.
        let mut park_at: Option<std::time::Instant> = None;

        loop {
            if !reg_armed {
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
                reg_armed = true;
            }

            let timeout_ms = match park_at {
                Some(t) => t
                    .saturating_duration_since(std::time::Instant::now())
                    .as_millis() as u32,
                None => INFINITE,
            };
            let w = match dir_notif {
                Some(dn) => WaitForMultipleObjects(&[event, dn], BOOL(0), timeout_ms),
                None => WaitForSingleObject(event, timeout_ms),
            };
            let fired = if w == WAIT_TIMEOUT {
                u32::MAX // park grace period elapsed
            } else if w == WAIT_FAILED {
                log("FATAL: wait failed");
                break;
            } else {
                w.0.wrapping_sub(WAIT_OBJECT_0.0)
            };

            if fired == u32::MAX {
                // no reopen within the grace period -> park now
                park_at = None;
                if read_auto_privacy() {
                    on_inactive();
                }
            } else if fired == 0 {
                // registry (camera consent store) changed
                reg_armed = false;
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
                    if park_at.take().is_some() {
                        // reopen within the grace period: the camera was never
                        // parked and is still aimed -- stay silent so the app's
                        // stream renegotiation sees an untouched device.
                        log("camera reopened within park grace period -> no action");
                    } else {
                        on_active();
                    }
                } else if closed && read_auto_privacy() {
                    log(&format!(
                        "camera released -> parking in {}s unless reopened",
                        PARK_DELAY.as_secs()
                    ));
                    park_at = Some(std::time::Instant::now() + PARK_DELAY);
                }
            } else if fired == 1 {
                // config dir changed (GUI toggle or api /shutdown)
                thread::sleep(Duration::from_millis(150)); // let the write finish
                if let Some(dn) = dir_notif {
                    let _ = FindNextChangeNotification(dn);
                }
            } else {
                log("FATAL: wait failed");
                break;
            }

            // apply the API-server toggle
            let want_api = read_api_enabled();
            if want_api && api.is_none() {
                api = start_api_server();
            } else if !want_api && api.is_some() {
                stop_api_server(api.take().unwrap());
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
