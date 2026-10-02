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
import fpv                                                           # noqa: E402
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

# Analog FPV video: ranges swept in 10 MHz steps (MHz). 5.8 GHz is every
# standard band (A/B/E/F/R) plus margin; 1.2 GHz the common 1080-1360 set;
# 3.3 GHz the 3.1-3.5 GHz range low-band VTXs are sold for. "L" (5362-5621)
# is in the 5.8 range of some goggles; add it with --video-bands 5.3.
VIDEO_RANGES = {"5.8": (5640, 5950), "5.3": (5355, 5630), "1.2": (1075, 1365),
                "3.3": (3100, 3500)}
# ExpressLRS: tuning points whose ~11 MHz windows cover each hopping domain.
ELRS_TUNES = {"2.4": [2405.0 + 10 * i for i in range(8)],          # 2400.4-2479.4
              "915": [909.3, 920.9],                                # FCC915 903.5-926.9
              "868": [866.4]}                                       # EU868 863.3-869.6


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
            # serial and positions exist only after a DragonScope lookup
            proto, model = "4", f"dji({int(d['hashcode'], 16)})"
            sn = d.get("serial_number", "") if d.get("decrypted") else ""
        else:
            proto = ""
            model = f"{d.get('product', 'DJI')}({d.get('product_type', 0)})"
            sn = d.get("serial_number", "")
        return ("dji_O,{p},{f:.1f},{r},{m},{sn},{lon:.7f},{lat:.7f},{plon:.7f},{plat:.7f},"
                "{hlon:.7f},{hlat:.7f},{alt:.3f}|{h:.2f},{ve}|{vn}|{vu};\n").format(
            p=proto, f=d["freq_mhz"], r=int(round(d.get("rssi_db", 0))), m=model.replace(",", " "),
            sn=sn.replace(",", " "),
            lon=d.get("longitude", 0.0), lat=d.get("latitude", 0.0),
            plon=d.get("app_longitude", 0.0), plat=d.get("app_latitude", 0.0),
            hlon=d.get("home_longitude", 0.0), hlat=d.get("home_latitude", 0.0),
            alt=d.get("altitude_m", 0.0) / 10.0, h=d.get("height_m", 0.0),
            ve=d.get("v_east_cms", 0), vn=d.get("v_north_cms", 0), vu=d.get("v_up_cms", 0))

    def alert(self, d):
        """An FPV video or LoRa/ExpressLRS detection, as a dji_O line that
        dji_receiver.py files as a "drone-alert-..." and places at the
        sensor's own position (it does that for any id starting with
        drone-alert). The model field carries what was detected."""
        if self._send(("dji_O,,{f:.1f},{r},{m}(0),drone-alert-{tag},0.0,0.0,0.0,0.0,0.0,0.0,"
                       "0|0,0|0|0;\n").format(f=d["freq_mhz"], r=int(round(d.get("rssi_db", 0))),
                                               m=d["label"].replace(",", " "), tag=d["tag"])):
            self.sent += 1

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


