EMEET Pixy - Reverse-Engineering research artifacts
===================================================
Supporting evidence + tooling used to RE the EMEET Pixy (VID 328F / PID 00C0)
control protocol. The finished documentation is ..\REpixy.md. The operational
helper scripts (pixy.py, pixy_autotrack.py, *.bat, webcam_usage.ps1) live in the
parent folder and are what the EmeetPixyAutoTrack task runs.

analysis-scripts/   Static RE of the vendor apps (lief + capstone over the
                    EMEET STUDIO Mach-O):
                      recover_final.py  - CANONICAL: recovers all 61 HID command
                                          headers via the static initializers.
                      recover_cmds/2/3.py - earlier iterations (kept for history).
                      ddis.py / floatdis.py - symbolizing disassemblers used to
                                          recover the frame format, payloads,
                                          MotorType, and the pan/tilt limits.

live-test-scripts/  Scripts run against the real device over HID:
                      hidquery*.py, hiddiag.py, hidfinal.py  - READ-ONLY queries
                          (version/SN/mode/position/privacy) - confirmed protocol.
                      hidmove.py / hidmove2.py - the verified, reversible pan test.

data/               Recovered artifacts / evidence:
                      cmd_headers.txt      - the 61 recovered HID command headers
                      studio_symbols.txt   - demangled C++ symbols (EMEET STUDIO)
                      studio_mangled.txt   - raw mangled symbols
                      emeetlink_strings.txt - strings dump (eMeetLink)
                      usb_descriptors.txt  - full lsusb -v of the Pixy
                      dlpage.html          - EMEET downloads page scrape

NOT included (bulky, re-downloadable - official URLs are in REpixy.md section 3):
  eMeetLink_macOS V5.3.0 .dmg, EMEET STUDIO V1.15.6 .pkg / .exe, and their
  extracted .app bundles / 299MB Mach-O (~1.1 GB total).
