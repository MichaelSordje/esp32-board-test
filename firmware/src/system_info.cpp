#include "system_info.h"

#include <Arduino.h>
#include <esp_chip_info.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>
#include <esp_system.h>
#include <soc/soc_caps.h>

#include "protocol.h"
#include "version.h"

static const char *resetReasonName(esp_reset_reason_t reason)
{
    switch (reason)
    {
        case ESP_RST_UNKNOWN: return "UNKNOWN";
        case ESP_RST_POWERON: return "POWERON";
        case ESP_RST_EXT: return "EXTERNAL";
        case ESP_RST_SW: return "SOFTWARE";
        case ESP_RST_PANIC: return "PANIC";
        case ESP_RST_INT_WDT: return "INT_WDT";
        case ESP_RST_TASK_WDT: return "TASK_WDT";
        case ESP_RST_WDT: return "WDT";
        case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
        case ESP_RST_BROWNOUT: return "BROWNOUT";
        case ESP_RST_SDIO: return "SDIO";
        case ESP_RST_USB: return "USB";
        case ESP_RST_JTAG: return "JTAG";
        case ESP_RST_EFUSE: return "EFUSE";
        case ESP_RST_PWR_GLITCH: return "POWER_GLITCH";
        case ESP_RST_CPU_LOCKUP: return "CPU_LOCKUP";
        default: return "OTHER";
    }
}

static String stationMacAddress()
{
    uint8_t mac[6] = {};

    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK)
    {
        return "unavailable";
    }

    char text[18];
    snprintf(text,
             sizeof(text),
             "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0],
             mac[1],
             mac[2],
             mac[3],
             mac[4],
             mac[5]);

    return String(text);
}

void reportSystemInfo()
{
    esp_chip_info_t chipInfo = {};
    esp_chip_info(&chipInfo);

    reportLine("SYSTEM", "chip=%s|revision=%u|cores=%u|cpu_mhz=%u|flash_bytes=%u|psram_bytes=%u|heap_free=%u|heap_min=%u|reset=%s|profile=%s",
               ESP.getChipModel(),
               static_cast<unsigned>(ESP.getChipRevision()),
               static_cast<unsigned>(chipInfo.cores),
               static_cast<unsigned>(ESP.getCpuFreqMHz()),
               static_cast<unsigned>(ESP.getFlashChipSize()),
               static_cast<unsigned>(ESP.getPsramSize()),
               static_cast<unsigned>(ESP.getFreeHeap()),
               static_cast<unsigned>(ESP.getMinFreeHeap()),
               resetReasonName(esp_reset_reason()),
               HWTEST_PROFILE);

    const String mac = stationMacAddress();
    reportLine("SYSTEM", "mac=%s|sdk=%s|arduino=%s", mac.c_str(), ESP.getSdkVersion(), ESP_ARDUINO_VERSION_STR);
    reportLine("SYSTEM", "firmware_version=%s|build_date=%s|build_time=%s", HWTEST_VERSION, __DATE__, __TIME__);

#if defined(CONFIG_IDF_TARGET_ESP32) || SOC_TEMP_SENSOR_SUPPORTED
    const float temperature = temperatureRead();
    if (!isnan(temperature))
    {
        reportLine("SYSTEM", "temperature_c=%.2f", temperature);
    }
    else
    {
        reportLine("SYSTEM", "temperature_c=unavailable");
    }
#else
    reportLine("SYSTEM", "temperature_c=unsupported");
#endif
}
