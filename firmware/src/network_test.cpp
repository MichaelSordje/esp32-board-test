#include "network_test.h"

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <cstring>
#include <cstdio>
#include <esp_heap_caps.h>
#include <esp_wifi.h>
#include <esp_private/wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mbedtls/base64.h>
#include <soc/soc_caps.h>

#include "protocol.h"
#include "self_tests.h"

extern "C" {
bool hwtest_wifi_disable_tx_ampdu = false;
}

#if SOC_BT_SUPPORTED
#include <BLEDevice.h>
#include <BLEScan.h>
#endif

static constexpr uint16_t kProbeUdpPort = 33333;
static constexpr uint16_t kRfReferenceUdpPort = 33334;
static constexpr uint16_t kRfDutUdpPort = 33335;

static WiFiUDP udp;
static IPAddress hostIp;
static uint16_t hostPort = 0;
static bool hostConfigured = false;
static bool wifiConfigured = false;
static bool udpStarted = false;
static bool wasConnected = false;
static bool hasGotIp = false;
static bool disconnectEpisodeActive = false;
static bool plannedDisconnectActive = false;
static bool plannedReconnectPending = false;
static uint32_t plannedReconnectAtMs = 0;
static uint32_t plannedReconnectAttemptCount = 0;
static bool plannedReconnectTargetValid = false;
static int32_t plannedReconnectTargetChannel = 0;
static uint8_t plannedReconnectTargetBssid[6] = {};
static String plannedReconnectTargetBssidText;
static constexpr uint32_t kPlannedReconnectDelayMs = 750;
static uint32_t disconnectCount = 0;
static uint32_t reconnectCount = 0;
static uint32_t heartbeatSequence = 0;
static uint32_t lastHeartbeatMs = 0;
static uint32_t udpPongSendFailures = 0;
static uint32_t udpHeartbeatSendFailures = 0;
static bool logNextUdpPong = true;
static bool logNextUdpHeartbeat = true;
static String serialCommand;
static String wifiSsid;
static String wifiPassword;
static bool normalWifiTxPowerConfigured = false;
static int32_t normalWifiTxPowerDbm = 0;

static uint32_t heapCheckIntervalMs = 30000;
static uint32_t lastHeapCheckMs = 0;

static bool selectedApValid = false;
static int32_t selectedApRssi = -127;
static int32_t selectedApChannel = 0;
static uint8_t selectedApBssid[6] = {};
static String selectedApBssidText;

// Dedicated RF-quality mode. The PC is intentionally not part of this data
// path. The DUT exchanges numbered UDP packets directly with the reference ESP.
static bool rfMode = false;
static WiFiUDP rfUdp;
static bool rfUdpStarted = false;
static IPAddress rfReferenceIp;
static uint32_t rfRxPackets = 0;
static uint32_t rfTxPackets = 0;
static int64_t rfRssiSum = 0;
static uint32_t rfRssiSamples = 0;
static int32_t rfRssiMin = 0;
static int32_t rfRssiMax = -127;
static int32_t rfTxPowerDbm = 20;
static int32_t rfRequestedTxPowerDbm = 20;
static bool rfRequestedTxPowerValid = false;
static uint32_t rfExpectedRxRunId = 0;
static bool rfExpectedRxRunValid = false;
static bool rfHelloAcknowledged = false;
static uint32_t rfLastHelloMs = 0;
static bool rfTxActive = false;
static uint32_t rfTxTargetCount = 0;
static uint32_t rfTxSequence = 0;
static uint32_t rfTxIntervalMs = 20;
static uint32_t rfNextTxAtMs = 0;
static uint32_t rfTxRunId = 0;
static bool rfProtocolConfigured = false;
static esp_err_t rfProtocolError = ESP_OK;
static esp_err_t rfProtocolGetError = ESP_OK;
static uint8_t rfProtocolBitmap = 0;
static esp_err_t rfPowerSaveError = ESP_OK;
static esp_err_t rfPowerSaveGetError = ESP_OK;
static wifi_ps_type_t rfPowerSaveState = WIFI_PS_MAX_MODEM;
static constexpr wifi_phy_rate_t kRfFixedPhyRate = WIFI_PHY_RATE_1M_L;
static bool rfFixedRateConfigured = false;
static esp_err_t rfFixedRateError = ESP_OK;

static bool configureRfRadioControl() {
    rfProtocolError = esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B);
    rfProtocolBitmap = 0;
    rfProtocolGetError = esp_wifi_get_protocol(WIFI_IF_STA, &rfProtocolBitmap);

    rfPowerSaveError = esp_wifi_set_ps(WIFI_PS_NONE);
    rfPowerSaveState = WIFI_PS_MAX_MODEM;
    rfPowerSaveGetError = esp_wifi_get_ps(&rfPowerSaveState);

    rfFixedRateError = esp_wifi_internal_set_fix_rate(
        WIFI_IF_STA,
        true,
        kRfFixedPhyRate);
    rfFixedRateConfigured = rfFixedRateError == ESP_OK;

    rfProtocolConfigured =
        rfProtocolError == ESP_OK
        && rfProtocolGetError == ESP_OK
        && rfProtocolBitmap == WIFI_PROTOCOL_11B;

    const bool powerSaveDisabled =
        rfPowerSaveError == ESP_OK
        && rfPowerSaveGetError == ESP_OK
        && rfPowerSaveState == WIFI_PS_NONE;

    return rfProtocolConfigured && powerSaveDisabled && rfFixedRateConfigured;
}