class FpvZmqSink:
    """Publish analog-video detections the way WarDragon's FPV scanner does.

    alphafox02/wardragon-fpv-detect (fpv_energy_scan.py) binds an XPUB on
    tcp://127.0.0.1:4226 and sends a JSON list: Basic ID ("fpv-alert-<MHz>"),
    Self-ID, Frequency Message and Signal Info (source "confirm", center_hz,
    bandwidth_hz, pal_conf, ntsc_conf on a 0-100 scale, rssi). DragonSync's
    FPV ingest (fpv_enabled = true, fpv_zmq_port = 4226) subscribes to it and
    draws an FPV marker beside the kit, taking the kit's position from the
    WarDragon monitor. Their "confirm" comes from a licensed suscli fpvdet
    plugin; here it comes from fpv.detect_video's line-rate comb.

    Confidence: fpvdet's publish threshold is 60, so the comb score maps to
    0 at the noise level (3 dB), 60 at the detection threshold (9 dB), and
    rises 2 points per dB to 100.
    Location is left out: DragonSync falls back to the kit's own GPS.
    Needs pyzmq (sudo apt install python3-zmq)."""

    def __init__(self, endpoint):
        import zmq
        self.zmq = zmq
        self.sock = zmq.Context.instance().socket(zmq.XPUB)
        self.sock.setsockopt(zmq.XPUB_VERBOSE, True)
        # Never hold the process open at exit for alerts nobody is reading.
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.bind(endpoint)
        self.sent = 0

    @staticmethod
    def conf(score_db):
        """Comb score (dB) -> 0-100: noise (<= 3 dB) 0, the detection
        threshold (9 dB) 60, then 2 points per dB up to 100."""
        if score_db <= 3.0:
            return 0.0
        if score_db <= 9.0:
            return round(10.0 * (score_db - 3.0), 1)
        return round(min(100.0, 60.0 + 2.0 * (score_db - 9.0)), 1)

    @classmethod
    def message(cls, a):
        # The carrier estimate wanders with the picture (it is the mean FM
        # frequency); DragonSync names the marker after the frequency, so use
        # the channel's nominal frequency, or whole MHz, to keep one marker.
        if a.get("channel"):
            hz = float(a["channel"].split()[1]) * 1e6
        else:
            hz = round(a["freq_mhz"]) * 1e6
        sig = {"source": "confirm", "center_hz": hz, "bandwidth_hz": 20e6,
               "pal_conf": cls.conf(a["score_db"]) if a["standard"] == "PAL" else cls.conf(a["other_db"]),
               "ntsc_conf": cls.conf(a["score_db"]) if a["standard"] == "NTSC" else cls.conf(a["other_db"])}
        if a.get("rssi_db") is not None:
            sig["rssi"] = a["rssi_db"]
        return [
            {"Basic ID": {"id_type": "Serial Number (ANSI/CTA-2063-A)",
                          "id": f"fpv-alert-{hz / 1e6:.3f}MHz", "description": "FPV Signal"}},
            {"Self-ID Message": {"text": f"FPV alert (confirm) {a['standard']}"
                                         + (f" {a['channel']}" if a.get("channel") else "")}},
            {"Frequency Message": {"frequency": hz}},
            {"Signal Info": sig},
        ]

    def publish(self, a):
        try:
            self.sock.send_string(json.dumps(self.message(a)), self.zmq.NOBLOCK)
            self.sent += 1
        except self.zmq.ZMQError:
            pass


class DragonScope:
    """Ask a DragonScope proxy to decrypt O4 packets.

    DragonScope is CEMAXecuter's licensed O4 service for WarDragon kits: a
    proxy on the WarDragon (dragonscope.py, port 80) forwards a packet's hex
    to their remote service, which answers with the drone's serial and
    position. MicroPhase's DragonScope firmware sends it every CRYP/INFP packet
    it receives; this sends the same packets, the logical packet as hex, to
    GET /api/o4online/decrypt?hex=. Without a license the proxy answers
    {"sn": ""} and nothing changes. This is a client only: the decryption,
    and the key that makes it possible, are DragonScope's.

    The reply's field names are taken from dragonscope.py (sn, lat, lon) and
    dji_receiver.py's proxy code; anything else in the reply is ignored."""

    KEYS = {"serial_number": ("sn", "serial"),
            "latitude": ("lat", "drone_lat", "latitude"),
            "longitude": ("lon", "drone_lon", "longitude"),
            "app_latitude": ("pilot_lat", "app_lat"), "app_longitude": ("pilot_lon", "app_lon"),
            "home_latitude": ("home_lat",), "home_longitude": ("home_lon",),
            "altitude_m": ("alt", "altitude"), "height_m": ("height_agl", "height")}

    def __init__(self, url, on_result, interval=1.0):
        self.url = url.rstrip("/")
        self.on_result = on_result
        self.interval = interval
        self.q = queue.Queue(maxsize=32)
        self.last = {}
        self.asked = self.answered = 0
        self.warned = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, d):
        key = (d["hashcode"], d["marker"])
        now = time.monotonic()
        if now - self.last.get(key, -1e9) < self.interval:
            return
        self.last[key] = now
        try:
            self.q.put_nowait(dict(d))
        except queue.Full:
            pass

    @classmethod
    def fields(cls, reply):
        out = {}
        for ours, theirs in cls.KEYS.items():
            for k in theirs:
                v = reply.get(k)
                if v in (None, ""):
                    continue
                if ours == "serial_number":
                    out[ours] = str(v)
                else:
                    try:
                        out[ours] = float(v)
                    except (TypeError, ValueError):
                        continue
                break
        return out

    def _run(self):
        import urllib.request
        while True:
            d = self.q.get()
            url = f"{self.url}/api/o4online/decrypt?hex={d['packet_hex']}"
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    reply = json.loads(r.read() or b"{}")
            except Exception as e:                       # noqa: BLE001 - any network failure
                if time.monotonic() - self.warned > 60:
                    log(f"DragonScope at {self.url} did not answer ({e})")
                    self.warned = time.monotonic()
                continue
            self.asked += 1
            got = self.fields(reply if isinstance(reply, dict) else {})
            if got.get("serial_number"):
                self.answered += 1
                d.update(got)
                d["decrypted"] = True
                self.on_result(d)


