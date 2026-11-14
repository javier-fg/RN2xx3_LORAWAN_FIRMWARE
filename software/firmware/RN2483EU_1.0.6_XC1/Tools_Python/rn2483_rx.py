#!/usr/bin/env python3
"""
rn2483_rx.py - Use an RN2483 as a LoRa sniffer for MeshCore or Meshtastic.

Configures the radio through the RN2483 text commands (mac pause, radio set ...),
then receives continuously and prints every packet with RSSI/SNR and a decode
of the MeshCore or Meshtastic header.

    pip install pyserial
    pip install cryptography        (optional: decrypts public/default channels)

    python rn2483_rx.py COM51 meshcore
    python rn2483_rx.py COM51 meshtastic
    python rn2483_rx.py COM51 meshtastic --sf 11      (EU LongFast)

Any radio parameter of the preset can be overridden on the command line.
Stop with Ctrl+C.
"""

import argparse
import base64
import datetime
import hashlib
import struct
import sys
import time

# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------
PRESETS = {
    # MeshCore EU/UK (Narrow): 869.618 MHz, BW 62.5 kHz, SF8, CR 4/8, private sync word
    "meshcore": dict(freq=869618000, bw="62.5", sf=8, cr="4/8", sync="12", prlen=16),
    # Meshtastic EU_868 on 869.525 MHz, BW 250 kHz. LongFast is SF11, MediumSlow SF10.
    "meshtastic": dict(freq=869525000, bw="250", sf=10, cr="4/5", sync="2B", prlen=16),
}

# --------------------------------------------------------------------------
# Optional AES (for the well-known default/public channel keys only)
# --------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

# MeshCore "Public" channel key (published default)
MESHCORE_PUBLIC_KEY = base64.b64decode("izOH6cXN6mrJ5e26oRXNcg==")
# Meshtastic default channel key ("AQ==" expands to this)
MESHTASTIC_DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
MESHTASTIC_PRESET_NAMES = ["LongFast", "LongSlow", "LongModerate", "LongTurbo", "MediumSlow",
                           "MediumFast", "ShortSlow", "ShortFast", "ShortTurbo", "VeryLongSlow"]


def _xor(data):
    r = 0
    for b in data:
        r ^= b
    return r


MESHTASTIC_HASHES = {_xor(n.encode()) ^ _xor(MESHTASTIC_DEFAULT_KEY): n for n in MESHTASTIC_PRESET_NAMES}


# --------------------------------------------------------------------------
# RN2483 text interface
# --------------------------------------------------------------------------
class RN2483:
    def __init__(self, port, echo=True):
        self.port = port
        self.echo = echo

    def read_line(self, timeout=None):
        """Read one reply line. timeout=None waits forever (Ctrl+C still works)."""
        buf = bytearray()
        deadline = None if timeout is None else time.monotonic() + timeout
        while deadline is None or time.monotonic() < deadline:
            c = self.port.read(1)
            if not c:
                continue
            buf += c
            if buf.endswith(b"\r\n"):
                return buf[:-2].decode("ascii", "replace")
        return None

    def cmd(self, text, timeout=1.0):
        self.port.reset_input_buffer()
        self.port.write(text.encode("ascii") + b"\r\n")
        reply = self.read_line(timeout)
        if self.echo:
            print("  > %-28s < %s" % (text, reply if reply is not None else "(no reply)"))
        return reply

    def flush(self):
        """Clear anything left in the module's command buffer."""
        self.port.write(b"\r\n")
        time.sleep(0.2)
        self.port.reset_input_buffer()


# --------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------
MC_ROUTE = {0: "TRANSPORT_FLOOD", 1: "FLOOD", 2: "DIRECT", 3: "TRANSPORT_DIRECT"}
MC_PAYLOAD = {0x0: "REQ", 0x1: "RESPONSE", 0x2: "TXT_MSG", 0x3: "ACK", 0x4: "ADVERT",
              0x5: "GRP_TXT", 0x6: "GRP_DATA", 0x7: "ANON_REQ", 0x8: "PATH", 0x9: "TRACE",
              0xA: "MULTIPART", 0xB: "CONTROL", 0xF: "RAW_CUSTOM"}
MC_NODE = {1: "chat", 2: "repeater", 3: "room server", 4: "sensor"}


