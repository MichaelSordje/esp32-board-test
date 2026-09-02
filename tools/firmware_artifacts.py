from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = ROOT / "firmware-artifacts" / "dut"
MANIFEST_PATH = ARTIFACT_ROOT / "manifest.json"
BASE_SETTINGS_PATH = ROOT / "config" / "test-settings.json"
LOCAL_SETTINGS_PATH = ROOT / "config" / "test-settings.local.json"
GENERATED_TEST_CONFIG_PATH = ROOT / "firmware" / "include" / "test_config.generated.h"
REFERENCE_PROJECT = ROOT / "reference-firmware"
REFERENCE_ARTIFACT_ROOT = ROOT / "firmware-artifacts" / "reference"
REFERENCE_MANIFEST_PATH = REFERENCE_ARTIFACT_ROOT / "manifest.json"
CORE_LAYOUT_MARKER = ".esp32-board-test-core-layout-v1"
REFERENCE_BUILD_TIMEOUT_SECONDS = 1800

ENVIRONMENTS = (
    "esp32",
    "esp32-c3",
    "esp32-s3",
    "esp32-s3-n16r8",
)

ENVIRONMENT_CHIPS = {
    "esp32": "esp32",
    "esp32-c3": "esp32c3",
    "esp32-s3": "esp32s3",
    "esp32-s3-n16r8": "esp32s3",
}

ENVIRONMENT_COMPILERS = {
    "esp32": "xtensa-esp32-elf-g++.exe" if os.name == "nt" else "xtensa-esp32-elf-g++",
    "esp32-c3": "riscv32-esp-elf-g++.exe" if os.name == "nt" else "riscv32-esp-elf-g++",
    "esp32-s3": "xtensa-esp32s3-elf-g++.exe" if os.name == "nt" else "xtensa-esp32s3-elf-g++",
    "esp32-s3-n16r8": "xtensa-esp32s3-elf-g++.exe" if os.name == "nt" else "xtensa-esp32s3-elf-g++",
}

ENVIRONMENT_BOOTLOADER_OFFSETS = {
    "esp32": 0x1000,
    "esp32-c3": 0x0000,
    "esp32-s3": 0x0000,
    "esp32-s3-n16r8": 0x0000,
}

PARTITION_TABLE_OFFSET = 0x8000
BOOT_APP0_OFFSET = 0xE000

# Upload settings resolved from the currently pinned pioarduino board
# definitions and builder. These mirror the previous PlatformIO upload path.
ENVIRONMENT_FLASH_SETTINGS = {
    "esp32": {
        "mode": "dio",
        "frequency": "40m",
        "size": "4MB",
    },
    "esp32-c3": {
        "mode": "dio",
        "frequency": "80m",
        "size": "4MB",
    },
    "esp32-s3": {
        "mode": "dio",
        "frequency": "80m",
        "size": "8MB",
    },
    "esp32-s3-n16r8": {
        "mode": "dio",
        "frequency": "80m",
        "size": "16MB",
    },
}

FIRMWARE_CONFIG_KEYS = (
    "ram",
    "psram",
    "flash",
    "cpu",
    "timer",
    "rng",
    "nvs",
    "ble",
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.relative_to(ROOT)} must contain a JSON object.")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_effective_settings() -> dict[str, Any]:
    settings = _load_json(BASE_SETTINGS_PATH)
    if LOCAL_SETTINGS_PATH.exists():
        settings = _deep_merge(settings, _load_json(LOCAL_SETTINGS_PATH))
    return settings


def _platformio_requirement() -> str:
    requirements = ROOT / "requirements.txt"
    if not requirements.exists():
        return ""
    for raw_line in requirements.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line.lower().startswith("platformio"):
            return line
    return ""


def _iter_firmware_input_files() -> list[Path]:
    files: list[Path] = []

    for path in (
        ROOT / "platformio.ini",
        ROOT / "partitions.csv",
        ROOT / "tools" / "patch_arduino_wifi_ampdu.py",
    ):
        if path.is_file():
            files.append(path)

    firmware_root = ROOT / "firmware"
    if firmware_root.is_dir():
        for path in firmware_root.rglob("*"):
            if not path.is_file():
                continue
            if path.resolve() == GENERATED_TEST_CONFIG_PATH.resolve():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            files.append(path)

    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix().lower())


def firmware_build_config(
    test_runner_module: Any,
    settings: dict[str, Any],
) -> dict[str, dict[str, bool]]:
    result: dict[str, dict[str, bool]] = {}
    for environment in ENVIRONMENTS:
        resolved = test_runner_module.resolve_test_config(settings, environment)
        result[environment] = {
            key: bool(resolved[key])
            for key in FIRMWARE_CONFIG_KEYS
        }
    return result


