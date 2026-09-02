#include "self_tests.h"

#include <Arduino.h>
#include <Preferences.h>
#include <esp_attr.h>
#include <esp_chip_info.h>
#include <esp_heap_caps.h>
#include <esp_partition.h>
#include <esp_random.h>
#include <esp_sleep.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <soc/soc_caps.h>

#include "protocol.h"

#if __has_include("test_config.generated.h")
#include "test_config.generated.h"
#endif

#ifndef HWTEST_RUN_RAM
#define HWTEST_RUN_RAM 1
#endif
#ifndef HWTEST_RUN_PSRAM
#define HWTEST_RUN_PSRAM 1
#endif
#ifndef HWTEST_RUN_FLASH
#define HWTEST_RUN_FLASH 1
#endif
#ifndef HWTEST_RUN_CPU
#define HWTEST_RUN_CPU 1
#endif
#ifndef HWTEST_RUN_TIMER
#define HWTEST_RUN_TIMER 1
#endif
#ifndef HWTEST_RUN_RNG
#define HWTEST_RUN_RNG 1
#endif
#ifndef HWTEST_RUN_BLE
#define HWTEST_RUN_BLE 1
#endif
#ifndef HWTEST_RUN_NVS
#define HWTEST_RUN_NVS 1
#endif

#if SOC_BT_SUPPORTED && HWTEST_RUN_BLE
#include <BLEDevice.h>
#include <BLEScan.h>
#endif

static constexpr uint32_t kDeepSleepMagic = 0x48575453u; // "HWTS"
RTC_DATA_ATTR static uint32_t deepSleepMagic = 0;

struct MemoryBenchmark {
    float writeMbps = 0.0f;
    float readMbps = 0.0f;
    uint32_t checksum = 0;
};

static int64_t positiveDuration(int64_t value) {
    return value > 0 ? value : 1;
}

static bool verifyBytePattern(uint8_t *buffer, size_t size, uint8_t value) {
    memset(buffer, value, size);
    for (size_t index = 0; index < size; ++index) {
        if (buffer[index] != value) {
            return false;
        }
    }
    return true;
}

static bool verifyWordPattern(uint8_t *buffer, size_t size) {
    uint32_t *words = reinterpret_cast<uint32_t *>(buffer);
    const size_t count = size / sizeof(uint32_t);

    for (size_t index = 0; index < count; ++index) {
        words[index] = 0xA5A50000u ^ static_cast<uint32_t>(index);
    }

    for (size_t index = 0; index < count; ++index) {
        const uint32_t expected = 0xA5A50000u ^ static_cast<uint32_t>(index);
        if (words[index] != expected) {
            return false;
        }
    }
    return true;
}

static MemoryBenchmark benchmarkMemory(uint8_t *buffer, size_t size) {
    MemoryBenchmark result;
    if (buffer == nullptr || size < sizeof(uint32_t)) {
        return result;
    }

    const size_t targetBytes = 4u * 1024u * 1024u;
    size_t repeats = targetBytes / size;
    repeats = max(static_cast<size_t>(1), min(static_cast<size_t>(32), repeats));

    const int64_t writeStarted = esp_timer_get_time();
    for (size_t iteration = 0; iteration < repeats; ++iteration) {
        memset(buffer, static_cast<int>((iteration * 37u) & 0xFFu), size);
    }
    const int64_t writeUs = positiveDuration(esp_timer_get_time() - writeStarted);

    volatile uint32_t checksum = 0;
    const uint32_t *words = reinterpret_cast<const uint32_t *>(buffer);
    const size_t wordCount = size / sizeof(uint32_t);
    const int64_t readStarted = esp_timer_get_time();
    for (size_t iteration = 0; iteration < repeats; ++iteration) {
        uint32_t local = static_cast<uint32_t>(iteration);
        for (size_t index = 0; index < wordCount; ++index) {
            local ^= words[index] + static_cast<uint32_t>(index * 2654435761u);
        }
        checksum ^= local;
    }
    const int64_t readUs = positiveDuration(esp_timer_get_time() - readStarted);

    const double totalBytes = static_cast<double>(size) * static_cast<double>(repeats);
    result.writeMbps = static_cast<float>((totalBytes * 1000000.0) / (static_cast<double>(writeUs) * 1024.0 * 1024.0));
    result.readMbps = static_cast<float>((totalBytes * 1000000.0) / (static_cast<double>(readUs) * 1024.0 * 1024.0));
    result.checksum = checksum;
    return result;
}