static void restoreNormalWifiRadioControl() {
    if (rfFixedRateConfigured) {
        (void)esp_wifi_internal_set_fix_rate(
            WIFI_IF_STA,
            false,
            kRfFixedPhyRate);
    }
    if (rfProtocolConfigured) {
        (void)esp_wifi_set_protocol(
            WIFI_IF_STA,
            WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    }
    rfProtocolConfigured = false;
    rfProtocolError = ESP_OK;
    rfProtocolGetError = ESP_OK;
    rfProtocolBitmap = 0;
    rfPowerSaveError = ESP_OK;
    rfPowerSaveGetError = ESP_OK;
    rfPowerSaveState = WIFI_PS_MAX_MODEM;
    rfFixedRateConfigured = false;
    rfFixedRateError = ESP_OK;
}

#if SOC_BT_SUPPORTED
static volatile bool bleCoexRunning = false;
static volatile bool bleCoexDone = false;
static volatile int bleCoexDevices = -1;
static volatile bool bleCoexInitFailed = false;
static uint32_t bleCoexDurationMs = 0;

static void bleCoexTask(void *parameter) {
    (void)parameter;
    int devices = -1;

    BLEDevice::init("ESP32-Hardware-Coex");
    if (!BLEDevice::getInitialized()) {
        bleCoexInitFailed = true;
    } else {
        BLEScan *scanner = BLEDevice::getScan();
        if (scanner != nullptr) {
            scanner->setActiveScan(true);
            scanner->setInterval(100);
            scanner->setWindow(90);
            const uint32_t seconds = ((bleCoexDurationMs + 999u) / 1000u > 0 ? (bleCoexDurationMs + 999u) / 1000u : 1u);
            BLEScanResults *results = scanner->start(seconds, false);
            devices = results == nullptr ? -1 : results->getCount();
            scanner->clearResults();
        }
        BLEDevice::deinit(false);
    }

    bleCoexDevices = devices;
    bleCoexRunning = false;
    bleCoexDone = true;
    vTaskDelete(nullptr);
}
#endif

static void resetRfStats(uint32_t runId = 0, bool useRunFilter = false) {
    rfRxPackets = 0;
    rfTxPackets = 0;
    rfRssiSum = 0;
    rfRssiSamples = 0;
    rfRssiMin = 0;
    rfRssiMax = -127;
    rfExpectedRxRunId = runId;
    rfExpectedRxRunValid = useRunFilter;
}

static void sampleRfRssi() {
    if (WiFi.status() != WL_CONNECTED) {
        return;
    }
    const int32_t rssi = WiFi.RSSI();
    if (rssi <= -127 || rssi > 0) {
        return;
    }
    rfRssiSum += rssi;
    ++rfRssiSamples;
    if (rfRssiSamples == 1 || rssi < rfRssiMin) {
        rfRssiMin = rssi;
    }
    if (rfRssiSamples == 1 || rssi > rfRssiMax) {
        rfRssiMax = rssi;
    }
}

static int32_t setRfTxPowerDbm(int32_t requestedDbm) {
    requestedDbm = constrain(requestedDbm, 2, 20);
    if (rfRequestedTxPowerValid && requestedDbm == rfRequestedTxPowerDbm) {
        return rfTxPowerDbm;
    }

    const int8_t quarterDbm = static_cast<int8_t>(requestedDbm * 4);
    if (esp_wifi_set_max_tx_power(quarterDbm) != ESP_OK) {
        return rfTxPowerDbm;
    }

    rfRequestedTxPowerDbm = requestedDbm;
    rfRequestedTxPowerValid = true;

    int8_t actualQuarterDbm = quarterDbm;
    if (esp_wifi_get_max_tx_power(&actualQuarterDbm) == ESP_OK) {
        rfTxPowerDbm = static_cast<int32_t>(actualQuarterDbm) / 4;
    } else {
        rfTxPowerDbm = requestedDbm;
    }
    return rfTxPowerDbm;
}

static bool applyNormalWifiTxPower() {
    if (!normalWifiTxPowerConfigured) {
        return true;
    }

    const int8_t requestedQuarterDbm =
        static_cast<int8_t>(normalWifiTxPowerDbm * 4);
    const esp_err_t setResult =
        esp_wifi_set_max_tx_power(requestedQuarterDbm);

    int8_t actualQuarterDbm = requestedQuarterDbm;
    const esp_err_t getResult =
        esp_wifi_get_max_tx_power(&actualQuarterDbm);

    reportLine(
        "WIFI",
        "status=TX_POWER|requested_dbm=%ld|actual_dbm=%ld|set_error=%ld|get_error=%ld",
        static_cast<long>(normalWifiTxPowerDbm),
        static_cast<long>(actualQuarterDbm) / 4L,
        static_cast<long>(setResult),
        static_cast<long>(getResult));

    return setResult == ESP_OK && getResult == ESP_OK;
}

static void sendRfHello() {
    if (!rfMode || !rfUdpStarted || WiFi.status() != WL_CONNECTED) {
        return;
    }
    rfReferenceIp = WiFi.gatewayIP();
    rfUdp.beginPacket(rfReferenceIp, kRfReferenceUdpPort);
    rfUdp.printf("RF|HELLO|%s", WiFi.macAddress().c_str());
    rfUdp.endPacket();
    rfLastHelloMs = millis();
}

static void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
    if (rfMode) {
        if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
            reportLine("RF_DUT", "status=ASSOCIATED|channel=%d|bssid=%s", WiFi.channel(), WiFi.BSSIDstr().c_str());
        } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
            if (rfUdpStarted) {
                rfUdp.stop();
            }
            rfUdpStarted = rfUdp.begin(kRfDutUdpPort) == 1;
            rfReferenceIp = WiFi.gatewayIP();
            rfHelloAcknowledged = false;
            rfLastHelloMs = 0;
            reportLine(
                "RF_DUT",
                "status=CONNECTED|ip=%s|gateway=%s|bssid=%s|channel=%d|rssi=%d|udp=%u|protocol=11b|protocol_ok=%u|protocol_bitmap=%u|power_save_off=%u|fixed_rate=1M_L|fixed_rate_ok=%u",
                WiFi.localIP().toString().c_str(),
                WiFi.gatewayIP().toString().c_str(),
                WiFi.BSSIDstr().c_str(),
                WiFi.channel(),
                WiFi.RSSI(),
                rfUdpStarted ? 1U : 0U,
                rfProtocolConfigured ? 1U : 0U,
                static_cast<unsigned>(rfProtocolBitmap),
                (rfPowerSaveGetError == ESP_OK && rfPowerSaveState == WIFI_PS_NONE) ? 1U : 0U,
                rfFixedRateConfigured ? 1U : 0U);
            sendRfHello();
        } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
            if (rfUdpStarted) {
                rfUdp.stop();
                rfUdpStarted = false;
            }
            rfHelloAcknowledged = false;
            rfLastHelloMs = 0;
            rfTxActive = false;
            reportLine(
                "RF_DUT",
                "status=DISCONNECTED|reason=%u",
                static_cast<unsigned>(info.wifi_sta_disconnected.reason));
        }
        return;
    }

    if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
        reportLine("WIFI_EVENT", "event=STA_CONNECTED|channel=%d", WiFi.channel());
    } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
        const bool isReconnect = hasGotIp;
        const bool wasPlanned = plannedDisconnectActive;
        hasGotIp = true;
        disconnectEpisodeActive = false;
        plannedReconnectPending = false;
        plannedReconnectAttemptCount = 0;
        plannedDisconnectActive = false;

        if (isReconnect) {
            ++reconnectCount;
        }

        reportLine("WIFI_EVENT", "event=GOT_IP|ip=%s|gateway=%s|bssid=%s|channel=%d|rssi=%d|reconnects=%u|planned=%u",
                   WiFi.localIP().toString().c_str(),
                   WiFi.gatewayIP().toString().c_str(),
                   WiFi.BSSIDstr().c_str(),
                   WiFi.channel(),
                   WiFi.RSSI(),
                   static_cast<unsigned>(reconnectCount),
                   wasPlanned ? 1U : 0U);

        if (wasPlanned) {
            const bool targetMatch =
                plannedReconnectTargetValid
                && WiFi.channel() == plannedReconnectTargetChannel
                && WiFi.BSSIDstr().equalsIgnoreCase(plannedReconnectTargetBssidText);
            reportLine(
                "RECONNECT_TEST",
                "status=GOT_IP|ip=%s|rssi=%d|target_bssid=%s|target_channel=%ld|actual_bssid=%s|actual_channel=%d|target_match=%u",
                WiFi.localIP().toString().c_str(),
                WiFi.RSSI(),
                plannedReconnectTargetBssidText.c_str(),
                static_cast<long>(plannedReconnectTargetChannel),
                WiFi.BSSIDstr().c_str(),
                WiFi.channel(),
                targetMatch ? 1U : 0U);
            WiFi.setAutoReconnect(true);
        }
    } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
        const bool runtimeDisconnect = hasGotIp;
        const bool planned = plannedDisconnectActive;
        const bool countedDisconnect = runtimeDisconnect && !planned && !disconnectEpisodeActive;

        if (countedDisconnect) {
            ++disconnectCount;
            disconnectEpisodeActive = true;
        }

        const char *phase = planned ? "planned" : (runtimeDisconnect ? "runtime" : "startup");
        reportLine("WIFI_EVENT",
                   "event=DISCONNECTED|reason=%u|disconnects=%u|phase=%s|counted=%u|reconnect_attempt=%u",
                   static_cast<unsigned>(info.wifi_sta_disconnected.reason),
                   static_cast<unsigned>(disconnectCount),
                   phase,
                   countedDisconnect ? 1U : 0U,
                   static_cast<unsigned>(plannedReconnectAttemptCount));

        if (planned) {
            reportLine("RECONNECT_TEST", "status=DISCONNECTED|reason=%u",
                       static_cast<unsigned>(info.wifi_sta_disconnected.reason));
        }
    }
}