def compute_firmware_fingerprint(
    test_runner_module: Any,
    settings: dict[str, Any] | None = None,
) -> str:
    effective_settings = settings if settings is not None else load_effective_settings()
    digest = hashlib.sha256()

    digest.update(b"ESP32-BOARD-TEST-FIRMWARE-FINGERPRINT-V1\0")

    for path in _iter_firmware_input_files():
        relative = path.relative_to(ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")

    config_payload = json.dumps(
        firmware_build_config(test_runner_module, effective_settings),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(b"firmware-test-config\0")
    digest.update(config_payload)
    digest.update(b"\0")

    digest.update(b"platformio-requirement\0")
    digest.update(_platformio_requirement().encode("utf-8"))
    digest.update(b"\0")

    return digest.hexdigest()


def compute_reference_fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(b"ESP32-BOARD-TEST-REFERENCE-FIRMWARE-FINGERPRINT-V1\0")

    for root in (REFERENCE_PROJECT, ROOT / "tools"):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or ".pio" in path.parts
                or path.suffix == ".pyc"
            ):
                continue
            if root == ROOT / "tools" and path.name != "patch_arduino_wifi_ampdu.py":
                continue
            relative = path.relative_to(ROOT).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")

    digest.update(b"platformio-requirement\0")
    digest.update(_platformio_requirement().encode("utf-8"))
    digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_upload_speed(
    environment: str,
    project_dir: Path = ROOT,
) -> int:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(project_dir / "platformio.ini", encoding="utf-8")

    value = "921600"
    if parser.has_section("env") and parser.has_option("env", "upload_speed"):
        value = parser.get("env", "upload_speed").strip()

    section = f"env:{environment}"
    if parser.has_section(section) and parser.has_option(section, "upload_speed"):
        value = parser.get(section, "upload_speed").strip()

    try:
        speed = int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid upload_speed for {environment} in "
            f"{(project_dir / 'platformio.ini').relative_to(ROOT)}: {value}"
        ) from exc

    if speed <= 0:
        raise RuntimeError(
            f"Invalid upload_speed for {environment} in platformio.ini: {speed}"
        )
    return speed


def _read_platform_spec(
    environment: str,
    project_dir: Path = ROOT,
) -> str:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(project_dir / "platformio.ini", encoding="utf-8")

    value = ""
    if parser.has_section("env") and parser.has_option("env", "platform"):
        value = parser.get("env", "platform").strip()

    section = f"env:{environment}"
    if parser.has_section(section) and parser.has_option(section, "platform"):
        value = parser.get(section, "platform").strip()

    if not value:
        raise RuntimeError(
            f"PlatformIO platform is not configured for {environment}."
        )
    return value


def _platformio_core_dir(environment: str, role: str = "dut") -> Path:
    """Return the fully isolated PlatformIO core for one firmware role.

    DUT and reference firmware never share packages. This is required because
    their platform releases can differ for the same chip (notably ESP32-S3).
    """
    if role not in {"dut", "reference"}:
        raise ValueError(f"Unsupported firmware role: {role}")
    return ROOT / ".platformio" / role / environment


def _ensure_isolated_core(environment: str, role: str) -> Path:
    core_dir = _platformio_core_dir(environment, role)
    marker = core_dir / CORE_LAYOUT_MARKER
    expected = f"{role}:{environment}\n"
    try:
        current = marker.read_text(encoding="ascii")
    except OSError:
        current = ""

    if core_dir.exists() and current != expected:
        print(f"Resetting obsolete PlatformIO core: {core_dir.relative_to(ROOT)}")
        shutil.rmtree(core_dir)

    core_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(expected, encoding="ascii")
    return core_dir


def _platformio_command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "platformio", *arguments]


def _run_checked_live(
    command: list[str],
    description: str,
    timeout: int,
    process_env: dict[str, str] | None = None,
) -> None:
    print(description)
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            check=False,
            timeout=timeout,
            env=process_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{description} timed out after {timeout} seconds."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"{description} failed with exit code {result.returncode}."
        )


def _run_platformio_package_install(
    environment: str,
    process_env: dict[str, str],
    project_dir: Path = ROOT,
) -> None:
    command = _platformio_command("pkg", "install", "-e", environment)
    if project_dir != ROOT:
        command.extend(("--project-dir", str(project_dir)))
    _run_checked_live(
        command,
        f"Preparing PlatformIO packages for {environment} ...",
        timeout=900,
        process_env=process_env,
    )


def _find_compiler_under(root: Path, compiler_name: str) -> Path | None:
    if not root.is_dir():
        return None

    candidates = [
        path
        for path in root.rglob(compiler_name)
        if path.is_file()
        and not any("broken-" in part.lower() for part in path.parts)
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda path: (len(path.parts), str(path).lower()))
    return candidates[0]


def _find_package_root(path: Path, boundary: Path) -> Path:
    boundary_resolved = boundary.resolve()
    current = path.resolve().parent

    while True:
        if (current / "package.json").is_file():
            return current

        if current == boundary_resolved or current.parent == current:
            break
        current = current.parent

    raise RuntimeError(
        f"Could not find package.json for staged toolchain: {path}"
    )


