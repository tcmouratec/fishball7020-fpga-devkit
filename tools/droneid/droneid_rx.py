#!/usr/bin/env python3
"""Receive DJI DroneID with this board, decode it on the host, and hand the
frames to the WarDragon/DragonSync pipeline.

    # run from: the repo root, on the Raspberry Pi (or any host with numpy)
    tools/droneid/droneid_rx.py --dji-receiver 127.0.0.1:52002     # into dji_receiver.py
    tools/droneid/droneid_rx.py --json                             # JSON lines on stdout
    tools/droneid/droneid_rx.py --band 2.4 --dwell 2 --json
    tools/droneid/droneid_rx.py --file capture.sigmf-meta --json   # a recording, no board

WHAT IT DOES. The board runs its own firmware, unchanged, as a receiver. This
program tunes it to one DroneID channel at a time, streams IQ over the network
(IIOD, the protocol libiio speaks, implemented in tools/selftest/iiod_min.py, so
neither libiio nor pyadi-iio is needed), and decodes on the host with
ocusync.py. It only receives: nothing here touches a transmit attribute.

INTO THE EXISTING PIPELINE. With --dji-receiver it connects to dji_receiver.py
the way MicroPhase's newer AntSDR firmware does: a TCP client sending one
"dji_O,..." line per frame, plus a "=" heartbeat every 30 s. dji_receiver.py,
DragonSync and TAK then need no change. Start dji_receiver.py in "new" or
"dual" mode (it listens on 52002 by default).

WHAT TO EXPECT. A drone sends a DroneID burst roughly every 600 ms and moves it
between channels. MicroPhase's E200 watches 61.44 MHz at once and predicts the
hops; this program watches one ~10 MHz channel at a time, so it catches the
bursts that land on the channel it is on: roughly one in (number of channels).
Narrow the plan with --band or --freqs once you know where your drones transmit.

Rates: 11.52 MSPS (default) is what one receive channel sustains over Ethernet
(~46 MB/s); 15.36 MSPS gives more margin at the band edges if your link keeps
up. Buffers that arrive late are dropped whole, never stitched, so every buffer
decoded is contiguous; the stats line counts what was dropped.

Exit 0 on a clean stop (Ctrl-C, --duration), 1 if the board cannot be reached
or configured, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import queue
import socket
import sys
import threading
import time

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "selftest"))
import ocusync                                                       # noqa: E402
from iiod_min import Iiod, IiodError, mask_for                       # noqa: E402

PHY = "ad9361-phy"
RX = "cf-ad9361-lpc"
FULL_SCALE = 2048.0          # 12-bit samples, sign-extended into int16

# DroneID channel centres, MHz. 2.4 GHz: those proto17/dji_droneid observed
# (a 15 MHz raster). 5.8 GHz: proto17's plus the centres MicroPhase's own
# decoder tunes to (5736.5 and 5816.5, read out of drone_dji_rid_decode).
BANDS = {
    "2.4": [2399.5, 2414.5, 2429.5, 2444.5, 2459.5],
    "5.8": [5736.5, 5756.5, 5776.5, 5796.5, 5816.5],
}


def to_complex(iq: np.ndarray) -> np.ndarray:
    """Interleaved int16 I/Q -> complex64 at +-1.0 full scale. Going through a
    float32 view is ~8x faster than building I + jQ, which matters on a Pi."""
    return (iq.astype(np.float32) * (1.0 / FULL_SCALE)).view(np.complex64)


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------- the board
class Board(Iiod):
    """iiod_min plus a long-lived receive buffer."""

    def __init__(self, uri: str, timeout: float = 15.0):
        host, port = uri[3:] if uri.startswith("ip:") else uri, 30431
        if ":" in host and not host.startswith("["):
            host, _, p = host.rpartition(":")
            port = int(p)
        super().__init__(host, port, timeout)
        self.connect()
        self.did, self.nscan = self.devices()[RX]
        self.open_n = 0

    def num(self, dev, ch, attr, output=False):
        return float(self.read(dev, ch, attr, output).split()[0])

    def configure(self, rate, bandwidth, gain, ch):
        """Receive side only. Returns what the board reports back."""
        v = f"voltage{ch}"
        self.write(PHY, v, "sampling_frequency", str(int(rate)))
        # The fabric must not decimate: the decoder needs the converter rate.
        fab = self.num(RX, "voltage0", "sampling_frequency")
        if abs(fab - rate) > 1:
            self.write(RX, "voltage0", "sampling_frequency", str(int(rate)))
        self.write(PHY, v, "rf_bandwidth", str(int(bandwidth)))
        if gain in ("fast_attack", "slow_attack", "hybrid"):
            self.write(PHY, v, "gain_control_mode", gain)
        else:
            self.write(PHY, v, "gain_control_mode", "manual")
            self.write(PHY, v, "hardwaregain", str(float(gain)))
        got = {
            "rate": self.num(PHY, v, "sampling_frequency"),
            "fabric_rate": self.num(RX, "voltage0", "sampling_frequency"),
            "bandwidth": self.num(PHY, v, "rf_bandwidth"),
            "gain_mode": self.read(PHY, v, "gain_control_mode"),
        }
        return got

    def tune(self, hz):
        self.write(PHY, "altvoltage0", "frequency", str(int(hz)), output=True)
        return self.num(PHY, "altvoltage0", "frequency", output=True)

    def gain_db(self, ch):
        try:
            return self.num(PHY, f"voltage{ch}", "hardwaregain")
        except (OSError, ValueError):
            return float("nan")

    def open_rx(self, nsamples, ch):
        mask = mask_for([2 * ch, 2 * ch + 1], self.nscan)
        cmd = f"OPEN {self.did} {nsamples} {mask}"
        self._send(cmd)
        self._status(cmd)
        self.open_n = nsamples

    def read_rx(self, out: np.ndarray):
        """Fill out (int16, 2 x open_n) with one buffer: one contiguous capture."""
        want = out.nbytes
        cmd = f"READBUF {self.did} {want}"
        self._send(cmd)
        view = memoryview(out).cast("B")
        got = 0
        while got < want:
            n = self._status(cmd)
            if n == 0:
                raise ConnectionError("IIOD returned an empty buffer")
            self._f.readline()                       # channel-mask echo
            end = got + n
            while got < end:
                r = self._f.readinto(view[got:end])
                if not r:
                    raise ConnectionError(f"IIOD sent {got} of {want} bytes")
                got += r

    def close_rx(self):
        self.close_buffer(self.did)
        self.open_n = 0


# --------------------------------------------------------------- sinks
class DjiReceiverSink:
    """A TCP client for dji_receiver.py's "new firmware" port, as an AntSDR
    running drone_dji_rid_decode would connect: one dji_O line per frame."""

    def __init__(self, host, port, id_only=False):
        self.addr = (host, port)
        self.id_only = id_only
        self.sock = None
        self.lock = threading.Lock()
        self.last = 0.0
        self.sent = 0
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _connect(self):
        if self.sock is None:
            try:
                self.sock = socket.create_connection(self.addr, timeout=3)
                log(f"connected to dji_receiver at {self.addr[0]}:{self.addr[1]}")
            except OSError as e:
                if time.time() - self.last > 30:
                    log(f"dji_receiver at {self.addr[0]}:{self.addr[1]} not reachable ({e}); will retry")
                    self.last = time.time()
                self.sock = None
        return self.sock

    def _send(self, line):
        with self.lock:
            for _ in range(2):
                s = self._connect()
                if s is None:
                    return False
                try:
                    s.sendall(line.encode())
                    return True
                except OSError:
                    try:
                        s.close()
                    finally:
                        self.sock = None
            return False

    def _heartbeat(self):
        while True:
            time.sleep(30)
            self._send("=\n")

    @staticmethod
    def line(d):
        """The dji_O CSV dji_receiver.py parses (parse_new_fw_line).

        Plaintext telemetry: it multiplies the first height field by 10 and
        divides speeds by 100, so altitude goes out in tens of metres and
        speeds in cm/s, which is what the record carries. The protocol field
        is left empty: a DroneID frame does not say whether the link is
        OcuSync 2 or 3.

        O4: protocol 4 and "dji(<session hash>)" with no serial, exactly the
        shape MicroPhase's O4 firmware sends, which dji_receiver.py turns into
        "drone-alert-<hash>" / "DJI Encrypted (O4)" at the sensor's position."""
        if d.get("generation") == "O4":
            return ("dji_O,4,{f:.1f},{r},dji({h}),,0.0,0.0,0.0,0.0,0.0,0.0,0|0,0|0|0;\n").format(
                f=d["freq_mhz"], r=int(round(d.get("rssi_db", 0))), h=int(d["hashcode"], 16))
        model = f"{d.get('product', 'DJI')}({d.get('product_type', 0)})"
        return ("dji_O,,{f:.1f},{r},{m},{sn},{lon:.7f},{lat:.7f},{plon:.7f},{plat:.7f},"
                "{hlon:.7f},{hlat:.7f},{alt:.3f}|{h:.2f},{ve}|{vn}|{vu};\n").format(
            f=d["freq_mhz"], r=int(round(d.get("rssi_db", 0))), m=model.replace(",", " "),
            sn=d.get("serial_number", "").replace(",", " "),
            lon=d.get("longitude", 0.0), lat=d.get("latitude", 0.0),
            plon=d.get("app_longitude", 0.0), plat=d.get("app_latitude", 0.0),
            hlon=d.get("home_longitude", 0.0), hlat=d.get("home_latitude", 0.0),
            alt=d.get("altitude_m", 0.0) / 10.0, h=d.get("height_m", 0.0),
            ve=d.get("v_east_cms", 0), vn=d.get("v_north_cms", 0), vu=d.get("v_up_cms", 0))

    def emit(self, d):
        """Send what dji_receiver.py can use: CRC-valid plaintext telemetry and
        O4 detections always; serial-only frames with --report-id-only, since
        they carry no position and would land at 0,0."""
        if not d.get("record_crc_ok"):
            return
        if d.get("generation") in ("O2/O3", "O4"):
            pass
        elif not (self.id_only and d.get("serial_number")):
            return
        if self._send(self.line(d)):
            self.sent += 1


