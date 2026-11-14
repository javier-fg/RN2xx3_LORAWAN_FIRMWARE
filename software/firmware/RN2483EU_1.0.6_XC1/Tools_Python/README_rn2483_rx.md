# rn2483_rx.py

Turns an RN2483 LoRa module into a simple receiver ("sniffer") for **MeshCore** and **Meshtastic** traffic. The tool configures the radio with the RN2483 text commands, listens continuously and prints every received packet with RSSI, SNR, the raw bytes and a decode of the MeshCore or Meshtastic header.

It also contains helpers to measure and correct the module's frequency error.

## Requirements

- Python 3
- pyserial: `python -m pip install pyserial`
- Optional, to decrypt messages on the open default channels: `python -m pip install cryptography`
- A USB-to-serial adapter with **3.3 V** logic levels (TX to module RX, RX to module TX, GND)
- RN2483 running the **patched firmware** from this repository (see below)

Close any terminal program (PuTTY, Tera Term) that has the COM port open before starting the tool.

### Firmware requirements

| Feature | Needed for |
|---|---|
| 62.5 kHz LoRa bandwidth (`radio set bw 62.5`) | MeshCore preset |
| Interrupt handler fix (typed function pointers in the MCC drivers) | Any reception or `radio_tx_ok`; without it the radio stays busy after the first packet |
| `mac pause` fix in `lorawan.c` | Stable operation; without it LoRaWAN can take the radio back and switch it to the RX2 settings |
| `radio set/get fcorr` | Frequency correction (`--fcorr`) |

The original Microchip firmware does not support 62.5 kHz, so the MeshCore preset is rejected with a clear error message.

## Usage

```
python rn2483_rx.py <PORT> <PRESET> [options]
```

```
python rn2483_rx.py COM51 meshcore
python rn2483_rx.py COM51 meshtastic --sf 11
```

Stop with **Ctrl+C**. The tool then sends `radio rxstop` and `mac resume`, which gives the radio back to the LoRaWAN stack.

## Presets

| | `meshcore` | `meshtastic` |
|---|---|---|
| Frequency | 869.618 MHz | 869.525 MHz |
| Bandwidth | 62.5 kHz | 250 kHz |
| Spreading factor | SF8 | SF10 |
| Coding rate | 4/8 | 4/5 |
| Sync word | 0x12 | 0x2B |
| Preamble | 16 | 16 |

- The MeshCore preset matches the EU/UK narrow settings (869.618 MHz, 62.5 kHz, SF8, CR 4/8).
- Meshtastic EU_868 **LongFast** uses SF11: add `--sf 11`. Other Meshtastic presets use other spreading factors and, unless configured otherwise, other frequency slots.
- The coding rate does not have to match for reception (the receiver reads it from the LoRa header), but spreading factor, bandwidth, frequency and sync word must.

Every preset value can be overridden on the command line.

## Options

| Option | Description |
|---|---|
| `--freq HZ` | Frequency in Hz, e.g. `869618000` |
| `--bw KHZ` | Bandwidth: `62.5`, `125`, `250`, `500` |
| `--sf N` | Spreading factor 7 to 12 |
| `--cr CR` | Coding rate: `4/5`, `4/6`, `4/7`, `4/8` |
| `--sync HEX` | Sync word, 2 hex digits |
| `--prlen N` | Preamble length in symbols |
| `--baud N` | Serial baud rate, default 57600 |
| `--raw` | Only print the hex bytes, no decoding |
| `--log FILE` | Also append every packet (time, RSSI, SNR, hex) to a file |
| `--fcorr PPB` | Store a frequency correction in the module (see Calibration) |
| `--txtest` | Transmit a 64-byte test packet every 2 s with the current settings |
| `--scan [KHZ]` | Search for a transmitter: listen at offsets up to +/-KHZ kHz (default 40) in 10 kHz steps |
| `--dwell S` | Seconds per step in `--scan` mode, default 20 |
| `--cw` | Transmit an unmodulated carrier (see note below) |

