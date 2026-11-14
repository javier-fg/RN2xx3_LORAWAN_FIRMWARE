# RN2483 EU firmware 1.0.6 XC1

Modified version of Microchip's **RN2483 EU 1.0.6 RC4** LoRaWAN firmware (from [MicrochipTech/RN2xx3_LORAWAN_FIRMWARE](https://github.com/MicrochipTech/RN2xx3_LORAWAN_FIRMWARE)).

Goals of this version:

- Build with current Microchip tools (MPLAB X IDE 6.20 and XC8 v4.00, free mode)
- Support the **62.5 kHz** LoRa bandwidth used by MeshCore
- Make the RN2483 usable as a plain LoRa radio for MeshCore / Meshtastic experiments
- Provide Python tools for firmware upload and radio testing

The LoRaWAN functionality is kept, but it has not been re-tested against a network server after the port.

`sys get ver` reports the version as `RN2483 1.0.6 XC1.xx <build date>`.

## Folder structure

| Folder / file | Contents |
|---|---|
| `Source/RN2483_Parser` | MPLAB X project (application firmware) |
| `Bootloader` | Microchip bootloader project (MPASM) and the original Java upload tool |
| `Bootloader_Python` | `rn2483_bootload.py`: firmware upload through the UART bootloader (replaces the Java tool) |
| `Tools_Python` | `rn2483_rx.py`: MeshCore / Meshtastic receiver, transmit test and frequency calibration |
| `Binary/offset`, `Binary/combined` | Built images, copied automatically after each build |
| `bugfix_since_1.0.5.txt`, `known_issues.txt`, `release_notes_public.txt` | Original Microchip release notes |

## Changes compared to RC4

### 1. Port to XC8 v4.00 (C99)

XC8 v4 no longer has the old C90 front end, so the legacy XC8 syntax had to be adapted.

| Change | Where |
|---|---|
| C standard set to **C99** | Project properties, all configurations |
| Legacy keywords mapped with macros: `interrupt=__interrupt()`, `reentrant=__reentrant` | Project properties, preprocessor macros |
| Removed `-fshort-double` / `-fshort-float` (no longer supported; float and double are now 32-bit instead of 24-bit) | `nbproject/configurations.xml` |
| New `parser/legacy_compat.c/.h`: `itoa`, `utoa`, `ultoa`, `xtoi`, `stricmp`, which are missing from the C99 library | Added to the project |
| `offset` and `combined` configurations switched from XC8 v1.33 to v4.00 (bootloader settings kept: code offset 0x300, checksum 0x0300-0xFFFD at 0xFFFE, fill 0xFFFF) | Project properties |

`legacy_compat.c` implements the legacy argument order (`itoa(buffer, value, base)`). Its `stricmp` deliberately does not use `tolower()` from `<ctype.h>`: in the XC8 C99 library that is a macro that evaluates its argument twice, which broke every string comparison with an even number of characters (`lora`, `sf10`, `62.5`, ...).

### 2. Interrupt handlers (critical fix)

The MCC drivers registered interrupt callbacks through `void*` parameters (`INT1_SetInterruptHandler(void* InterruptHandler)` and similar). XC8 v4 treats these as data pointers, and the callbacks were never reached. Symptoms: no `radio_tx_ok`, no received packets, the radio stayed `busy` after the first transmission, and no software timer ever expired.

All registration functions now take a real function pointer, `void (* InterruptHandler)(void)`:

- `ext_int.c/.h`: `INT0/1/2_SetInterruptHandler` (radio DIO0, DIO1, DIO5)
- `pin_manager.c/.h`: `IOCB4/5_SetInterruptHandler` (radio DIO2, DIO4)
- `tmr1.c/.h`: `TMR1_SetInterruptHandler` (software timers)

### 3. 62.5 kHz LoRa bandwidth

- `radio_driver_SX1276.h`: new `BW_62_5KHZ = 6` (SX1276 bandwidth code)
- `parser_radio.c`: `radio set bw` / `radio get bw` accept and report `62.5`
- `radio_driver_SX1276.c`:
  - frequency hopping period calculation for 62.5 kHz
  - LowDataRateOptimize enabled for SF10 to SF12 at 62.5 kHz (symbol time > 16 ms)
  - SX1276 errata register settings applied as for 125/250 kHz

The LoRaWAN stack itself never selects 62.5 kHz.

### 4. Frequency correction (`radio set fcorr`)

The SX1276 has no crystal trim, and the module tested was about 23 ppm (20 kHz) low, which is more than a 62.5 kHz receiver tolerates. A software correction was added:

| Command | Description |
|---|---|
| `radio set fcorr <ppb>` | Correction in parts per billion, -100000 to +100000. Stored in EEPROM (0x2F0-0x2F3) and loaded at startup |
| `radio get fcorr` | Returns the stored value |

The correction is applied to every frequency written to the radio (LoRa, FSK, LoRaWAN, receive and transmit). `radio get freq` still reports the requested frequency. `sys factoryRESET` erases the correction.

