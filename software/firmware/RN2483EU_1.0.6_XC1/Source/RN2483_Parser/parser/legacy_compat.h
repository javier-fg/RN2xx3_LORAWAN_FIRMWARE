/*
 * legacy_compat.h
 *
 * Replacements for non-standard string/number helpers that were part of the
 * legacy XC8 (C90) library but are missing from the XC8 v2+/v3+/v4 C99 libc:
 *   itoa, utoa, ultoa, xtoi, stricmp
 *
 * Signatures follow the legacy XC8 convention: the destination BUFFER COMES
 * FIRST, e.g. itoa(buf, value, base). This is different from the common
 * desktop itoa(value, buf, base).
 */
#ifndef LEGACY_COMPAT_H
#define LEGACY_COMPAT_H

char *itoa(char *buf, int val, int base);
char *utoa(char *buf, unsigned int val, int base);
char *ultoa(char *buf, unsigned long val, int base);
unsigned int xtoi(const char *s);
int stricmp(const char *s1, const char *s2);

#endif /* LEGACY_COMPAT_H */