# --------------------------------------------------------------- sources
def file_source(path, rate, fmt, freq_mhz, chunk):
    """Yield (freq_mhz, gain_db, complex64 chunk) from a recording."""
    meta = None
    if path.endswith(".sigmf-meta") or path.endswith(".sigmf-data"):
        base = path.rsplit(".", 1)[0]
        with open(base + ".sigmf-meta") as f:
            meta = json.load(f)
        path = base + ".sigmf-data"
        g = meta["global"]
        rate = rate or g.get("core:sample_rate")
        dt = g.get("core:datatype", "ci16_le")
        fmt = fmt or ("cf32" if dt.startswith("cf32") else "ci16")
        caps = meta.get("captures") or [{}]
        if freq_mhz is None and caps[0].get("core:frequency"):
            freq_mhz = caps[0]["core:frequency"] / 1e6
    if not rate:
        sys.exit("--file-rate is required for a raw recording")
    fmt = fmt or "cf32"
    dtype = np.complex64 if fmt == "cf32" else np.int16
    data = np.memmap(path, dtype=dtype, mode="r")
    step = chunk if fmt == "cf32" else 2 * chunk
    resample = None
    try:
        ocusync.Numerology.for_rate(rate)
        out_rate = rate
    except ValueError:
        out_rate = 15.36e6 if rate >= 15.36e6 else 11.52e6
        from fractions import Fraction
        try:
            from scipy.signal import resample_poly
        except ImportError:
            sys.exit(f"{rate / 1e6:g} MSPS needs resampling, which needs scipy (pip install scipy)")
        fr = Fraction(out_rate / rate).limit_denominator(10000)
        resample = (resample_poly, fr.numerator, fr.denominator)
        log(f"recording at {rate / 1e6:g} MSPS: resampling to {out_rate / 1e6:g} MSPS")
    yield out_rate
    for i in range(0, len(data), step):
        c = np.asarray(data[i:i + step])
        if fmt == "ci16":
            c = to_complex(c)
        if resample:
            c = resample[0](c, resample[1], resample[2]).astype(np.complex64)
        yield (freq_mhz or 0.0, 0.0, c)


