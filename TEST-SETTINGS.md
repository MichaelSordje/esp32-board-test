# test-settings.json Reference

`config/test-settings.json` contains the tracked public settings for ESP32 Board
Test.

Machine/test-station-specific overrides belong in:

```text
config/test-settings.local.json
```

The local file is ignored by Git and recursively merged over the tracked
settings.

## Important: RF thresholds are fixture-specific

RF RSSI and packet-loss limits are **not universal ESP32 specifications**.

They depend on the complete physical measurement setup, including:

- reference ESP hardware
- reference/DUT distance
- board orientation
- fixture/enclosure/nearby objects
- RF channel
- reference firmware/RF mode
- DUT profile

The tracked repository currently contains calibrated RF values for
`esp32-s3-n16r8`. Those values were derived from the maintainer's fixture and
must **not** be assumed correct for another test station.

For a new fixture, set that profile's thresholds to `null` in
`config/test-settings.local.json`, collect your own baseline, and then configure
your own limits.

## Current tracked structure

The current tracked configuration is equivalent to:

```json
{
  "label": {
    "mode": "off",
    "backend": "windows",
    "printer_name": "",
    "linux_backend": "cups",
    "linux_printer_name": "",
    "cups_media": "",
    "cups_options": {},
    "width_mm": 62.0,
    "height_mm": 0.0,
    "dpi": 300,
    "margin_mm": 2.0,
    "brother_template": "templates/label_brother.lbx"
  },
  "serial": {
    "dut_baud": 115200,
    "reference_baud": 115200
  },
  "network": {
    "udp_port": 33333,
    "ping_timeout_ms": 1000
  },
  "peer_comparison": {
    "minimum_samples": 3,
    "warn_ratio": 0.8,
    "outlier_ratio": 0.65
  },
  "tests": {
    "default": {
      "ram": true,
      "psram": true,
      "flash": true,
      "cpu": true,
      "timer": true,
      "rng": true,
      "nvs": true,
      "ble": true,
      "deep_sleep": true,
      "wifi": {
        "enabled": true,
        "warmup_seconds": 30
      },
      "rf_quality": {
        "enabled": true,
        "reference_mac": "",
        "channel": 6,
        "reference_tx_power_dbm": 20,
        "packets_per_repetition": 100,
        "packet_interval_ms": 20,
        "repetitions_per_direction": 3,
        "thresholds": {
          "reference_to_dut_min_rssi_dbm": null,
          "dut_to_reference_min_rssi_dbm": null,
          "max_loss_percent": null
        }
      },
      "soak": {
        "enabled": true,
        "duration_minutes": 15,
        "probe_interval_seconds": 1.0,
        "ping": true,
        "udp": true,
        "heap_integrity": {
          "enabled": true,
          "interval_seconds": 30
        },
        "thresholds": {
          "ping_loss_warn_percent": 0.5,
          "ping_loss_fail_percent": 2.0,
          "udp_loss_warn_percent": 0.5,
          "udp_loss_fail_percent": 2.0,
          "longest_outage_warn_seconds": 2.0,
          "longest_outage_fail_seconds": 5.0,
          "disconnects_warn": 1,
          "disconnects_fail": 3,
          "rssi_warn_dbm": -75,
          "heap_drop_warn_bytes": 16384,
          "heap_drop_fail_bytes": 32768,
          "serial_heartbeat_warn_seconds": 3.0,
          "serial_heartbeat_fail_seconds": 10.0
        }
      },
      "reconnect": {
        "enabled": true,
        "timeout_seconds": 15,
        "recovery_timeout_seconds": 30,
        "settle_seconds": 2
      },
      "ble_coexistence": {
        "enabled": true,
        "duration_seconds": 5.0,
        "probe_interval_seconds": 0.25
      }
    },
    "profiles": {
      "esp32": {},
      "esp32-c3": {},
      "esp32-s3": {},
      "esp32-s3-n16r8": {
        "rf_quality": {
          "thresholds": {
            "reference_to_dut_min_rssi_dbm": -42,
            "dut_to_reference_min_rssi_dbm": -38,
            "max_loss_percent": 1.0
          }
        }
      }
    }
  }
}
```

## Profile overrides

`tests.default` is the base configuration.