static String decodeBase64(const String &encoded) {
    size_t outputLength = 0;
    const size_t maximumLength = (encoded.length() * 3) / 4 + 4;
    uint8_t *buffer = static_cast<uint8_t *>(malloc(maximumLength + 1));
    if (buffer == nullptr) {
        return "";
    }

    const int result = mbedtls_base64_decode(buffer, maximumLength, &outputLength, reinterpret_cast<const unsigned char *>(encoded.c_str()), encoded.length());
    if (result != 0) {
        free(buffer);
        return "";
    }

    buffer[outputLength] = 0;
    String decoded(reinterpret_cast<char *>(buffer));
    free(buffer);
    return decoded;
}

static bool parseBssid(const String &text, uint8_t bssid[6]) {
    if (text.length() != 17) {
        return false;
    }

    unsigned int values[6] = {};
    if (sscanf(
            text.c_str(),
            "%x:%x:%x:%x:%x:%x",
            &values[0],
            &values[1],
            &values[2],
            &values[3],
            &values[4],
            &values[5]) != 6) {
        return false;
    }

    for (size_t index = 0; index < 6; ++index) {
        if (values[index] > 0xFFu) {
            return false;
        }
        bssid[index] = static_cast<uint8_t>(values[index]);
    }
    return true;
}

static bool runWifiScan() {
    reportLine("WIFI_SCAN", "status=START");

    selectedApValid = false;
    selectedApRssi = -127;
    selectedApChannel = 0;
    memset(selectedApBssid, 0, sizeof(selectedApBssid));
    selectedApBssidText = "";

    const int count = WiFi.scanNetworks(false, true);

    if (count >= 0) {
        for (int index = 0; index < count; ++index) {
            if (WiFi.SSID(index) != wifiSsid || WiFi.RSSI(index) <= selectedApRssi) {
                continue;
            }

            const uint8_t *candidateBssid = WiFi.BSSID(index);
            if (candidateBssid == nullptr) {
                continue;
            }

            selectedApRssi = WiFi.RSSI(index);
            selectedApChannel = WiFi.channel(index);
            memcpy(selectedApBssid, candidateBssid, sizeof(selectedApBssid));
            selectedApBssidText = WiFi.BSSIDstr(index);
            selectedApValid = true;
        }
    }

    reportLine("WIFI_SCAN", "status=%s|aps=%d|target_rssi=%d|target_channel=%d|target_bssid=%s", selectedApValid ? "PASS" : "FAIL", count, selectedApRssi, selectedApChannel, selectedApValid ? selectedApBssidText.c_str() : "none");

    WiFi.scanDelete();
    return selectedApValid;
}

