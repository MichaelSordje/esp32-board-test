# ESP32 Board Test

[![CI](https://github.com/MichaelSordje/esp32-board-test/actions/workflows/ci.yml/badge.svg)](https://github.com/MichaelSordje/esp32-board-test/actions/workflows/ci.yml)

Automated hardware and WiFi quality tester for ESP32 boards.

Connect a board by USB, run the test, and get a reproducible `PASS` or `FAIL`
result with detailed reports, logs, performance measurements, RF measurements,
peer comparison, and optional label printing.

The host tools support **Windows 10/11 and Linux**. Windows printing supports a
standard Windows printer driver or Brother b-PAC. Linux printing uses CUPS.

**Linux status: UNTESTED on real hardware.** The Linux artifact workflow is
implemented and syntax/path checked, but has not yet completed an end-to-end
physical Linux DUT/reference test.

See [LINUX.md](LINUX.md) for Linux-specific setup details.

## Supported boards

| ESP32 type | PlatformIO environment |
|---|---|
| Classic ESP32 | `esp32` |
| ESP32-C3 | `esp32-c3` |
| ESP32-S3 without detected PSRAM | `esp32-s3` |
| ESP32-S3 with 16 MB flash / 8 MB PSRAM | `esp32-s3-n16r8` |

The correct DUT profile is selected automatically from the detected hardware.
Unsupported ESP32-S3 flash/PSRAM combinations are rejected instead of being
guessed.

## Requirements

Common requirements:

- Python 3.10+
- USB data cable
- 2.4 GHz WiFi when the resolved test profile enables normal WiFi testing
- one dedicated ESP32 reference board over USB when `rf_quality.enabled=true`
- Internet access on first setup and when Python/PlatformIO dependencies must be installed

### Windows

- Windows 10 or Windows 11
- an installed Windows printer driver only if generic label printing is used
- Brother b-PAC only if the `brother-bpac` backend is used

The Windows DUT workflow uses **precompiled firmware artifacts**.

Optional: prepare the isolated PlatformIO package sets for DUT and reference
firmware before compiling:

```text
scripts\prepare_all.cmd
```

After a fresh clone, and again whenever the tester reports that firmware
artifacts are missing or outdated, run:

```text
scripts\compile_all.cmd
```

This builds the four DUT firmware environments and publishes verified component
images under the ignored local `firmware-artifacts/dut/` directory.

Then start a board test with:

```text
scripts\start-test.cmd
```

During a normal Windows board test, PlatformIO/compiler access is not used for
the DUT. The selected precompiled images are verified and flashed directly with
esptool.

The current DUT artifact layout contains:

```text
bootloader.bin
partitions.bin
boot_app0.bin
firmware.bin
```

For ESP32-S3 / ESP32-S3-N16R8 the offsets are:

```text
0x0000   bootloader.bin
0x8000   partitions.bin
0xE000   boot_app0.bin
0x10000  firmware.bin
```

Prepare reference firmware once (and again after reference-source changes):

```text
scripts\compile_reference_all.cmd
```

`scripts\flash-reference.cmd` then selects the matching verified image from
`firmware-artifacts/reference/` and flashes it directly with esptool.

### Linux

**Status: UNTESTED on real Linux hardware.**

Typical Debian/Ubuntu prerequisites:

```bash
sudo apt install python3 python3-venv
```

The host `ping` command is required only when an enabled test actually uses it
(soak ping, controlled reconnect, or WiFi+BLE coexistence):

```bash
sudo apt install iputils-ping
```

Optional CUPS label printing:

```bash
sudo apt install cups-client
```

Build/update the four DUT firmware artifacts:

```bash
bash scripts/compile_all.sh
```

Optional PlatformIO package preparation for all DUT/reference profiles:

```bash
bash scripts/prepare_all.sh
```

Then run board tests with:

```bash
bash scripts/start-test.sh
```

Linux now uses the same compile-once DUT artifact architecture as Windows.
`compile_all.sh` performs the PlatformIO/compiler work; `start-test.sh` verifies
the artifacts and calls `tools/run_test_artifact.py`, so PlatformIO/compiler is
not invoked for the DUT during a normal board test.

Build reference artifacts separately with `bash scripts/compile_reference_all.sh`.
The subsequent `flash-reference.sh` uses only verified artifacts and esptool.

See [LINUX.md](LINUX.md).

## Important: the test overwrites the board

This project is intended for testing empty/new ESP32 boards.

Running the test flashes its own firmware and partition table to the ESP32. The
previously installed application and partition layout are overwritten. The
dedicated `hwtest` flash partition is erased, written, read, and verified. The
NVS test writes to and clears its own `hwtest` namespace.

**Do not run the tester on a board that contains firmware or data you still
need.**

## Setup

### WiFi credentials

WiFi credentials are needed only when the resolved board profile has
`wifi.enabled=true`.

Copy:

```text
secrets.example.ini
```

to:

```text
secrets.ini
```

and enter the local WiFi credentials:

```ini
WIFI_SSID=MyWiFi
WIFI_PASSWORD=MyPassword
```

`secrets.ini` is ignored by Git. Credentials are read after the board profile
and test selection are known and are sent to the DUT over serial. They are not
compiled into the DUT firmware.

The dedicated RF reference test does not use the home-WiFi credentials. It uses
its own temporary private reference SoftAP.

### Local test settings

Tracked public settings:

```text
config/test-settings.json
```

Machine/test-station-specific overrides:

```text
config/test-settings.local.json
```

The local file is ignored by Git and recursively merged over the tracked file.

Use the local file for:

- the fixed reference ESP MAC
- fixture-specific RF thresholds
- local WiFi/test duration choices
- local label/printer settings

Example:

```json
{
  "tests": {
    "default": {
      "rf_quality": {
        "reference_mac": "AA:BB:CC:DD:EE:FF"
      },
      "soak": {
        "duration_minutes": 5
      }
    }
  },
  "label": {
    "mode": "off"
  }
}
```

Windows `-DurationMinutes` and Linux/Python `--duration-minutes` override the
configured soak duration for that run.

See [TEST-SETTINGS.md](TEST-SETTINGS.md) for the complete settings reference.

## RF quality and home-network stability

The tester separates two different questions:

1. **RF QUALITY** measures the DUT radio/link against a dedicated, fixed
   reference ESP. The PC coordinates the test but is not in the RF packet path.
2. **STABILITY** observes the normal home-network connection over time. This is
   diagnostic because the AP, routing, host, other clients, and local
   interference are not a controlled RF fixture.

### Reference ESP: one-time setup

Windows:

```text
scripts\flash-reference.cmd
```

Linux:

```bash
bash scripts/flash-reference.sh
```

The reference flash tool detects the connected ESP32 profile and builds/uploads
`reference-firmware/` through PlatformIO. If several unconfigured ESP32 boards
are connected, select the intended reference board explicitly.

After flashing, normal tests discover the reference by its serial
`REF|READY`/`REF|INFO` identity. The reference radio is normally off and is
enabled as a private SoftAP only during the RF phase.

See [REFERENCE-ESP.md](REFERENCE-ESP.md) for placement, protocol, and
calibration details.

### RF threshold calibration is fixture-specific

RF RSSI limits are **not universal ESP32 specifications**. They depend on at
least:

- the reference ESP
- reference/DUT distance
- board orientation
- fixture construction and nearby objects
- selected RF channel
- reference firmware/RF mode
- DUT profile

Therefore every physical test station must calibrate its own RF thresholds
before using `RF QUALITY: PASS/FAIL` as a production decision.

The tracked configuration currently contains calibrated values for
`esp32-s3-n16r8`:

```json
{
  "reference_to_dut_min_rssi_dbm": -42,
  "dut_to_reference_min_rssi_dbm": -38,
  "max_loss_percent": 1.0
}
```

These values belong to the maintainer's calibrated fixture. **Do not assume
they are correct for another reference board, distance, orientation, or test
station.**

For a new fixture, override the profile thresholds locally to `null` first:

```json
{
  "tests": {
    "profiles": {
      "esp32-s3-n16r8": {
        "rf_quality": {
          "thresholds": {
            "reference_to_dut_min_rssi_dbm": null,
            "dut_to_reference_min_rssi_dbm": null,
            "max_loss_percent": null
          }
        }
      }
    }
  }
}
```

With all three thresholds `null`, a completed RF measurement is
`RF QUALITY: UNRATED`. Raw values are still written to `summary.json`,
`report.txt`/`report.html`, and `rf-quality.csv`.

After collecting repeatable measurements from multiple independently verified
good boards and preferably one or more known weak/bad boards in the final
fixture, set your own numeric values in `config/test-settings.local.json`.

When at least one numeric RF threshold is configured, RF quality is rated as
`PASS` or `FAIL`; the `UNRATED` calibration warnings no longer appear for a
normally completed rated measurement.

The RF test uses one controlled fixed-power session:

- 802.11b only
- fixed 1 Mbit/s long-preamble PHY rate
- DUT power save disabled
- fixed reference TX power
- DUT left at the driver's normal maximum TX power
- configured repetitions and packet count in both directions

### Home-network soak

The optional soak measures:

- ICMP/ping
- UDP
- WiFi disconnect/reconnect state
- serial/UDP heartbeats
- heap trend and periodic heap-integrity checks

A soak `FAIL` is reported as `STABILITY: FAIL`, but the home-network soak alone
does not classify the antenna/radio as defective.

To skip only the long soak:

```json
{
  "tests": {
    "default": {
      "soak": {
        "enabled": false
      }
    }
  }
}
```

WiFi setup, controlled reconnect, BLE coexistence, and RF quality can still run.

## Serial access on Linux

USB serial devices such as `/dev/ttyUSB0` and `/dev/ttyACM0` require normal user
access.

On Debian/Ubuntu:

```bash
sudo usermod -aG dialout "$USER"
```

Log out and back in afterwards.

## Run a test

### Windows

Compile/update DUT artifacts when required:

```text
scripts\compile_all.cmd
```

Run:

```text
scripts\start-test.cmd
```

Optional:

```powershell
.\scripts\start-test.ps1 -Port COM8 -ReferencePort COM11 -DurationMinutes 5
```

The Windows launcher calls `tools/run_test_artifact.py`, which installs only the
artifact build/flash boundary and then delegates to the existing
`tools/run_test.py` / orchestrator.

### Linux

**UNTESTED on real Linux hardware.**

Compile/update DUT artifacts when required:

```bash
bash scripts/compile_all.sh
```

Run:

```bash
bash scripts/start-test.sh
```

Optional:

```bash
bash scripts/start-test.sh --port /dev/ttyACM0 --reference-port /dev/ttyUSB0 --duration-minutes 5
```

The Linux launcher also calls `tools/run_test_artifact.py`. The DUT uses
verified precompiled firmware artifacts; no PlatformIO/compiler build occurs
inside a normal board test.

## Test flow

After the host-specific build/flash preparation, the common test flow is:

1. discover the reference ESP when RF quality is enabled
2. detect the DUT on a different serial port
3. detect chip/flash/PSRAM and select the DUT profile
4. resolve public plus local settings
5. flash the selected DUT firmware
6. run internal hardware/self tests
7. perform the deep-sleep / RTC wake-up test when enabled
8. restart/verify the reference ESP and run the fixed bidirectional RF test
9. stop DUT RF mode and the reference SoftAP
10. connect the DUT to normal WiFi and perform the configured warm-up
11. run the optional home-network soak
12. run controlled reconnect and WiFi+BLE coexistence checks
13. evaluate board tests, rated RF quality, and diagnostic stability
14. generate reports, CSV/JSON/index files, and optional labels

On both Windows and the new Linux launcher, step 5 uses verified precompiled DUT
artifacts. The Linux implementation remains marked **UNTESTED** until a real
end-to-end Linux hardware run has been completed.

## Board IDs

All board types share one local global number range.

Canonical format:

```text
001-E32
002-C3
003-S3
004-E32
```

Types:

- `E32` = classic ESP32
- `C3` = ESP32-C3
- `S3` = ESP32-S3

Numbers `001` to `999` are globally reserved and are not reused.

The local registry is:

```text
config/board-registry.local.json
```

and is ignored by Git.

Legacy IDs such as `E32-001` remain readable and are normalized automatically.

## Tests

The quality suite includes:

- internal RAM verification and performance
- PSRAM verification and performance when present
- dedicated flash erase/write/read/verify test
- CPU deterministic kernel and per-core test
- timer, RNG, and NVS checks
- BLE basic functionality
- deep-sleep / RTC wake-up
- normal WiFi scan/connection
- controlled WiFi reconnect
- WiFi + BLE coexistence
- RF quality against the dedicated reference ESP
- optional home-network stability soak
- peer comparison against previous PASS boards of the same DUT profile

All test selection and test-specific options live below `tests.default`.
Profile-specific values are recursively merged from
`tests.profiles.<environment>`.

See [TEST-SETTINGS.md](TEST-SETTINGS.md).

### Test-selection semantics

The selected test set is authoritative.

`PASS` means all selected board tests and all configured/rated RF thresholds
passed. `RF QUALITY: UNRATED` means the dedicated RF measurement completed but
no numeric RF threshold is configured for the resolved profile/fixture.

Home-network `STABILITY` is displayed separately. It is diagnostic and does not
by itself turn an otherwise valid board test into an RF failure.

`quality_suite_complete` remains informational.

## Label printing

Automatic label handling is controlled by:

```json
"label": {
  "mode": "off"
}
```

Values:

| Value | Behavior |
|---|---|
| `auto` | Create and automatically print |
| `ask` | Create and ask before printing |
| `off` | Do not start the automatic label workflow |

The tracked default is `off`.

Manual Windows label:

```text
scripts\label.cmd
```

Manual Linux label:

```bash
bash scripts/label.sh
```

Windows backends:

- `windows` — generic PNG through the installed Windows printer driver
- `brother-bpac` — Brother LBX/b-PAC

Linux uses the generic PNG label through CUPS.

## Results

Each board keeps its current result under:

```text
results/<BOARD-ID>/
```

Typical files:

```text
report.html
report.txt
summary.json
serial.log
reference-serial.log
network.csv
rf-quality.csv
events.csv
esptool.txt
build-flash.log
test-config.json
label_<BOARD-ID>.png
label_<BOARD-ID>_brother.lbx
label-print.json
```

The tester also generates:

```text
results/index.html
results/boards.csv
```

`results/` is ignored by Git except for `.gitkeep`.

### Privacy when sharing reports

Generated reports/logs can contain network/device identifiers such as:

- ESP32 MAC address
- local IP addresses
- WiFi BSSID/SSID
- serial/network diagnostic data

Review diagnostic files before attaching them to a public GitHub issue.

## PASS / FAIL

Hard board failures include, among others:

- memory/flash/CPU/NVS verification errors
- unexpected resets outside the diagnostic soak
- failed deep-sleep wake-up
- missing required normal WiFi connection
- failed controlled reconnect
- failed BLE coexistence
- RF fixture/link setup failures
- calibrated RF threshold failures

RF packet loss and RSSI are measurement values. Their board-quality meaning is
defined only by the calibrated thresholds of the active fixture/profile.

The normal home-network soak is reported separately as
`STABILITY: PASS/WARN/FAIL`.

## CI

GitHub Actions checks source/config syntax, Linux and Windows host behavior, and
all supported DUT/reference firmware profiles. CI does not replace a physical
hardware/RF fixture test.

The new Linux artifact workflow remains **UNTESTED on real hardware** until a
physical Linux DUT/reference run has been completed.

## Public release checklist

Before publishing a release:

1. CI must be green.
2. Perform a real-board Windows test.
3. Perform a real-board Linux test before describing Linux as physically tested.
4. Review generated logs before publishing examples.
5. Create the matching version tag and GitHub Release.
6. Review RF threshold documentation so fixture-specific values are not presented as universal limits.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT License. See `LICENSE`.