def _materialize_staged_toolchain(
    environment: str,
    core_dir: Path,
) -> Path | None:
    compiler_name = ENVIRONMENT_COMPILERS[environment]
    packages_root = core_dir / "packages"
    tools_root = core_dir / "tools"

    # Best case: PlatformIO already has the compiler as a normal package.
    packaged = _find_compiler_under(packages_root, compiler_name)
    if packaged is not None:
        return packaged

    # pioarduino can stage ESP-IDF toolchains below core_dir/tools before
    # registering them as normal PlatformIO packages.
    staged = _find_compiler_under(tools_root, compiler_name)
    if staged is None:
        # Nothing to register yet. The compiler is provisioned lazily by
        # idf_tools.py during the build itself; the caller falls back to
        # that path.
        return None

    staged_package = _find_package_root(staged, tools_root)

    print(
        f"Registering staged toolchain as PlatformIO package: "
        f"{staged_package.relative_to(ROOT)}"
    )

    # Use PlatformIO's package manager directly. This is the same mechanism
    # pioarduino 54 uses internally:
    #   pm.install("file://<local tool directory>")
    # Do not use a URL-encoded Windows file URI here because PlatformIO 6.1.19
    # can misinterpret file:///C:/... as /C:/...
    from platformio.package.manager.tool import ToolPackageManager

    packages_root.mkdir(parents=True, exist_ok=True)
    manager = ToolPackageManager(package_dir=str(packages_root))

    if os.name == "nt":
        local_path = str(staged_package.resolve())
    else:
        local_path = staged_package.resolve().as_posix()

    package_uri = f"file://{local_path}"

    try:
        manager.install(package_uri)
    except Exception as exc:
        raise RuntimeError(
            f"Could not register the staged toolchain for {environment}: {exc}"
        ) from exc

    packaged = _find_compiler_under(packages_root, compiler_name)
    if packaged is None:
        raise RuntimeError(
            f"The staged toolchain for {environment} was registered, but "
            f"{compiler_name} is still missing below "
            f"{packages_root.relative_to(ROOT)}."
        )

    return packaged


def _build_process_environment(
    environment: str,
    role: str = "dut",
    project_dir: Path = ROOT,
) -> tuple[dict[str, str], Path | None]:
    core_dir = _ensure_isolated_core(environment, role)

    process_env = os.environ.copy()
    process_env["PLATFORMIO_CORE_DIR"] = str(core_dir)

    # pioarduino's lazy toolchain installer (idf_tools.py) refuses to run
    # inside an MSYS/MinGW shell. Drop those markers so a build started from
    # Git Bash provisions the same way a plain cmd/PowerShell run does.
    for msys_var in ("MSYSTEM", "MSYSCON", "MINGW_PREFIX", "MSYS2_PATH_TYPE"):
        process_env.pop(msys_var, None)

    compiler_name = ENVIRONMENT_COMPILERS[environment]
    compiler = _find_compiler_under(
        core_dir / "packages",
        compiler_name,
    )

    if compiler is None:
        staged = _find_compiler_under(
            core_dir / "tools",
            compiler_name,
        )
        if staged is None:
            _run_platformio_package_install(environment, process_env, project_dir)

        compiler = _find_compiler_under(core_dir / "packages", compiler_name)
        if compiler is None:
            compiler = _materialize_staged_toolchain(
                environment,
                core_dir,
            )
        if compiler is None:
            print(
                f"Compiler for {environment} will be provisioned during the "
                f"build by idf_tools.py"
            )
    else:
        print(
            f"PlatformIO packages: CACHED / {environment} / "
            f"{core_dir.relative_to(ROOT)}"
        )

    if compiler is not None:
        compiler_bin = str(compiler.parent)
        current_path = process_env.get("PATH", "")
        path_parts = [part for part in current_path.split(os.pathsep) if part]

        normalized = {
            os.path.normcase(os.path.normpath(part))
            for part in path_parts
        }
        compiler_key = os.path.normcase(os.path.normpath(compiler_bin))
        if compiler_key not in normalized:
            process_env["PATH"] = compiler_bin + os.pathsep + current_path

        print(f"Compiler package: {compiler}")

    return process_env, compiler


def _parse_size_value(value: str) -> int:
    text = value.strip().lower()
    if not text:
        raise ValueError("empty size")
    multiplier = 1
    if text.endswith("k"):
        multiplier = 1024
        text = text[:-1]
    elif text.endswith("m"):
        multiplier = 1024 * 1024
        text = text[:-1]
    return int(text, 0) * multiplier


def _application_offset_from_partitions() -> int:
    path = ROOT / "partitions.csv"
    if not path.is_file():
        raise RuntimeError(
            "partitions.csv is missing; the firmware application offset "
            "cannot be determined safely."
        )

    next_offset = 0
    first_app_offset: int | None = None

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 5:
            continue

        _name, part_type, _subtype, offset_text, size_text = fields[:5]
        is_app = part_type.lower() in {"app", "0", "0x00"}
        alignment = 0x10000 if is_app else 4

        if offset_text:
            offset = _parse_size_value(offset_text)
        else:
            offset = (next_offset + alignment - 1) & ~(alignment - 1)

        size = _parse_size_value(size_text)
        if is_app and first_app_offset is None:
            first_app_offset = offset

        next_offset = offset + size

    if first_app_offset is None:
        raise RuntimeError(
            "No application partition was found in partitions.csv."
        )

    return first_app_offset