# --------------------------------------------------------------- sources
def file_source(path, rate, fmt, freq_mhz, chunk, kind="droneid"):
    """Yield (freq_mhz, gain_db, complex64 chunk, kind) from a recording."""
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
        if kind == "droneid":           # the other detectors take any rate
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
        yield (freq_mhz or 0.0, 0.0, c, kind)


def build_plan(args):
    """[(MHz, kind, dwell s, buffer samples)] for one sweep.

    DroneID needs a long dwell (a burst every ~600 ms) and a big buffer; video
    is continuous, so a short dwell and buffer find it; ExpressLRS sends a
    packet every 2-40 ms, so a quarter second catches several."""
    kinds = set(args.scan.split(","))
    bad = kinds - {"droneid", "video", "elrs"}
    if bad:
        raise ValueError(f"--scan: unknown {', '.join(sorted(bad))}")
    plan = []
    if "droneid" in kinds:
        freqs = ([float(f) for f in args.freqs.split(",")] if args.freqs
                 else BANDS["2.4"] + BANDS["5.8"] if args.band == "all" else BANDS[args.band])
        plan += [(f, "droneid", args.dwell, args.buffer) for f in freqs]
    if "video" in kinds:
        for b in args.video_bands.split(","):
            lo, hi = (VIDEO_RANGES[b] if b in VIDEO_RANGES
                      else tuple(float(v) for v in b.split("-")))
            f = lo + 5.0
            while f <= hi - 5.0 + 1e-6:
                plan.append((f, "video", 0.12, 1 << 19))
                f += 10.0
    if "elrs" in kinds:
        for b in args.elrs_bands.split(","):
            if b in ELRS_TUNES:
                tunes = ELRS_TUNES[b]
            else:                       # LO-HI MHz: links re-tuned off the usual bands
                lo, hi = (float(v) for v in b.split("-"))
                tunes = list(np.arange(lo + 5.0, hi - 5.0 + 1e-6, 10.0)) or [(lo + hi) / 2]
            plan += [(float(f), "elrs", 0.25, 1 << 20) for f in tunes]   # 91 ms buffers
    return plan