static void connectWifi() {
#if defined(CONFIG_IDF_TARGET_ESP32)
    reportLine("WIFI", "status=CONNECTING|mode=automatic|scan_target_rssi=%d|scan_target_channel=%d|scan_target_bssid=%s", selectedApRssi, selectedApChannel, selectedApValid ? selectedApBssidText.c_str() : "none");
    WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
#else
    if (selectedApValid) {
        reportLine("WIFI", "status=CONNECTING|mode=pinned|target_rssi=%d|target_channel=%d|target_bssid=%s", selectedApRssi, selectedApChannel, selectedApBssidText.c_str());
        WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str(), selectedApChannel, selectedApBssid, true);
        return;
    }

    reportLine("WIFI", "status=CONNECTING_FALLBACK|reason=scan_target_missing");
    WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
#endif
}

static void stopRfMode(bool reportStopped) {
    rfTxActive = false;
    rfTxRunId = 0;
    rfExpectedRxRunId = 0;
    rfExpectedRxRunValid = false;
    rfHelloAcknowledged = false;
    rfLastHelloMs = 0;
    if (rfUdpStarted) {
        rfUdp.stop();
        rfUdpStarted = false;
    }

    // RF quality deliberately disables station power saving. Before the normal
    // home-WiFi test starts, tear the RF association down completely instead
    // of reusing the driver's previous state. This avoids carrying the
    // reference-AP association/rate-control state into the home-network test.
    if (rfMode) {
        (void)setRfTxPowerDbm(20);
        restoreNormalWifiRadioControl();
        WiFi.disconnect(true, false);
        delay(150);
        WiFi.mode(WIFI_OFF);
        delay(100);
        hwtest_wifi_disable_tx_ampdu = false;
    }

    rfMode = false;
    wasConnected = false;
    if (reportStopped) {
        reportLine("RF_DUT", "status=STOPPED");
    }
}

static void parseHostCommand(const String &command) {
    const int separator = command.indexOf('|', 5);
    if (separator < 0) {
        return;
    }

    const String ipText = command.substring(5, separator);
    const String portText = command.substring(separator + 1);

    IPAddress parsed;
    if (!parsed.fromString(ipText)) {
        reportLine("HOST", "status=FAIL|reason=invalid_ip");
        return;
    }

    const long parsedPort = portText.toInt();
    if (parsedPort <= 0 || parsedPort > 65535) {
        reportLine("HOST", "status=FAIL|reason=invalid_port");
        return;
    }

    hostIp = parsed;
    hostPort = static_cast<uint16_t>(parsedPort);
    hostConfigured = true;
    reportLine("HOST", "status=OK|ip=%s|port=%u", hostIp.toString().c_str(), hostPort);
}

static void parseWifiCommand(const String &command) {
    const int firstSeparator = command.indexOf('|', 7);
    if (firstSeparator < 0) {
        reportLine("WIFI", "status=FAIL|reason=config_format");
        return;
    }

    const int secondSeparator =
        command.indexOf('|', firstSeparator + 1);

    const String decodedSsid =
        decodeBase64(command.substring(7, firstSeparator));
    const String decodedPassword = decodeBase64(
        secondSeparator >= 0
            ? command.substring(firstSeparator + 1, secondSeparator)
            : command.substring(firstSeparator + 1));

    if (decodedSsid.length() == 0) {
        reportLine("WIFI", "status=FAIL|reason=ssid_missing");
        return;
    }

    normalWifiTxPowerConfigured = false;
    normalWifiTxPowerDbm = 0;
    if (secondSeparator >= 0) {
        const String powerText = command.substring(secondSeparator + 1);
        const long requestedPower = powerText.toInt();
        if (requestedPower < 2 || requestedPower > 20) {
            reportLine(
                "WIFI",
                "status=FAIL|reason=invalid_tx_power|requested_dbm=%ld",
                requestedPower);
            return;
        }
        normalWifiTxPowerConfigured = true;
        normalWifiTxPowerDbm = static_cast<int32_t>(requestedPower);
    }

    stopRfMode(false);
    if (udpStarted) {
        udp.stop();
        udpStarted = false;
    }

    // Always enter the normal WiFi test from a clean STA state. The RF test
    // uses different credentials, disables modem sleep and changes TX power.
    // Reusing that association caused occasional home-WiFi connect timeouts.
    hwtest_wifi_disable_tx_ampdu = false;
    WiFi.disconnect(true, false);
    delay(100);
    WiFi.mode(WIFI_STA);
    if (!applyNormalWifiTxPower()) {
        reportLine("WIFI", "status=FAIL|reason=tx_power_apply_failed");
        return;
    }
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(true);
    delay(100);

    wifiSsid = decodedSsid;
    wifiPassword = decodedPassword;
    wifiConfigured = true;
    hasGotIp = false;
    plannedDisconnectActive = false;
    plannedReconnectPending = false;
    plannedReconnectAttemptCount = 0;
    plannedReconnectTargetValid = false;
    plannedReconnectTargetChannel = 0;
    plannedReconnectTargetBssidText = "";
    memset(plannedReconnectTargetBssid, 0, sizeof(plannedReconnectTargetBssid));
    disconnectEpisodeActive = false;

    reportLine(
        "WIFI",
        "status=CONFIGURED|ssid_length=%u|tx_power_dbm=%ld",
        static_cast<unsigned>(wifiSsid.length()),
        static_cast<long>(
            normalWifiTxPowerConfigured ? normalWifiTxPowerDbm : 0));
    runWifiScan();
    connectWifi();
}

