#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <esp_private/wifi.h>

extern "C" {
bool hwtest_wifi_disable_tx_ampdu = false;
}

static constexpr char kReferenceVersion[] = "1.0.13";
static constexpr char kApPassword[] = "esp32-board-test";
static constexpr uint16_t kReferenceUdpPort = 33334;
static constexpr uint16_t kDutUdpPort = 33335;

static WiFiUDP udp;
static String serialCommand;
static String apSsid;
static String referenceMac;
static bool apRunning = false;
static bool udpRunning = false;
static IPAddress dutIp;
static bool dutKnown = false;

static uint32_t rxPackets = 0;
static uint32_t txPackets = 0;
static int64_t rssiSum = 0;
static uint32_t rssiSamples = 0;
static int32_t rssiMin = 0;
static int32_t rssiMax = -127;
static int32_t currentTxPowerDbm = 20;
static int32_t requestedTxPowerDbm = 20;
static bool requestedTxPowerValid = false;
static uint32_t expectedRxRunId = 0;
static bool expectedRxRunValid = false;

static bool txActive = false;
static uint32_t txTargetCount = 0;
static uint32_t txSequence = 0;
static uint32_t txIntervalMs = 20;
static uint32_t nextTxAtMs = 0;
static uint32_t txRunId = 0;

static bool rfProtocolConfigured = false;
static esp_err_t rfProtocolError = ESP_OK;
static esp_err_t rfProtocolGetError = ESP_OK;
static uint8_t rfProtocolBitmap = 0;
static constexpr wifi_phy_rate_t kRfFixedPhyRate = WIFI_PHY_RATE_1M_L;
static bool rfFixedRateConfigured = false;
static esp_err_t rfFixedRateError = ESP_OK;

static bool configureRfRadio() {
    rfProtocolError = esp_wifi_set_protocol(WIFI_IF_AP, WIFI_PROTOCOL_11B);
    rfProtocolBitmap = 0;
    rfProtocolGetError = esp_wifi_get_protocol(WIFI_IF_AP, &rfProtocolBitmap);
    rfFixedRateError = esp_wifi_internal_set_fix_rate(
        WIFI_IF_AP,
        true,
        kRfFixedPhyRate);
    rfFixedRateConfigured = rfFixedRateError == ESP_OK;
    rfProtocolConfigured = rfProtocolError == ESP_OK && rfProtocolGetError == ESP_OK && rfProtocolBitmap == WIFI_PROTOCOL_11B;
    return rfProtocolConfigured && rfFixedRateConfigured;
}

static void resetRfRadioControl() {
    if (rfFixedRateConfigured) {
        (void)esp_wifi_internal_set_fix_rate(
            WIFI_IF_AP,
            false,
            kRfFixedPhyRate);
    }
    rfProtocolConfigured = false;
    rfProtocolError = ESP_OK;
    rfProtocolGetError = ESP_OK;
    rfProtocolBitmap = 0;
    rfFixedRateConfigured = false;
    rfFixedRateError = ESP_OK;
}

static String macSuffix() {
    String mac = referenceMac.length() > 0 ? referenceMac : WiFi.macAddress();
    mac.replace(":", "");
    mac.toUpperCase();
    if (mac.length() >= 6) {
        return mac.substring(mac.length() - 6);
    }
    return mac;
}

static void resetStats(uint32_t runId = 0, bool useRunFilter = false) {
    rxPackets = 0;
    txPackets = 0;
    rssiSum = 0;
    rssiSamples = 0;
    rssiMin = 0;
    rssiMax = -127;
    expectedRxRunId = runId;
    expectedRxRunValid = useRunFilter;
}

static int32_t stationRssi() {
    wifi_sta_list_t stations = {};
    if (esp_wifi_ap_get_sta_list(&stations) != ESP_OK || stations.num == 0) {
        return -127;
    }
    return static_cast<int32_t>(stations.sta[0].rssi);
}

static void sampleStationRssi() {
    const int32_t rssi = stationRssi();
    if (rssi <= -127 || rssi > 0) {
        return;
    }
    rssiSum += rssi;
    ++rssiSamples;
    if (rssiSamples == 1 || rssi < rssiMin) {
        rssiMin = rssi;
    }
    if (rssiSamples == 1 || rssi > rssiMax) {
        rssiMax = rssi;
    }
}