`tests.profiles.<environment>` is recursively merged over it, so a profile needs
to contain only the settings that differ.

Example:

```json
{
  "tests": {
    "profiles": {
      "esp32-c3": {
        "soak": {
          "duration_minutes": 5
        }
      }
    }
  }
}
```

## Simple hardware tests

These settings are boolean switches:

```text
ram
psram
flash
cpu
timer
rng
nvs
ble
deep_sleep
```

Some hardware-test selections are compiled into the Windows firmware artifacts.
If a settings/source change makes those artifacts stale, `start-test.cmd` stops
and asks for:

```text
scripts\compile_all.cmd
```

Host-only threshold and reporting changes do not by themselves need to be
compiled into the DUT firmware.

## WiFi

```json
"wifi": {
  "enabled": true,
  "warmup_seconds": 30
}
```

The warm-up happens before the normal home-network phase. The dedicated RF
reference measurement runs earlier and independently.

Normal home-WiFi credentials come from `secrets.ini` or the supported
environment variables. They are not stored in `test-settings.json`.

## RF quality

Default RF section:

```json
"rf_quality": {
  "enabled": true,
  "reference_mac": "",
  "channel": 6,
  "reference_tx_power_dbm": 20,
  "packets_per_repetition": 100,
  "packet_interval_ms": 20,
  "repetitions_per_direction": 3,
  "thresholds": {
    "reference_to_dut_min_rssi_dbm": null,
    "dut_to_reference_min_rssi_dbm": null,
    "max_loss_percent": null
  }
}
```

### reference_mac

The tracked value is intentionally empty.

For a fixed production/test fixture, set the actual dedicated reference MAC
locally:

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

When a reference MAC is configured, the runner rejects a different reference
ESP. When it is empty, the discovered reference identity is still recorded, but
there is no configured-MAC equality requirement.

### RF measurement behavior

The current RF measurement is:

- one fixed-power bidirectional session
- 802.11b only
- fixed 1 Mbit/s long-preamble rate
- DUT power save disabled
- reference SoftAP pinned to the configured channel/BSSID
- no DUT TX-power sweep
- reference restarted once before the measurement

Runtime limits applied by the implementation:

| Setting | Runtime behavior |
|---|---|
| `channel` | reference firmware constrains to channels 1..13 |
| `reference_tx_power_dbm` | host constrains to 8..20 dBm |
| `packets_per_repetition` | minimum 10 |
| `packet_interval_ms` | minimum 5 ms |
| `repetitions_per_direction` | constrained to 1..5 |

Each direction records packet loss, average/min/max RSSI, RSSI sample count, and
sender TX power.

### RF threshold semantics

```json
"thresholds": {
  "reference_to_dut_min_rssi_dbm": null,
  "dut_to_reference_min_rssi_dbm": null,
  "max_loss_percent": null
}
```

Meaning:

- `reference_to_dut_min_rssi_dbm`: REF→DUT average RSSI must be greater than or equal to this value
- `dut_to_reference_min_rssi_dbm`: DUT→REF average RSSI must be greater than or equal to this value
- `max_loss_percent`: packet loss in each direction must be less than or equal to this value
- `null`: that individual threshold is not evaluated

If **all three** values are `null`, a successfully completed RF measurement is
`RF QUALITY: UNRATED`.

If at least one threshold is numeric, RF quality is rated. A completed
measurement becomes `PASS` when all configured thresholds pass and `FAIL` when
one fails. Measurement timeouts also produce RF failure.

### Current S3-N16R8 values

The tracked repository currently has:

```json
"esp32-s3-n16r8": {
  "rf_quality": {
    "thresholds": {
      "reference_to_dut_min_rssi_dbm": -42,
      "dut_to_reference_min_rssi_dbm": -38,
      "max_loss_percent": 1.0
    }
  }
}
```

These are **fixture-specific maintainer values**.

They are not a general quality specification for all ESP32-S3-N16R8 boards.

For another fixture, neutralize them locally before calibration:

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

Then:

1. fix the final reference and DUT positions
2. use the same reference board and orientation for every run
3. test multiple independently verified good boards
4. preferably include known weak/bad boards
5. inspect repeatability and separation in `rf-quality.csv`/`summary.json`
6. choose your own profile-specific limits
7. store them in `config/test-settings.local.json`