## Example output

```
[11:20:49] #1  124 bytes  RSSI -45 dBm  SNR 7 dB
  12 00 41 7a 84 7a 31 b7 ...
  MeshCore  route=DIRECT  type=ADVERT  ver=0
  path: 0 hop(s)
  payload 122 bytes
  ADVERT  key 417a847a..  time 2026-03-01 01:56:17  node=chat  pos=52.21639,6.90076  name='TEST Enschede'
```

## What is decoded

**MeshCore**

- Header: route type, payload type, version
- Transport codes (for transport route types)
- Path: number of hops and the hop hashes
- ADVERT: public key (first bytes), timestamp, node type, position and name
- GRP_TXT on the **Public** channel: decrypted text (needs `cryptography`)

**Meshtastic**

- 16-byte header: sender, destination, packet ID, hop count, want_ack, via MQTT, channel hash, next hop, relay node
- The channel hash is matched against the Meshtastic presets with the default key (e.g. `0x08` = LongFast)
- Packets on a default-key channel are decrypted and the port is shown; text messages are printed (needs `cryptography`)

Only the publicly known default keys are used. Messages on private channels are shown as encrypted payload.

## Calibration (frequency correction)

At 62.5 kHz bandwidth the receiver only tolerates roughly +/-15 kHz of frequency error between transmitter and receiver. The RN2483 crystal can be off by more than that (about 20 kHz, or 23 ppm, was measured on one module), in which case nothing is received at all.

1. **Measure** with an SDR you trust (e.g. a LimeSDR):
   ```
   python rn2483_rx.py COM51 meshcore --txtest
   ```
   Read the center of the transmitted 62.5 kHz band. The error is that center minus 869.618 MHz.

2. **Calculate** the correction in parts per billion:
   ```
   ppb = -(error in Hz) / (frequency in MHz) * 1000
   ```
   Example: the module transmits 20 kHz low (error = -20000 Hz): 20000 / 869.618 * 1000 = **23000**.

3. **Store** it in the module (kept in EEPROM, applied to every frequency, also for LoRaWAN):
   ```
   python rn2483_rx.py COM51 meshcore --fcorr 23000 --txtest
   ```
   Check on the SDR that the packet is now centered. `radio get fcorr` shows the stored value; `sys factoryRESET` erases it.

**Without an SDR**, use the scan mode while a node sends adverts:
```
python rn2483_rx.py COM51 meshcore --scan 60
```
The summary shows at which offsets packets (or CRC errors) were seen and suggests a `--freq` value.

**About `--cw`:** the RN2483 generates its carrier with undocumented radio test registers, so the tone is not guaranteed to sit exactly on the set frequency, and the radio may need a power cycle afterwards. Prefer `--txtest` for measurements.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `radio set bw 62.5` rejected | Firmware without 62.5 kHz support |
| Nothing received at all | Check SF, bandwidth and sync word against the node; then the frequency error (calibrate); then try `--prlen 8` |
| Only `radio_err` lines | Packets are detected but fail the CRC: usually a frequency offset close to the limit |
| `--txtest` shows `(nothing)` or `busy` after the first packet | Firmware without the interrupt handler fix |
| `--txtest` settings change to `869525000 / 125 / sf12 / iqi=on` | LoRaWAN took the radio back: firmware without the `mac pause` fix |
| `'mac pause' returned 0` | LoRaWAN was busy; the tool resets the module and retries automatically |
| Very strong signal but no packets | Receiver overload: move the node about a meter away |

## Limitations

- After each packet the tool asks for SNR and RSSI and then restarts reception. A packet arriving in those few milliseconds is missed, so this is a test tool, not a complete capture.
- Only one frequency, bandwidth and spreading factor can be received at a time.
- Packets longer than the RN2483 receive buffer (255 bytes) are not possible on LoRa anyway; MeshCore and Meshtastic stay well below that.