def _find_framework_boot_app0(environment: str, role: str = "dut") -> Path:
    """Return boot_app0 from the PlatformIO core used for this environment."""
    core_dir = _platformio_core_dir(environment, role)
    preferred = (
        core_dir
        / "packages"
        / "framework-arduinoespressif32"
        / "tools"
        / "partitions"
        / "boot_app0.bin"
    )
    if preferred.is_file() and preferred.stat().st_size > 0:
        return preferred

    candidates: list[Path] = []
    for search_root in (
        core_dir / "packages",
        core_dir / "tools",
    ):
        if not search_root.is_dir():
            continue
        for candidate in search_root.rglob("boot_app0.bin"):
            if (
                candidate.is_file()
                and candidate.stat().st_size > 0
                and candidate.parent.name == "partitions"
                and candidate.parent.parent.name == "tools"
                and "framework-arduinoespressif32" in candidate.as_posix()
            ):
                candidates.append(candidate)

    if not candidates:
        raise RuntimeError(
            "PlatformIO build completed but framework boot_app0.bin was not found."
        )

    candidates.sort(key=lambda path: (len(path.parts), str(path).lower()))
    return candidates[0]


def _collect_flash_images(
    environment: str,
    build_dir: Path,
    role: str = "dut",
    app_offset: int | None = None,
) -> list[dict[str, Any]]:
    resolved_app_offset = (
        _application_offset_from_partitions()
        if app_offset is None
        else app_offset
    )
    boot_app0 = _find_framework_boot_app0(environment, role)

    image_specs = (
        (
            ENVIRONMENT_BOOTLOADER_OFFSETS[environment],
            "bootloader.bin",
            build_dir / "bootloader.bin",
        ),
        (
            PARTITION_TABLE_OFFSET,
            "partitions.bin",
            build_dir / "partitions.bin",
        ),
        (
            BOOT_APP0_OFFSET,
            "boot_app0.bin",
            boot_app0,
        ),
        (
            resolved_app_offset,
            "firmware.bin",
            build_dir / "firmware.bin",
        ),
    )

    images: list[dict[str, Any]] = []
    for offset, filename, source in image_specs:
        if not source.is_file() or source.stat().st_size <= 0:
            raise RuntimeError(
                f"Build for {environment} completed but {filename} is missing."
            )

        images.append(
            {
                "offset": offset,
                "filename": filename,
                "source": source,
            }
        )

    return images


def _build_environment(
    test_runner_module: Any,
    settings: dict[str, Any],
    environment: str,
    staging_root: Path,
) -> dict[str, Any]:
    resolved = test_runner_module.resolve_test_config(settings, environment)

    print("")
    print("=" * 70)
    print(f"Building {environment}")
    print("=" * 70)

    process_env, _compiler = _build_process_environment(environment)

    test_runner_module.write_generated_test_config(environment, resolved)
    try:
        try:
            result = subprocess.run(
                _platformio_command("run", "-e", environment),
                cwd=str(ROOT),
                check=False,
                env=process_env,
                timeout=900,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"PlatformIO build timed out for {environment} after 900 seconds."
            ) from exc
    finally:
        try:
            GENERATED_TEST_CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass

    if result.returncode != 0:
        raise RuntimeError(
            f"PlatformIO build failed for {environment}. "
            "Existing firmware artifacts remain unchanged."
        )

    build_dir = ROOT / ".pio" / "build" / environment
    source_images = _collect_flash_images(environment, build_dir)

    target_dir = staging_root / environment
    target_dir.mkdir(parents=True, exist_ok=True)

    flash_images: list[dict[str, Any]] = []
    for image in source_images:
        source = Path(image["source"])
        filename = str(image["filename"])
        target = target_dir / filename
        shutil.copy2(source, target)

        file_hash = _sha256_file(target)
        size = target.stat().st_size
        offset = int(image["offset"])

        flash_images.append(
            {
                "offset": offset,
                "file": f"{environment}/{filename}",
                "sha256": file_hash,
                "size": size,
            }
        )

        print(
            f"Artifact: 0x{offset:X} / "
            f"{target.relative_to(staging_root)} / "
            f"{size} bytes / SHA256 {file_hash}"
        )

    return {
        "chip": ENVIRONMENT_CHIPS[environment],
        "flash_images": flash_images,
        "flash_settings": dict(ENVIRONMENT_FLASH_SETTINGS[environment]),
        "upload_speed": _read_upload_speed(environment),
        "firmware_tests": {
            key: bool(resolved[key])
            for key in FIRMWARE_CONFIG_KEYS
        },
    }