static void parseRfWifiCommand(const String &command) {
    const int firstSeparator = command.indexOf('|', 10);
    const int secondSeparator = firstSeparator >= 0
        ? command.indexOf('|', firstSeparator + 1)
        : -1;
    const int thirdSeparator = secondSeparator >= 0
        ? command.indexOf('|', secondSeparator + 1)
        : -1;
    if (firstSeparator < 0 || secondSeparator < 0 || thirdSeparator < 0) {
        reportLine("RF_DUT", "status=FAIL|reason=config_format");
        return;
    }

    const String decodedSsid = decodeBase64(command.substring(10, firstSeparator));
    const String decodedPassword = decodeBase64(
        command.substring(firstSeparator + 1, secondSeparator));
    const int32_t channel = command.substring(
        secondSeparator + 1, thirdSeparator).toInt();
    const String bssidText = command.substring(thirdSeparator + 1);
    uint8_t bssid[6] = {};

    if (decodedSsid.length() == 0) {
        reportLine("RF_DUT", "status=FAIL|reason=ssid_missing");
        return;
    }
    if (channel < 1 || channel > 13) {
        reportLine("RF_DUT", "status=FAIL|reason=invalid_channel");
        return;
    }
    if (!parseBssid(bssidText, bssid)) {
        reportLine("RF_DUT", "status=FAIL|reason=invalid_bssid");
        return;
    }

    if (udpStarted) {
        udp.stop();
        udpStarted = false;
    }
    hostConfigured = false;
    plannedDisconnectActive = false;
    plannedReconnectPending = false;
    plannedReconnectAttemptCount = 0;
    rfMode = true;
    rfHelloAcknowledged = false;
    rfTxActive = false;
    rfRequestedTxPowerValid = false;
    resetRfStats();
    wasConnected = false;

    // The reference AP is fully known by the host. Enter RF mode from a clean
    // STA state and connect directly to its fixed channel/BSSID. No RF scan is
    // needed or wanted here.
    hwtest_wifi_disable_tx_ampdu = true;
    WiFi.disconnect(true, false);
    delay(100);
    WiFi.mode(WIFI_STA);
    if (normalWifiTxPowerConfigured) {
        (void)setRfTxPowerDbm(20);
    }
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(false);
    delay(100);
    if (!configureRfRadioControl()) {
        reportLine(
            "RF_DUT",
            "status=FAIL|reason=rf_radio_control_failed|protocol_err=%ld|protocol_get_err=%ld|protocol_bitmap=%u|ps_err=%ld|ps_get_err=%ld|ps_state=%u|fixed_rate=1M_L|fixed_rate_err=%ld",
            static_cast<long>(rfProtocolError),
            static_cast<long>(rfProtocolGetError),
            static_cast<unsigned>(rfProtocolBitmap),
            static_cast<long>(rfPowerSaveError),
            static_cast<long>(rfPowerSaveGetError),
            static_cast<unsigned>(rfPowerSaveState),
            static_cast<long>(rfFixedRateError));
        stopRfMode(false);
        return;
    }
    reportLine(
        "RF_DUT",
        "status=CONNECTING|mode=pinned|ssid_length=%u|target_channel=%ld|target_bssid=%s|protocol=11b|protocol_ok=1|protocol_bitmap=%u|power_save_off=1|fixed_rate=1M_L|fixed_rate_ok=1",
        static_cast<unsigned>(decodedSsid.length()),
        static_cast<long>(channel),
        bssidText.c_str(),
        static_cast<unsigned>(rfProtocolBitmap));
    WiFi.begin(
        decodedSsid.c_str(),
        decodedPassword.c_str(),
        channel,
        bssid,
        true);
}

static void printRfStats() {
    const double averageRssi = rfRssiSamples > 0
        ? static_cast<double>(rfRssiSum) / static_cast<double>(rfRssiSamples)
        : -127.0;
    int8_t actualQuarterDbm = static_cast<int8_t>(rfTxPowerDbm * 4);
    if (WiFi.status() == WL_CONNECTED && esp_wifi_get_max_tx_power(&actualQuarterDbm) == ESP_OK) {
        rfTxPowerDbm = static_cast<int32_t>(actualQuarterDbm) / 4;
    }

    reportLine(
        "RF_DUT",
        "status=STATS|connected=%u|hello=%u|rx_packets=%u|tx_packets=%u|rssi=%d|rssi_avg=%.2f|rssi_min=%d|rssi_max=%d|rssi_samples=%u|tx_power_dbm=%d|rx_run_id=%u|protocol=11b|protocol_ok=%u|protocol_bitmap=%u|power_save_off=%u|fixed_rate=1M_L|fixed_rate_ok=%u",
        WiFi.status() == WL_CONNECTED ? 1U : 0U,
        rfHelloAcknowledged ? 1U : 0U,
        static_cast<unsigned>(rfRxPackets),
        static_cast<unsigned>(rfTxPackets),
        WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127,
        averageRssi,
        rfRssiSamples ? rfRssiMin : -127,
        rfRssiSamples ? rfRssiMax : -127,
        static_cast<unsigned>(rfRssiSamples),
        rfTxPowerDbm,
        static_cast<unsigned>(rfExpectedRxRunValid ? rfExpectedRxRunId : 0),
        rfProtocolConfigured ? 1U : 0U,
        static_cast<unsigned>(rfProtocolBitmap),
        (rfPowerSaveGetError == ESP_OK && rfPowerSaveState == WIFI_PS_NONE) ? 1U : 0U,
        rfFixedRateConfigured ? 1U : 0U);
}

static void startPlannedReconnect() {
    if (rfMode || !wifiConfigured || WiFi.status() != WL_CONNECTED) {
        reportLine("RECONNECT_TEST", "status=FAIL|reason=not_connected");
        return;
    }

    plannedReconnectTargetBssidText = WiFi.BSSIDstr();
    plannedReconnectTargetChannel = WiFi.channel();
    memset(plannedReconnectTargetBssid, 0, sizeof(plannedReconnectTargetBssid));
    plannedReconnectTargetValid =
        plannedReconnectTargetChannel >= 1
        && plannedReconnectTargetChannel <= 13
        && parseBssid(plannedReconnectTargetBssidText, plannedReconnectTargetBssid);
    if (!plannedReconnectTargetValid) {
        reportLine(
            "RECONNECT_TEST",
            "status=FAIL|reason=target_capture_failed|bssid=%s|channel=%ld",
            plannedReconnectTargetBssidText.c_str(),
            static_cast<long>(plannedReconnectTargetChannel));
        return;
    }

    plannedDisconnectActive = true;
    plannedReconnectPending = true;
    plannedReconnectAtMs = millis() + kPlannedReconnectDelayMs;
    plannedReconnectAttemptCount = 0;
    reportLine(
        "RECONNECT_TEST",
        "status=START|ip=%s|target_bssid=%s|target_channel=%ld",
        WiFi.localIP().toString().c_str(),
        plannedReconnectTargetBssidText.c_str(),
        static_cast<long>(plannedReconnectTargetChannel));

    // The reconnect must test recovery to the exact same AP, not roaming.
    // Disable automatic reconnect so it cannot race the pinned WiFi.begin().
    WiFi.setAutoReconnect(false);
    WiFi.disconnect(false, false);
}

