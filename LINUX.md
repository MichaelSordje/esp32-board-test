# Linux support

## Status: UNTESTED

**UNTESTED:** The Linux host path has not yet been validated end-to-end with
real ESP32 hardware on a physical Linux machine.

The shell files and Python call paths are checked structurally/syntactically,
but this is **not** equivalent to a real Linux USB/serial/RF hardware test.
Do not describe Linux as physically tested until such a test has been completed.

ESP32 Board Test uses the same DUT source code, firmware artifacts, test logic,
result format, board IDs, RF evaluation, and
`config/test-settings*.json` configuration model on Windows and Linux.

## Linux artifact workflow

Linux now follows the same compile-once approach as Windows.

Build all four DUT firmware variants:

```bash
bash scripts/compile_all.sh
```

Optionally prepare the isolated PlatformIO package sets for DUT and reference
firmware first:

```bash
bash scripts/prepare_all.sh
```

Then run board tests with:

```bash
bash scripts/start-test.sh
```

The intended flow is:

```text
compile_all.sh
  -> PlatformIO/compiler
  -> build all four DUT environments
  -> verified firmware-artifacts/dut/

start-test.sh
  -> verify firmware artifact fingerprint, size and SHA256
  -> detect DUT profile
  -> direct esptool flash of the matching precompiled images
  -> hardware/RF/network test
```

During a normal `start-test.sh` board test, PlatformIO/compiler is **not used**
for the DUT firmware. PlatformIO is used by `compile_all.sh` when artifacts are
created or refreshed.

The reference firmware is compiled separately with
`compile_reference_all.sh` and published below `firmware-artifacts/reference/`.
`flash-reference.sh` flashes the matching verified reference artifact directly
with esptool and does not invoke PlatformIO.

PlatformIO package caches are isolated under `.platformio/dut/<environment>/`
and `.platformio/reference/<environment>/`. The reference ESP32-S3 profile
stays on its pinned pioarduino 54 release; it does not share a cache with the
DUT ESP32-S3 profiles pinned to pioarduino 55.

## Required packages

Typical Debian/Ubuntu setup:

```bash
sudo apt install python3 python3-venv
```

The host `ping` command is needed when the selected tests use it:

- home-network soak ping
- controlled reconnect
- WiFi+BLE coexistence

Install it with:

```bash
sudo apt install iputils-ping
```

Optional CUPS label printing:

```bash
sudo apt install cups-client
```

Internet access is needed on first setup and when Python/PlatformIO packages
must be installed for a new build environment.

## Python environment

Both Linux launchers use:

```text
.venv-linux/
```

by default.

A different virtual environment can be selected with:

```bash
export ESP_TEST_VENV=/path/to/venv
```

`requirements.txt` is hashed. When it changes, the launchers update the
environment before continuing.

Python 3.10 or newer is required.

## Compile firmware artifacts

After a fresh clone, and whenever DUT firmware sources or firmware-relevant
configuration changed:

```bash
bash scripts/compile_all.sh
```

This builds:

```text
esp32
esp32-c3
esp32-s3
esp32-s3-n16r8
```

and publishes the verified images below:

```text
firmware-artifacts/dut/
```

Each environment contains:

```text
bootloader.bin
partitions.bin
boot_app0.bin
firmware.bin
```

The manifest contains the firmware fingerprint, image size/SHA256, flash
settings, upload speed, and compiled firmware-test switches.

Artifacts are published only after all four environments build successfully.

If `start-test.sh` detects missing, outdated, incomplete, or modified artifacts,
it stops before testing the DUT and asks for:

```bash
bash scripts/compile_all.sh
```

## Start a board test

Automatic DUT/reference discovery:

```bash
bash scripts/start-test.sh
```

Specific DUT port:

```bash
bash scripts/start-test.sh --port /dev/ttyACM0
```

Specific reference port:

```bash
bash scripts/start-test.sh --reference-port /dev/ttyUSB0
```

Short soak duration:

```bash
bash scripts/start-test.sh --duration-minutes 5
```

Combined example:

```bash
bash scripts/start-test.sh \
  --port /dev/ttyACM0 \
  --reference-port /dev/ttyUSB0 \
  --duration-minutes 5
```

If executable bits are set, these are equivalent:

```bash
./scripts/compile_all.sh
./scripts/start-test.sh
```

## Artifact test behavior

`start-test.sh` first verifies the complete local artifact set. The Python
runtime then selects only the matching environment after DUT detection.

The runtime artifact path uses:

```text
tools/run_test_artifact.py
```

which installs only the precompiled firmware build/flash boundary and then uses
the normal `run_test.py`/orchestrator behavior.

Therefore the board-test behavior, reports, board IDs, RF measurement,
stability evaluation and Linux post-processing remain shared with the existing
test implementation.

## Serial/USB permissions

ESP32 boards commonly appear as:

```text
/dev/ttyUSB0
/dev/ttyACM0
```

If access is denied on Debian/Ubuntu:

```bash
sudo usermod -aG dialout "$USER"
```

Log out and back in afterwards.

Do not run the tester as root merely to bypass serial permissions.

## WiFi credentials

Credentials are required only when the resolved DUT profile enables normal
WiFi testing.

Create:

```bash
cp secrets.example.ini secrets.ini
```

and enter:

```ini
WIFI_SSID=MyWiFi
WIFI_PASSWORD=MyPassword
```

`secrets.ini` is ignored by Git.

The dedicated RF reference test uses its own reference SoftAP and does not use
the home-WiFi credentials.

## Reference ESP

When `rf_quality.enabled=true`, connect the dedicated reference ESP by USB in
addition to the DUT.

One-time reference flash:

```bash
bash scripts/flash-reference.sh
```

If several unconfigured ESP32 boards are connected:

```bash
bash scripts/flash-reference.sh --port /dev/ttyUSB0
```

Build/update all reference artifacts before flashing:

```bash
bash scripts/compile_reference_all.sh
```

`flash-reference.sh` checks the separate reference fingerprint and image
SHA256 values before directly flashing the matching artifact. It does not build
firmware during the flash operation.

Normal tests discover the reference automatically.

See [REFERENCE-ESP.md](REFERENCE-ESP.md).

## Local settings

Use:

```text
config/test-settings.local.json
```

for local test-station settings such as:

- reference MAC
- fixture-specific RF thresholds
- shorter soak duration
- label/CUPS settings

The file is ignored by Git and recursively merged over
`config/test-settings.json`.

Firmware-relevant test switches are part of the artifact fingerprint. If they
change, `start-test.sh` requires a new `compile_all.sh`.

Host-only thresholds/reporting settings do not require DUT recompilation.

## RF thresholds on another fixture

The tracked `esp32-s3-n16r8` RF thresholds are calibrated for the maintainer's
fixture and are **not universal values**.

For another Linux test station/fixture, set those thresholds to `null` locally
until you have calibrated your own limits:

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

See [TEST-SETTINGS.md](TEST-SETTINGS.md) and
[REFERENCE-ESP.md](REFERENCE-ESP.md).

## Disable the home-network stability soak

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

When `soak.enabled=false`:

- the timed soak is skipped
- soak ping/UDP probing is skipped
- periodic soak heap-integrity monitoring is skipped
- WiFi setup can still run
- controlled reconnect can still run
- WiFi+BLE coexistence can still run
- RF quality can still run
- `quality_suite_complete` becomes informationally incomplete
- label handling still follows `label.mode`

## Linux labels and CUPS

Install CUPS client tools if needed:

```bash
sudo apt install cups-client
```

List printers/default:

```bash
lpstat -p -d
```

Linux label settings:

```json
{
  "label": {
    "linux_backend": "cups",
    "linux_printer_name": "",
    "cups_media": "",
    "cups_options": {}
  }
}
```

An empty `linux_printer_name` uses the CUPS default printer.

Manual label workflow:

```bash
bash scripts/label.sh
```

Generate without printing:

```bash
bash scripts/label.sh --summary results/001-E32/summary.json --no-print
```

Brother b-PAC is Windows-only. Brother hardware can still be used through a
normal Linux CUPS queue.

## Report opening

On a graphical desktop the report is opened with `xdg-open`, with `gio open` as
fallback.

On a headless host the path is printed instead.

## Validation status

The delivered Linux artifact launchers have been checked for:

- Bash syntax
- expected repository-relative paths
- Python 3.10+ bootstrap logic
- shared `.venv-linux` / `ESP_TEST_VENV` handling
- requirements hash/update path
- `firmware_artifacts.py compile-all` invocation
- artifact status verification before DUT access
- `run_test_artifact.py` invocation
- exit-code propagation

**Still UNTESTED:**

- real Linux USB device discovery
- real ESP32 flashing from Linux through the artifact path
- Linux serial permissions on an actual installation
- full DUT + reference RF run
- physical CUPS label printing

CI and syntax checks do not replace those hardware tests.