def compile_all() -> int:
    import test_runner as tr

    settings = load_effective_settings()
    fingerprint = compute_firmware_fingerprint(tr, settings)

    staging_root = ROOT / (
        f".firmware-artifacts-staging-{os.getpid()}-{int(time.time())}"
    )
    staging_root.mkdir(parents=True, exist_ok=False)

    try:
        environments: dict[str, Any] = {}
        for environment in ENVIRONMENTS:
            environments[environment] = _build_environment(
                tr,
                settings,
                environment,
                staging_root,
            )

        manifest = {
            "schema": 3,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "firmware_fingerprint": fingerprint,
            "platformio_requirement": _platformio_requirement(),
            "environments": environments,
        }

        # Only publish artifacts after every environment built successfully.
        # manifest.json is written last, so an interrupted copy can never look valid.
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        for environment in ENVIRONMENTS:
            source_dir = staging_root / environment
            target_dir = ARTIFACT_ROOT / environment
            target_dir.mkdir(parents=True, exist_ok=True)

            for filename in (
                "bootloader.bin",
                "partitions.bin",
                "boot_app0.bin",
                "firmware.bin",
            ):
                shutil.copy2(
                    source_dir / filename,
                    target_dir / filename,
                )

        manifest_tmp = ARTIFACT_ROOT / "manifest.json.tmp"
        manifest_tmp.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_tmp, MANIFEST_PATH)

    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        try:
            GENERATED_TEST_CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass

    print("")
    print("Firmware artifacts: CURRENT")
    print(f"Fingerprint: {fingerprint}")
    print(f"Manifest: {MANIFEST_PATH.relative_to(ROOT)}")
    return 0


def _build_reference_environment(
    environment: str,
    staging_root: Path,
) -> dict[str, Any]:
    print("")
    print("=" * 70)
    print(f"Building reference {environment}")
    print("=" * 70)

    process_env, _compiler = _build_process_environment(
        environment,
        role="reference",
        project_dir=REFERENCE_PROJECT,
    )
    try:
        result = subprocess.run(
            _platformio_command(
                "run",
                "--project-dir",
                str(REFERENCE_PROJECT),
                "-e",
                environment,
            ),
            cwd=str(ROOT),
            check=False,
            env=process_env,
            timeout=REFERENCE_BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Reference PlatformIO build timed out for {environment} after "
            f"{REFERENCE_BUILD_TIMEOUT_SECONDS} seconds."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"Reference PlatformIO build failed for {environment}. "
            "Existing reference firmware artifacts remain unchanged."
        )

    build_dir = REFERENCE_PROJECT / ".pio" / "build" / environment
    source_images = _collect_flash_images(
        environment,
        build_dir,
        role="reference",
        app_offset=0x10000,
    )
    target_dir = staging_root / environment
    target_dir.mkdir(parents=True, exist_ok=True)

    flash_images: list[dict[str, Any]] = []
    for image in source_images:
        source = Path(image["source"])
        filename = str(image["filename"])
        target = target_dir / filename
        shutil.copy2(source, target)
        flash_images.append(
            {
                "offset": int(image["offset"]),
                "file": f"{environment}/{filename}",
                "sha256": _sha256_file(target),
                "size": target.stat().st_size,
            }
        )

    return {
        "chip": ENVIRONMENT_CHIPS[environment],
        "flash_images": flash_images,
        "flash_settings": dict(ENVIRONMENT_FLASH_SETTINGS[environment]),
        "upload_speed": _read_upload_speed(environment, REFERENCE_PROJECT),
    }


def compile_reference_all() -> int:
    fingerprint = compute_reference_fingerprint()
    staging_root = ROOT / (
        f".reference-firmware-artifacts-staging-{os.getpid()}-{int(time.time())}"
    )
    staging_root.mkdir(parents=True, exist_ok=False)

    try:
        environments = {
            environment: _build_reference_environment(environment, staging_root)
            for environment in ENVIRONMENTS
        }
        manifest = {
            "schema": 3,
            "role": "reference",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "firmware_fingerprint": fingerprint,
            "platformio_requirement": _platformio_requirement(),
            "environments": environments,
        }
        REFERENCE_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        for environment in ENVIRONMENTS:
            source_dir = staging_root / environment
            target_dir = REFERENCE_ARTIFACT_ROOT / environment
            target_dir.mkdir(parents=True, exist_ok=True)
            for filename in (
                "bootloader.bin",
                "partitions.bin",
                "boot_app0.bin",
                "firmware.bin",
            ):
                shutil.copy2(source_dir / filename, target_dir / filename)

        manifest_tmp = REFERENCE_ARTIFACT_ROOT / "manifest.json.tmp"
        manifest_tmp.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_tmp, REFERENCE_MANIFEST_PATH)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    print("")
    print("Reference firmware artifacts: CURRENT")
    print(f"Fingerprint: {fingerprint}")
    print(f"Manifest: {REFERENCE_MANIFEST_PATH.relative_to(ROOT)}")
    _remove_legacy_build_data_if_ready()
    return 0