def decode_meshcore(p):
    out = []
    if len(p) < 2:
        return ["  (too short for MeshCore)"]
    hdr = p[0]
    route, ptype, ver = hdr & 0x03, (hdr >> 2) & 0x0F, hdr >> 6
    i = 1
    out.append("  MeshCore  route=%s  type=%s  ver=%d"
               % (MC_ROUTE[route], MC_PAYLOAD.get(ptype, "0x%X" % ptype), ver))
    if route in (0, 3):
        if len(p) < i + 4:
            return out + ["  (truncated transport codes)"]
        tc1, tc2 = struct.unpack_from("<HH", p, i)
        out.append("  transport codes 0x%04X 0x%04X" % (tc1, tc2))
        i += 4
    if len(p) <= i:
        return out + ["  (truncated before path length)"]
    pl = p[i]
    i += 1
    hash_size, hops = ((pl >> 6) & 0x03) + 1, pl & 0x3F
    path = p[i:i + hops * hash_size]
    i += hops * hash_size
    hops_txt = " ".join(path[k:k + hash_size].hex() for k in range(0, len(path), hash_size))
    out.append("  path: %d hop(s)%s" % (hops, (" [" + hops_txt + "]") if hops else ""))
    payload = p[i:]
    out.append("  payload %d bytes" % len(payload))

    if ptype == 0x4 and len(payload) >= 101:              # ADVERT
        pub = payload[0:32]
        ts = struct.unpack_from("<I", payload, 32)[0]
        app = payload[100:]
        line = "  ADVERT  key %s..  time %s" % (pub[:4].hex(), _fmt_ts(ts))
        if app:
            flags = app[0]
            j = 1
            line += "  node=%s" % MC_NODE.get(flags & 0x0F, "0x%X" % (flags & 0x0F))
            if flags & 0x10 and len(app) >= j + 8:
                lat, lon = struct.unpack_from("<ii", app, j)
                line += "  pos=%.5f,%.5f" % (lat / 1e6, lon / 1e6)
                j += 8
            if flags & 0x20:
                j += 2
            if flags & 0x40:
                j += 2
            if flags & 0x80:
                line += "  name='%s'" % app[j:].decode("utf-8", "replace")
        out.append(line)
    elif ptype == 0x5 and len(payload) >= 3:              # GRP_TXT
        ch = payload[0]
        public_hash = hashlib.sha256(MESHCORE_PUBLIC_KEY).digest()[0]
        out.append("  GRP_TXT channel hash 0x%02X%s" % (ch, " (Public)" if ch == public_hash else ""))
        if ch == public_hash and HAVE_CRYPTO:
            ct = payload[3:]
            ct = ct[:len(ct) - len(ct) % 16]
            if ct:
                dec = Cipher(algorithms.AES(MESHCORE_PUBLIC_KEY), modes.ECB()).decryptor()
                pt = dec.update(ct) + dec.finalize()
                ts = struct.unpack_from("<I", pt, 0)[0]
                msg = pt[5:].rstrip(b"\x00").decode("utf-8", "replace")
                out.append("  [%s] %s" % (_fmt_ts(ts), msg))
    return out


def _fmt_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(ts)


MT_PORTS = {1: "TEXT_MESSAGE", 3: "POSITION", 4: "NODEINFO", 5: "ROUTING", 6: "ADMIN",
            8: "WAYPOINT", 34: "PAXCOUNTER", 64: "SERIAL", 65: "STORE_FORWARD", 66: "RANGE_TEST",
            67: "TELEMETRY", 70: "TRACEROUTE", 71: "NEIGHBORINFO", 73: "MAP_REPORT"}


def _varint(b, i):
    v = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return v, i
    raise ValueError("truncated varint")


