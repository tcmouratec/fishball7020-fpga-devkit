#!/usr/bin/env python3
"""End-to-end test of droneid_rx.py against a fake board and a fake dji_receiver.

    # run from: the repo root
    python3 tools/droneid/test_droneid_rx.py

The fake board speaks IIOD the way tools/selftest/iiod_min.py documents it
(the same client the self-test uses on real hardware), and puts a DroneID
burst into its samples only while tuned to 2429.5 MHz. The test checks that
droneid_rx.py configures receive only, hops, retunes through a fresh buffer,
decodes, and sends dji_receiver.py a line its parser accepts.
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_ocusync as T                                             # noqa: E402

RATE = 11.52e6
LIVE_MHZ = 2429.5         # a plaintext O2/O3 drone transmits here
O4_MHZ = 2414.5           # an encrypted O4 drone here
CONTEXT = """<?xml version="1.0" encoding="utf-8"?><context name="network">
<device id="iio:device1" name="ad9361-phy"><channel id="voltage0" type="input"/>
<channel id="altvoltage0" type="output"/></device>
<device id="iio:device3" name="cf-ad9361-lpc">
<channel id="voltage0" type="input"><scan-element index="0" format="le:S12/16&gt;&gt;0"/></channel>
<channel id="voltage1" type="input"><scan-element index="1" format="le:S12/16&gt;&gt;0"/></channel>
<channel id="voltage2" type="input"><scan-element index="2" format="le:S12/16&gt;&gt;0"/></channel>
<channel id="voltage3" type="input"><scan-element index="3" format="le:S12/16&gt;&gt;0"/></channel>
</device></context>"""


class FakeBoard(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.attrs = {("cf-ad9361-lpc", "voltage0", "sampling_frequency"): "30720000",
                      ("ad9361-phy", "voltage0", "hardwaregain"): "40.000000 dB"}
        self.writes = []
        self.opens = []
        o4 = T.o4_packet(b"INFP", bytes.fromhex("1a2b3c4d"), 138)
        self.bursts = {
            LIVE_MHZ: (0.25 * T.burst(T.codeword(T.record()), RATE)).astype(np.complex64),
            O4_MHZ: (0.25 * T.burst(T.codeword(o4), RATE)).astype(np.complex64),
        }
        self.rng = np.random.default_rng(1)

    def run(self):
        while True:
            c, _ = self.srv.accept()
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def samples(self, n):
        x = 0.01 * (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        lo = float(self.attrs.get(("ad9361-phy", "altvoltage0", "frequency"), 0))
        for mhz, b in self.bursts.items():
            if abs(lo - mhz * 1e6) < 1:
                for at in range(5000, n - len(b), 300000):
                    x[at:at + len(b)] += b
        iq = np.empty(2 * n, dtype=np.int16)
        iq[0::2] = np.clip(x.real * 2048, -2047, 2047)
        iq[1::2] = np.clip(x.imag * 2048, -2047, 2047)
        return iq.tobytes()

    def serve(self, c):
        try:
            self._serve(c)
        except OSError:
            pass                     # the client went away mid-command

    def _serve(self, c):
        f = c.makefile("rwb")
        n_open = 0
        while True:
            line = f.readline()
            if not line:
                return
            w = line.decode().strip().split()
            if not w:
                continue
            cmd = w[0]
            if cmd == "PRINT":
                b = CONTEXT.encode() + b"\0"
                f.write(b"%d\n" % len(b) + b + b"\n")
            elif cmd == "READ":
                key = (w[1], w[3], w[4]) if len(w) == 5 else (w[1], "", w[2])
                v = (self.attrs.get(key, "0") + "\0").encode()
                f.write(b"%d\n" % len(v) + v + b"\n")
            elif cmd == "WRITE":
                n = int(w[-1])
                v = f.read(n).rstrip(b"\0").decode()
                key = (w[1], w[3], w[4])
                self.writes.append((w[2],) + key + (v,))
                self.attrs[key] = v
                f.write(b"%d\n" % n)
            elif cmd == "OPEN":
                n_open = int(w[2])
                self.opens.append((w[1], n_open, w[3],
                                   self.attrs.get(("ad9361-phy", "altvoltage0", "frequency"))))
                f.write(b"0\n")
            elif cmd == "READBUF":
                want = int(w[2])
                data = self.samples(want // 4)
                f.write(b"%d\n00000003\n" % len(data) + data)
            elif cmd == "CLOSE":
                f.write(b"0\n")
            else:
                f.write(b"-22\n")
            f.flush()


class FakeDjiReceiver(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.lines = []

    def run(self):
        c, _ = self.srv.accept()
        buf = b""
        while True:
            d = c.recv(4096)
            if not d:
                return
            buf += d
            while b"\n" in buf:
                ln, buf = buf.split(b"\n", 1)
                self.lines.append(ln.decode())


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    return bool(cond)


def main():
    board, dji = FakeBoard(), FakeDjiReceiver()
    board.start()
    dji.start()
    cmd = [sys.executable, os.path.join(HERE, "droneid_rx.py"),
           "--uri", f"ip:127.0.0.1:{board.port}", "--freqs", f"{O4_MHZ},{LIVE_MHZ}",
           "--dwell", "0.6", "--duration", "6", "--json", "--buffer", "262144",
           "--dji-receiver", f"127.0.0.1:{dji.port}"]
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    took = time.time() - t
    ok = check(p.returncode == 0, f"exits cleanly ({took:.1f} s)")
    if p.returncode:
        print(p.stderr[-2000:])
    import json
    frames = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
    plain = [f for f in frames if f.get("generation") == "O2/O3"]
    o4 = [f for f in frames if f.get("generation") == "O4"]
    ok &= check(len(plain) >= 2 and len(o4) >= 2 and len(plain) + len(o4) == len(frames),
                f"decodes both channels ({len(plain)} plaintext, {len(o4)} O4 frames)")
    ok &= check(all(abs(f["freq_mhz"] - LIVE_MHZ) < 0.01 for f in plain)
                and all(abs(f["freq_mhz"] - O4_MHZ) < 0.01 for f in o4),
                "every frame is attributed to the channel it was on")
    ok &= check(all(f["serial_number"] == "1WNBH3900201N1" and f["record_crc_ok"] for f in plain),
                "plaintext: serial and record CRC intact")
    ok &= check(all(f["hashcode"] == "1a2b3c4d" and f["marker"] == "INFP" and f["record_crc_ok"]
                    for f in o4), "O4: marker and session hashcode read, CRC-16 valid")
    touched = {(w[1], w[2], w[3]) for w in board.writes}
    ok &= check(not any(dev == "cf-ad9361-dds-core-lpc" or ch.startswith("altvoltage1")
                        or w == "OUTPUT" and ch != "altvoltage0"
                        for w, dev, ch, _a, _v in board.writes),
                "writes no transmit attribute (only RX_LO among outputs)")
    ok &= check(("cf-ad9361-lpc", "voltage0", "sampling_frequency") in touched,
                "takes the fabric decimator out (fabric rate set to the converter rate)")
    tunes = [o[3] for o in board.opens]
    ok &= check({"2414500000", "2429500000"} <= set(tunes),
                "opens a fresh buffer after every retune, on both channels")
    time.sleep(0.5)
    dl = [ln for ln in dji.lines if ln.startswith("dji_O,")]
    ok &= check(len(dl) == len(frames), f"one dji_O line per frame ({len(dl)} for {len(frames)})")
    o4l = [ln.rstrip(";").split(",") for ln in dl if ln.startswith("dji_O,4,")]
    ok &= check(len(o4l) == len(o4) and all(p[4] == f"dji({0x1a2b3c4d})" and p[5] == ""
                                            and abs(float(p[2]) - O4_MHZ) < 0.01 for p in o4l),
                "O4 goes out as protocol 4, dji(<session hash>), no serial: MicroPhase's O4 shape")
    dl = [ln for ln in dl if not ln.startswith("dji_O,4,")]
    if dl:
        parts = dl[0].rstrip(";").split(",")
        ok &= check(len(parts) == 14 and parts[5] == "1WNBH3900201N1"
                    and re.match(r"Mavic Air 2\(58\)", parts[4])
                    and abs(float(parts[7]) - 51.4463) < 1e-4
                    and abs(float(parts[12].split("|")[0]) * 10 - 42.97) < 0.01,
                    "the dji_O line has dji_receiver.py's 14 fields, in its units")
    ok &= file_mode()
    ok &= bench_mode()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def file_mode():
    """A SigMF ci16 recording, the format tools/sigmf-capture.py writes."""
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        b = (0.3 * T.burst(T.codeword(T.record()), 15.36e6))
        x = np.zeros(400000, dtype=np.complex64)
        x[10000:10000 + len(b)] += b
        x[200000:200000 + len(b)] += b
        x += 0.01 * (np.random.default_rng(2).standard_normal(len(x)) +
                     1j * np.random.default_rng(3).standard_normal(len(x)))
        iq = np.empty(2 * len(x), dtype=np.int16)
        iq[0::2], iq[1::2] = x.real * 2048, x.imag * 2048
        iq.tofile(os.path.join(d, "rec.sigmf-data"))
        meta = {"global": {"core:datatype": "ci16_le", "core:sample_rate": 15.36e6},
                "captures": [{"core:sample_start": 0, "core:frequency": 2444.5e6}]}
        with open(os.path.join(d, "rec.sigmf-meta"), "w") as f:
            json.dump(meta, f)
        p = subprocess.run([sys.executable, os.path.join(HERE, "droneid_rx.py"), "--file",
                            os.path.join(d, "rec.sigmf-meta"), "--json"],
                           capture_output=True, text=True, timeout=120)
        frames = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
        return check(p.returncode == 0 and len(frames) == 2
                     and all(abs(f["freq_mhz"] - 2444.5) < 0.01 for f in frames),
                     f"--file reads a SigMF ci16 recording ({len(frames)} of 2 bursts)")



def bench_mode():
    """bench.py on a wideband recording shaped like DroneDetect V2's: cf32 at
    60 MSPS centred on 2437.5 MHz, a plaintext drone on 2429.5 and an O4 one
    on 2414.5. It must channelize both out and find each where it is."""
    import importlib.util
    import tempfile
    if importlib.util.find_spec("scipy") is None:
        print("  --    bench.py needs scipy; not installed, skipped")
        return True
    from scipy.signal import resample_poly
    fs, fc = 60e6, 2437.5e6
    rng = np.random.default_rng(4)
    x = (0.003 * (rng.standard_normal(3_000_000) + 1j * rng.standard_normal(3_000_000)))
    o4 = T.o4_packet(b"CRYP", bytes.fromhex("0badf00d"), 173)
    for mhz, cw, at in ((2429.5, T.codeword(T.record()), 500_000),
                        (2414.5, T.codeword(o4), 1_800_000)):
        b = resample_poly(T.burst(cw, 15.36e6), 625, 160) * 0.2       # 15.36 -> 60 MSPS
        t = np.arange(len(b)) / fs
        x[at:at + len(b)] += b * np.exp(2j * np.pi * (mhz * 1e6 - fc) * t)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "AIR_ON_00.dat")
        x.astype(np.complex64).tofile(path)
        jl = os.path.join(d, "frames.jsonl")
        p = subprocess.run([sys.executable, os.path.join(HERE, "bench.py"), "--preset",
                            "dronedetect", "--jsonl", jl, path],
                           capture_output=True, text=True, timeout=300)
        import json
        frames = [json.loads(ln) for ln in open(jl)] if os.path.exists(jl) else []
    got = {(f["channel_mhz"], f.get("generation")) for f in frames}
    return check(p.returncode == 0 and got == {(2429.5, "O2/O3"), (2414.5, "O4")}
                 and len(frames) == 2,
                 f"bench.py channelizes a 60 MSPS recording: {sorted(got)}")


if __name__ == "__main__":
    sys.exit(main())