def board_reader(board, args, plan, work, stop, counters):
    """Hop, stream, and queue buffers for the decoder; never blocks on it."""
    bufs = {}
    while not stop.is_set():
        for mhz, kind, dwell, nbuf in plan:
            if stop.is_set():
                return
            buf = bufs.setdefault(nbuf, np.empty(2 * nbuf, dtype=np.int16))
            try:
                got = board.tune(mhz * 1e6)
                # A retune reaches the samples only through a fresh buffer: the
                # old one still holds samples from the previous channel.
                board.open_rx(nbuf, args.rx_channel)
                gain = board.gain_db(args.rx_channel)
                end = time.monotonic() + dwell
                first = True
                # Video and ExpressLRS are on the air continuously: one buffer
                # per tuning point finds them, and their detectors cost more per
                # buffer than DroneID's, so never let them crowd it out.
                left = 10 ** 9 if kind == "droneid" else 1
                while time.monotonic() < end and left and not stop.is_set():
                    board.read_rx(buf)
                    counters["buffers"] += 1
                    if first:                     # may straddle the retune
                        first = False
                        continue
                    left -= 1
                    try:
                        work.put_nowait((got / 1e6, gain, buf.copy(), kind))
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
    if d.get("generation") == "O4":
        d["packet_hex"] = frame.raw[:d["pkt_len"]].hex()
    return d


def video_alert(v, mhz, gain_db):
    """dict for an analog video detection tuned at mhz."""
    f = mhz + v["offset_hz"] / 1e6
    ch = fpv.nearest_video_channel(f, tol=6.0)
    name = ch.split()[0] if ch else f"{f:.0f}"
    d = dict(v, freq_mhz=round(f, 2), channel=ch, tuned_mhz=mhz,
             label=f"Analog FPV video {v['standard']}", tag=f"fpv-video-{name}")
    if gain_db == gain_db:
        d["rssi_db"] = round(v["power_dbfs"] - gain_db, 1)
    return d


def lora_alert(found, mhz):
    """dict for the strongest LoRa detection tuned at mhz, or None."""
    if not found:
        return None
    b = max(found, key=lambda f: (f["preambles"], f["par_db"]))
    band = b["band"]
    elrs = band == "2.4" or b["hopping"]          # 812.5 kHz LoRa at 2.4 GHz is ELRS's own mode
    label = (f"ExpressLRS {'2.4G' if band == '2.4' else '900'} SF{b['sf']}" if elrs
             else f"LoRa {b['bw_hz'] / 1e3:g}k SF{b['sf']}")
    return dict(b, freq_mhz=mhz, tuned_mhz=mhz, elrs=elrs, label=label,
                tag=f"elrs-{band}" if elrs else f"lora-{mhz:.0f}")


