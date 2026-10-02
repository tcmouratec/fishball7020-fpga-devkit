# tools/droneid: DJI DroneID with this board

This board can detect DJI drones. It receives with its own firmware, unchanged.
The DroneID bursts are decoded on a host, a Raspberry Pi 5 for example, and each
frame goes into the WarDragon pipeline (`dji_receiver.py` → DragonSync → TAK).
None of that pipeline changes.

| | |
|---|---|
| `droneid_rx.py` | the receiver: tunes the board, streams, decodes, sends frames on |
| `ocusync.py` | the decoder, numpy only: detection, OFDM demodulation, turbo decoding, the DroneID record |
| `droneid-rx.service` | systemd unit for the Pi |
| `bench.py` | runs the decoder over the public drone RF datasets ([below](#testing-against-the-public-datasets)) |
| `test_ocusync.py`, `test_droneid_rx.py` | the tests CI runs; no board, no captures |
| `zynq_bootimg.py`, `pinmap_compare.py` | why MicroPhase's own DroneID image cannot run here ([the findings](../../docs/droneid-microphase.md)) |

## Running it

On the Pi you need Python 3.8+ and numpy (`sudo apt install python3-numpy`).
libiio and pyadi-iio are not needed. Connect the board by **Ethernet**: the
USB gadget carries about 10 MB/s, a quarter of what the default rate needs.

```bash
# run from: the repo root, on the Pi
tools/droneid/droneid_rx.py --json                                # see it work
tools/droneid/droneid_rx.py --dji-receiver 127.0.0.1:52002        # into dji_receiver.py
```

`dji_receiver.py` must run in `new` (its default) or `dual` mode, which listens
on 52002. Each frame then arrives exactly as from an AntSDR running MicroPhase's
newer firmware: one `dji_O,...` line over TCP, with a `=` heartbeat every 30 s.
To run at boot, see the comments in `droneid-rx.service`.

Options that matter:

| | |
|---|---|
| `--band 2.4` / `5.8` / `all` | which channels to hop through (default `all`) |
| `--freqs 2414.5,2429.5` | your own channel list, MHz |
| `--dwell 1.3` | seconds per channel: at least two burst periods (~600 ms) |
| `--rate 11.52e6` | 11.52 MSPS fits the Ethernet link; 15.36e6 if yours keeps up |
| `--gain fast_attack` | or `slow_attack`, or a manual gain in dB such as `--gain 50` |
| `--uri ip:ADDRESS` | otherwise `tools/board_addr.py` finds the board (`fishball.local`) |
| `--save-failed DIR` | keep the IQ of bursts found but not decoded, for study |
| `--file REC.sigmf-meta` | decode a recording (SigMF from `tools/sigmf-capture.py`, or raw with `--file-rate`) |

It only receives. The only output attribute it writes is the RX LO frequency;
nothing on the transmit side is touched.

## What one frame looks like

```
[14:02:11] 2429.5 MHz  Mavic Air 2  1WNBH3900201N1  51.44633,7.26722  alt 43 m  SNR 26 dB  hard
```

`--json` prints the full record: serial, drone, pilot and home positions,
altitude, height above take-off, velocities, yaw, GPS time, model, and the
receiver's own measurements (frequency, CFO, SNR, burst power, which decoder
succeeded). Every frame printed has passed the turbo codeword's CRC-24A.
`record_crc_ok` is the DroneID record's own CRC-16.

Units: positions in degrees. Heights are stored in feet and converted to
metres, following the NDSS 2023 receiver, which compared them with flight logs.
Velocities are in cm/s. `rssi_db` is the burst power minus the receive gain: a
relative level, not calibrated to dBm.

## Expectations

- **One channel at a time.** A drone moves its DroneID burst between channels.
  MicroPhase's E200 watches 61.44 MHz at once and predicts the hops. This
  receiver watches one channel of about 10 MHz, so it catches roughly one burst
  in (number of channels). A short channel list catches more.
- **Sensitivity.** In tests it decodes down to about 0 dB SNR in the 9 MHz the
  burst occupies. It tries hard decisions first and turns to the turbo decoder
  only when they fail; the turbo decoder is worth about 10 dB.
- **O4 is detected, not decoded.** DJI's O4 drones (Mini 5 Pro and later)
  send their DroneID over the same radio layer, so the bursts decode and pass
  both CRCs. The contents are encrypted:
  - `CRYP` packets carry an AES session key wrapped with a private key that is
    not public;
  - `INFP` packets carry the telemetry under that key.

  What the receiver can read is the 4-byte session hashcode the two share. It
  reports that as `DJI O4 (encrypted)`, and sends dji_receiver.py the same
  protocol-4 line MicroPhase's O4 firmware sends. You get a
  `drone-alert-<hash>` at the sensor's position, with frequency and level, but
  no serial or drone position. The hash changes each time the drone is
  powered on.
- **Some DJI drones are newer than this.** A 10-symbol O4+ burst with
  different Zadoff-Chu roots has been reported, and encrypted DroneID has been
  seen from an Inspire 3 (O3). Neither is handled; the 10-symbol form would not
  even be detected.
- **Not yet run against this board and a drone.** The decoder has been checked
  on real captures from a Mini 2 and a Mavic Air 2. Everything else is checked
  only against simulation. See
  [what is and is not measured](../../docs/droneid-microphase.md#5-the-route-that-works-this-board-as-the-receiver-decoding-on-the-host).
  The stats line (every `--stats` seconds) is the first thing to read in the
  field: buffers read, buffers dropped, candidates, frames decoded, and frames
  found but not decodable.

## Testing against the public datasets

`bench.py` runs the same decoder over recordings from the public drone RF
datasets. They were recorded with 20 to 200 MHz of bandwidth, where this board
sees about 10 MHz. bench.py cuts every DroneID channel that fits out of a
recording, resamples it to 15.36 MSPS and decodes it. Per file it reports:
plaintext drones (model and serial), O4 sessions, serial-only frames, and
bursts found but not decoded. Use `--jsonl` to keep every frame. It needs scipy
(`pip install scipy`).

```bash
# run from: the repo root
tools/droneid/bench.py --preset dronedetect --max-seconds 2 DroneDetect_V2/CLEAN/*/*.dat
tools/droneid/bench.py --preset rfuav --center-mhz 2440 RFUAV/DJI_MINI_3/*.iq --jsonl out.jsonl
```

| Dataset | What is in it | Format (bench preset) | Where |
|---|---|---|---|
| **RFUAV** (Zhejiang, 2025) | 37 drone types, DJI (Mini 3, Mavic 3 Pro, Avata 2, FPV Combo, ...) and FPV/DIY links; ~1.3 TB | cf32, 100 MSPS, USRP X310 (`rfuav`; centre per drone, from the dataset's table) | [GitHub](https://github.com/kitoweeknd/RFUAV), [Hugging Face](https://huggingface.co/datasets/kitofrank/RFUAV) |
| **DroneDetect V2** (Essex) | Mavic 2 Air S, Mavic Pro, Mavic Pro 2, Inspire 2, Mavic Mini, Phantom 4, Parrot Disco; clean, WiFi and Bluetooth interference | cf32, 60 MSPS, 2437.5 MHz, BladeRF (`dronedetect`) | [IEEE DataPort](https://ieee-dataport.org/open-access/dronedetect-dataset-radio-frequency-dataset-unmanned-aerial-system-uas-signals-machine) |
| **Tampere** (2020) | 10 consumer drones, control and video, anechoic | ci16, 120 MSPS at 2.44 GHz / 200 MSPS at 5.8 GHz (`tampere24`, `tampere58`) | [Zenodo 4264467](https://zenodo.org/records/4264467) |
| **KU Leuven / RMA** | Matrice 300, Mavic, FrSky Taranis and more, semi-anechoic | 100 MSPS at 2.44 GHz, stored as MATLAB v7.3; convert to cf32 first | [KU Leuven RDR](https://rdr.kuleuven.be/dataset.xhtml?persistentId=doi%3A10.48804%2FHZRVNZ) |
| **DroneRFa** (Fudan) | 24 types, 915 MHz / 2.4 / 5.8 GHz, indoor and outdoor | 100 MSPS; check the format before running | [ScienceDB](https://www.scidb.cn/en/detail?dataSetId=34f0a91e8a544904998b8fdc44477380) |
| **NDSS 2023 samples** | DJI Mini 2, Mavic Air 2, DroneID bursts only | cf32, 50 MSPS | [RUB-SysSec/DroneSecurity](https://github.com/RUB-SysSec/DroneSecurity) (AGPL, not copied here) |

The last set is what the decoder was checked against. bench.py decodes 10 of
10 Mini 2 bursts and all 3 Mavic Air 2 bursts through its channelizer, one of
them 0.54 MHz off the channel raster.

Two datasets are no use here:
- [DroneRF](https://al-sad.github.io/DroneRF/) (2019) is real-valued, not IQ.
- The [remote-controller dataset](https://ieee-dataport.org/open-access/drone-remote-controller-rf-signal-dataset)
  is oscilloscope captures of controllers.

Expect most files in the other datasets to show **no DroneID**, for three
reasons:
- Several of the drones predate DroneID over OcuSync, or are not DJI.
- A drone sends DroneID only with its motors running.
- A recording that does not cover the channel the burst landed on cannot
  contain it.

That is a result, not a failure. The per-file "found but not decoded" count is
the one to look at: a high count means bursts the decoder sees but cannot
read, and `--jsonl` plus `droneid_rx.py --save-failed` are how to send them back.

## Other brands, FPV and military drones

Only DJI broadcasts an identity in this OcuSync burst, so this receiver
identifies DJI and nothing else. For everything else:

- **Remote ID** (ASTM F3411 / ASD-STAN prEN 4709-002) is the broadcast every
  brand must send where it is required: the FAA in the US, the C1–C3 classes
  in the EU. Autel, Skydio, Parrot and DJI all send it. It goes out over WiFi
  (Beacon, NAN) and Bluetooth (4 legacy, 5 Long Range), with serial, position
  and operator location in clear. A WiFi or Bluetooth radio receives it better
  than this SDR does. The WarDragon already does this with its ESP32 and
  Bluetooth sniffers ([alphafox02/DroneID](https://github.com/alphafox02/DroneID));
  run it alongside. Reference decoders:
  [opendroneid](https://github.com/opendroneid/receiver-android) and
  [open-remote-id-parser](https://github.com/iannil/open-remote-id-parser).
  Autel's implementation has been reported as flawed (a fixed MAC address and
  `default-ssid`), and on older models the pilot can turn it off.
- **FPV and home-built drones**, including most military FPV use, broadcast no
  identity at all. They can be *detected* by what their links look like:
  - ExpressLRS / TBS Crossfire control links at 868/915 MHz and 2.4 GHz
    (LoRa chirps, FLRC, frequency hopping);
  - analog FM video at 5.8 GHz (often 1.2 and 3.3 GHz as well), recognisable
    by its line sync;
  - DJI O3/O4 air units, which carry the OcuSync signal.

  This board tunes all of those bands. Detectors for them are not written yet.
  [deye](https://github.com/subeep/deye) (GPL-3/AGPL) and
  [beyond-the-goggles](https://github.com/Ray1172004/beyond-the-goggles) are
  open prior art. RFUAV and DroneRFa contain such links for testing.
- **Military datalinks** proper have no public decoders or datasets that this
  search found. At most, an energy or spectrum-shape detector can say that
  something is transmitting.

## How the decoder works

`ocusync.py` has a longer description in its docstring. In short:

1. **Gate.** The power in 256-sample blocks, against the noise floor. A quiet
   channel costs one pass over the samples.
2. **Detect.** Each OFDM symbol's cyclic prefix is correlated against the end
   of that symbol, summed over the burst's 9 (or 8) symbols. This is blind to
   frequency offset and gives the fractional carrier offset.
3. **Confirm.** The two Zadoff-Chu symbols (roots 600 and 147). The
   whole-carrier offset is the shift at which the channel seen through one
   root agrees with the channel seen through the other. A plain ZC correlation
   cannot tell a frequency shift from a time shift.
4. **Equalise.** With the ZC channel estimate and the phase drift between the
   two ZC symbols, then QPSK soft bits, LTE descrambling and rate de-matching.
5. **Decode.** Hard decisions on the systematic bits first. If they fail, a
   windowed max-log-MAP turbo decoder. The CRC-24A decides.

Any sample rate works if it is a multiple of 15 kHz and gives whole cyclic
prefixes: 11.52, 15.36, 23.04, 30.72 MSPS. The FFT size follows the rate, so
nothing is resampled.

## The image-checking tools

These were written to find out why MicroPhase's ANTSDR E200 DroneID image is
silent on this board. They answer the same question for any other Zynq-7020
board's image. Both need only Python 3.

```bash
# run from: the repo root
tools/droneid/zynq_bootimg.py info    BOOT.bin                # partitions, md5s, compressed or not, IDCODE
tools/droneid/zynq_bootimg.py split   BOOT.bin OUTDIR         # write each partition out
tools/droneid/zynq_bootimg.py ps7     BOOT.bin                # what the FSBL's ps7_init sets: PLLs, FCLKs, MIO, DDR, UART
tools/droneid/zynq_bootimg.py ps7diff A/BOOT.bin B/BOOT.bin   # where two FSBLs disagree, and which UART each uses
tools/droneid/zynq_bootimg.py graft   OUT.bin --fsbl A --bit B --uboot A
tools/droneid/pinmap_compare.py THIS_BOARD.xdc OTHER_BOARD.xdc
```

- **`ps7`, `ps7diff`** read the register-write tables that `ps7_init.c`
  compiles into an FSBL. The decode was checked against the factory XSA's own
  `ps7_init.c`.
- **`graft`** keeps everything up to and including the donor's FSBL byte for
  byte. Grafting an image onto itself reproduces it exactly. **Flash a graft
  only if its bitstream was built for this board.**
- **`pinmap_compare.py`** marks each ball where the other board's bitstream
  would drive a trace that something on this board already drives.