static int32_t setTxPowerDbm(int32_t requestedDbm) {
    requestedDbm = constrain(requestedDbm, 2, 20);
    if (requestedTxPowerValid && requestedDbm == requestedTxPowerDbm) {
        return currentTxPowerDbm;
    }

    const int8_t quarterDbm = static_cast<int8_t>(requestedDbm * 4);
    if (esp_wifi_set_max_tx_power(quarterDbm) != ESP_OK) {
        return currentTxPowerDbm;
    }

    requestedTxPowerDbm = requestedDbm;
    requestedTxPowerValid = true;

    int8_t actualQuarterDbm = quarterDbm;
    if (esp_wifi_get_max_tx_power(&actualQuarterDbm) == ESP_OK) {
        currentTxPowerDbm = static_cast<int32_t>(actualQuarterDbm) / 4;
    } else {
        currentTxPowerDbm = requestedDbm;
    }
    return currentTxPowerDbm;
}

static void printReady(const char *kind = "READY") {
    Serial.printf(
        "REF|%s|role=reference|version=%s|mac=%s|chip=%s|revision=%u|sdk=%s|arduino=%s|build_date=%s|build_time=%s|ap=%u|ssid=%s|channel=%d|tx_power_dbm=%ld\n",
        kind,
        kReferenceVersion,
        referenceMac.length() > 0 ? referenceMac.c_str() : WiFi.macAddress().c_str(),
        ESP.getChipModel(),
        static_cast<unsigned>(ESP.getChipRevision()),
        ESP.getSdkVersion(),
        ESP_ARDUINO_VERSION_STR,
        __DATE__,
        __TIME__,
        apRunning ? 1U : 0U,
        apRunning ? apSsid.c_str() : "-",
        apRunning ? WiFi.channel() : 0,
        static_cast<long>(currentTxPowerDbm));
}

static void printStats() {
    const int32_t currentRssi = stationRssi();
    int8_t driverQuarterDbm = static_cast<int8_t>(currentTxPowerDbm * 4);
    if (apRunning && esp_wifi_get_max_tx_power(&driverQuarterDbm) == ESP_OK) {
        currentTxPowerDbm = static_cast<int32_t>(driverQuarterDbm) / 4;
    }
    const double averageRssi = rssiSamples > 0 ? static_cast<double>(rssiSum) / static_cast<double>(rssiSamples) : -127.0;
    wifi_sta_list_t stations = {};
    uint16_t stationCount = 0;
    if (apRunning && esp_wifi_ap_get_sta_list(&stations) == ESP_OK) {
        stationCount = stations.num;
    }

    Serial.printf("REF|STATS|station_count=%u|dut_known=%u|station_rssi=%ld|rssi_avg=%.2f|rssi_min=%ld|rssi_max=%ld|rssi_samples=%lu|rx_packets=%lu|tx_packets=%lu|tx_power_dbm=%ld|tx_power_driver_dbm=%ld|rx_run_id=%lu|fixed_rate=1M_L|fixed_rate_ok=%u\n",
                  static_cast<unsigned>(stationCount), dutKnown ? 1U : 0U, static_cast<long>(currentRssi), averageRssi, static_cast<long>(rssiSamples ? rssiMin : -127), static_cast<long>(rssiSamples ? rssiMax : -127),
                  static_cast<unsigned long>(rssiSamples), static_cast<unsigned long>(rxPackets), static_cast<unsigned long>(txPackets), static_cast<long>(currentTxPowerDbm), static_cast<long>(currentTxPowerDbm),
                  static_cast<unsigned long>(expectedRxRunValid ? expectedRxRunId : 0), rfFixedRateConfigured ? 1U : 0U);
}

static void stopAp() {
    txActive = false;
    if (udpRunning) {
        udp.stop();
        udpRunning = false;
    }
    // softAPdisconnect(true) clears the AP config by calling AP.begin() first.
    // Near a broken RF link that re-entry can hit ESP_ERR_INVALID_STATE in
    // esp_netif_start_api. We do not need to clear the RAM-only AP config here;
    // stop the WiFi driver directly instead.
    resetRfRadioControl();
    WiFi.mode(WIFI_OFF);
    delay(100);
    hwtest_wifi_disable_tx_ampdu = false;
    apRunning = false;
    dutKnown = false;
    dutIp = IPAddress();
    resetStats();
    Serial.println("REF|AP|status=STOPPED");
}

