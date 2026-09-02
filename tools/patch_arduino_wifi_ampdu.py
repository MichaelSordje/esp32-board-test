Import("env")

from pathlib import Path

if env.IsIntegrationDump():
    Return()

MARKER = "hwtest_wifi_disable_tx_ampdu"
DECL_ANCHOR = "static bool lowLevelInitDone = false;\n"
INIT_ANCHOR = "    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();\n"

DECL_PATCH = '''extern "C" bool hwtest_wifi_disable_tx_ampdu __attribute__((weak));
static bool lowLevelInitDone = false;
'''

INIT_PATCH = '''    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    if (&hwtest_wifi_disable_tx_ampdu != nullptr && hwtest_wifi_disable_tx_ampdu) {
        cfg.ampdu_tx_enable = false;
    }
'''

platform = env.PioPlatform()
framework_dir = platform.get_package_dir("framework-arduinoespressif32")
if not framework_dir:
    raise RuntimeError(
        "framework-arduinoespressif32 package directory not found after framework setup"
    )

source = Path(framework_dir) / "libraries" / "WiFi" / "src" / "WiFiGeneric.cpp"
if not source.is_file():
    raise RuntimeError(f"Arduino WiFi source not found: {source}")

text = source.read_text(encoding="utf-8")

if MARKER not in text:
    if DECL_ANCHOR not in text or INIT_ANCHOR not in text:
        raise RuntimeError(
            "Unsupported Arduino WiFiGeneric.cpp: expected WiFi-init anchors not found"
        )

    text = text.replace(DECL_ANCHOR, DECL_PATCH, 1)
    text = text.replace(INIT_ANCHOR, INIT_PATCH, 1)
    source.write_text(text, encoding="utf-8")

verified = source.read_text(encoding="utf-8")
required_fragments = (
    'extern "C" bool hwtest_wifi_disable_tx_ampdu __attribute__((weak));',
    "cfg.ampdu_tx_enable = false;",
)

if not all(fragment in verified for fragment in required_fragments):
    raise RuntimeError(
        "Arduino WiFi AMPDU patch verification failed for WiFiGeneric.cpp"
    )