def describe(d):
    if d.get("kind") == "analog_video":
        return (f"{d['freq_mhz']:.1f} MHz  ANALOG FPV VIDEO {d['standard']}"
                f"{'  ' + d['channel'] if d.get('channel') else ''}  comb {d['score_db']} dB")
    if d.get("kind") == "lora":
        return f"{fpv.describe_lora(d, d['tuned_mhz'])}  [{d['label']}]"
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
    rf.add_argument("--scan", default="droneid",
                    help="what to look for, comma-separated: droneid, video, elrs (default droneid)")
    rf.add_argument("--video-bands", default="5.8",
                    help="analog video ranges: 5.8, 5.3, 1.2, 3.3 or LO-HI in MHz (default 5.8)")
    rf.add_argument("--elrs-bands", default="2.4,915",
                    help="ExpressLRS bands: 2.4, 915, 868, or LO-HI in MHz (default 2.4,915)")
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
    out.add_argument("--fpv-zmq", nargs="?", const="tcp://127.0.0.1:4226", metavar="ENDPOINT",
                     help="publish analog-video detections for DragonSync's FPV ingest, as "
                          "WarDragon's FPV scanner does (default tcp://127.0.0.1:4226)")
    out.add_argument("--dragonscope", metavar="URL",
                     help="ask a DragonScope proxy to decrypt O4 packets, e.g. http://127.0.0.1")
    out.add_argument("--alert-interval", type=float, default=5.0,
                     help="seconds between repeated video/ExpressLRS alerts for one channel")
    out.add_argument("--save-failed", metavar="DIR",
                     help="save the IQ of bursts that were found but did not decode")
    out.add_argument("--stats", type=float, default=60, help="seconds between stats lines")
    out.add_argument("--duration", type=float, help="stop after this many seconds")
    args = ap.parse_args()

    try:
        if "droneid" in args.scan.split(","):
            ocusync.Numerology.for_rate(args.rate)
        plan = build_plan(args)
    except (ValueError, KeyError) as e:
        ap.error(str(e))

    sink = None
    if args.dji_receiver:
        host, _, port = args.dji_receiver.rpartition(":")
        sink = DjiReceiverSink(host or "127.0.0.1", int(port), args.report_id_only)

    def decrypted(d):
        log("DragonScope: " + describe(d) + f"  -> {d['serial_number']}"
            + (f" {d['latitude']:.5f},{d['longitude']:.5f}" if d.get("latitude") else ""))
        if args.json:
            print(json.dumps(d), flush=True)
        if sink:
            sink.emit(d)
    scope = DragonScope(args.dragonscope, decrypted) if args.dragonscope else None
    fpvpub = None
    if args.fpv_zmq:
        try:
            fpvpub = FpvZmqSink(args.fpv_zmq)
        except ImportError:
            ap.error("--fpv-zmq needs pyzmq: sudo apt install python3-zmq")
        except Exception as e:                       # noqa: BLE001 - bind failures
            ap.error(f"--fpv-zmq: cannot bind {args.fpv_zmq} ({e}); is WarDragon's "
                     "fpv-receiver service running? Stop it, or use another port")
    if args.save_failed:
        os.makedirs(args.save_failed, exist_ok=True)

    counters = {"buffers": 0, "dropped": 0, "errors": 0, "frames": 0, "video": 0, "lora": 0}
    stop = threading.Event()
    work = queue.Queue(maxsize=8)

    if args.file:
        kind = args.scan.split(",")[0]
        gen = file_source(args.file, args.file_rate, args.file_format, args.file_freq,
                          args.buffer, kind)
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
            f"gain {got['gain_mode']}; {len(plan)} tuning points ({args.scan}), "
            f"{sum(p[2] for p in plan):.1f} s per sweep")
        rx = None
        threading.Thread(target=board_reader, args=(board, args, plan, work, stop, counters),
                         daemon=True).start()

    receivers = {}
    alerted = {}
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
                freq, gain, data, kind = item
                x = data if np.iscomplexobj(data) else to_complex(data)
                if kind != "droneid":
                    if kind == "video":
                        v = fpv.detect_video(x, rate)
                        a = video_alert(v, freq, gain) if v else None
                    else:
                        a = lora_alert(fpv.detect_lora(x, rate, "2.4" if freq > 1500 else "900"), freq)
                    if a:
                        counters["video" if kind == "video" else "lora"] += 1
                        now = time.monotonic()
                        if now - alerted.get(a["tag"], -1e9) >= args.alert_interval:
                            alerted[a["tag"]] = now
                            a["time"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
                                timespec="milliseconds")
                            log(describe(a))
                            if args.json:
                                print(json.dumps(a), flush=True)
                            if kind == "video" and fpvpub:
                                fpvpub.publish(a)            # DragonSync's own FPV path
                            elif sink and (kind == "video" or a.get("elrs")):
                                sink.alert(a)
                    continue
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
                    if scope and d.get("generation") == "O4" and d.get("record_crc_ok"):
                        scope.submit(d)
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
                    + (f", video {counters['video']}, LoRa {counters['lora']}"
                       if args.scan != "droneid" else "")
                    + (f", DragonScope {scope.answered}/{scope.asked}" if scope else "")
                    + (f", {sink.sent} sent to dji_receiver" if sink else ""))
            if args.duration and now - t0 > args.duration:
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    s = total_stats(receivers)
    log(f"done: {counters['frames']} DroneID frames; {s['cp_candidates']} candidates, "
        f"{s['crc_fail']} found but not decodable; video {counters['video']}, "
        f"LoRa {counters['lora']} detections")
    return 0


if __name__ == "__main__":
    sys.exit(main())