static void startAp(int32_t channel, int32_t powerDbm) {
    if (apRunning) {
        stopAp();
        delay(100);
    }

    channel = constrain(channel, 1, 13);
    apSsid = String("ESP32-BOARD-TEST-RF-") + macSuffix();

    WiFi.persistent(false);
    hwtest_wifi_disable_tx_ampdu = true;
    WiFi.mode(WIFI_AP);
    const bool ok = WiFi.softAP(apSsid.c_str(), kApPassword, channel, false, 1);
    if (!ok) {
        WiFi.mode(WIFI_OFF);
        hwtest_wifi_disable_tx_ampdu = false;
        Serial.println("REF|AP|status=FAIL|reason=softap_start_failed");
        return;
    }

    apRunning = true;
    requestedTxPowerValid = false;
    if (!configureRfRadio()) {
        Serial.printf("REF|AP|status=FAIL|reason=rf_radio_control_failed|protocol_err=%ld|protocol_get_err=%ld|protocol_bitmap=%u|fixed_rate=1M_L|fixed_rate_err=%ld\n", static_cast<long>(rfProtocolError), static_cast<long>(rfProtocolGetError),
                      static_cast<unsigned>(rfProtocolBitmap), static_cast<long>(rfFixedRateError));
        stopAp();
        return;
    }
    currentTxPowerDbm = setTxPowerDbm(powerDbm);
    udpRunning = udp.begin(kReferenceUdpPort) == 1;
    dutKnown = false;
    resetStats();

    if (!udpRunning) {
        stopAp();
        Serial.println("REF|AP|status=FAIL|reason=udp_bind_failed");
        return;
    }

    const String apBssid = WiFi.softAPmacAddress();
    Serial.printf("REF|AP|status=STARTED|ssid=%s|password=%s|ip=%s|channel=%ld|bssid=%s|udp_port=%u|dut_port=%u|tx_power_dbm=%ld|protocol=11b|protocol_ok=%u|protocol_bitmap=%u|fixed_rate=1M_L|fixed_rate_ok=%u\n", apSsid.c_str(), kApPassword, WiFi.softAPIP().toString().c_str(),
                  static_cast<long>(channel), apBssid.c_str(), static_cast<unsigned>(kReferenceUdpPort), static_cast<unsigned>(kDutUdpPort), static_cast<long>(currentTxPowerDbm), rfProtocolConfigured ? 1U : 0U, static_cast<unsigned>(rfProtocolBitmap), rfFixedRateConfigured ? 1U : 0U);
}

static void serviceUdp() {
    if (!udpRunning) {
        return;
    }

    while (true) {
        const int packetSize = udp.parsePacket();
        if (packetSize <= 0) {
            break;
        }

        char buffer[128];
        const int received = udp.read(buffer, sizeof(buffer) - 1);
        if (received <= 0) {
            continue;
        }
        buffer[received] = '\0';
        const String text(buffer);
        const IPAddress remoteIp = udp.remoteIP();

        if (text.startsWith("RF|HELLO|")) {
            dutIp = remoteIp;
            dutKnown = true;
            udp.beginPacket(dutIp, kDutUdpPort);
            udp.print("RF|HELLO_ACK");
            udp.endPacket();
            Serial.printf("REF|DUT|status=HELLO|ip=%s|rssi=%ld\n", dutIp.toString().c_str(), static_cast<long>(stationRssi()));
            continue;
        }

        if (text.startsWith("RF|DATA|")) {
            const int separator = text.indexOf('|', 8);
            if (separator <= 8) {
                continue;
            }
            const uint32_t runId = static_cast<uint32_t>(text.substring(8, separator).toInt());
            if (expectedRxRunValid && runId != expectedRxRunId) {
                continue;
            }
            dutIp = remoteIp;
            dutKnown = true;
            ++rxPackets;
            if (rxPackets % 10u == 0u) {
                sampleStationRssi();
            }
        }
    }
}

static void serviceTx() {
    if (!txActive || !dutKnown || !udpRunning) {
        return;
    }

    const uint32_t now = millis();
    if (static_cast<int32_t>(now - nextTxAtMs) < 0) {
        return;
    }

    ++txSequence;
    udp.beginPacket(dutIp, kDutUdpPort);
    udp.printf("RF|DATA|%lu|%lu", static_cast<unsigned long>(txRunId), static_cast<unsigned long>(txSequence));
    const bool ok = udp.endPacket() == 1;
    if (ok) {
        ++txPackets;
    }

    if (txSequence >= txTargetCount) {
        txActive = false;
        Serial.printf("REF|TX_DONE|requested=%lu|sent=%lu|tx_power_dbm=%ld|run_id=%lu\n", static_cast<unsigned long>(txTargetCount), static_cast<unsigned long>(txPackets), static_cast<long>(currentTxPowerDbm),
                      static_cast<unsigned long>(txRunId));
        return;
    }

    nextTxAtMs = now + txIntervalMs;
}