def board_reader(board, args, plan, work, stop, counters):
    """Hop, stream, and queue buffers for the decoder; never blocks on it."""
    buf = np.empty(2 * args.buffer, dtype=np.int16)
    while not stop.is_set():
        for mhz in plan:
            if stop.is_set():
                return
            try:
                got = board.tune(mhz * 1e6)
                # A retune reaches the samples only through a fresh buffer: the
                # old one still holds samples from the previous channel.
                board.open_rx(args.buffer, args.rx_channel)
                gain = board.gain_db(args.rx_channel)
                end = time.monotonic() + args.dwell
                first = True
                while time.monotonic() < end and not stop.is_set():
                    board.read_rx(buf)
                    counters["buffers"] += 1
                    if first:                     # may straddle the retune
                        first = False
                        continue
                    try:
                        work.put_nowait((got / 1e6, gain, buf.copy()))
                    except queue.Full:
                        counters["dropped"] += 1
                board.close_rx()
            except (OSError, IiodError, ConnectionError) as e:
                log(f"board: {e}; reconnecting in 3 s")
                counters["errors"] += 1
                try:
                    board.close()
                except OSError:
                    pass
                time.sleep(3)
                try:
                    board.connect()
                    board._devices = None
                    board.did, board.nscan = board.devices()[RX]
                except OSError as e2:
                    log(f"board: reconnect failed: {e2}")