Do not copy thresholds between profiles or fixtures without new measurements.

### RF peer comparison

RF peer comparison uses historical compatible results only. Compatibility
includes the DUT profile, reference identity, and RF measurement mode/rate.

RF peer metrics are informational; they do not replace calibrated absolute
thresholds.

## Soak

All long home-network stability settings live below `soak`:

```json
"soak": {
  "enabled": true,
  "duration_minutes": 15,
  "probe_interval_seconds": 1.0,
  "ping": true,
  "udp": true,
  "heap_integrity": {
    "enabled": true,
    "interval_seconds": 30
  },
  "thresholds": {
    "ping_loss_warn_percent": 0.5,
    "ping_loss_fail_percent": 2.0,
    "udp_loss_warn_percent": 0.5,
    "udp_loss_fail_percent": 2.0,
    "longest_outage_warn_seconds": 2.0,
    "longest_outage_fail_seconds": 5.0,
    "disconnects_warn": 1,
    "disconnects_fail": 3,
    "rssi_warn_dbm": -75,
    "heap_drop_warn_bytes": 16384,
    "heap_drop_fail_bytes": 32768,
    "serial_heartbeat_warn_seconds": 3.0,
    "serial_heartbeat_fail_seconds": 10.0
  }
}
```

When `soak.enabled=false`:

- the timed soak is skipped
- soak ping/UDP probes are not executed
- periodic soak heap-integrity monitoring is not executed
- reconnect, BLE coexistence, RF quality, and WiFi setup remain independent
- reports keep the child switches configured but show `soak_disabled`

The command-line duration option overrides `soak.duration_minutes` for that run.

Home-network stability is diagnostic and is deliberately separate from the
dedicated RF fixture quality decision.

## Controlled reconnect

```json
"reconnect": {
  "enabled": true,
  "timeout_seconds": 15,
  "recovery_timeout_seconds": 30,
  "settle_seconds": 2
}
```

A failed controlled reconnect is a board-test failure. If possible, the
orchestrator then restores home WiFi before later tests so one failure does not
automatically invalidate BLE coexistence.

## WiFi + BLE coexistence

```json
"ble_coexistence": {
  "enabled": true,
  "duration_seconds": 5.0,
  "probe_interval_seconds": 0.25
}
```

`ble_coexistence.enabled=true` requires the basic `ble` test to be enabled.

The implementation compares a short baseline network probe with an active BLE
scan. Coexistence degradation warnings are separate from the dedicated RF
fixture thresholds.

## Shared network settings

```json
"network": {
  "udp_port": 33333,
  "ping_timeout_ms": 1000
}
```

`network.udp_port` is the host/DUT UDP test port.
`network.ping_timeout_ms` is used by host ICMP probes.

## Peer comparison

Tracked values:

```json
"peer_comparison": {
  "minimum_samples": 3,
  "warn_ratio": 0.8,
  "outlier_ratio": 0.65
}
```

Performance comparison uses completed `PASS` results from other boards of the
same DUT profile.

For higher-is-better metrics, the current value is compared with the historical
median. For lower-is-better metrics such as flash erase time, the ratio is
inverted accordingly.

With fewer than `minimum_samples`, median/quartiles may still be displayed but
the comparison is marked `INSUFFICIENT_DATA`.

Performance peer warnings are diagnostic. RF peer metrics are separately
restricted to compatible RF fixture results.

## Local override example

A realistic local fixture override can contain the reference identity and
fixture-specific RF calibration without modifying tracked public files:

```json
{
  "tests": {
    "default": {
      "rf_quality": {
        "reference_mac": "AA:BB:CC:DD:EE:FF"
      }
    },
    "profiles": {
      "esp32-s3-n16r8": {
        "rf_quality": {
          "thresholds": {
            "reference_to_dut_min_rssi_dbm": -40,
            "dut_to_reference_min_rssi_dbm": -39,
            "max_loss_percent": 0.5
          }
        }
      }
    }
  },
  "label": {
    "mode": "off"
  }
}
```

The values above are examples only; use measured values from your own fixture.

Unknown top-level settings, unknown test names/options, and invalid
object/boolean shapes are rejected instead of being silently ignored.