static void handleCommand(const String &command) {
    if (command == "INFO") {
        printReady("INFO");
        return;
    }
    if (command == "RESTART") {
        Serial.printf("REF|RESTART|status=OK|version=%s\n", kReferenceVersion);
        Serial.flush();
        delay(50);
        ESP.restart();
        return;
    }
    if (command == "STATS") {
        printStats();
        return;
    }
    if (command == "RESET_STATS") {
        resetStats();
        Serial.println("REF|RESET|status=OK|run_id=0");
        return;
    }
    if (command.startsWith("RESET_STATS|")) {
        const uint32_t runId = static_cast<uint32_t>(command.substring(12).toInt());
        if (runId == 0) {
            Serial.println("REF|RESET|status=FAIL|reason=invalid_run_id");
            return;
        }
        resetStats(runId, true);
        Serial.printf("REF|RESET|status=OK|run_id=%lu\n", static_cast<unsigned long>(runId));
        return;
    }
    if (command == "ABORT_TX") {
        txActive = false;
        Serial.printf("REF|TX_ABORT|status=OK|requested=%lu|sent=%lu|tx_power_dbm=%ld|run_id=%lu\n", static_cast<unsigned long>(txTargetCount), static_cast<unsigned long>(txPackets), static_cast<long>(currentTxPowerDbm),
                      static_cast<unsigned long>(txRunId));
        return;
    }
    if (command == "AP_STOP") {
        stopAp();
        return;
    }
    if (command.startsWith("AP_START|")) {
        const int separator = command.indexOf('|', 9);
        if (separator < 0) {
            Serial.println("REF|AP|status=FAIL|reason=invalid_command");
            return;
        }
        const int32_t channel = command.substring(9, separator).toInt();
        const int32_t powerDbm = command.substring(separator + 1).toInt();
        startAp(channel, powerDbm);
        return;
    }
    if (command.startsWith("SET_TX_POWER|")) {
        if (!apRunning) {
            Serial.println("REF|TX_POWER|status=FAIL|reason=ap_not_running");
            return;
        }
        const int32_t requested = command.substring(13).toInt();
        const int32_t actual = setTxPowerDbm(requested);
        Serial.printf("REF|TX_POWER|status=OK|requested_dbm=%ld|actual_dbm=%ld\n", static_cast<long>(requested), static_cast<long>(actual));
        return;
    }
    if (command.startsWith("TX|")) {
        if (!apRunning || !udpRunning || !dutKnown) {
            Serial.println("REF|TX_DONE|status=FAIL|reason=dut_not_ready");
            return;
        }
        const int firstSeparator = command.indexOf('|', 3);
        if (firstSeparator < 0) {
            Serial.println("REF|TX_DONE|status=FAIL|reason=invalid_command");
            return;
        }
        const int secondSeparator = command.indexOf('|', firstSeparator + 1);
        const bool hasRunId = secondSeparator >= 0;
        const long runId = hasRunId ? command.substring(3, firstSeparator).toInt() : 0;
        const long count = hasRunId ? command.substring(firstSeparator + 1, secondSeparator).toInt() : command.substring(3, firstSeparator).toInt();
        const long interval = hasRunId ? command.substring(secondSeparator + 1).toInt() : command.substring(firstSeparator + 1).toInt();
        if ((hasRunId && runId <= 0) || count <= 0 || count > 10000 || interval < 5 || interval > 1000) {
            Serial.println("REF|TX_DONE|status=FAIL|reason=invalid_range");
            return;
        }
        txRunId = static_cast<uint32_t>(runId);
        txTargetCount = static_cast<uint32_t>(count);
        txIntervalMs = static_cast<uint32_t>(interval);
        txSequence = 0;
        txPackets = 0;
        txActive = true;
        nextTxAtMs = millis();
        Serial.printf("REF|TX|status=START|count=%ld|interval_ms=%ld|tx_power_dbm=%ld|run_id=%ld\n", count, interval, static_cast<long>(currentTxPowerDbm), runId);
        return;
    }

    Serial.println("REF|ERROR|reason=unknown_command");
}

static void serviceSerial() {
    while (Serial.available() > 0) {
        const char value = static_cast<char>(Serial.read());
        if (value == '\n' || value == '\r') {
            if (serialCommand.length() > 0) {
                handleCommand(serialCommand);
                serialCommand = "";
            }
        } else if (serialCommand.length() < 192) {
            serialCommand += value;
        }
    }
}

void setup() {
    Serial.begin(115200);
    delay(150);
    WiFi.persistent(false);
    hwtest_wifi_disable_tx_ampdu = false;
    WiFi.mode(WIFI_STA);
    delay(20);
    referenceMac = WiFi.macAddress();
    WiFi.mode(WIFI_OFF);
    printReady();
}

void loop() {
    serviceSerial();
    serviceUdp();
    serviceTx();
    delay(1);
}
