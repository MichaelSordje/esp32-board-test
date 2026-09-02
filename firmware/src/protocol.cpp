#include "protocol.h"

#include <cstdarg>

void reportLine(const char *category, const char *format, ...)
{
    char buffer[384];
    va_list args;

    va_start(args, format);
    vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);

    Serial.printf("HTEST|%s|%s\n", category, buffer);
}