static void startBleCoexistence(uint32_t durationMs) {
#if SOC_BT_SUPPORTED
    if (rfMode || bleCoexRunning) {
        reportLine("BLE_COEX", "status=FAIL|reason=%s", rfMode ? "rf_mode" : "already_running");
        return;
    }

    bleCoexDurationMs = max(static_cast<uint32_t>(1000), min(static_cast<uint32_t>(15000), durationMs));
    bleCoexDone = false;
    bleCoexDevices = -1;
    bleCoexInitFailed = false;
    bleCoexRunning = true;

    const BaseType_t created = xTaskCreate(bleCoexTask, "hwtest_ble_coex", 8192, nullptr, 1, nullptr);
    if (created != pdPASS) {
        bleCoexRunning = false;
        reportLine("BLE_COEX", "status=FAIL|reason=task_create");
        return;
    }

    reportLine("BLE_COEX", "status=START|duration_ms=%u", static_cast<unsigned>(bleCoexDurationMs));
#else
    (void)durationMs;
    reportLine("BLE_COEX", "status=SKIP|reason=unsupported");
#endif
}

static void parseSerialCommand(const String &command) {
    if (command.startsWith("HOST|")) {
        parseHostCommand(command);
    } else if (command.startsWith("WIFI64|")) {
        parseWifiCommand(command);
    } else if (command.startsWith("RF_WIFI64|")) {
        parseRfWifiCommand(command);
    } else if (command == "RF_STOP") {
        stopRfMode(true);
    } else if (command == "RF_RESET") {
        resetRfStats();
        reportLine("RF_DUT", "status=RESET|run_id=0");
    } else if (command.startsWith("RF_RESET|")) {
        const uint32_t runId = static_cast<uint32_t>(command.substring(9).toInt());
        if (runId == 0) {
            reportLine("RF_DUT", "status=RESET_FAIL|reason=invalid_run_id");
        } else {
            resetRfStats(runId, true);
            reportLine("RF_DUT", "status=RESET|run_id=%u", static_cast<unsigned>(runId));
        }
    } else if (command == "RF_ABORT_TX") {
        rfTxActive = false;
        reportLine(
            "RF_DUT",
            "status=TX_ABORTED|requested=%u|sent=%u|tx_power_dbm=%d|run_id=%u",
            static_cast<unsigned>(rfTxTargetCount),
            static_cast<unsigned>(rfTxPackets),
            rfTxPowerDbm,
            static_cast<unsigned>(rfTxRunId));
    } else if (command == "RF_STATS") {
        printRfStats();
    } else if (command.startsWith("RF_TX_POWER|")) {
        if (!rfMode) {
            reportLine("RF_DUT", "status=TX_POWER_FAIL|reason=rf_mode_inactive");
        } else {
            const int32_t requested = command.substring(12).toInt();
            const int32_t actual = setRfTxPowerDbm(requested);
            reportLine("RF_DUT", "status=TX_POWER|requested_dbm=%d|actual_dbm=%d", requested, actual);
        }
    } else if (command.startsWith("RF_TX|")) {
        const int firstSeparator = command.indexOf('|', 6);
        const int secondSeparator = firstSeparator >= 0
            ? command.indexOf('|', firstSeparator + 1)
            : -1;
        if (!rfMode || WiFi.status() != WL_CONNECTED || !rfUdpStarted || !rfHelloAcknowledged) {
            reportLine("RF_DUT", "status=TX_DONE|result=FAIL|reason=reference_not_ready");
        } else if (firstSeparator < 0 || secondSeparator < 0) {
            reportLine("RF_DUT", "status=TX_DONE|result=FAIL|reason=invalid_command");
        } else {
            const long runId = command.substring(6, firstSeparator).toInt();
            const long count = command.substring(firstSeparator + 1, secondSeparator).toInt();
            const long interval = command.substring(secondSeparator + 1).toInt();
            if (runId <= 0 || count <= 0 || count > 10000 || interval < 5 || interval > 1000) {
                reportLine("RF_DUT", "status=TX_DONE|result=FAIL|reason=invalid_range");
            } else {
                rfTxRunId = static_cast<uint32_t>(runId);
                rfTxTargetCount = static_cast<uint32_t>(count);
                rfTxIntervalMs = static_cast<uint32_t>(interval);
                rfTxSequence = 0;
                rfTxPackets = 0;
                rfTxActive = true;
                rfNextTxAtMs = millis();
                reportLine(
                    "RF_DUT",
                    "status=TX_START|count=%ld|interval_ms=%ld|tx_power_dbm=%d|run_id=%ld",
                    count, interval, rfTxPowerDbm, runId);
            }
        }
    } else if (command == "RESET_NET_STATS") {
        disconnectCount = 0;
        reconnectCount = 0;
        disconnectEpisodeActive = false;
        reportLine("NET_STATS", "status=RESET");
    } else if (command == "FORCE_RECONNECT") {
        startPlannedReconnect();
    } else if (command.startsWith("BLE_COEX|")) {
        startBleCoexistence(static_cast<uint32_t>(command.substring(9).toInt()));
    } else if (command.startsWith("HEAP_CHECK_INTERVAL|")) {
        long seconds = command.substring(20).toInt();
        seconds = max(0L, min(600L, seconds));
        heapCheckIntervalMs = static_cast<uint32_t>(seconds) * 1000u;
        lastHeapCheckMs = millis();
        reportLine("HEAP_CHECK", "status=CONFIGURED|interval_s=%ld", seconds);
    } else if (command == "DEEP_SLEEP_TEST") {
        beginDeepSleepTest();
    }
}

