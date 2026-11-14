#!/usr/bin/env python3
"""
rn2483_bootload.py - Upload an "offset" firmware image to an RN2483/RN2903
module through its UART bootloader.

This is a re-implementation of the protocol used by Microchip's
JavaApplicationOneButton.jar (RN2xx3_LORAWAN_FIRMWARE/.../Bootloader),
without the MPLAB comm library. It only needs Python 3 and pyserial:

    pip install pyserial

Usage:

    python rn2483_bootload.py COM5 RN2483_Parser.production.hex

The tool first asks the application for 'sys get ver'. If it answers, it
sends 'sys eraseFW' to switch the module into the bootloader. If it does not
answer, the module is assumed to be in the bootloader already.

Use the HEX from dist/offset/production (application linked at 0x300).
Do NOT use the "default" or "combined" HEX with this tool.

Protocol (reverse engineered from the jar):
  Request : 55 CMD LEN_L LEN_H UNL0 UNL1 A0 A1 A2 A3 [payload]
            UNL0/UNL1 = 55 AA for write/erase commands, 00 00 otherwise
            A0..A3    = byte address, little endian
  Response: the 10-byte request header echoed back, then the reply data
            (status byte 0x01 = OK for write/erase)
  Commands: 0x00 read version (26-byte reply)
            0x02 write flash, 64 bytes per packet (11-byte reply)
            0x03 erase flash, LEN = number of 64-byte rows (11-byte reply)
            0x08 checksum, LEN = byte count (12-byte reply, LE 16-bit sum)
            0x09 reset (no reply)
"""

import argparse
import sys
import time

SYNC = 0x55

CMD_READ_VERSION = 0x00
CMD_WRITE_FLASH = 0x02
CMD_ERASE_FLASH = 0x03
CMD_WRITE_EEPROM = 0x05
CMD_CHECKSUM = 0x08
CMD_RESET = 0x09

UNLOCK_CMDS = (CMD_WRITE_FLASH, CMD_ERASE_FLASH, CMD_WRITE_EEPROM)

APP_START = 0x0300        # application start (bootloader occupies 0x0000-0x02FF)
FLASH_END = 0x10000       # PIC18LF46K22: 64 KB program memory
ROW_SIZE = 64             # erase row size and write block size
ERASE_CHUNKS = 4          # same split as the original tool

HEADER_LEN = 10


class BootloaderError(Exception):
    pass


# --------------------------------------------------------------------------
# Intel HEX
# --------------------------------------------------------------------------
def load_hex(path):
    """Return {address: byte} for every data byte in an Intel HEX file."""
    data = {}
    upper = 0
    with open(path, "r") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise BootloaderError("%s:%d: not an Intel HEX line" % (path, line_no))
            raw = bytes.fromhex(line[1:])
            if (sum(raw) & 0xFF) != 0:
                raise BootloaderError("%s:%d: bad record checksum" % (path, line_no))
            count, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
            payload = raw[4:4 + count]
            if rtype == 0x00:
                base = upper + addr
                for i, b in enumerate(payload):
                    data[base + i] = b
            elif rtype == 0x01:
                break
            elif rtype == 0x02:
                upper = ((payload[0] << 8) | payload[1]) << 4
            elif rtype == 0x04:
                upper = ((payload[0] << 8) | payload[1]) << 16
            # 0x03 / 0x05 (start address) are irrelevant for a PIC
    return data


def build_image(hexdata, force=False):
    """Program-memory image for APP_START..FLASH_END, unused bytes = 0xFF."""
    image = bytearray(b"\xFF" * FLASH_END)
    below = [a for a in hexdata if a < APP_START]
    if below and not force:
        raise BootloaderError(
            "HEX file contains data below 0x%04X (first at 0x%04X).\n"
            "This looks like a 'default' or 'combined' build. Use the HEX from\n"
            "dist/offset/production, or pass --force if you are sure." % (APP_START, min(below)))
    ignored = 0
    for addr, b in hexdata.items():
        if APP_START <= addr < FLASH_END:
            image[addr] = b
        elif addr >= FLASH_END:
            ignored += 1      # config words (0x300000) / EEPROM (0xF00000)
    return image, ignored


def host_checksum(image):
    """16-bit sum of little-endian words over APP_START..FLASH_END-2 (as the jar does)."""
    total = 0
    for addr in range(APP_START, FLASH_END - 2, 2):
        total += image[addr] | (image[addr + 1] << 8)
    return total & 0xFFFF


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------
def build_packet(cmd, length, address, payload=b""):
    unlock = (0x55, 0xAA) if cmd in UNLOCK_CMDS else (0x00, 0x00)
    header = bytes([
        SYNC, cmd,
        length & 0xFF, (length >> 8) & 0xFF,
        unlock[0], unlock[1],
        address & 0xFF, (address >> 8) & 0xFF, (address >> 16) & 0xFF, (address >> 24) & 0xFF,
    ])
    return header + bytes(payload)