# --------------------------------------------------------------- main
def total_stats(receivers):
    out = {"cp_candidates": 0, "crc_ok": 0, "crc_fail": 0}
    for r in receivers.values():
        for k in out:
            out[k] += r.stats[k]
    return out


def frame_dict(frame, freq_mhz, gain_db):
    d = frame.as_dict()
    d["freq_mhz"] = round(freq_mhz + frame.cfo_hz / 1e6, 4)
    d["time"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    # power at the converter minus the receive gain: a level relative to the
    # antenna port, uncalibrated (no absolute reference has been measured)
    if gain_db == gain_db:                      # not NaN
        d["rssi_db"] = round(d.get("power_dbfs", 0) - gain_db, 1)
    return d


def describe(d):
    crc = "" if d.get("record_crc_ok") else "  RECORD CRC FAILED"
    t = d.get("msg_type")
    if d.get("generation") == "O2/O3":
        pos = (f"{d['latitude']:.5f},{d['longitude']:.5f}" if d["latitude"] or d["longitude"]
               else "no GPS fix")
        return (f"{d['freq_mhz']:.1f} MHz  {d['product']}  {d['serial_number']}  {pos}"
                f"  alt {d['altitude_m']:.0f} m  SNR {d['snr_db']:.0f} dB  {d['decoder']}{crc}")
    if d.get("generation") == "O4":
        return (f"{d['freq_mhz']:.1f} MHz  DJI O4 (encrypted)  {d['marker']}  session "
                f"{d['hashcode']}  SNR {d['snr_db']:.0f} dB{crc}")
    return (f"{d['freq_mhz']:.1f} MHz  type 0x{t:02x} frame"
            f"  serial {d.get('serial_number') or '?'}  SNR {d['snr_db']:.0f} dB{crc}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("source")
    src.add_argument("--uri", help="board, e.g. ip:fishball.local (default: tools/board_addr.py)")
    src.add_argument("--file", help="decode a recording instead (.sigmf-meta, or raw with --file-rate)")
    src.add_argument("--file-rate", type=float, help="sample rate of a raw recording")
    src.add_argument("--file-format", choices=("cf32", "ci16"), help="raw recording format")
    src.add_argument("--file-freq", type=float, help="centre frequency of the recording, MHz")
    rf = ap.add_argument_group("radio")
    rf.add_argument("--rate", type=float, default=11.52e6, help="sample rate (default 11.52e6)")
    rf.add_argument("--band", choices=("2.4", "5.8", "all"), default="all")
    rf.add_argument("--freqs", help="comma-separated channel centres in MHz (overrides --band)")
    rf.add_argument("--dwell", type=float, default=1.3, help="seconds per channel (default 1.3)")
    rf.add_argument("--gain", default="fast_attack",
                    help="fast_attack (default), slow_attack, hybrid, or a manual gain in dB")
    rf.add_argument("--bandwidth", type=float, default=10e6, help="analog bandwidth (default 10e6)")
    rf.add_argument("--rx-channel", type=int, choices=(0, 1), default=0, help="RX1 (0) or RX2 (1)")
    rf.add_argument("--buffer", type=int, default=1 << 20, help="samples per buffer (default 1 Mi)")
    out = ap.add_argument_group("output")
    out.add_argument("--json", action="store_true", help="one JSON object per frame on stdout")
    out.add_argument("--dji-receiver", metavar="HOST:PORT",
                     help="send dji_O lines to dji_receiver.py, e.g. 127.0.0.1:52002")
    out.add_argument("--report-id-only", action="store_true",
                     help="also send frames that carry a serial but no position (they land at 0,0)")
    out.add_argument("--save-failed", metavar="DIR",
                     help="save the IQ of bursts that were found but did not decode")
    out.add_argument("--stats", type=float, default=60, help="seconds between stats lines")
    out.add_argument("--duration", type=float, help="stop after this many seconds")
    args = ap.parse_args()

    try:
        ocusync.Numerology.for_rate(args.rate)
    except ValueError as e:
        ap.error(str(e))
    plan = ([float(f) for f in args.freqs.split(",")] if args.freqs
            else BANDS["2.4"] + BANDS["5.8"] if args.band == "all" else BANDS[args.band])

    sink = None
    if args.dji_receiver:
        host, _, port = args.dji_receiver.rpartition(":")
        sink = DjiReceiverSink(host or "127.0.0.1", int(port), args.report_id_only)
    if args.save_failed:
        os.makedirs(args.save_failed, exist_ok=True)

    counters = {"buffers": 0, "dropped": 0, "errors": 0, "frames": 0}
    stop = threading.Event()
    work = queue.Queue(maxsize=8)

    if args.file:
        gen = file_source(args.file, args.file_rate, args.file_format, args.file_freq, args.buffer)
        rate = next(gen)
        rx = None

        def feeder():
            for item in gen:
                work.put(item)
                counters["buffers"] += 1
            work.put(None)
        threading.Thread(target=feeder, daemon=True).start()
    else:
        from board_addr import uri as board_uri
        uri = args.uri or board_uri()
        try:
            board = Board(uri)
            got = board.configure(args.rate, args.bandwidth, args.gain, args.rx_channel)
        except (OSError, IiodError, KeyError) as e:
            log(f"cannot use the board at {uri}: {e}")
            return 1
        if abs(got["rate"] - args.rate) > 1 or abs(got["fabric_rate"] - args.rate) > 1:
            log(f"the board runs at {got['rate']:.0f} S/s (fabric {got['fabric_rate']:.0f}), "
                f"not {args.rate:.0f}: refusing to decode at the wrong rate")
            return 1
        rate = args.rate
        log(f"board {uri}: {got['rate'] / 1e6:g} MSPS, {got['bandwidth'] / 1e6:g} MHz, "
            f"gain {got['gain_mode']}; {len(plan)} channels, {args.dwell:g} s each")
        rx = None
        threading.Thread(target=board_reader, args=(board, args, plan, work, stop, counters),
                         daemon=True).start()

    receivers = {}
    t0 = time.monotonic()
    last_stats = t0
    try:
        while True:
            try:
                item = work.get(timeout=0.5)
            except queue.Empty:
                item = False
            if item is None:
                break
            if item:
                freq, gain, data = item
                x = data if np.iscomplexobj(data) else to_complex(data)
                # One receiver per channel: each keeps its own noise floor, which
                # differs from channel to channel once the AGC has its say.
                rx = receivers.get(freq)
                if rx is None:
                    rx = receivers[freq] = ocusync.Receiver(rate)
                failed = [] if args.save_failed else None
                for fr in rx.process(x, failed=failed):
                    d = frame_dict(fr, freq, gain)
                    counters["frames"] += 1
                    log(describe(d))
                    if args.json:
                        print(json.dumps(d), flush=True)
                    if sink:
                        sink.emit(d)
                for start, fmt in failed or []:
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    seg = x[max(0, start - 2000):start + 12000]
                    base = os.path.join(args.save_failed, f"{stamp}_{freq:.1f}MHz_{rx.fs / 1e6:g}Msps")
                    seg.astype(np.complex64).tofile(base + ".cf32")
            now = time.monotonic()
            if now - last_stats >= args.stats:
                last_stats = now
                s = total_stats(receivers)
                log(f"stats: {counters['buffers']} buffers ({counters['dropped']} dropped by the decoder), "
                    f"{s['cp_candidates']} candidates, {s['crc_ok']} decoded, {s['crc_fail']} not decodable"
                    + (f", {sink.sent} sent to dji_receiver" if sink else ""))
            if args.duration and now - t0 > args.duration:
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    s = total_stats(receivers)
    log(f"done: {counters['frames']} frames; {s['cp_candidates']} candidates, "
        f"{s['crc_fail']} found but not decodable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
