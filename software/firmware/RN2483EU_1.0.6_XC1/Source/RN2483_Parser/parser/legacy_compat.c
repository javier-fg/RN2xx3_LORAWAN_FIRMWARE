/*
 * legacy_compat.c
 *
 * Implementations of legacy XC8 library helpers removed from the C99 libc.
 * See legacy_compat.h.
 */
#include <stdint.h>
#include <ctype.h>
#include "legacy_compat.h"

/* Convert an unsigned long to text in the given base (2..16).
 * Hex digits are upper case. */
static char *ul_to_str(char *buf, unsigned long val, int base, uint8_t negative)
{
    char tmp[33];               /* enough for 32 binary digits */
    uint8_t i = 0;
    char *p = buf;

    if ((base < 2) || (base > 16))
    {
        base = 10;
    }

    do
    {
        uint8_t d = (uint8_t)(val % (unsigned long)base);
        tmp[i++] = (char)((d < 10U) ? ('0' + d) : ('A' + d - 10U));
        val /= (unsigned long)base;
    } while (val != 0UL);

    if (negative)
    {
        *p++ = '-';
    }

    while (i > 0U)
    {
        *p++ = tmp[--i];
    }

    *p = '\0';
    return buf;
}

char *itoa(char *buf, int val, int base)
{
    /* Only base 10 is treated as signed, as in the legacy library;
     * other bases print the raw unsigned bit pattern. */
    if ((base == 10) && (val < 0))
    {
        return ul_to_str(buf, (unsigned long)(-(long)val), base, 1U);
    }
    return ul_to_str(buf, (unsigned long)(unsigned int)val, base, 0U);
}

char *utoa(char *buf, unsigned int val, int base)
{
    return ul_to_str(buf, (unsigned long)val, base, 0U);
}

char *ultoa(char *buf, unsigned long val, int base)
{
    return ul_to_str(buf, val, base, 0U);
}

/* Parse a hexadecimal string (no 0x prefix required). Leading white space
 * is skipped; parsing stops at the first non-hex character. */
unsigned int xtoi(const char *s)
{
    unsigned int result = 0U;
    char c;

    while (isspace((unsigned char)*s))
    {
        s++;
    }

    for (;;)
    {
        c = *s++;
        if ((c >= '0') && (c <= '9'))
        {
            result = (result << 4) | (unsigned int)(c - '0');
        }
        else if ((c >= 'a') && (c <= 'f'))
        {
            result = (result << 4) | (unsigned int)(c - 'a' + 10);
        }
        else if ((c >= 'A') && (c <= 'F'))
        {
            result = (result << 4) | (unsigned int)(c - 'A' + 10);
        }
        else
        {
            break;
        }
    }
    return result;
}

/* Case-insensitive string compare. */
int stricmp(const char *s1, const char *s2)
{
    unsigned char c1;
    unsigned char c2;

    do
    {
        c1 = (unsigned char)tolower((unsigned char)*s1++);
        c2 = (unsigned char)tolower((unsigned char)*s2++);
        if (c1 != c2)
        {
            return (int)c1 - (int)c2;
        }
    } while (c1 != 0U);

    return 0;
}
