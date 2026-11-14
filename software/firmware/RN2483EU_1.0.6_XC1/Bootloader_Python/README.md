# rn2483_bootload.py

Command-line tool to upload firmware to an RN2483 / RN2903 LoRa module through its UART bootloader.

It replaces Microchip's `JavaApplicationOneButton.jar`, which depends on the MPLAB comm library and fails to find serial ports on current Java versions. The protocol was reverse engineered from that jar, and the packets the script sends are byte-identical to the ones the jar builds.

## Requirements

- Python 3
- pyserial: `pip install pyserial`
- A USB-to-serial adapter with **3.3 V** logic levels (TX to module RX, RX to module TX, GND)
- The Microchip bootloader must be present on the module (see below)

## Usage

```
python rn2483_bootload.py <PORT> <HEX> [options]
```

```
python rn2483_bootload.py COM5 dist/offset/production/RN2483_Parser.production.hex
```

The tool first sends `sys get ver`. If the application answers, it prints the current version and sends `sys eraseFW`, which switches the module into the bootloader. If nothing answers, the module is assumed to be in the bootloader already (for example after an interrupted update).

By default the tool prints every bootloader command it sends (`TX`) and the reply (`RX`), except the individual write blocks. After the reset it asks for `sys get ver` again to show the new version.

### Options

| Option | Description |
|---|---|
| `--no-enter` | Skip the `sys get ver` / `sys eraseFW` step and talk to the bootloader directly. |
| `--baud N` | Baud rate, default 57600. The bootloader auto-detects the baud rate from the first byte of each packet. |
| `--no-skip-blank` | Also write 64-byte blocks that are all 0xFF (normally skipped, the flash is already erased). |
| `--no-reset` | Do not reset the module after programming. |
| `--force` | Accept a HEX that contains data below 0x0300. Only use this if you know what you are doing. |
| `-v`, `--verbose` | Also print every write packet. |
| `-q`, `--quiet` | Do not print packets. |

## Which HEX file to use

The project has three build configurations:

| Configuration | Contents | Starts at | Programmed with |
|---|---|---|---|
| `default` | Application only, no bootloader | 0x0000 | PICkit |
| `combined` | Bootloader + application | 0x0000 + 0x0300 | PICkit |
| `offset` | Application only | 0x0300 | **This tool** |

Only use the **offset** HEX with this tool. The script refuses files with data below 0x0300 to protect the bootloader.

If the module was programmed with the `default` configuration, the bootloader is gone. Program the `combined` HEX once with a PICkit to restore it.

## What the tool does

1. Asks the application for `sys get ver` and sends `sys eraseFW` (skipped with `--no-enter`)
2. Reads the bootloader version (confirms the bootloader responds)
3. Erases program memory 0x0300 to 0xFFFF
4. Writes the HEX in 64-byte blocks
5. Asks the module for a checksum and compares it with the one calculated from the HEX
6. Resets the module (only if the checksum matches) and reads the new `sys get ver`

EEPROM and configuration bits are **not** written, so data stored in EEPROM (LoRaWAN keys, settings) is kept.

## Troubleshooting

- **No reply to the version request (`RX (nothing)`):** the bootloader is not responding. Check that it is installed (a module programmed with the `default` build has none), the TX/RX wiring, and that no terminal program has the port open.
- **Checksum mismatch:** run the tool again. The module stays in the bootloader and will not start the incomplete application.
- **Module does not start after a successful upload:** check that the HEX was built with the `offset` configuration (code offset 0x300, checksum at 0xFFFE).

## Protocol reference

Request: `55 CMD LEN_L LEN_H UNL0 UNL1 A0 A1 A2 A3 [payload]`

- `UNL0 UNL1` = `55 AA` for write/erase commands, `00 00` otherwise
- `A0..A3` = byte address, little endian

Response: the 10-byte request header echoed back, followed by the reply data. For write/erase, the reply is one status byte (`01` = OK).

| CMD | Function | LEN | Reply length |
|---|---|---|---|
| `00` | Read version | 0 | 26 bytes |
| `02` | Write flash | 64 (bytes) | 11 bytes |
| `03` | Erase flash | number of 64-byte rows | 11 bytes |
| `08` | Checksum | byte count | 12 bytes (16-bit sum, little endian) |
| `09` | Reset | 0 | none |

The checksum is the 16-bit sum of little-endian words from 0x0300 to 0xFFFD.
