#include <Arduino.h>

#include "network_test.h"
#include "protocol.h"
#include "self_tests.h"
#include "system_info.h"
#include "version.h"

static uint32_t bootId = 0;

void setup()
{
    Serial.begin(115200);
    delay(1200);

    bootId = esp_random();
    reportLine(
        "BOOT",
        "id=%08X|version=%s",
        static_cast<unsigned>(bootId),
        HWTEST_VERSION);

    // Store the project/firmware version in the normal SYSTEM data as well.
    // The host already persists SYSTEM values into summary.json.
    reportLine("SYSTEM", "firmware_version=%s", HWTEST_VERSION);

    // If the host requested the controlled deep-sleep test before the reset,
    // report its result before continuing with the network phases.
    const bool resumedAfterDeepSleepTest = reportDeepSleepWakeResult();

    reportSystemInfo();
    bool selfTestsOk = true;
    if (!resumedAfterDeepSleepTest)
    {
        selfTestsOk = runSelfTests();
    }
    else
    {
        // The host keeps the first boot's self-test results. Re-running the
        // destructive flash test here would add wear without adding coverage.
        reportLine("SELFTEST", "status=RESUMED_AFTER_DEEP_SLEEP");
    }

    reportLine("READY",
               "selftests=%s|deep_sleep_resume=%u",
               selfTestsOk ? "PASS" : "FAIL",
               resumedAfterDeepSleepTest ? 1U : 0U);

    beginNetworkTest();
}

void loop()
{
    serviceNetworkTest();
    delay(2);
}
