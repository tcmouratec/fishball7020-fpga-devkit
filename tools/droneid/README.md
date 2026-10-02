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
- **Not O4.** DJI's O4 drones (Mini 5 and later) encrypt their DroneID. It is
  decoded only from OcuSync 2/3 drones (Mini 2, Mini 3, Air 2/2S, Mavic 3, ...).
- **Not yet run against this board and a drone.** The decoder has been checked
  on real captures from a Mini 2 and a Mavic Air 2. Everything else is checked
  only against simulation. See
  [what is and is not measured](../../docs/droneid-microphase.md#5-the-route-that-works-this-board-as-the-receiver-decoding-on-the-host).
  The stats line (every `--stats` seconds) is the first thing to read in the
  field: buffers read, buffers dropped, candidates, frames decoded, and frames
  found but not decodable.

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