Calculation: `ppb = -(measured error in Hz) / (frequency in MHz) * 1000`. Example: transmitting 20 kHz low at 869.618 MHz gives `radio set fcorr 23000`. See `Tools_Python` for the measurement procedure.

### 5. `mac pause` fix

`LORAWAN_Pause()` returned an uninitialized value (often 0, meaning "not paused") when the MAC was not in the idle state on a device that is neither joined nor joining. That happens after a `radio tx` sent without pausing. LoRaWAN then treated the next radio transmission as its own uplink, opened its receive windows and switched the radio to the RX2 settings (869.525 MHz, SF12, 125 kHz, IQ inverted, CRC off).

Now, when the device is not joined and not joining, `mac pause` cancels pending receive windows, returns the MAC to idle and always grants the pause (`4294967245`).

### 6. Build and project housekeeping

- **Post-build copy:** the `combined` and `offset` configurations copy their HEX to `../../Binary/<configuration>/` after each build:
  `${MKDIR} ../../Binary/${ConfName} && ${CP} ${ImagePath} ../../Binary/${ConfName}/`
- **Bootloader as HEX loadable:** the bootloader project is written for MPASM, which is not part of MPLAB X 6.x. The `combined` configuration therefore uses a prebuilt bootloader HEX as loadable instead of rebuilding `LoRaBtCs.X`. To change the bootloader, build it with MPLAB X 5.35 (last version with MPASM).
- **`.gitignore`:** build output, per-user IDE files and MPLAB-generated makefiles are no longer tracked.

### Modified and added source files

| File | Change |
|---|---|
| `main.c` | Load frequency correction at startup |
| `parser/legacy_compat.c/.h` | New: legacy library functions |
| `parser/parser_radio.c/.h` | 62.5 kHz, `fcorr` commands |
| `parser/parser_commands.c` | `radio set/get fcorr` registered |
| `parser/parser.c` | Version string |
| `mcc_generated_files/ext_int.c/.h`, `pin_manager.c/.h`, `tmr1.c/.h` | Typed interrupt handler pointers |
| `mcc_generated_files/LoRaWAN/radio_driver_SX1276.c/.h` | 62.5 kHz, frequency correction |
| `mcc_generated_files/LoRaWAN/lorawan.c` | `mac pause` fix |
| `nbproject/configurations.xml` | Toolchain, C99, macros, post-build step, loadable |

## Building

Requirements:

- **MPLAB X IDE 6.20**. This is the last version that supports the PICkit 3; 6.25 and later do not.
- **XC8 v4.00** (free mode is sufficient; the image uses about 83% of program memory)
- PIC18F-K_DFP device pack (installed with MPLAB X)

Open `Source/RN2483_Parser` in MPLAB X, select the configuration and run **Clean and Build** (not the debug build).

| Configuration | Image | Use |
|---|---|---|
| `default` | Application at 0x0000, no bootloader | Programming with a PICkit; removes the bootloader |
| `offset` | Application at 0x0300 | Upload through the bootloader (`rn2483_bootload.py`) |
| `combined` | Bootloader + application | Programming with a PICkit while keeping (or restoring) the bootloader |

Avoid building the project inside a folder synchronized by Nextcloud or similar: file locking caused `unable to redirect stdout` errors from the compiler.

## Programming

**With a PICkit 3** (ICSP: MCLR, VDD, GND, PGD, PGC; the module is 3.3 V only):

- MPLAB X: select the PICkit 3 as tool, then **Make and Program Device**, or
- MPLAB IPE: device `PIC18LF46K22`, select the HEX, **Program**.

If MPLAB X reports `Connection Failed`, the PICkit 3 may be in the standalone programmer mode: use **PICkit 3 Programmer Application v3.10 > Tools > Revert to MPLAB mode**.

**Through the bootloader** (module must contain the bootloader, i.e. was programmed with `combined` or with Microchip firmware):

```
python Bootloader_Python/rn2483_bootload.py COM51 Binary/offset/RN2483_Parser.production.hex
```

The tool switches a running module into the bootloader with `sys eraseFW`, programs, verifies the checksum and resets. See `Bootloader_Python/README.md`.

## Testing

```
python Tools_Python/rn2483_rx.py COM51 meshcore
python Tools_Python/rn2483_rx.py COM51 meshtastic --sf 11
python Tools_Python/rn2483_rx.py COM51 meshcore --txtest
```

Receives and decodes MeshCore / Meshtastic packets, sends test packets and helps with the frequency calibration. See `Tools_Python/README_rn2483_rx.md`.

Serial settings for manual use: 57600 baud, 8N1, no flow control, commands terminated with CR+LF.

## Known limitations

- LoRaWAN operation (join, uplink, downlink) has not been re-verified after the compiler port.
- `radio cw on` uses undocumented SX1276 test registers; the carrier is not exactly on the set frequency, and the radio may need a power cycle afterwards.
- The frequency correction compensates the crystal at the temperature where it was measured; the crystal still drifts a few ppm with temperature.
- `float`/`double` are 32-bit now (24-bit in the original build); the LoRaWAN stack does not depend on this as far as known.
