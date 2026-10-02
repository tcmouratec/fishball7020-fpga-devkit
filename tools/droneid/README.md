# tools/droneid: DJI DroneID with this board

This board can detect DJI drones. It receives with its own firmware, unchanged.
The DroneID bursts are decoded on a host, a Raspberry Pi 5 for example, and each
frame goes into the WarDragon pipeline (`dji_receiver.py` → DragonSync → TAK).
None of that pipeline changes.

| | |
|---|---|
| `droneid_rx.py` | the receiver: tunes the board, streams, decodes, sends frames on |
| `ocusync.py` | the decoder, numpy only: detection, OFDM demodulation, turbo decoding, the DroneID record |
| `fpv.py` | detectors for analog FPV video and ExpressLRS/LoRa control links ([below](#fpv-video-and-expresslrs)) |
| `droneid-rx.service` | systemd unit for the Pi |
| `bench.py` | runs the decoder over the public drone RF datasets ([below](#testing-against-the-public-datasets)) |
| `test_ocusync.py`, `test_fpv.py`, `test_droneid_rx.py` | the tests CI runs; no board, no captures |
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
| `--scan droneid,video,elrs` | what to sweep for (default `droneid`); see [FPV video and ExpressLRS](#fpv-video-and-expresslrs) |
| `--video-bands 5.8,1.2` | analog video ranges: `5.8`, `5.3`, `1.2`, `3.3`, or `LO-HI` in MHz |
| `--elrs-bands 2.4,915` | ExpressLRS domains: `2.4`, `915`, `868` |
| `--dragonscope http://127.0.0.1` | ask a licensed DragonScope proxy for O4 serial and position ([below](#dragonscope-o4-positions)) |
| `--fpv-zmq` | publish analog-video detections on 4226 for DragonSync's FPV ingest ([below](#on-a-wardragon-kit)) |
| `--save-failed DIR` | keep the IQ of bursts found but not decoded, for study |
| `--file REC.sigmf-meta` | decode a recording (SigMF from `tools/sigmf-capture.py`, or raw with `--file-rate`) |

It only receives. The only output attribute it writes is the RX LO frequency;
nothing on the transmit side is touched.

## With F5OEO's tezuka firmware

[tezuka_fw](https://github.com/F5OEO/tezuka_fw) is a Pluto-family firmware
with a `fishball7020` build for this board. It boots from the SD card, so the
flash keeps the firmware it has. Two of its features matter here:

- **8-bit I/Q (`--cs8`).** With only the I channel enabled, tezuka's FPGA
  packs I8/Q8 into the I channel's 16-bit slot. That halves the Ethernet
  traffic: 11.52 MSPS goes from 46 MB/s to 23 MB/s. The board can then stream
  a dwell with fewer gaps, so more bursts are caught. 8 bits is enough here:
  DroneID decodes near 0 dB SNR, far below what 8 bits can carry. **Use
  `--cs8` only with tezuka.** Stock firmware sends I alone in that mode, and
  nothing decodes.
- **Maia SDR.** A web waterfall served by the board, for checking by eye
  that the antenna sees the drone's 2.4/5.8 GHz signal. It and this receiver
  use the same receive chain, so run one at a time.

```sh
# run from: the repo root, on the Pi, with the board booted from a tezuka SD card
python3 tools/droneid/droneid_rx.py --cs8 --scan droneid,video,elrs --json
```

Boot it the way this repo boots any image: from the SD card only. Copy the
`sdimg` folder of the release's `fishball7020` zip to a FAT32 card and boot
from it. Never use `frm` or
DFU. tezuka runs `/mnt/jffs2/autorun.sh` like the stock firmware does, so
check that file first. Not yet tried on this board.

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

## FPV video and ExpressLRS

FPV and home-built drones broadcast no identity, so for them the receiver
**detects** rather than identifies. `--scan` adds two detectors from `fpv.py`
to the sweep:

```bash
# run from: the repo root
tools/droneid/droneid_rx.py --scan droneid,video,elrs --dji-receiver 127.0.0.1:52002
tools/droneid/droneid_rx.py --scan video --video-bands 5.8,1.2,3.3 --json
```

- **Analog video** (`video`). It FM-demodulates, then looks for the comb of
  harmonics at the video line rate: 15 625 Hz for PAL, 15 734 Hz for NTSC.
  Every analog camera puts a sync pulse on every line, and OFDM, LoRa, a
  plain carrier or noise do not make that comb. It reports the standard and
  the nearest channel (`R4 5769`). It sweeps 10 MHz steps across the chosen
  ranges (5.8 GHz: 5640–5950 MHz, every A/B/E/F/R channel) at ~0.1 s per step.
- **ExpressLRS** (`elrs`). The air modes come from the ExpressLRS source:
  - 2.4 GHz: LoRa at 812.5 kHz, SF5–SF8, hopping over 2400.4–2479.4 MHz;
  - 900 MHz: 500 kHz, SF5–SF9, over FCC915 903.5–926.9 or EU868.

  Each window is dechirped against the reference chirp and an FFT checks
  whether the energy lands in one bin, repeated across a preamble. A carrier
  or noise responds the same to the opposite chirp; a real preamble does not,
  and that contrast is required. Several distinct frequencies within one
  buffer mark a hopping link. 812.5 kHz LoRa at 2.4 GHz is ExpressLRS's own
  mode, so it is labelled ExpressLRS. At 900 MHz it is labelled ExpressLRS
  only if it hops; otherwise it may be a LoRaWAN device and is reported as
  plain LoRa.

Both go to dji_receiver.py as `drone-alert-fpv-video-R4` and
`drone-alert-elrs-2.4`, at most one per channel every `--alert-interval`
seconds. Their model field carries the label (for example "Analog FPV video
PAL"). dji_receiver.py places any `drone-alert-*` at the sensor's own position,
so they appear in TAK as alerts around the WarDragon. They are not drone
positions.

Tested against synthetic signals (`test_fpv.py`):
- PAL and NTSC at ±3 and ±7 MHz deviation, including the wrap at 11.52 MSPS,
  and down to 3 dB carrier-to-noise;
- ExpressLRS SF5–SF8, hopping, down to 0 dB SNR in its bandwidth;
- no alarms on noise, OFDM, a CW carrier, FM without video, or the other
  detector's signal.

`bench.py --detect video,elrs` runs the same detectors over wideband dataset
recordings. **None of this has been tried on a real VTX or ExpressLRS radio
yet**; that is the first field test to do.

Not covered yet:
- ExpressLRS's FLRC and FSK modes (its fastest packet rates);
- TBS Crossfire's FSK mode;
- the FSK hopping of FrSky, FlySky and Spektrum radios;
- digital FPV video (DJI O3/O4 air units, Walksnail, HDZero).

Crossfire's LoRa modes may be caught by the 900 MHz detector if their
parameters match; that is not verified.

## On a WarDragon kit

The kit's services (from alphafox02/DragonSync `services/README.md`):

| Service | What it does | Port |
|---|---|---|
| `zmq-decoder` (droneid-go) | Remote ID from the WiFi adapter, the Sniffle Bluetooth dongle and the ESP32, plus DJI from `dji-receiver` (`-dji 127.0.0.1:4221`) | pub 4224 |
| `dji-receiver` (`dji_receiver.py`) | DJI DroneID lines from an SDR, in on 52002 | pub 4221 |
| `fpv-receiver` (wardragon-fpv-detect, optional) | analog FPV scan on a Pluto; "confirm" needs the licensed `suscli fpvdet` plugin | pub 4226 |
| `wardragon-monitor` | the kit's GPS and health | pub 4225 |
| `dragonsync` | everything above to TAK / MQTT | sub 4224, 4225, 4226 |

This receiver slots into that chain without replacing anything:

- **DJI DroneID and O4:** `--dji-receiver 127.0.0.1:52002` feeds the
  `dji-receiver` that is already running (its parser is identical in the
  antsdr and dragonsdr repos), and droneid-go carries the result to
  DragonSync on 4224.
- **Remote ID** (FIMI, Potensic, Autel, Skydio, DJI's own RID...) stays with
  droneid-go and its WiFi/Bluetooth/ESP32 hardware. This receiver does not
  duplicate it.
- **Analog FPV video:** `--fpv-zmq` publishes on 4226 exactly what
  wardragon-fpv-detect publishes:
  - `fpv-alert-<MHz>` in Basic ID;
  - Signal Info with `source: "confirm"`, `center_hz`, and
    `pal_conf`/`ntsc_conf` on a 0–100 scale.

  DragonSync then shows it as its own FPV marker, positioned from the kit's
  GPS. Set `fpv_enabled = true` in DragonSync's `config.ini`. The confirmation
  comes from `fpv.py`'s line-rate comb, so no licensed plugin is needed.
  Checked against DragonSync's own parser: the source is accepted, the UID is
  `fpv-alert-5769MHz`, and the callsign is `fpv-alert-5769.000MHz`.
- **ExpressLRS** has no source DragonSync's FPV ingest accepts by default, so
  it goes through dji-receiver as `drone-alert-elrs-<band>`.

**One SDR, one owner.** If `fpv-receiver` is configured to use this
PlutoSky, it and `droneid_rx.py` will fight over the board. Its DJI guard
only knows how to pause an AntSDR. Stop it (`sudo systemctl disable --now
fpv-receiver`), or point it at a different SDR. `droneid_rx.py --scan
droneid,video,elrs` interleaves all three on the one board. If
`fpv-receiver` holds port 4226, `--fpv-zmq` says so and stops; give it
another port and match `fpv_zmq_port` in DragonSync's `config.ini`.

```bash
# the whole set on the kit (needs: sudo apt install python3-numpy python3-zmq)
tools/droneid/droneid_rx.py --scan droneid,video,elrs \
    --dji-receiver 127.0.0.1:52002 --fpv-zmq
```

## DragonScope (O4 positions)

DragonScope is CEMAXecuter's licensed service for WarDragon kits. A proxy on
the WarDragon (`dragonscope.py`, port 80) forwards each O4 packet to their
remote service, which answers with the drone's serial and position.
MicroPhase's DragonScope firmware sends it every CRYP/INFP packet. With
`--dragonscope URL` this receiver does the same: it sends the logical packet's
hex to `GET /api/o4online/decrypt?hex=`, at most once a second per session and
packet type. When the answer carries a serial, the O4 drone goes to
dji_receiver.py as "DJI O4 (Decrypted)" with serial, drone, pilot and home
position.

```bash
tools/droneid/droneid_rx.py --dji-receiver 127.0.0.1:52002 --dragonscope http://127.0.0.1
```

- **Without a license key** the proxy answers `{"sn": ""}`, and O4 drones stay
  as `drone-alert-<hash>`.
- **This is only a client.** The decryption, and the key it needs, are
  DragonScope's.
- **Not yet tested against the real service, because there is no license
  here.** The request and reply shapes come from `dragonscope.py` (`sn`, `lat`,
  `lon`) and dji_receiver.py's proxy code; other reply fields are ignored. The
  end-to-end test uses a fake DragonScope.
- **One open question:** MicroPhase's firmware may send the packet in a
  slightly different form, for example the whole 176-byte block. If a licensed
  proxy answers empty for packets that MicroPhase's firmware gets answered,
  that is the first thing to check.

## Other brands and Remote ID

Only DJI broadcasts an identity in the OcuSync burst. For every other brand,
FIMI, Potensic (Atom), Autel, Skydio and Parrot included, the identity is in
**Remote ID**:
- **Standard:** ASTM F3411 / ASD-STAN prEN 4709-002.
- **Where it is required:** the FAA in the US, the C1–C3 classes in the EU.
- **How it is sent:** WiFi (Beacon, NAN) and Bluetooth (4 legacy, 5 Long
  Range), with serial, position and operator location in clear.

Some aircraft send it themselves (FIMI's X8 SE 2022 and X8 Tele are listed as
Remote-ID approved). Others need an add-on module (Potensic sells the RID-916
for the Atom/Atom SE/LT). Either way it is the same standard message, so one
Remote ID receiver covers them all.

- **The WarDragon already receives it** with its ESP32 and Bluetooth sniffers
  ([alphafox02/DroneID](https://github.com/alphafox02/DroneID)); run that
  alongside. A WiFi/Bluetooth chip hears these short packets better than an SDR
  hopping across bands does.
- **The reference message codec** is
  [opendroneid-core-c](https://github.com/opendroneid/opendroneid-core-c)
  (Apache-2.0, which this GPL-2.0-only repository cannot include). Others:
  [open-remote-id-parser](https://github.com/iannil/open-remote-id-parser)
  (C++),
  [micropython-remoteid](https://github.com/Gurkengewuerz/micropython-remoteid)
  (Python), and the [opendroneid Android receiver](https://github.com/opendroneid/receiver-android).
- **Decoding Bluetooth 4 legacy Remote ID on this board** (GFSK at 1 Mbit/s on
  advertising channels 37/38/39) is possible in numpy and would be a
  reasonable next step. WiFi NAN/Beacon (802.11 OFDM/DSSS) is a much bigger
  job.
- **Autel's Remote ID has been reported as flawed**: a fixed MAC address and
  `default-ssid`. On older models the pilot can turn it off.
- **Military datalinks:** no public decoder and no public raw recordings were
  found. A 2024 Ukrainian paper studies recognising Crossfire and ExpressLRS
  signals ("Method of recognition of FPV-UAV radio signals formed according to
  Crossfire and ExpressLRS standards"). Reporting from both sides agrees that
  FPV control is mostly Crossfire and ExpressLRS, including at re-tuned
  frequencies. That is why the ExpressLRS detector takes any `--elrs-bands`
  range the AD9363 can tune.

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