static void serviceSerialCommands() {
    while (Serial.available() > 0) {
        const char value = static_cast<char>(Serial.read());
        if (value == '\n' || value == '\r') {
            if (serialCommand.length() > 0) {
                parseSerialCommand(serialCommand);
                serialCommand = "";
            }
        } else if (serialCommand.length() < 256) {
            serialCommand += value;
        }
    }
}

static void serviceRfUdp() {
    if (!rfUdpStarted) {
        return;
    }

    while (true) {
        const int packetSize = rfUdp.parsePacket();
        if (packetSize <= 0) {
            break;
        }
        char buffer[128];
        const int received = rfUdp.read(buffer, sizeof(buffer) - 1);
        if (received <= 0) {
            continue;
        }
        buffer[received] = '\0';
        const String text(buffer);
        if (text.startsWith("RF|HELLO_ACK")) {
            rfHelloAcknowledged = true;
            reportLine("RF_DUT", "status=REFERENCE_READY|rssi=%d", WiFi.RSSI());
        } else if (text.startsWith("RF|DATA|")) {
            const int separator = text.indexOf('|', 8);
            if (separator <= 8) {
                continue;
            }
            const uint32_t runId = static_cast<uint32_t>(text.substring(8, separator).toInt());
            if (rfExpectedRxRunValid && runId != rfExpectedRxRunId) {
                continue;
            }
            ++rfRxPackets;
            if (rfRxPackets % 10u == 0u) {
                sampleRfRssi();
            }
        }
    }
}

static void serviceRfTx() {
    if (!rfTxActive || !rfUdpStarted || !rfHelloAcknowledged || WiFi.status() != WL_CONNECTED) {
        return;
    }

    const uint32_t now = millis();
    if (static_cast<int32_t>(now - rfNextTxAtMs) < 0) {
        return;
    }

    ++rfTxSequence;
    rfUdp.beginPacket(rfReferenceIp, kRfReferenceUdpPort);
    rfUdp.printf(
        "RF|DATA|%lu|%lu",
        static_cast<unsigned long>(rfTxRunId),
        static_cast<unsigned long>(rfTxSequence));
    if (rfUdp.endPacket() == 1) {
        ++rfTxPackets;
    }

    if (rfTxSequence >= rfTxTargetCount) {
        rfTxActive = false;
        reportLine(
            "RF_DUT",
            "status=TX_DONE|result=PASS|requested=%u|sent=%u|tx_power_dbm=%d|run_id=%u",
            static_cast<unsigned>(rfTxTargetCount),
            static_cast<unsigned>(rfTxPackets),
            rfTxPowerDbm,
            static_cast<unsigned>(rfTxRunId));
        return;
    }

    rfNextTxAtMs = now + rfTxIntervalMs;
}

static void serviceUdp() {
    while (true) {
        const int packetSize = udp.parsePacket();
        if (packetSize <= 0) {
            return;
        }

        char buffer[96];
        const IPAddress remoteIp = udp.remoteIP();
        const uint16_t remotePort = udp.remotePort();
        const int received = udp.read(buffer, sizeof(buffer) - 1);
        if (received <= 0) {
            reportLine("UDP_DIAG", "op=PONG|stage=read_failed|packet_size=%d|received=%d", packetSize, received);
            continue;
        }

        buffer[received] = '\0';
        if (strncmp(buffer, "PING|", 5) != 0) {
            continue;
        }

        const int beginResult = udp.beginPacket(remoteIp, remotePort);
        size_t written = 0;
        int endResult = 0;
        if (beginResult == 1) {
            written = udp.printf("PONG|%s|%lu", buffer + 5, static_cast<unsigned long>(millis()));
            endResult = udp.endPacket();
        }

        const bool ok = beginResult == 1 && written > 0 && endResult == 1;
        if (!ok) {
            ++udpPongSendFailures;
        }
        const bool logFailure = !ok && (udpPongSendFailures <= 5 || (udpPongSendFailures % 25U) == 0U);
        if (logNextUdpPong || logFailure) {
            reportLine(
                "UDP_DIAG",
                "op=PONG|status=%s|remote=%s|port=%u|begin=%d|written=%u|end=%d|failures=%u|wifi=%d|rssi=%d|heap_free=%u",
                ok ? "PASS" : "FAIL",
                remoteIp.toString().c_str(),
                static_cast<unsigned>(remotePort),
                beginResult,
                static_cast<unsigned>(written),
                endResult,
                static_cast<unsigned>(udpPongSendFailures),
                static_cast<int>(WiFi.status()),
                WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127,
                static_cast<unsigned>(ESP.getFreeHeap()));
        }
        if (ok) {
            logNextUdpPong = false;
        }
    }
}

static void sendHeartbeat() {
    if (!hostConfigured || WiFi.status() != WL_CONNECTED) {
        return;
    }

    ++heartbeatSequence;
    const int beginResult = udp.beginPacket(hostIp, hostPort);
    size_t written = 0;
    int endResult = 0;
    if (beginResult == 1) {
        written = udp.printf(
            "HB|%lu|%lu|%d|%u|%u",
            static_cast<unsigned long>(heartbeatSequence),
            static_cast<unsigned long>(millis()),
            WiFi.RSSI(),
            static_cast<unsigned>(ESP.getFreeHeap()),
            static_cast<unsigned>(ESP.getMinFreeHeap()));
        endResult = udp.endPacket();
    }

    const bool ok = beginResult == 1 && written > 0 && endResult == 1;
    if (!ok) {
        ++udpHeartbeatSendFailures;
    }
    const bool logFailure = !ok && (udpHeartbeatSendFailures <= 5 || (udpHeartbeatSendFailures % 25U) == 0U);
    if (logNextUdpHeartbeat || logFailure) {
        reportLine(
            "UDP_DIAG",
            "op=HEARTBEAT|status=%s|host=%s|port=%u|begin=%d|written=%u|end=%d|failures=%u|wifi=%d|rssi=%d|heap_free=%u",
            ok ? "PASS" : "FAIL",
            hostIp.toString().c_str(),
            static_cast<unsigned>(hostPort),
            beginResult,
            static_cast<unsigned>(written),
            endResult,
            static_cast<unsigned>(udpHeartbeatSendFailures),
            static_cast<int>(WiFi.status()),
            WiFi.RSSI(),
            static_cast<unsigned>(ESP.getFreeHeap()));
    }
    if (ok) {
        logNextUdpHeartbeat = false;
    }
}