static bool runRamTest() {
    reportLine("TEST", "name=RAM|status=START");

    const size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    size_t testSize = largest > 32768 ? largest - 16384 : largest / 2;
    testSize = min(testSize, static_cast<size_t>(131072));
    testSize &= ~static_cast<size_t>(3);

    uint8_t *buffer = static_cast<uint8_t *>(heap_caps_malloc(testSize, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (buffer == nullptr || testSize < 4096) {
        if (buffer != nullptr) {
            heap_caps_free(buffer);
        }
        reportLine("TEST", "name=RAM|status=FAIL|reason=allocation|bytes=%u", static_cast<unsigned>(testSize));
        return false;
    }

    bool ok = verifyBytePattern(buffer, testSize, 0x00);
    ok = ok && verifyBytePattern(buffer, testSize, 0xFF);
    ok = ok && verifyBytePattern(buffer, testSize, 0xAA);
    ok = ok && verifyBytePattern(buffer, testSize, 0x55);
    ok = ok && verifyWordPattern(buffer, testSize);

    const MemoryBenchmark benchmark = benchmarkMemory(buffer, testSize);
    heap_caps_free(buffer);
    ok = ok && heap_caps_check_integrity_all(true);

    reportLine("TEST", "name=RAM|status=%s|bytes=%u|write_mb_s=%.2f|read_mb_s=%.2f|checksum=%08X", ok ? "PASS" : "FAIL", static_cast<unsigned>(testSize), benchmark.writeMbps, benchmark.readMbps, static_cast<unsigned>(benchmark.checksum));
    return ok;
}

static bool runPsramTest() {
    const size_t psramSize = ESP.getPsramSize();
    if (psramSize == 0) {
        reportLine("TEST", "name=PSRAM|status=SKIP|reason=not_present");
        return true;
    }

    reportLine("TEST", "name=PSRAM|status=START|total_bytes=%u", static_cast<unsigned>(psramSize));

    const size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    size_t testSize = min(largest, static_cast<size_t>(2 * 1024 * 1024));
    testSize &= ~static_cast<size_t>(3);

    uint8_t *buffer = static_cast<uint8_t *>(heap_caps_malloc(testSize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buffer == nullptr || testSize < 65536) {
        if (buffer != nullptr) {
            heap_caps_free(buffer);
        }
        reportLine("TEST", "name=PSRAM|status=FAIL|reason=allocation|bytes=%u", static_cast<unsigned>(testSize));
        return false;
    }

    bool ok = verifyBytePattern(buffer, testSize, 0x00);
    ok = ok && verifyBytePattern(buffer, testSize, 0xFF);
    ok = ok && verifyBytePattern(buffer, testSize, 0xAA);
    ok = ok && verifyBytePattern(buffer, testSize, 0x55);
    ok = ok && verifyWordPattern(buffer, testSize);

    const MemoryBenchmark benchmark = benchmarkMemory(buffer, testSize);
    heap_caps_free(buffer);
    ok = ok && heap_caps_check_integrity_all(true);

    reportLine("TEST", "name=PSRAM|status=%s|bytes=%u|write_mb_s=%.2f|read_mb_s=%.2f|checksum=%08X", ok ? "PASS" : "FAIL", static_cast<unsigned>(testSize), benchmark.writeMbps, benchmark.readMbps, static_cast<unsigned>(benchmark.checksum));
    return ok;
}

static uint8_t flashPattern(size_t absoluteIndex) {
    uint32_t value = static_cast<uint32_t>(absoluteIndex) * 2654435761u;
    value ^= value >> 13;
    value *= 2246822519u;
    value ^= value >> 16;
    return static_cast<uint8_t>(value & 0xFFu);
}

static bool runFlashTest() {
    reportLine("TEST", "name=FLASH|status=START");

    const esp_partition_t *partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "hwtest");
    if (partition == nullptr) {
        reportLine("TEST", "name=FLASH|status=FAIL|reason=partition_missing");
        return false;
    }

    constexpr size_t chunkSize = 4096;
    uint8_t *writeBuffer = static_cast<uint8_t *>(heap_caps_malloc(chunkSize, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    uint8_t *readBuffer = static_cast<uint8_t *>(heap_caps_malloc(chunkSize, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (writeBuffer == nullptr || readBuffer == nullptr) {
        if (writeBuffer != nullptr) {
            heap_caps_free(writeBuffer);
        }
        if (readBuffer != nullptr) {
            heap_caps_free(readBuffer);
        }
        reportLine("TEST", "name=FLASH|status=FAIL|reason=buffer_allocation");
        return false;
    }

    const int64_t eraseStarted = esp_timer_get_time();
    esp_err_t result = esp_partition_erase_range(partition, 0, partition->size);
    const int64_t eraseUs = positiveDuration(esp_timer_get_time() - eraseStarted);
    if (result != ESP_OK) {
        reportLine("TEST", "name=FLASH|status=FAIL|reason=erase|error=%d", static_cast<int>(result));
        heap_caps_free(writeBuffer);
        heap_caps_free(readBuffer);
        return false;
    }

    int64_t writeUs = 0;
    int64_t readUs = 0;

    for (size_t offset = 0; offset < static_cast<size_t>(partition->size); offset += chunkSize) {
        const size_t remaining = static_cast<size_t>(partition->size) - offset;
        const size_t length = min(chunkSize, remaining);

        for (size_t index = 0; index < length; ++index) {
            writeBuffer[index] = flashPattern(offset + index);
        }

        const int64_t writeStarted = esp_timer_get_time();
        result = esp_partition_write(partition, offset, writeBuffer, length);
        writeUs += positiveDuration(esp_timer_get_time() - writeStarted);
        if (result != ESP_OK) {
            reportLine("TEST", "name=FLASH|status=FAIL|reason=write|offset=%u|error=%d", static_cast<unsigned>(offset), static_cast<int>(result));
            heap_caps_free(writeBuffer);
            heap_caps_free(readBuffer);
            return false;
        }

        const int64_t readStarted = esp_timer_get_time();
        result = esp_partition_read(partition, offset, readBuffer, length);
        readUs += positiveDuration(esp_timer_get_time() - readStarted);
        if (result != ESP_OK || memcmp(writeBuffer, readBuffer, length) != 0) {
            reportLine("TEST", "name=FLASH|status=FAIL|reason=verify|offset=%u|error=%d", static_cast<unsigned>(offset), static_cast<int>(result));
            heap_caps_free(writeBuffer);
            heap_caps_free(readBuffer);
            return false;
        }
    }

    heap_caps_free(writeBuffer);
    heap_caps_free(readBuffer);

    const double bytes = static_cast<double>(partition->size);
    const float writeMbps = static_cast<float>((bytes * 1000000.0) / (static_cast<double>((writeUs > 0 ? writeUs : 1)) * 1024.0 * 1024.0));
    const float readMbps = static_cast<float>((bytes * 1000000.0) / (static_cast<double>((readUs > 0 ? readUs : 1)) * 1024.0 * 1024.0));

    reportLine("TEST", "name=FLASH|status=PASS|bytes=%u|erase_ms=%.2f|write_mb_s=%.2f|read_mb_s=%.2f", static_cast<unsigned>(partition->size), static_cast<double>(eraseUs) / 1000.0, writeMbps, readMbps);
    return true;
}

static uint32_t runCpuKernel(uint32_t seed) {
    uint32_t value = seed;
    for (uint32_t index = 0; index < 1000000u; ++index) {
        value ^= value << 13;
        value ^= value >> 17;
        value ^= value << 5;
    }
    return value;
}

struct CoreTestContext {
    volatile bool done = false;
    uint32_t result = 0;
    uint32_t elapsedUs = 0;
    int observedCore = -1;
};

static void coreTestTask(void *parameter) {
    CoreTestContext *context = static_cast<CoreTestContext *>(parameter);
    const int64_t started = esp_timer_get_time();
    context->result = runCpuKernel(0x12345678u);
    context->elapsedUs = static_cast<uint32_t>(positiveDuration(esp_timer_get_time() - started));
    context->observedCore = xPortGetCoreID();
    context->done = true;
    vTaskDelete(nullptr);
}

static bool testOneCore(int core, CoreTestContext &context) {
    TaskHandle_t handle = nullptr;
    const BaseType_t created = xTaskCreatePinnedToCore(coreTestTask, "hwtest_cpu", 4096, &context, 2, &handle, core);

    if (created != pdPASS) {
        return false;
    }

    const uint32_t started = millis();
    while (!context.done && millis() - started < 5000) {
        delay(1);
    }

    return context.done && context.observedCore == core && context.result == 0x33C657F5u;
}

static bool runCpuTest() {
    reportLine("TEST", "name=CPU|status=START");

    const int64_t started = esp_timer_get_time();
    const uint32_t value = runCpuKernel(0x12345678u);
    const uint32_t elapsedUs = static_cast<uint32_t>(positiveDuration(esp_timer_get_time() - started));
    const bool kernelOk = value == 0x33C657F5u;
    const float iterationsPerMs = 1000000000.0f / static_cast<float>(elapsedUs);

    reportLine("TEST", "name=CPU|status=%s|result=%08X|elapsed_ms=%.3f|iterations_per_ms=%.2f", kernelOk ? "PASS" : "FAIL", static_cast<unsigned>(value), static_cast<double>(elapsedUs) / 1000.0, iterationsPerMs);

    if (!kernelOk) {
        reportLine("TEST", "name=CPU_CORES|status=FAIL|reason=cpu_kernel");
        return false;
    }

    esp_chip_info_t chipInfo = {};
    esp_chip_info(&chipInfo);
    const int cores = chipInfo.cores > 0 ? static_cast<int>(chipInfo.cores) : 1;

    CoreTestContext core0;
    CoreTestContext core1;
    bool core0Ok = testOneCore(0, core0);
    bool core1Ok = true;

    if (cores > 1) {
        core1Ok = testOneCore(1, core1);
    }

    const bool coresOk = core0Ok && core1Ok;
    reportLine("TEST", "name=CPU_CORES|status=%s|cores=%d|core0_ms=%.3f|core1_ms=%.3f|core0_observed=%d|core1_observed=%d", coresOk ? "PASS" : "FAIL", cores, static_cast<double>(core0.elapsedUs) / 1000.0,
               cores > 1 ? static_cast<double>(core1.elapsedUs) / 1000.0 : 0.0, core0.observedCore, cores > 1 ? core1.observedCore : -1);
    return coresOk;
}

static bool runTimerTest() {
    reportLine("TEST", "name=TIMER|status=START");

    const int64_t started = esp_timer_get_time();
    delay(250);
    const int64_t elapsed = esp_timer_get_time() - started;
    const bool ok = elapsed >= 240000 && elapsed <= 300000;

    reportLine("TEST", "name=TIMER|status=%s|elapsed_us=%lld", ok ? "PASS" : "FAIL", static_cast<long long>(elapsed));
    return ok;
}

static bool runRngTest() {
    reportLine("TEST", "name=RNG|status=START");

    uint32_t first = esp_random();
    uint32_t previous = first;
    uint32_t different = 0;
    uint32_t ones = __builtin_popcount(first);

    for (uint32_t index = 1; index < 1024; ++index) {
        const uint32_t value = esp_random();
        if (value != previous) {
            ++different;
        }
        previous = value;
        ones += __builtin_popcount(value);
    }

    const float oneRatio = static_cast<float>(ones) / (1024.0f * 32.0f);
    const bool ok = different > 1000 && oneRatio > 0.35f && oneRatio < 0.65f;

    reportLine("TEST", "name=RNG|status=%s|different=%u|one_ratio=%.4f", ok ? "PASS" : "FAIL", static_cast<unsigned>(different), oneRatio);
    return ok;
}

static bool runNvsTest() {
    reportLine("TEST", "name=NVS|status=START");

    Preferences preferences;
    if (!preferences.begin("hwtest", false)) {
        reportLine("TEST", "name=NVS|status=FAIL|reason=open");
        return false;
    }

    uint8_t source[256];
    uint8_t target[256];
    for (size_t index = 0; index < sizeof(source); ++index) {
        source[index] = static_cast<uint8_t>((index * 73u + 19u) & 0xFFu);
    }
    memset(target, 0, sizeof(target));

    preferences.clear();
    const size_t written = preferences.putBytes("blob", source, sizeof(source));
    const uint32_t magicWritten = preferences.putUInt("magic", 0xC0DEC0DEu);
    const size_t read = preferences.getBytes("blob", target, sizeof(target));
    const uint32_t magicRead = preferences.getUInt("magic", 0u);

    const bool ok = written == sizeof(source) && magicWritten == sizeof(uint32_t) && read == sizeof(target) && magicRead == 0xC0DEC0DEu && memcmp(source, target, sizeof(source)) == 0;

    preferences.clear();
    preferences.end();

    reportLine("TEST", "name=NVS|status=%s|bytes=%u", ok ? "PASS" : "FAIL", static_cast<unsigned>(sizeof(source)));
    return ok;
}

#if HWTEST_RUN_BLE
static bool runBleTest() {
#if SOC_BT_SUPPORTED
    reportLine("TEST", "name=BLE|status=START");

    BLEDevice::init("ESP32-Hardware-Test");
    if (!BLEDevice::getInitialized()) {
        reportLine("TEST", "name=BLE|status=FAIL|reason=init_failed");
        return false;
    }

    BLEScan *scanner = BLEDevice::getScan();
    if (scanner == nullptr) {
        BLEDevice::deinit(false);
        reportLine("TEST", "name=BLE|status=FAIL|reason=scanner_unavailable");
        return false;
    }

    scanner->setActiveScan(true);
    scanner->setInterval(100);
    scanner->setWindow(90);
    BLEScanResults *results = scanner->start(3, false);
    const int count = results == nullptr ? -1 : results->getCount();
    scanner->clearResults();

    // Keep the controller memory available because BLE is initialized again
    // later during the WiFi + BLE coexistence test in the same boot.
    BLEDevice::deinit(false);

    const bool ok = count >= 0;
    reportLine("TEST", "name=BLE|status=%s|devices=%d", ok ? "PASS" : "FAIL", count);
    return ok;
#else
    reportLine("TEST", "name=BLE|status=SKIP|reason=unsupported");
    return true;
#endif
}
#endif

bool reportDeepSleepWakeResult() {
    if (deepSleepMagic != kDeepSleepMagic) {
        return false;
    }

    const esp_reset_reason_t reason = esp_reset_reason();
    const bool ok = reason == ESP_RST_DEEPSLEEP;
    deepSleepMagic = 0;

    reportLine("TEST", "name=DEEP_SLEEP|status=%s|reset_reason=%d", ok ? "PASS" : "FAIL", static_cast<int>(reason));
    reportLine("DEEP_SLEEP", "status=WAKE|result=%s|reset_reason=%d", ok ? "PASS" : "FAIL", static_cast<int>(reason));

    // Only skip the destructive self-tests after a verified deep-sleep wake.
    // A stale RTC marker after another reset must not suppress normal testing.
    return ok;
}

void beginDeepSleepTest() {
    deepSleepMagic = kDeepSleepMagic;
    const esp_err_t result = esp_sleep_enable_timer_wakeup(1000000ULL);
    if (result != ESP_OK) {
        deepSleepMagic = 0;
        reportLine("TEST", "name=DEEP_SLEEP|status=FAIL|reason=timer_config|error=%d", static_cast<int>(result));
        reportLine("DEEP_SLEEP", "status=FAIL|reason=timer_config|error=%d", static_cast<int>(result));
        return;
    }

    reportLine("DEEP_SLEEP", "status=START|sleep_ms=1000");
    Serial.flush();
    delay(50);
    esp_deep_sleep_start();

    deepSleepMagic = 0;
    reportLine("TEST", "name=DEEP_SLEEP|status=FAIL|reason=returned");
}

bool runSelfTests() {
    bool ok = true;

#if HWTEST_RUN_RAM
    ok = runRamTest() && ok;
#else
    reportLine("TEST", "name=RAM|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_PSRAM
    ok = runPsramTest() && ok;
#else
    reportLine("TEST", "name=PSRAM|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_FLASH
    ok = runFlashTest() && ok;
#else
    reportLine("TEST", "name=FLASH|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_CPU
    ok = runCpuTest() && ok;
#else
    reportLine("TEST", "name=CPU|status=SKIP|reason=disabled_by_config");
    reportLine("TEST", "name=CPU_CORES|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_TIMER
    ok = runTimerTest() && ok;
#else
    reportLine("TEST", "name=TIMER|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_RNG
    ok = runRngTest() && ok;
#else
    reportLine("TEST", "name=RNG|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_NVS
    ok = runNvsTest() && ok;
#else
    reportLine("TEST", "name=NVS|status=SKIP|reason=disabled_by_config");
#endif

#if HWTEST_RUN_BLE
    ok = runBleTest() && ok;
#else
    reportLine("TEST", "name=BLE|status=SKIP|reason=disabled_by_config");
#endif

    reportLine("SELFTEST", "status=%s", ok ? "PASS" : "FAIL");
    return ok;
}
