from __future__ import annotations

import argparse
import time
from pathlib import Path

from serial.tools import list_ports

import test_runner as tr
from firmware_artifacts import flash_reference_artifact
from reference_device import find_reference_device


ROOT = Path(__file__).resolve().parents[1]


def _candidate_esp_ports() -> list[tuple[str, str, dict[str, object]]]:
    found: list[tuple[str, str, dict[str, object]]] = []
    for item in list_ports.comports():
        if not tr.likely_esp_port(item):
            continue
        ok, output = tr.esptool_flash_id(item.device)
        if not ok:
            continue
        info = tr.parse_esptool_info(output)
        found.append((item.device, output, info))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flash the dedicated ESP32 Board Test reference firmware."
    )
    parser.add_argument("--port", default="")
    args = parser.parse_args()

    port = args.port.strip()
    info: dict[str, object] | None = None

    if not port:
        existing = find_reference_device(required=False)
        if existing is not None:
            port = existing.identity.port
            existing.close()
            ok, output = tr.esptool_flash_id(port)
            if not ok:
                raise RuntimeError(f"Existing reference ESP on {port} could not enter bootloader.\n{output}")
            info = tr.parse_esptool_info(output)
        else:
            candidates = _candidate_esp_ports()
            if len(candidates) == 0:
                raise RuntimeError("No flashable ESP32 was found for the reference firmware.")
            if len(candidates) > 1:
                ports = ", ".join(item[0] for item in candidates)
                raise RuntimeError(
                    "Multiple unconfigured ESP32 boards are connected. For the one-time "
                    f"reference setup, select the desired board with --port. Found: {ports}"
                )
            port, _output, info = candidates[0]
    else:
        ok, output = tr.esptool_flash_id(port)
        if not ok:
            raise RuntimeError(f"No responsive ESP32 was found on {port}.\n{output}")
        info = tr.parse_esptool_info(output)

    if info is None:
        raise RuntimeError("Reference board type could not be determined.")

    environment = tr.choose_environment(info)
    print(f"Reference board: {port} / {info.get('chip_family', 'ESP32')} / profile {environment}")
    flash_reference_artifact(port, environment)

    print("Verifying reference signature ...")
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        reference = find_reference_device(requested_port=port, required=False)
        if reference is not None:
            print(
                f"Reference ESP ready: {reference.identity.port} / "
                f"{reference.identity.mac} / FW {reference.identity.version}"
            )
            reference.close()
            return 0
        time.sleep(0.5)

    raise RuntimeError(
        "Flashing finished, but the reference firmware signature was not detected afterwards."
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