def _remove_legacy_build_data_if_ready() -> None:
    """Remove superseded shared caches only after both artifact sets verify."""
    import test_runner as tr

    try:
        for environment in ENVIRONMENTS:
            verify_artifact(tr, environment)
            verify_reference_artifact(environment)
    except RuntimeError:
        print("Legacy PlatformIO data retained until both artifact sets verify.")
        return

    for environment in ENVIRONMENTS:
        for role in ("dut", "reference"):
            if not _platformio_core_dir(environment, role).is_dir():
                print("Legacy PlatformIO data retained because a new core is missing.")
                return

    legacy_platformio_entries = (
        ".cache", "appstate.json", "build", "contrib-piohome", "cores",
        "dist", "idf-env.json", "iperf", "lib", "packages", "penv",
        "platforms", "tools",
    )
    for name in legacy_platformio_entries:
        path = ROOT / ".platformio" / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    for path in (ROOT / ".pio", REFERENCE_PROJECT / ".pio"):
        if path.is_dir():
            shutil.rmtree(path)

    for environment in ENVIRONMENTS:
        legacy_artifact_dir = ROOT / "firmware-artifacts" / environment
        if legacy_artifact_dir.is_dir():
            shutil.rmtree(legacy_artifact_dir)
    legacy_manifest = ROOT / "firmware-artifacts" / "manifest.json"
    if legacy_manifest.is_file():
        legacy_manifest.unlink()

    print("Removed superseded shared PlatformIO caches and flat firmware artifacts.")


def prepare_all() -> int:
    """Install each role/environment package set into its own core.

    pioarduino can defer individual toolchain payloads until its first build.
    Such a deferred download remains confined to the matching core.
    """
    for role, project_dir in (
        ("dut", ROOT),
        ("reference", REFERENCE_PROJECT),
    ):
        for environment in ENVIRONMENTS:
            print("")
            print(f"Preparing {role} {environment} ...")
            _build_process_environment(environment, role, project_dir)
    print("")
    print("PlatformIO package preparation completed.")
    return 0


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        raise RuntimeError(
            "Compiled test firmware is missing. "
            "Run scripts\\compile_all.cmd first."
        )

    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Compiled test firmware manifest is unreadable. "
            "Run scripts\\compile_all.cmd again."
        ) from exc

    if not isinstance(manifest, dict) or manifest.get("schema") != 3:
        raise RuntimeError(
            "Compiled test firmware manifest has an unsupported format. "
            "Run scripts\\compile_all.cmd again."
        )
    return manifest


def verify_artifact(
    test_runner_module: Any,
    environment: str,
) -> tuple[dict[str, Any], list[tuple[int, Path]]]:
    manifest = _load_manifest()

    current_fingerprint = compute_firmware_fingerprint(test_runner_module)
    built_fingerprint = str(manifest.get("firmware_fingerprint") or "")

    if built_fingerprint != current_fingerprint:
        raise RuntimeError(
            "WARNING: Test firmware sources/configuration changed since the last "
            "compile_all.\n"
            "Run scripts\\compile_all.cmd before testing another board."
        )

    environments = manifest.get("environments")
    if not isinstance(environments, dict):
        raise RuntimeError(
            "Compiled test firmware manifest is incomplete. "
            "Run scripts\\compile_all.cmd again."
        )

    entry = environments.get(environment)
    if not isinstance(entry, dict):
        raise RuntimeError(
            f"Compiled firmware for {environment} is missing. "
            "Run scripts\\compile_all.cmd again."
        )

    flash_images = entry.get("flash_images")
    if not isinstance(flash_images, list) or not flash_images:
        raise RuntimeError(
            f"Compiled firmware layout for {environment} is missing. "
            "Run scripts\\compile_all.cmd again."
        )

    verified: list[tuple[int, Path]] = []
    for image in flash_images:
        if not isinstance(image, dict):
            raise RuntimeError(
                f"Compiled firmware layout for {environment} is invalid. "
                "Run scripts\\compile_all.cmd again."
            )

        offset = int(image.get("offset"))
        relative_file = str(image.get("file") or "")
        artifact = ARTIFACT_ROOT / relative_file
        if not artifact.is_file():
            raise RuntimeError(
                f"Compiled firmware file for {environment} is missing: "
                f"{relative_file}. Run scripts\\compile_all.cmd again."
            )

        expected_size = int(image.get("size") or 0)
        if artifact.stat().st_size != expected_size:
            raise RuntimeError(
                f"Compiled firmware file for {environment} has the wrong size: "
                f"{relative_file}. Run scripts\\compile_all.cmd again."
            )

        expected_hash = str(image.get("sha256") or "").lower()
        actual_hash = _sha256_file(artifact).lower()
        if not expected_hash or actual_hash != expected_hash:
            raise RuntimeError(
                f"Compiled firmware file for {environment} failed SHA256 "
                f"verification: {relative_file}. "
                "Run scripts\\compile_all.cmd again."
            )

        verified.append((offset, artifact))

    return entry, verified


