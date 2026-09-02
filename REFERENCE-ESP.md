# Reference ESP for RF quality testing

The dedicated reference ESP provides a reproducible local 2.4 GHz RF endpoint
for ESP32 Board Test.

It is not the DUT and it is not the normal home WiFi access point.

## Purpose

The normal home-network path contains uncontrolled variables such as:

- access point
- routing
- host network stack
- other clients
- local interference

Therefore the home-network soak is useful for runtime diagnostics but is not a
controlled antenna/radio-quality reference.

The dedicated RF phase uses:

```text
PC --USB/Serial--> reference ESP
PC --USB/Serial--> DUT

reference ESP  <---- direct 2.4 GHz WiFi/UDP ---->  DUT
```

The PC coordinates the measurement but is not in the RF packet path.

## One-time reference flash

Windows:

```text
scripts\flash-reference.cmd
```

Linux:

```bash
bash scripts/flash-reference.sh
```

The reference flash path uses its own verified artifact set. Before the first
flash, and after changes under `reference-firmware/`, build all four reference
profiles once:

```text
scripts\compile_reference_all.cmd
```

or:

```bash
bash scripts/compile_reference_all.sh
```

`tools/flash_reference.py` detects the reference board profile, verifies its
reference artifact fingerprint and SHA256 values, then flashes it directly
with esptool. PlatformIO is not run during flashing.

If exactly one unconfigured ESP32 is connected, no port argument is required.
If several candidate boards are connected, select the intended reference:

```powershell
.\scripts\flash-reference.ps1 -Port COM8
```

or:

```bash
bash scripts/flash-reference.sh --port /dev/ttyUSB0
```

After flashing, the tool verifies that the reference firmware identity can be
read back.

The RF runner requires reference firmware 1.0.12 or newer.

## Automatic discovery

At test startup the host:

1. scans usable USB serial ports
2. sends `INFO`
3. recognizes `REF|READY` / `REF|INFO` with `role=reference`
4. keeps the reference serial port separate from DUT detection
5. records reference port, MAC, and firmware version

If more than one reference ESP is detected, normal automatic discovery stops
with an explicit error unless a reference-port override is used.

No reference IP address is stored in configuration.

## Reference MAC

Tracked public configuration leaves:

```json
"reference_mac": ""
```

empty.

For a fixed test station, configure the actual reference MAC in
`config/test-settings.local.json`:

```json
{
  "tests": {
    "default": {
      "rf_quality": {
        "reference_mac": "AA:BB:CC:DD:EE:FF"
      }
    }
  }
}
```

When a MAC is configured, the runner verifies it before and after restarting the
reference. With an empty configured MAC, the discovered identity is still
recorded but no configured-MAC equality check is applied.

## Radio behavior

The reference radio is normally off.

During the RF phase the host starts a temporary reference SoftAP. The reference
firmware:

- creates a unique SSID derived from its MAC
- uses the configured RF channel
- reports its SoftAP BSSID
- accepts one DUT station
- exchanges numbered UDP packets directly with the DUT
- reports packet counts and RSSI statistics over USB serial
- uses the configured reference TX power for the fixed measurement

The host sends the known SSID, password, channel, and BSSID to the DUT over USB
serial. The DUT connects directly to the known reference AP without doing a
normal RF scan for this phase.

The two directions are measured independently:

- **REF -> DUT**: primarily evaluates the DUT receive side/link margin
- **DUT -> REF**: primarily evaluates the DUT transmit side/link margin

## Controlled fixed-power mode

The current RF test uses one controlled radio condition:

- fixed reference channel
- 802.11b only
- fixed 1 Mbit/s long-preamble PHY rate
- DUT power save disabled
- configured fixed reference TX power
- DUT remains at the WiFi driver's normal maximum TX power
- no DUT TX-power sweep during the measurement

The reference is restarted once before the RF phase. Its firmware identity is
verified, and its MAC is also checked when `reference_mac` is configured.

Each direction runs the configured number of repetitions and records:

- packets sent
- packets received
- packet loss
- average RSSI
- minimum RSSI
- maximum RSSI
- RSSI sample count
- sender TX power reported by the driver

The tracked packet settings are three repetitions of 100 packets per direction.

Detailed values are stored in:

```text
report.html
report.txt
summary.json
rf-quality.csv
reference-serial.log
serial.log
```

After RF testing, the DUT RF mode and reference AP are stopped before the DUT
joins normal home WiFi.

## Physical placement

RF thresholds are meaningful only when the fixture is repeatable.

Use:

- one fixed reference position
- one fixed DUT position
- the same DUT orientation every time
- the same reference orientation every time
- stable USB power/cables
- no loose boards or large objects moved between reference and DUT
- the same reference board for calibration and later tests

There is deliberately no universal hard-coded distance. Choose a fixture
spacing that gives measurable separation between good and weak hardware without
making normal good boards unstable.

## Calibration is mandatory for each fixture

RF RSSI thresholds cannot be copied blindly between test stations.

They depend on the complete fixture, including:

- reference ESP
- geometry/distance/orientation
- channel
- surrounding material/objects
- DUT profile
- RF firmware/mode

### Current tracked S3-N16R8 thresholds

The repository currently contains these values for `esp32-s3-n16r8`:

```json
{
  "reference_to_dut_min_rssi_dbm": -42,
  "dut_to_reference_min_rssi_dbm": -38,
  "max_loss_percent": 1.0
}
```

These are the maintainer's calibrated fixture values.

**They are not universal ESP32-S3-N16R8 limits.**

If you use another reference board, placement, fixture, or environment, do not
use them as production limits without calibration.

### New fixture: start UNRATED

Override the tracked profile locally:

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

With all RF thresholds `null`, a successfully completed measurement reports:

```text
RF QUALITY: UNRATED
```

This means the measurement ran successfully but no absolute production limit is
being applied.

### Establish your own limits

Use the final fixture and collect repeated measurements from:

- multiple independently verified good DUTs
- preferably one or more known weak/bad DUTs

Compare:

```text
rf-quality.csv
summary.json
```

Choose limits only when the measurement is repeatable and there is useful
separation between acceptable and weak hardware.

Store test-station-specific limits in:

```text
config/test-settings.local.json
```

Thresholds should normally be calibrated separately for each DUT profile.

## PASS / FAIL / UNRATED

The RF quality evaluator works as follows:

- all three RF thresholds `null` -> successful RF measurement is `UNRATED`
- at least one numeric RF threshold -> RF result is rated
- every configured threshold passes -> `PASS`
- any configured threshold fails -> `FAIL`
- RF measurement timeout/setup failure -> `FAIL`

The RSSI thresholds compare **average** RSSI for their respective direction.
`max_loss_percent` is applied independently to both directions.

## RF peer comparison

RF peer history is restricted to compatible RF results.

Compatibility includes:

- DUT PlatformIO profile
- reference identity
- RF measurement mode/rate

Peer comparison is useful for spotting drift/outliers, but it does not replace
fixture-specific absolute calibration.

## Reference firmware project

The reference firmware is isolated from the DUT firmware:

```text
reference-firmware/
  platformio.ini
  src/main.cpp
```

It supports the same four board-profile names used by DUT detection.

PlatformIO caches are isolated for every role/profile below `.platformio/`.
The reference ESP32-S3 profile deliberately remains on pioarduino 54, while
the DUT ESP32-S3 profiles use their separately isolated pioarduino 55 cores.

CI builds the supported DUT and reference-firmware profiles, but CI cannot
validate physical RF fixture behavior.
