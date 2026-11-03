# DEVICEIQ — Android Diagnostic Workstation

Cross-platform desktop application for Windows, macOS and Linux. It uses Android Debug Bridge (ADB) to discover Android devices, read device properties, collect diagnostic snapshots, launch interactive test screens on the phone, record technician-observed results, persist inspection history in SQLite and export reports.

## Important scope and honesty

ADB does not expose every hardware measurement on every Android model. DEVICEIQ collects information actually available through Android/ADB and provides guided on-device/manual tests for physical features. A test marked PASS means the technician observed and recorded a pass; it is not represented as an automated hardware measurement. Battery health percentage is not universally available through standard ADB; `dumpsys battery` reports live battery state/temperature/voltage/level where exposed, not necessarily battery capacity or cycle health. Do not treat those values as battery-health certification.

This initial release is an operational Android workstation, not a manufacturer-authorized diagnostic suite or a replacement for official OEM service tools. It does not bypass screen locks, activation locks, FRP, MDM or other security controls. iOS is not supported by ADB.

## Requirements

- Python 3.10 or newer
- Android SDK Platform-Tools (`adb`)
- USB data cable
- Android phone with Developer options and USB debugging enabled
- On the phone, accept the RSA authorization prompt for the workstation

Install ADB:
- Windows/macOS/Linux: download official Android SDK Platform-Tools from https://developer.android.com/tools/releases/platform-tools
- Add the `platform-tools` directory to PATH.
- Confirm with `adb version`.

## Run from source

```bash
python deviceiq.py
```

No third-party Python packages are required; the UI uses Python's Tkinter and records are stored in `~/.deviceiq/deviceiq.sqlite3` (Windows uses the current user's home directory).

## Build standalone executable

Install PyInstaller:

```bash
python -m pip install pyinstaller
pyinstaller --noconfirm --windowed --name DEVICEIQ deviceiq.py
```

The executable is created in `dist/`. ADB is an external dependency: install Android Platform-Tools and ensure `adb` is on PATH. For managed deployments, bundle the matching platform-tools binary and comply with its license/redistribution terms.

## Workflow

1. Start DEVICEIQ.
2. Connect an Android phone using a USB data cable.
3. Enable Developer options > USB debugging.
4. Accept the RSA authorization prompt on the phone.
5. Click Refresh and select the device whose state is `device` (not `unauthorized` or `offline`).
6. Read device information.
7. Run ADB diagnostics.
8. Start an inspection.
9. Select each interactive test and click **Trigger selected test on phone**. Complete the actual test on the phone, then record PASS/FAIL/N/A.
10. Add notes where useful.
11. Finalize the inspection. Incomplete or failed tests are not approved.
12. Export the HTML report or CSV results.

## Diagnostic commands collected

- `getprop`: manufacturer, model, product, Android version, SDK and build fingerprint
- `dumpsys battery`
- `dumpsys meminfo`
- `df -h /data`
- `/proc/cpuinfo`, `/proc/uptime`
- `wm size`, `wm density`
- `ip addr`
- `dumpsys wifi`
- `dumpsys bluetooth_manager`
- `dumpsys sensorservice`
- `dumpsys thermalservice`
- `pm list packages -3`
- verified boot / bootloader properties where exposed

Unavailable commands are explicitly labelled. Some commands vary by Android release and vendor.

## Data and privacy

Inspection records and audit events are stored locally in SQLite. Diagnostic snapshots can contain device identifiers, network details, installed package names and other potentially sensitive information. Store exports securely, obtain device-owner authorization, and follow your organization's privacy policy. DEVICEIQ does not upload data to a cloud service.

## Troubleshooting

- **ADB not found:** install Platform-Tools and add `adb` to PATH; restart DEVICEIQ.
- **unauthorized:** unlock the phone and accept the USB debugging authorization prompt. Revoke USB debugging authorizations only on a device you own/manage, then reconnect.
- **No device:** try a data-capable cable, another USB port and the correct USB mode; install the relevant Windows OEM USB driver if needed.
- **Multiple devices:** choose the correct serial from the dropdown.
- **Command unavailable:** Android vendors restrict or change some shell services; the result is retained as unavailable rather than fabricated.
- **Tkinter missing on Linux:** install your distribution's `python3-tk` package.

## Security notes

Only connect devices you are authorized to inspect. Keep USB debugging disabled on devices after inspection where appropriate. DEVICEIQ deliberately does not implement security bypasses, covert data extraction or destructive testing.