void beginNetworkTest() {
    reportLine("WIFI", "status=WAITING_FOR_CONFIG|sleep=default|autoreconnect=on|rf_quality=ready");
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.onEvent(onWiFiEvent);
}

void serviceNetworkTest() {
    serviceSerialCommands();

#if SOC_BT_SUPPORTED
    if (bleCoexDone) {
        bleCoexDone = false;
        if (bleCoexInitFailed) {
            reportLine("BLE_COEX", "status=FAIL|reason=init_failed|devices=%d|duration_ms=%u", bleCoexDevices, static_cast<unsigned>(bleCoexDurationMs));
        } else {
            reportLine("BLE_COEX", "status=%s|devices=%d|duration_ms=%u", bleCoexDevices >= 0 ? "DONE" : "FAIL", bleCoexDevices, static_cast<unsigned>(bleCoexDurationMs));
        }
    }
#endif

    const bool connected = WiFi.status() == WL_CONNECTED;
    const uint32_t nowMs = millis();

    if (rfMode) {
        serviceRfUdp();
        serviceRfTx();

        if (connected && rfUdpStarted && !rfHelloAcknowledged &&
            static_cast<uint32_t>(nowMs - rfLastHelloMs) >= 1000u) {
            sendRfHello();
        }

        return;
    }

    if (udpStarted) {
        serviceUdp();
    }

    if (!connected && wasConnected) {
        if (udpStarted) {
            udp.stop();
            udpStarted = false;
        }
    }

    if (connected && !wasConnected) {
        if (udpStarted) {
            udp.stop();
        }
        udpStarted = udp.begin(kProbeUdpPort) == 1;
        logNextUdpPong = true;
        logNextUdpHeartbeat = true;

        reportLine("UDP", "status=%s|port=%u", udpStarted ? "READY" : "FAIL", static_cast<unsigned>(kProbeUdpPort));
        reportLine(
            "WIFI",
            "status=CONNECTED|ip=%s|rssi=%d|bssid=%s|channel=%d|reconnects=%u|target_bssid=%s|target_channel=%d|target_rssi=%d",
            WiFi.localIP().toString().c_str(),
            WiFi.RSSI(),
            WiFi.BSSIDstr().c_str(),
            WiFi.channel(),
            static_cast<unsigned>(reconnectCount),
            selectedApValid ? selectedApBssidText.c_str() : "none",
            selectedApChannel,
            selectedApRssi);
    }
    wasConnected = connected;

    if (!connected && plannedReconnectPending && static_cast<int32_t>(nowMs - plannedReconnectAtMs) >= 0) {
        plannedReconnectPending = false;
        plannedReconnectAttemptCount += 1U;
        reportLine(
            "WIFI",
            "status=RECONNECT_ATTEMPT|attempt=%u|disconnects=%u|target_bssid=%s|target_channel=%ld|planned=1|method=pinned_begin",
            static_cast<unsigned>(plannedReconnectAttemptCount),
            static_cast<unsigned>(disconnectCount),
            plannedReconnectTargetValid ? plannedReconnectTargetBssidText.c_str() : "none",
            static_cast<long>(plannedReconnectTargetChannel));

        // Recreate the STA interface, then reconnect to exactly the AP that was
        // connected when the test started. This keeps mesh/multi-AP networks
        // from turning the reconnect test into an accidental roaming test.
        WiFi.mode(WIFI_OFF);
        delay(120);
        WiFi.mode(WIFI_STA);
        if (!applyNormalWifiTxPower()) {
            reportLine("RECONNECT_TEST", "status=FAIL|reason=tx_power_apply_failed");
            return;
        }
        WiFi.setSleep(true);
        WiFi.setAutoReconnect(false);
        if (!plannedReconnectTargetValid) {
            reportLine("RECONNECT_TEST", "status=FAIL|reason=target_missing");
        } else {
            WiFi.begin(
                wifiSsid.c_str(),
                wifiPassword.c_str(),
                plannedReconnectTargetChannel,
                plannedReconnectTargetBssid,
                true);
        }
    }

    if (heapCheckIntervalMs > 0 && millis() - lastHeapCheckMs >= heapCheckIntervalMs) {
        lastHeapCheckMs = millis();
        const bool ok = heap_caps_check_integrity_all(false);
        reportLine("HEAP_CHECK", "status=%s|heap_free=%u|heap_min=%u", ok ? "PASS" : "FAIL", static_cast<unsigned>(ESP.getFreeHeap()), static_cast<unsigned>(ESP.getMinFreeHeap()));
    }

    if (millis() - lastHeartbeatMs >= 1000) {
        lastHeartbeatMs = millis();
        reportLine(
            "HEARTBEAT",
            "seq=%lu|uptime_ms=%lu|wifi=%d|rssi=%d|heap_free=%u|heap_min=%u|disconnects=%u|reconnects=%u",
            static_cast<unsigned long>(heartbeatSequence + 1),
            static_cast<unsigned long>(millis()),
            connected ? 1 : 0,
            connected ? WiFi.RSSI() : -127,
            static_cast<unsigned>(ESP.getFreeHeap()),
            static_cast<unsigned>(ESP.getMinFreeHeap()),
            static_cast<unsigned>(disconnectCount),
            static_cast<unsigned>(reconnectCount));
        sendHeartbeat();
    }
}