class Bootloader:
    def __init__(self, port, log=print, trace_writes=False, retries=3):
        self.port = port
        self.log = log
        self.trace_writes = trace_writes
        self.retries = retries

    def _transact(self, cmd, length, address, payload=b"", reply_len=0, timeout=1.0):
        packet = build_packet(cmd, length, address, payload)
        trace = cmd != CMD_WRITE_FLASH or self.trace_writes
        last_error = None
        for attempt in range(self.retries):
            self.port.reset_input_buffer()
            self.port.write(packet)
            if trace:
                extra = " (+%d data bytes)" % len(payload) if payload else ""
                retry = "  [retry %d]" % attempt if attempt else ""
                self.log("    TX %s%s%s" % (packet[:HEADER_LEN].hex(" "), extra, retry))
            if reply_len == 0:
                return b""
            reply = self._read_exact(reply_len, timeout)
            if trace or len(reply) < reply_len:
                self.log("    RX %s" % (reply.hex(" ") if reply else "(nothing)"))
            if len(reply) < reply_len:
                last_error = "timeout (got %d of %d bytes)" % (len(reply), reply_len)
                continue
            if reply[0] != SYNC or reply[1] != cmd:
                last_error = "unexpected reply %s" % reply[:HEADER_LEN].hex(" ")
                continue
            return reply
        raise BootloaderError("command 0x%02X at 0x%04X failed: %s" % (cmd, address, last_error))

    def _read_exact(self, n, timeout):
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < n and time.monotonic() < deadline:
            chunk = self.port.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def read_version(self):
        r = self._transact(CMD_READ_VERSION, 0, 0, reply_len=26, timeout=1.0)
        return {
            "bootloader_version": r[10],
            "device_id": r[16] | (r[17] << 8),
            "erase_row": r[20],
            "write_latches": r[21],
            "raw": r,
        }

    def erase(self, start=APP_START, end=FLASH_END):
        rows_total = (end - start) // ROW_SIZE
        per_chunk = -(-rows_total // ERASE_CHUNKS)   # ceil
        addr = start
        while addr < end:
            rows = min(per_chunk, (end - addr) // ROW_SIZE)
            r = self._transact(CMD_ERASE_FLASH, rows, addr, reply_len=11, timeout=10.0)
            if r[-1] != 0x01:
                raise BootloaderError("erase at 0x%04X returned status 0x%02X" % (addr, r[-1]))
            addr += rows * ROW_SIZE

    def write(self, image, start=APP_START, end=FLASH_END, skip_blank=True, progress=None):
        blocks = range(start, end, ROW_SIZE)
        for i, addr in enumerate(blocks):
            block = image[addr:addr + ROW_SIZE]
            if skip_blank and block == b"\xFF" * ROW_SIZE:
                pass   # flash was erased, nothing to write
            else:
                r = self._transact(CMD_WRITE_FLASH, ROW_SIZE, addr, block, reply_len=11, timeout=2.0)
                if r[-1] != 0x01:
                    raise BootloaderError("write at 0x%04X returned status 0x%02X" % (addr, r[-1]))
            if progress:
                progress(i + 1, len(blocks))

    def checksum(self, start=APP_START, end=FLASH_END):
        length = end - start - 2
        r = self._transact(CMD_CHECKSUM, length, start, reply_len=12, timeout=5.0)
        return r[10] | (r[11] << 8)

    def reset(self):
        self._transact(CMD_RESET, 0, 0, reply_len=0)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def progress_bar(done, total):
    width = 40
    filled = int(width * done / total)
    sys.stdout.write("\r  [%s%s] %3d%%" % ("#" * filled, "." * (width - filled), 100 * done // total))
    if done == total:
        sys.stdout.write("\n")
    sys.stdout.flush()


def app_command(port, cmd, wait=1.0):
    """Send a text command to the RN2483 application, return the reply line(s)."""
    port.reset_input_buffer()
    port.write(cmd.encode("ascii") + b"\r\n")
    buf = bytearray()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        chunk = port.read(64)
        if chunk:
            buf.extend(chunk)
            if buf.endswith(b"\r\n"):
                break
    return buf.decode("ascii", "replace").strip()


def probe_application(port):
    """Return the 'sys get ver' string if the application firmware answers, else None."""
    # Flush the module's command buffer: it may hold garbage (e.g. bootloader
    # packets from a previous attempt), which would corrupt the next command.
    app_command(port, "", wait=0.5)
    ver = app_command(port, "sys get ver", wait=1.0)
    if ver and ver.startswith("RN2"):
        return ver
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Upload an offset HEX to an RN2483/RN2903 through its UART bootloader.")
    ap.add_argument("port", help="serial port, e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("hexfile", help="HEX from dist/offset/production")
    ap.add_argument("--baud", type=int, default=57600, help="baud rate (default 57600)")
    ap.add_argument("--no-enter", action="store_true",
                    help="do not talk to the application; assume the module is already in the bootloader")
    ap.add_argument("--no-skip-blank", action="store_true", help="also write all-0xFF blocks")
    ap.add_argument("--no-reset", action="store_true", help="do not reset the module after programming")
    ap.add_argument("--force", action="store_true", help="accept HEX data below 0x0300")
    ap.add_argument("-v", "--verbose", action="store_true", help="also print every write packet")
    ap.add_argument("-q", "--quiet", action="store_true", help="do not print packets")
    args = ap.parse_args(argv)

    log = (lambda *a: None) if args.quiet else print

    try:
        import serial
    except ImportError:
        print("pyserial is missing. Install it with:  python -m pip install pyserial")
        return 2

    print("=== RN2483 bootloader upload ===")
    try:
        hexdata = load_hex(args.hexfile)
        image, ignored = build_image(hexdata, args.force)
        expected = host_checksum(image)
        used = [a for a in hexdata if APP_START <= a < FLASH_END]
        print("HEX file : %s" % args.hexfile)
        print("  program data 0x%04X-0x%04X (%d bytes), checksum 0x%04X"
              % (min(used), max(used), len(used), expected))
        if ignored:
            print("  %d bytes outside program memory (config/EEPROM) will not be written" % ignored)

        port = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1, timeout=0.1)
        print("Port     : %s, %d baud, 8N1" % (args.port, args.baud))
    except (BootloaderError, OSError, ValueError, serial.SerialException) as e:
        print("ERROR: %s" % e)
        return 1

    try:
        # --- Step 1: application or bootloader? ---------------------------------
        if not args.no_enter:
            print("\n[1] Checking for the running application (sys get ver) ...")
            ver = probe_application(port)
            if ver:
                print("    Application answered: %s" % ver)
                print("    Sending 'sys eraseFW' (erases the application start, module resets into the bootloader) ...")
                port.write(b"sys eraseFW\r\n")
                time.sleep(1.5)
                leftover = port.read(256)
                if leftover:
                    log("    (discarded %d bytes after reset: %s)" % (len(leftover), leftover.hex(" ")))
            else:
                print("    No answer from the application: assuming the module is already in the bootloader.")
        else:
            print("\n[1] Skipped application check (--no-enter)")

        bl = Bootloader(port, log=log, trace_writes=args.verbose)

        # --- Step 2: bootloader version -----------------------------------------
        print("\n[2] Reading bootloader version ...")
        v = bl.read_version()
        print("    Bootloader version %d, device ID 0x%04X, erase row %d bytes, write latches %d bytes"
              % (v["bootloader_version"], v["device_id"], v["erase_row"], v["write_latches"]))

        # --- Step 3: erase --------------------------------------------------------
        print("\n[3] Erasing 0x%04X-0x%04X ..." % (APP_START, FLASH_END - 1))
        t0 = time.monotonic()
        bl.erase()
        print("    Erase OK (%.1f s)" % (time.monotonic() - t0))

        # --- Step 4: write ----------------------------------------------------------
        blocks = [a for a in range(APP_START, FLASH_END, ROW_SIZE)
                  if args.no_skip_blank or image[a:a + ROW_SIZE] != b"\xFF" * ROW_SIZE]
        print("\n[4] Writing %d blocks of %d bytes ..." % (len(blocks), ROW_SIZE))
        t0 = time.monotonic()
        bl.write(image, skip_blank=not args.no_skip_blank,
                 progress=None if args.verbose else progress_bar)
        print("    Write OK (%.1f s)" % (time.monotonic() - t0))

        # --- Step 5: checksum ---------------------------------------------------------
        print("\n[5] Verifying checksum ...")
        device = bl.checksum()
        print("    Device 0x%04X, HEX file 0x%04X" % (device, expected))
        if device != expected:
            print("ERROR: checksum mismatch. The module was NOT reset and stays in the bootloader;"
                  " run the tool again with --no-enter.")
            return 1
        print("    Checksum OK")

        # --- Step 6: reset and check ---------------------------------------------------
        if args.no_reset:
            print("\n[6] Not resetting (--no-reset)")
        else:
            print("\n[6] Resetting module ...")
            bl.reset()
            time.sleep(1.5)
            port.read(256)
            ver = probe_application(port)
            print("    New firmware: %s" % (ver if ver else "(no answer to 'sys get ver')"))
        print("\nDone.")
        return 0
    except BootloaderError as e:
        print("\nERROR: %s" % e)
        print("Hints:\n"
              "  - Is the bootloader installed? A module programmed with the 'default' build has none;\n"
              "    program the 'combined' HEX once with a PICkit.\n"
              "  - Check TX/RX wiring and that no other program (PuTTY, Tera Term) has the port open.\n"
              "  - If the module is already in the bootloader, run again with --no-enter.")
        return 1
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