def _parse_data(pb):
    """Minimal parse of the meshtastic Data protobuf: field 1 portnum, field 2 payload."""
    port, payload, i = None, b"", 0
    while i < len(pb):
        key, i = _varint(pb, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(pb, i)
            if field == 1:
                port = v
        elif wt == 2:
            ln, i = _varint(pb, i)
            if i + ln > len(pb):
                raise ValueError("truncated field")
            if field == 2:
                payload = pb[i:i + ln]
            i += ln
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            raise ValueError("bad wire type")
    if port is None:
        raise ValueError("no portnum")
    return port, payload


def decode_meshtastic(p):
    if len(p) < 16:
        return ["  (too short for Meshtastic)"]
    to, frm, pid = struct.unpack_from("<III", p, 0)
    flags, chash, nxt, relay = p[12], p[13], p[14], p[15]
    hop_limit, want_ack, via_mqtt, hop_start = flags & 7, (flags >> 3) & 1, (flags >> 4) & 1, flags >> 5
    out = ["  Meshtastic  from !%08x  to %s  id 0x%08x"
           % (frm, "broadcast" if to == 0xFFFFFFFF else "!%08x" % to, pid),
           "  hops %d/%d  want_ack=%d  mqtt=%d  channel hash 0x%02X%s  next_hop 0x%02X  relay 0x%02X"
           % (hop_start - hop_limit, hop_start, want_ack, via_mqtt, chash,
              (" (%s, default key)" % MESHTASTIC_HASHES[chash]) if chash in MESHTASTIC_HASHES else "",
              nxt, relay)]
    enc = p[16:]
    out.append("  encrypted payload %d bytes" % len(enc))
    if chash in MESHTASTIC_HASHES and HAVE_CRYPTO and enc:
        nonce = struct.pack("<QI", pid, frm) + b"\x00" * 4
        dec = Cipher(algorithms.AES(MESHTASTIC_DEFAULT_KEY), modes.CTR(nonce)).decryptor()
        pt = dec.update(enc) + dec.finalize()
        try:
            port, data = _parse_data(pt)
            name = MT_PORTS.get(port, str(port))
            if port == 1:
                out.append("  %s: %s" % (name, data.decode("utf-8", "replace")))
            else:
                out.append("  %s (%d bytes): %s" % (name, len(data), data.hex()))
        except ValueError:
            out.append("  (decrypted, but not a valid Data message)")
    return out


# --------------------------------------------------------------------------
# Calibration helpers
# --------------------------------------------------------------------------
def print_packet(rn, data, count, decoder, raw):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    snr = rn.cmd("radio get snr")
    rssi = rn.cmd("radio get rssi")
    print("[%s] #%d  %d bytes  RSSI %s dBm  SNR %s dB" % (now, count, len(data), rssi, snr))
    print("  " + data.hex(" "))
    if not raw:
        try:
            for text in decoder(data):
                print(text)
        except Exception as e:
            print("  (decode error: %s)" % e)
    print()
    return rssi, snr


def carrier_mode(rn, cfg):
    """Unmodulated carrier on the configured frequency: shows the SDR's own offset."""
    print("\nTransmitting an unmodulated carrier on %.6f MHz at low power." % (cfg["freq"] / 1e6))
    print("In SDR++, the difference between this line and %.6f MHz is your SDR's offset." % (cfg["freq"] / 1e6))
    rn.cmd("radio set pwr 2")
    if rn.cmd("radio cw on") != "ok":
        print("ERROR: 'radio cw on' rejected")
        return 1
    try:
        input("Press Enter to stop the carrier ...")
    except KeyboardInterrupt:
        pass
    rn.cmd("radio cw off", timeout=0.3)     # the RN2483 resets itself to stop CW
    print("Carrier off (module was reset).")
    return 0


def txtest_mode(rn, cfg):
    """Send real LoRa packets so their occupied band can be measured on an SDR."""
    bw_khz = float(cfg["bw"])
    print("\nSending a 64-byte LoRa packet every 2 s on %.6f MHz at low power." % (cfg["freq"] / 1e6))
    print("On the SDR the packet should occupy %.4f-%.4f MHz. Its real center minus %.6f MHz"
          % ((cfg["freq"] / 1e3 - bw_khz / 2) / 1e3, (cfg["freq"] / 1e3 + bw_khz / 2) / 1e3, cfg["freq"] / 1e6))
    print("is the RN2483's frequency error. Ctrl+C to stop.\n")
    rn.cmd("radio set pwr 2")
    # A transmit watchdog: if the module never reports the end of a transmission,
    # it answers radio_err after 5 s instead of staying 'busy' forever.
    rn.cmd("radio set wdt 5000")
    payload = bytes(range(64)).hex()
    n = 0
    rn.echo = False
    try:
        while True:
            n += 1
            t0 = time.monotonic()
            reply = rn.cmd("radio tx %s" % payload)
            if reply == "ok":
                result = rn.read_line(7)
                print("    packet %d: ok -> %s after %.2f s"
                      % (n, result if result else "(nothing)", time.monotonic() - t0))
                if result == "radio_err":
                    print("      The transmit-done interrupt was not seen (watchdog expired).")
            else:
                print("    packet %d: 'radio tx' answered %s" % (n, reply))
            # Read the radio settings back after every packet to catch anything
            # that changes them behind our back
            now = {k: rn.cmd("radio get %s" % k) for k in ("freq", "bw", "sf", "cr", "iqi", "crc", "sync")}
            print("      settings: " + "  ".join("%s=%s" % kv for kv in now.items()))
            if n == 1:
                first = now
            elif now != first:
                print("      !!! settings changed since packet 1: "
                      + ", ".join("%s %s->%s" % (k, first[k], now[k]) for k in now if now[k] != first[k]))
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped.")
        rn.cmd("mac resume")
    return 0


def scan_mode(rn, cfg, span_khz, dwell, decoder, raw):
    """Step the receive frequency to find a transmitter with an unknown offset."""
    center = cfg["freq"]
    offsets = list(range(-span_khz, span_khz + 1, 10))
    results = {}
    rn.echo = False
    print("\nScanning %.3f MHz +/- %d kHz in 10 kHz steps, %d s each (%d s total)."
          % (center / 1e6, span_khz, dwell, dwell * len(offsets)))
    print("Keep sending adverts from the node during the scan.\n")
    count = 0
    for off in offsets:
        freq = center + off * 1000
        rn.cmd("radio set freq %d" % freq)
        rn.cmd("radio set wdt %d" % (dwell * 1000))
        ok = errs = 0
        print("--- %+3d kHz (%.3f MHz) ---" % (off, freq / 1e6))
        deadline = time.monotonic() + dwell
        while time.monotonic() < deadline:
            if rn.cmd("radio rx 0") != "ok":
                break
            line = rn.read_line(dwell + 3)
            if line is None:
                break
            if line.startswith("radio_rx"):
                count += 1
                ok += 1
                parts = line.split()
                print_packet(rn, bytes.fromhex(parts[-1]) if len(parts) > 1 else b"", count, decoder, raw)
            elif line.startswith("radio_err"):
                if time.monotonic() < deadline - 0.5:
                    errs += 1           # an error before the timeout = CRC/header error
        results[off] = (ok, errs)
        print("    packets %d, receive errors %d" % (ok, errs))
    rn.cmd("radio set wdt 0")
    rn.cmd("radio set freq %d" % center)
    print("\nSummary (offset: packets / errors):")
    for off in offsets:
        ok, errs = results[off]
        print("  %+3d kHz  %.3f MHz  %d / %d%s" % (off, (center + off * 1000) / 1e6, ok, errs,
                                                    "   <==" if ok else ""))
    best = [o for o in offsets if results[o][0]]
    if best:
        mid = sum(best) / len(best)
        print("\nBest guess: use --freq %d" % (center + mid * 1000))
    else:
        print("\nNothing received. Check SF/BW/sync word, or widen the scan (--scan 60).")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Receive MeshCore or Meshtastic packets with an RN2483.")
    ap.add_argument("port", help="serial port, e.g. COM51")
    ap.add_argument("preset", choices=sorted(PRESETS), help="radio preset")
    ap.add_argument("--freq", type=int, help="frequency in Hz (e.g. 869618000)")
    ap.add_argument("--bw", help="bandwidth in kHz: 62.5, 125, 250, 500")
    ap.add_argument("--sf", type=int, help="spreading factor 7-12")
    ap.add_argument("--cr", help="coding rate: 4/5, 4/6, 4/7, 4/8")
    ap.add_argument("--sync", help="sync word, 2 hex digits (MeshCore 12, Meshtastic 2B)")
    ap.add_argument("--prlen", type=int, help="preamble length in symbols")
    ap.add_argument("--fcorr", type=int, metavar="PPB",
                    help="store a crystal correction in the module (parts per billion, saved in "
                         "EEPROM; needs the patched firmware). E.g. measured -20 kHz at 869.618 MHz "
                         "-> --fcorr 23000")
    ap.add_argument("--baud", type=int, default=57600)
    ap.add_argument("--raw", action="store_true", help="only print the hex, no decoding")
    ap.add_argument("--log", help="also append received packets to this file")
    ap.add_argument("--cw", action="store_true",
                    help="transmit a plain carrier on the preset frequency (to calibrate an SDR), "
                         "Enter stops it")
    ap.add_argument("--txtest", action="store_true",
                    help="transmit a LoRa test packet every 2 s with the preset settings "
                         "(to measure the module's real frequency on an SDR), Ctrl+C stops")
    ap.add_argument("--scan", type=int, metavar="KHZ", nargs="?", const=40,
                    help="search for the right frequency: listen at offsets up to +/-KHZ kHz "
                         "(default 40) in 10 kHz steps")
    ap.add_argument("--dwell", type=int, default=20,
                    help="seconds per frequency step in --scan mode (default 20)")
    args = ap.parse_args(argv)

    cfg = dict(PRESETS[args.preset])
    for k in cfg:
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)

    try:
        import serial
    except ImportError:
        print("pyserial is missing. Install it with:  python -m pip install pyserial")
        return 2

    try:
        port = serial.Serial(args.port, args.baud, timeout=0.1)
    except (OSError, ValueError, serial.SerialException) as e:
        print("ERROR: %s" % e)
        return 1

    rn = RN2483(port)
    log = open(args.log, "a") if args.log else None
    decoder = decode_meshcore if args.preset == "meshcore" else decode_meshtastic

    try:
        print("=== RN2483 %s receiver on %s ===" % (args.preset, args.port))
        rn.flush()
        ver = rn.cmd("sys get ver")
        if not ver or not ver.startswith("RN2"):
            print("ERROR: no answer from the RN2483 application firmware.")
            return 1

        print("\nConfiguring radio:")
        pause = rn.cmd("mac pause")
        if pause == "0":
            # LoRaWAN is busy (e.g. left in a receive window by an earlier radio tx)
            # and refuses to pause. A reset brings the MAC back to idle.
            print("    'mac pause' returned 0: LoRaWAN did not release the radio. Resetting the module ...")
            rn.cmd("sys reset", timeout=2.0)
            time.sleep(1.0)
            rn.flush()
            pause = rn.cmd("mac pause")
        if not pause or not pause.isdigit() or pause == "0":
            print("ERROR: 'mac pause' failed (%s); LoRaWAN would take the radio back." % pause)
            return 1
        if args.fcorr is not None:
            if rn.cmd("radio set fcorr %d" % args.fcorr) != "ok":
                print("ERROR: 'radio set fcorr' rejected (firmware without fcorr, or outside +/-100000).")
                return 1
        steps = [
            ("radio set mod lora", None),
            ("radio set freq %d" % cfg["freq"], None),
            ("radio set bw %s" % cfg["bw"],
             "This firmware does not accept this bandwidth (62.5 needs the patched firmware)."),
            ("radio set sf sf%d" % cfg["sf"], None),
            ("radio set cr %s" % cfg["cr"], None),
            ("radio set sync %s" % cfg["sync"], None),
            ("radio set prlen %d" % cfg["prlen"], None),
            ("radio set crc on", None),
            ("radio set iqi off", None),
            ("radio set wdt 0", None),      # no receive timeout
        ]
        for command, hint in steps:
            if rn.cmd(command) != "ok":
                print("ERROR: '%s' was rejected.%s" % (command, (" " + hint) if hint else ""))
                return 1

        print("\nRadio settings read back:")
        for item in ("freq", "bw", "sf", "cr", "sync", "prlen", "crc", "fcorr"):
            rn.cmd("radio get %s" % item)

        if args.cw:
            return carrier_mode(rn, cfg)
        if args.txtest:
            return txtest_mode(rn, cfg)
        if args.scan:
            return scan_mode(rn, cfg, args.scan, args.dwell, decoder, args.raw)

        print("\nListening (%s, %.3f MHz, BW %s kHz, SF%d, CR %s, sync 0x%s). Ctrl+C to stop.%s\n"
              % (args.preset, cfg["freq"] / 1e6, cfg["bw"], cfg["sf"], cfg["cr"], cfg["sync"],
                 "" if HAVE_CRYPTO or args.raw else
                 "\n(pip install cryptography to decrypt the public/default channel)"))

        rn.echo = False
        count = 0
        while True:
            if rn.cmd("radio rx 0") != "ok":
                print("ERROR: 'radio rx 0' rejected, retrying ...")
                time.sleep(1)
                continue
            line = rn.read_line(None)
            now = datetime.datetime.now().strftime("%H:%M:%S")
            if line.startswith("radio_rx"):
                data = bytes.fromhex(line.split()[-1]) if len(line.split()) > 1 else b""
                snr = rn.cmd("radio get snr")
                rssi = rn.cmd("radio get rssi")
                count += 1
                print("[%s] #%d  %d bytes  RSSI %s dBm  SNR %s dB" % (now, count, len(data), rssi, snr))
                print("  " + data.hex(" "))
                if not args.raw:
                    try:
                        for text in decoder(data):
                            print(text)
                    except Exception as e:     # never stop the sniffer on a decode problem
                        print("  (decode error: %s)" % e)
                print()
                if log:
                    log.write("%s %s rssi=%s snr=%s %s\n"
                              % (datetime.datetime.now().isoformat(timespec="seconds"),
                                 args.preset, rssi, snr, data.hex()))
                    log.flush()
            elif line.startswith("radio_err"):
                print("[%s] radio_err (CRC error or receive problem)" % now)
            else:
                print("[%s] unexpected: %s" % (now, line))
    except KeyboardInterrupt:
        print("\nStopping ...")
        rn.echo = True
        rn.cmd("radio rxstop")
        rn.cmd("mac resume")
        return 0
    finally:
        if log:
            log.close()
        port.close()


if __name__ == "__main__":
    sys.exit(main())