def _load_reference_manifest() -> dict[str, Any]:
    if not REFERENCE_MANIFEST_PATH.is_file():
        raise RuntimeError(
            "Compiled reference firmware is missing. "
            "Run scripts\\compile_reference_all.cmd first."
        )
    try:
        manifest = json.loads(REFERENCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Compiled reference firmware manifest is unreadable. "
            "Run scripts\\compile_reference_all.cmd again."
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 3
        or manifest.get("role") != "reference"
    ):
        raise RuntimeError(
            "Compiled reference firmware manifest has an unsupported format. "
            "Run scripts\\compile_reference_all.cmd again."
        )
    return manifest


def verify_reference_artifact(
    environment: str,
) -> tuple[dict[str, Any], list[tuple[int, Path]]]:
    manifest = _load_reference_manifest()
    if str(manifest.get("firmware_fingerprint") or "") != compute_reference_fingerprint():
        raise RuntimeError(
            "WARNING: Reference firmware sources/configuration changed since "
            "the last compile_reference_all.\n"
            "Run scripts\\compile_reference_all.cmd before flashing the reference ESP."
        )

    environments = manifest.get("environments")
    if not isinstance(environments, dict):
        raise RuntimeError("Compiled reference firmware manifest is incomplete.")
    entry = environments.get(environment)
    if not isinstance(entry, dict):
        raise RuntimeError(f"Compiled reference firmware for {environment} is missing.")
    flash_images = entry.get("flash_images")
    if not isinstance(flash_images, list) or not flash_images:
        raise RuntimeError(f"Compiled reference firmware layout for {environment} is missing.")

    verified: list[tuple[int, Path]] = []
    for image in flash_images:
        if not isinstance(image, dict):
            raise RuntimeError(f"Compiled reference firmware layout for {environment} is invalid.")
        offset = int(image.get("offset"))
        relative_file = str(image.get("file") or "")
        artifact = REFERENCE_ARTIFACT_ROOT / relative_file
        if not artifact.is_file():
            raise RuntimeError(f"Compiled reference firmware file is missing: {relative_file}.")
        if artifact.stat().st_size != int(image.get("size") or 0):
            raise RuntimeError(f"Compiled reference firmware file has the wrong size: {relative_file}.")
        expected_hash = str(image.get("sha256") or "").lower()
        if not expected_hash or _sha256_file(artifact).lower() != expected_hash:
            raise RuntimeError(
                f"Compiled reference firmware file failed SHA256 verification: {relative_file}."
            )
        verified.append((offset, artifact))
    return entry, verified


def flash_reference_artifact(port: str, environment: str) -> None:
    entry, flash_images = verify_reference_artifact(environment)
    flash_settings = entry.get("flash_settings")
    if not isinstance(flash_settings, dict):
        raise RuntimeError(f"Compiled reference flash settings for {environment} are missing.")
    flash_mode = str(flash_settings.get("mode") or "")
    flash_frequency = str(flash_settings.get("frequency") or "")
    flash_size = str(flash_settings.get("size") or "")
    if not flash_mode or not flash_frequency or not flash_size:
        raise RuntimeError(f"Compiled reference flash settings for {environment} are incomplete.")

    command = [
        sys.executable, "-m", "esptool", "--chip",
        str(entry.get("chip") or ENVIRONMENT_CHIPS[environment]),
        "--port", port, "--baud", str(int(entry.get("upload_speed") or 921600)),
        "--before", "default_reset", "--after", "hard_reset", "write_flash", "-z",
        "--flash_mode", flash_mode, "--flash_freq", flash_frequency,
        "--flash_size", flash_size,
    ]
    for offset, artifact in flash_images:
        command.extend((f"0x{offset:X}", str(artifact)))

    print(f"Flashing verified reference firmware ({environment}) ...")
    result = subprocess.run(command, cwd=str(ROOT), timeout=600, creationflags=0)
    if result.returncode != 0:
        raise RuntimeError("Reference firmware flash failed. The esptool error is shown above.")


def _artifact_preflight(
    test_runner_module: Any,
    environment: str,
    test_config: dict[str, bool],
    settings: dict[str, Any],
) -> None:
    print("")
    print("Preflight check ...")

    if test_runner_module.requires_ping_command(test_config):
        ping_executable = shutil.which("ping")
        if not ping_executable:
            raise RuntimeError(
                "The host 'ping' command is required by the enabled "
                "ping/reconnect/BLE coexistence tests but was not found in PATH."
            )
        print(f"  Host ping: PASS / {ping_executable}")
    else:
        print("  Host ping: SKIP / not required by selected tests")

    entry, flash_images = verify_artifact(test_runner_module, environment)
    manifest = _load_manifest()
    fingerprint = str(manifest.get("firmware_fingerprint") or "")

    # Defensive cross-check: manifest and current resolved firmware switches
    # must describe the same firmware even if the fingerprint format changes later.
    expected_tests = {
        key: bool(test_config[key])
        for key in FIRMWARE_CONFIG_KEYS
    }
    if entry.get("firmware_tests") != expected_tests:
        raise RuntimeError(
            "WARNING: Compiled firmware test switches no longer match the "
            "current configuration.\n"
            "Run scripts\\compile_all.cmd before testing another board."
        )

    image_text = ", ".join(
        f"0x{offset:X}:{artifact.relative_to(ROOT)}"
        for offset, artifact in flash_images
    )
    print(
        f"  Test firmware: PASS / {environment} / {image_text}"
    )
    print(f"  Firmware fingerprint: {fingerprint}")
    print("  PlatformIO/compiler: NOT USED during board test")
    print("Preflight: PASS")
    print("")


def _artifact_flash(
    test_runner_module: Any,
    port: str,
    environment: str,
    log_path: Path,
) -> None:
    entry, flash_images = verify_artifact(test_runner_module, environment)
    chip = str(entry.get("chip") or ENVIRONMENT_CHIPS[environment])
    upload_speed = int(entry.get("upload_speed") or 921600)

    flash_settings = entry.get("flash_settings")
    if not isinstance(flash_settings, dict):
        raise RuntimeError(
            f"Compiled flash settings for {environment} are missing. "
            "Run scripts\\compile_all.cmd again."
        )

    flash_mode = str(flash_settings.get("mode") or "")
    flash_frequency = str(flash_settings.get("frequency") or "")
    flash_size = str(flash_settings.get("size") or "")
    if not flash_mode or not flash_frequency or not flash_size:
        raise RuntimeError(
            f"Compiled flash settings for {environment} are incomplete. "
            "Run scripts\\compile_all.cmd again."
        )

    command = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(upload_speed),
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        "write_flash",
        "-z",
        "--flash_mode",
        flash_mode,
        "--flash_freq",
        flash_frequency,
        "--flash_size",
        flash_size,
    ]

    for offset, artifact in flash_images:
        command.extend([f"0x{offset:X}", str(artifact)])

    print(
        f"Flashing precompiled firmware ({environment}) "
        "from verified component images ..."
    )
    print("esptool output:")
    print("-" * 60)
    started = time.monotonic()

    result = subprocess.run(
        command,
        cwd=str(ROOT),
        timeout=600,
        creationflags=0,
    )

    elapsed = int(time.monotonic() - started)
    minutes, seconds = divmod(elapsed, 60)
    print("-" * 60)

    layout_lines = [
        f"0x{offset:X}: {artifact.relative_to(ROOT)}"
        for offset, artifact in flash_images
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "Precompiled firmware flashed directly with esptool.",
                "PlatformIO/compiler was not invoked by the board test.",
                f"Environment: {environment}",
                f"Port: {port}",
                f"Upload speed: {upload_speed}",
                f"Flash mode: {flash_mode}",
                f"Flash frequency: {flash_frequency}",
                f"Flash size: {flash_size}",
                "Flash layout:",
                *[f"  {line}" for line in layout_lines],
                f"Exit code: {result.returncode}",
                f"Duration: {minutes:02d}:{seconds:02d}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Firmware flash failed. The esptool error output is shown directly "
            "above in the console."
        )

    print(f"Firmware flashed successfully ({minutes:02d}:{seconds:02d}).")


def install_runtime_artifact_mode(test_runner_module: Any) -> None:
    def preflight(
        environment: str,
        test_config: dict[str, bool],
        settings: dict[str, Any],
    ) -> None:
        _artifact_preflight(
            test_runner_module,
            environment,
            test_config,
            settings,
        )

    def write_generated_test_config_noop(
        environment: str,
        tests: dict[str, bool],
    ) -> None:
        # The selected test switches are already compiled into the verified
        # artifact. Re-generating this header during a board test would make
        # the source tree look dirty and is intentionally suppressed.
        return None

    def flash(
        port: str,
        environment: str,
        log_path: Path,
    ) -> None:
        _artifact_flash(
            test_runner_module,
            port,
            environment,
            log_path,
        )

    test_runner_module.preflight_test_environment = preflight
    test_runner_module.write_generated_test_config = write_generated_test_config_noop
    test_runner_module.flash_firmware = flash


def _print_status() -> int:
    import test_runner as tr

    try:
        manifest = _load_manifest()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    current = compute_firmware_fingerprint(tr)
    built = str(manifest.get("firmware_fingerprint") or "")

    if current != built:
        print("Firmware artifacts: OUTDATED")
        print("Run scripts\\compile_all.cmd.")
        return 2

    for environment in ENVIRONMENTS:
        try:
            _entry, flash_images = verify_artifact(tr, environment)
            layout = ", ".join(
                f"0x{offset:X}:{artifact.relative_to(ROOT)}"
                for offset, artifact in flash_images
            )
            print(f"{environment}: OK / {layout}")
        except RuntimeError as exc:
            print(f"{environment}: ERROR / {exc}")
            return 2

    print("Firmware artifacts: CURRENT")
    print(f"Fingerprint: {current}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("compile-all", "compile-reference-all", "prepare-all", "status"),
    )
    args = parser.parse_args()

    if args.command == "compile-all":
        return compile_all()
    if args.command == "compile-reference-all":
        return compile_reference_all()
    if args.command == "prepare-all":
        return prepare_all()
    if args.command == "status":
        return _print_status()

    raise AssertionError("unreachable")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
