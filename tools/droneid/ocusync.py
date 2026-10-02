"""DJI DroneID (OcuSync 2/3) receiver: IQ samples in, decoded frames out.

Pure numpy. No GNU Radio, no MATLAB, no C extension, so it runs on a Raspberry
Pi as it is.

    from ocusync import Receiver
    rx = Receiver(fs=11.52e6)
    for frame in rx.process(iq):          # complex64 numpy array, any length
        print(frame.as_dict())

THE SIGNAL. A DroneID burst is a short LTE-like OFDM transmission, about 0.6 ms
long, sent every ~600 ms on one of a handful of channels in 2.4 and 5.8 GHz:

  - 15 kHz subcarriers, 600 of them in use plus an empty DC bin (~9 MHz wide)
  - 9 OFDM symbols, cyclic prefixes long/short/.../short/long (80 and 72
    samples at 15.36 MSPS: LTE's normal CP); some older drones send 8, omitting
    the first
  - symbols 3 and 5 (2 and 4 in the 8-symbol form) carry Zadoff-Chu sequences
    of root 600 and 147, which serve as both preamble and channel reference
  - the other six carry QPSK: 7200 bits, scrambled with LTE's Gold sequence
    (c_init 0x12345678), which hold one LTE turbo codeword (K = 1408 bits,
    rate matching with rv 0) of 176 bytes ending in a CRC-24A
  - inside it, a 91-byte DroneID record with its own CRC-16

Sources, none of which is copied here: the NDSS 2023 paper "Drone Security and
the Mysterious Case of DJI's DroneID" (Schiller et al.), proto17/dji_droneid
(MIT; the numerology, ZC roots and turbo parameters), 3GPP TS 36.211 and
36.212 (scrambler, QPSK mapping, rate matching, turbo code), and the
"Anatomy of DJI's Drone ID Implementation" white paper (the record layout).

ANY SAMPLE RATE THAT IS A MULTIPLE OF 15 kHz AND MAKES WHOLE CYCLIC PREFIXES
works, because the receiver works in the rate's own FFT size rather than
resampling to 15.36 MSPS. Two matter on this board:

    11.52 MSPS  FFT 768,  CP 60/54   ~46 MB/s of int16 IQ: what the Ethernet
                                     link sustains, so few dropped buffers
    15.36 MSPS  FFT 1024, CP 80/72   LTE's own rate, more margin on the band edges

DETECTION is done on the cyclic prefixes first (cheap, blind to frequency
offset), then confirmed by the Zadoff-Chu symbols, so the expensive work runs
only on candidates. DECODING tries hard decisions on the systematic bits first
(enough at good SNR, and what the original research receiver does), then a
max-log-MAP turbo decoder, which buys several dB. Every frame reported has
passed the CRC-24A of the turbo codeword; the record's own CRC-16 is reported
alongside.
"""
from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field

import numpy as np

SCS = 15e3                      # subcarrier spacing, Hz
N_DATA = 600                    # used subcarriers (DC excluded)
ZC_ROOTS = (600, 147)
SCRAMBLER_INIT = 0x12345678
E_BITS = 7200                   # coded bits per burst: 6 symbols x 600 carriers x 2
K_INFO = 1408                   # turbo block size: 176 bytes
D_LEN = K_INFO + 4              # turbo streams incl. tail
QPP = (43, 88)                  # TS 36.212 table 5.1.3-3, K = 1408


# --------------------------------------------------------------- numerology
@dataclass(frozen=True)
class Numerology:
    fs: float
    nfft: int
    cp_long: int
    cp_short: int

    @staticmethod
    def for_rate(fs: float) -> "Numerology":
        nfft = fs / SCS
        cpl = fs / 192000.0                 # 80 samples at 15.36 MSPS
        cps = fs * 72 / 15.36e6
        if not all(abs(v - round(v)) < 1e-6 for v in (nfft, cpl, cps)):
            raise ValueError(
                f"{fs / 1e6:g} MSPS does not give a whole FFT and cyclic prefix; "
                "use a multiple of 1.92 MSPS such as 11.52, 15.36, 23.04 or 30.72")
        if nfft < N_DATA + 2:
            raise ValueError(f"{fs / 1e6:g} MSPS is too narrow for a 9 MHz signal")
        return Numerology(fs, int(round(nfft)), int(round(cpl)), int(round(cps)))

    def data_bins(self) -> np.ndarray:
        """Indices of the 600 used carriers in an fftshift-ed spectrum, low to high."""
        dc = self.nfft // 2
        return np.r_[dc - N_DATA // 2:dc, dc + 1:dc + 1 + N_DATA // 2]


@dataclass(frozen=True)
class FrameFormat:
    name: str
    n_symbols: int
    zc_symbols: tuple
    data_symbols: tuple

    def cps(self, num: Numerology) -> list:
        return [num.cp_long] + [num.cp_short] * (self.n_symbols - 2) + [num.cp_long]

    def offsets(self, num: Numerology) -> list:
        """Start (CP included) of each symbol, relative to the burst start."""
        out, t = [], 0
        for cp in self.cps(num):
            out.append(t)
            t += cp + num.nfft
        return out

    def length(self, num: Numerology) -> int:
        return sum(self.cps(num)) + self.n_symbols * num.nfft


FORMATS = (
    FrameFormat("9-symbol", 9, (3, 5), (1, 2, 4, 6, 7, 8)),
    FrameFormat("8-symbol", 8, (2, 4), (0, 1, 3, 5, 6, 7)),
)


def zc_freq(root: int) -> np.ndarray:
    """The 600 frequency-domain values a ZC symbol carries, low carrier first."""
    n = np.arange(601)
    z = np.exp(-1j * np.pi * root * n * (n + 1) / 601)
    return np.delete(z, 300).astype(np.complex64)


# --------------------------------------------------------------- bit level
def gold(length: int, c_init: int = SCRAMBLER_INIT, nc: int = 1600) -> np.ndarray:
    """LTE pseudo-random sequence c(n), TS 36.211 7.2."""
    n = nc + length + 31
    x1 = np.zeros(n, dtype=np.uint8)
    x2 = np.zeros(n, dtype=np.uint8)
    x1[0] = 1
    x2[:31] = [(c_init >> i) & 1 for i in range(31)]
    for i in range(nc + length):
        x1[i + 31] = x1[i + 3] ^ x1[i]
        x2[i + 31] = x2[i + 3] ^ x2[i + 2] ^ x2[i + 1] ^ x2[i]
    return x1[nc:nc + length] ^ x2[nc:nc + length]


SUBBLOCK_PERM = (0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
                 1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31)


def rate_match_map(d_len: int = D_LEN, e: int = E_BITS, rv: int = 0):
    """For each of the e transmitted bits: (stream 0/1/2, index into d_stream).

    TS 36.212 5.1.4.1: sub-block interleaving of each stream, bit collection
    into a circular buffer, and reading e bits from k0 while skipping the
    dummy (NULL) positions."""
    c = 32
    r = -(-d_len // c)
    kpi = r * c
    nd = kpi - d_len
    p = np.array(SUBBLOCK_PERM)
    k = np.arange(kpi)
    y01 = p[k // r] + c * (k % r)               # y index of v0/v1[k]
    y2 = (p[k // r] + c * (k % r) + 1) % kpi    # y index of v2[k]
    # circular buffer w: v0, then v1 and v2 interleaved
    stream = np.empty(3 * kpi, dtype=np.int8)
    yidx = np.empty(3 * kpi, dtype=np.int64)
    stream[:kpi], yidx[:kpi] = 0, y01
    stream[kpi::2], yidx[kpi::2] = 1, y01
    stream[kpi + 1::2], yidx[kpi + 1::2] = 2, y2
    valid = yidx >= nd
    ncb = 3 * kpi
    k0 = r * (2 * math.ceil(ncb / (8 * r)) * rv + 2)
    order = (k0 + np.arange(ncb)) % ncb
    order = order[valid[order]]
    reps = -(-e // len(order))
    order = np.tile(order, reps)[:e]
    return stream[order], yidx[order] - nd


def qpp(k: int = K_INFO, f: tuple = QPP) -> np.ndarray:
    i = np.arange(k, dtype=np.int64)
    return (f[0] * i + f[1] * i * i) % k


def crc24a(data: bytes) -> int:
    """CRC-24A, TS 36.212 5.1.1 (CRC-24/LTE-A: no reflection, no final XOR)."""
    reg = 0
    for b in data:
        reg ^= b << 16
        for _ in range(8):
            reg <<= 1
            if reg & 0x1000000:
                reg ^= 0x1864CFB
    return reg & 0xFFFFFF


def crc24a_ok(block: bytes) -> bool:
    """True if a block ending in its own CRC-24A is intact (the CRC of the
    whole block, its CRC included, is then zero)."""
    return crc24a(block) == 0


def crc16_droneid(data: bytes) -> int:
    """The DroneID record's CRC-16: CCITT polynomial, reflected, seed 0x3692."""
    reg = 0x3692
    for b in data:
        reg ^= b
        for _ in range(8):
            reg = (reg >> 1) ^ 0x8408 if reg & 1 else reg >> 1
    return reg


# --------------------------------------------------------------- turbo code
# LTE constituent encoder: 8 states, feedback g0 = 1 + D^2 + D^3,
# feedforward g1 = 1 + D + D^3. State = (s1, s2, s3), the register contents.
def _trellis():
    nxt = np.zeros((8, 2), dtype=np.int64)
    par = np.zeros((8, 2), dtype=np.int8)
    term_in = np.zeros(8, dtype=np.int8)       # the input that feeds the register a 0
    for s in range(8):
        s1, s2, s3 = (s >> 2) & 1, (s >> 1) & 1, s & 1
        fb = s2 ^ s3
        for u in (0, 1):
            a = u ^ fb
            par[s, u] = a ^ s1 ^ s3
            nxt[s, u] = (a << 2) | (s1 << 1) | s2
        term_in[s] = fb
    return nxt, par, term_in


NEXT, PARITY, TERM_IN = _trellis()


def rsc_encode(bits: np.ndarray):
    """One constituent encoder with trellis termination: (parity, tail x, tail z)."""
    s = 0
    z = np.empty(len(bits), dtype=np.int8)
    for i, u in enumerate(bits):
        z[i] = PARITY[s, u]
        s = NEXT[s, u]
    tx, tz = [], []
    for _ in range(3):
        u = TERM_IN[s]
        tx.append(u)
        tz.append(PARITY[s, u])
        s = NEXT[s, u]
    return z, tx, tz


def turbo_encode(c: np.ndarray):
    """TS 36.212 5.1.3.2: the three streams d0, d1, d2 of length K + 4."""
    k = len(c)
    z, tx, tz = rsc_encode(c)
    z2, tx2, tz2 = rsc_encode(c[qpp(k)])
    d0 = np.r_[c, tx[0], tz[1], tx2[0], tz2[1]]
    d1 = np.r_[z, tz[0], tx[2], tz2[0], tx2[2]]
    d2 = np.r_[z2, tx[1], tz[2], tx2[1], tz2[2]]
    return d0.astype(np.int8), d1.astype(np.int8), d2.astype(np.int8)


# predecessors of each state: PREV_S[s', j] in state PREV_U[s', j] input -> s'
PREV_S = np.zeros((8, 2), dtype=np.int64)
PREV_U = np.zeros((8, 2), dtype=np.int64)
_fill = np.zeros(8, dtype=np.int64)
for _s in range(8):
    for _u in (0, 1):
        _n = NEXT[_s, _u]
        PREV_S[_n, _fill[_n]], PREV_U[_n, _fill[_n]] = _s, _u
        _fill[_n] += 1
NEG = -1e9


def _bcjr(ls, lp, tail_s, tail_p, la, window=64, warmup=32):
    """Max-log-MAP for one constituent code, as overlapping windows run side by side.

    ls, lp: channel LLRs of the systematic and parity bits (positive = 0).
    la: a-priori LLRs of the information bits. Returns the extrinsic LLRs.

    A plain BCJR is one long sequential recursion, which in numpy costs a
    Python step per bit. Hardware decoders cut the block into windows and
    start each recursion `warmup` steps early from "every state equally
    likely"; the trellis forgets its starting point within a few constraint
    lengths, so the loss is negligible and the windows run in parallel. Here
    that turns 1411 Python steps per pass into window + warmup.
    The three tail steps force the trellis back to state 0."""
    k = len(ls)
    steps = k + 3
    sys_ = np.r_[ls + la, tail_s]
    pty = np.r_[lp, tail_p]
    su = np.array([1.0, -1.0])                    # bit 0 -> +, bit 1 -> -
    pm = 1.0 - 2.0 * PARITY                       # (8, 2)
    gam = 0.5 * (su[None, None, :] * sys_[:, None, None] + pm[None] * pty[:, None, None])
    # tail steps: only the input that feeds the register a 0 is allowed
    allowed = np.zeros((8, 2), dtype=bool)
    allowed[np.arange(8), TERM_IN] = True
    gam[k:][:, ~allowed] = NEG
    nw = -(-steps // window)
    total = nw * window
    # pad: warmup steps before 0 and after the end are uninformative (zero metric)
    gp = np.zeros((warmup + total + warmup, 8, 2))
    gp[warmup:warmup + steps] = gam
    gp[warmup + steps:, :, 1] = NEG               # past the end: stay put in state 0
    init0 = np.full(8, NEG)
    init0[0] = 0.0
    wi = np.arange(nw)
    # forward
    alpha = np.zeros((nw, 8))
    A = np.empty((window, nw, 8))
    for st in range(window + warmup):
        t = wi * window - warmup + st              # time of this step, per window
        if st == warmup:
            alpha[0] = init0                       # window 0 knows its start state
        if st >= warmup:
            A[st - warmup] = alpha
        g = gp[t + warmup]                         # (nw, 8, 2)
        cand = alpha[:, PREV_S] + g[:, PREV_S, PREV_U]
        alpha = cand.max(axis=2)
        alpha -= alpha.max(axis=1, keepdims=True)
    # backward
    beta = np.zeros((nw, 8))
    out = np.zeros(total)
    end = wi * window + window + warmup            # first time NOT processed
    for st in range(window + warmup):
        t = end - 1 - st
        reset = (t + 1) == steps
        if reset.any():
            beta[reset] = init0
        g = gp[t + warmup]
        bn = beta[:, NEXT]                         # (nw, 8, 2): beta_{t+1}(next(s, u))
        own = st >= warmup
        if own:
            a = A[window - 1 - (st - warmup)]
            m = a[:, :, None] + g + bn
            out[t] = m[:, :, 0].max(axis=1) - m[:, :, 1].max(axis=1)
        beta = (g + bn).max(axis=2)
        beta -= beta.max(axis=1, keepdims=True)
    return out[:k] - ls - la


def turbo_decode(l0, l1, l2, iterations=6, check=None):
    """Iterative max-log-MAP decoding of LTE turbo streams (LLR, positive = 0).

    check(bits) -> bool stops early once a hard decision passes it."""
    k = len(l0) - 4
    pi = qpp(k)
    ls, lp1, lp2 = l0[:k], l1[:k], l2[:k]
    # tail LLRs, TS 36.212 5.1.3.2.2
    t1s = np.array([l0[k], l2[k], l1[k + 1]])
    t1p = np.array([l1[k], l0[k + 1], l2[k + 1]])
    t2s = np.array([l0[k + 2], l2[k + 2], l1[k + 3]])
    t2p = np.array([l1[k + 2], l0[k + 3], l2[k + 3]])
    la = np.zeros(k)
    bits = (ls < 0).astype(np.uint8)
    for _ in range(iterations):
        e1 = 0.75 * _bcjr(ls, lp1, t1s, t1p, la)          # scaled max-log-MAP
        e2 = 0.75 * _bcjr(ls[pi], lp2, t2s, t2p, e1[pi])
        la = np.empty(k)
        la[pi] = e2
        bits = ((ls + e1 + la) < 0).astype(np.uint8)
        if check is not None and check(bits):
            return bits, True
    return bits, check(bits) if check is not None else False


# --------------------------------------------------------------- the record
PRODUCT_TYPES = {
    1: "Inspire 1", 2: "Phantom 3 Series", 3: "Phantom 3 Series", 4: "Phantom 3 Std",
    5: "M100", 6: "ACEONE", 7: "WKM", 8: "NAZA", 9: "A2", 10: "A3", 11: "Phantom 4",
    12: "MG1", 14: "M600", 15: "Phantom 3 4k", 16: "Mavic Pro", 17: "Inspire 2",
    18: "Phantom 4 Pro", 20: "N2", 21: "Spark", 23: "M600 Pro", 24: "Mavic Air",
    25: "M200", 26: "Phantom 4 Series", 27: "Phantom 4 Adv", 28: "M210", 30: "M210RTK",
    31: "A3_AG", 32: "MG2", 34: "MG1A", 35: "Phantom 4 RTK", 36: "Phantom 4 Pro V2.0",
    38: "MG1P", 40: "MG1P-RTK", 41: "Mavic 2", 44: "M200 V2 Series",
    51: "Mavic 2 Enterprise", 53: "Mavic Mini", 58: "Mavic Air 2", 59: "P4M",
    60: "M300 RTK", 61: "DJI FPV", 63: "Mini 2", 64: "AGRAS T10", 65: "AGRAS T30",
    66: "Air 2S", 67: "M30", 68: "Mavic 3", 69: "Mavic 2 Enterprise Advanced",
    70: "Mini SE",
}

RECORD = struct.Struct("<BBBHH16siihhhhhhQiiiiBB20sH")   # 91 bytes
DEG = 174533.0                  # position words are radians x 1e7


@dataclass
class Frame:
    """One decoded DroneID frame."""
    raw: bytes                      # the 176-byte turbo codeword payload
    record_crc_ok: object            # True/False for telemetry, None for other types
    fields: dict
    sample_index: int = 0           # where in the input the burst started
    cfo_hz: float = 0.0
    snr_db: float = 0.0
    method: str = ""                # "hard" or "turbo"
    fmt: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(self.fields)
        d.update(record_crc_ok=self.record_crc_ok, sample_index=self.sample_index,
                 cfo_hz=round(self.cfo_hz, 1),
                 snr_db=round(self.snr_db, 1), decoder=self.method, format=self.fmt)
        d.update(self.extra)
        return d


TELEMETRY = 0x10


def parse_record(raw: bytes) -> tuple:
    """(fields, crc_ok) from the start of a payload.

    Only message type 0x10, the flight-telemetry record, has a known layout.
    Its field order and scaling follow the NDSS 2023 receiver, which was
    checked against flight logs: altitude before height, home longitude before
    home latitude, both heights in feet. The Kismet parser names some of these
    in the other order; on the published captures only this order puts the
    home point next to the drone.

    Other types do occur (a Mavic Air 2 capture carries a type 0x11 frame
    holding its serial number right after the type byte); they are returned
    with their type, the bytes, and any serial-looking text, and crc_ok None,
    because their own checksum is not known."""
    if raw[1] != TELEMETRY:
        m = re.match(rb"[0-9A-Z]{8,20}", raw[2:22])
        f = {"msg_type": raw[1], "serial_number": m.group().decode() if m else "",
             "raw_hex": raw[:32].hex()}
        return f, None
    v = RECORD.unpack(raw[:RECORD.size])
    crc_ok = crc16_droneid(raw[:RECORD.size - 2]) == v[22]
    f = {
        "pkt_len": v[0], "msg_type": v[1], "version": v[2],
        "sequence_number": v[3], "state_info": v[4],
        "serial_number": v[5].split(b"\0")[0].decode("ascii", "replace"),
        "longitude": v[6] / DEG, "latitude": v[7] / DEG,
        "altitude_m": round(v[8] / 3.281, 2), "height_m": round(v[9] / 3.281, 2),
        "v_north_cms": v[10], "v_east_cms": v[11], "v_up_cms": v[12],
        "yaw_deg": round(v[13] / 100.0, 2), "gps_time_ms": v[14],
        "app_latitude": v[15] / DEG, "app_longitude": v[16] / DEG,
        "home_longitude": v[17] / DEG, "home_latitude": v[18] / DEG,
        "product_type": v[19], "product": PRODUCT_TYPES.get(v[19], f"unknown ({v[19]})"),
        "uuid": v[21][:v[20]].decode("ascii", "replace") if v[20] <= 20 else "",
    }
    return f, crc_ok


# --------------------------------------------------------------- receiver
class Receiver:
    """Find and decode DroneID bursts in complex baseband samples at rate fs."""

    def __init__(self, fs: float = 15.36e6, cp_threshold: float = 0.30,
                 zc_threshold: float = 0.35, max_cfo_hz: float = 60e3,
                 turbo: bool = True, iterations: int = 6, gate_db=1.5,
                 max_offset_hz: float = 1.2e6):
        self.num = Numerology.for_rate(fs)
        self.max_offset_hz = max_offset_hz
        self.gate_db = gate_db
        self.floor = None
        self.longest = max(f.length(self.num) for f in FORMATS)
        self.fs = fs
        self.cp_threshold = cp_threshold
        self.zc_threshold = zc_threshold
        self.max_bins = int(math.ceil(max_cfo_hz / SCS))
        self.turbo = turbo
        self.iterations = iterations
        self.bins = self.num.data_bins()
        self.zc = [zc_freq(r) for r in ZC_ROOTS]
        self.scr = gold(E_BITS).astype(np.float64)
        self.rm_stream, self.rm_index = rate_match_map()
        # cp_candidates: stretches the cyclic-prefix test flagged; crc_ok:
        # frames decoded; crc_fail: confirmed by the ZC symbols, not decodable
        self.stats = {"cp_candidates": 0, "crc_ok": 0, "crc_fail": 0}

    # -- detection --------------------------------------------------------
    def _active_segments(self, x: np.ndarray):
        """[(start, stop)] of the stretches worth searching: where the power in
        256-sample blocks rises gate_db above the noise floor, padded by a
        burst length either side. A quiet channel costs one pass over |x|^2."""
        if self.gate_db is None:
            return [(0, len(x))]
        blk = 256
        nb = len(x) // blk
        if nb == 0:
            return []
        f = x[:nb * blk].view(np.float32).reshape(nb, 2 * blk)
        pw = np.einsum("ij,ij->i", f, f) / blk
        floor = float(np.percentile(pw, 20))
        # Remember the quietest floor seen lately: a chunk filled by one long
        # transmission would otherwise raise its own floor and hide its bursts.
        if self.floor is None or floor < self.floor:
            self.floor = floor
        else:
            self.floor = 0.9 * self.floor + 0.1 * floor
        act = np.flatnonzero(pw > self.floor * 10 ** (self.gate_db / 10))
        if len(act) == 0:
            return []
        pad = self.longest + self.num.nfft
        segs = []
        for i in act:
            a, b = max(0, i * blk - pad), min(len(x), (i + 1) * blk + pad)
            if segs and a <= segs[-1][1]:
                segs[-1] = (segs[-1][0], max(segs[-1][1], b))
            else:
                segs.append((a, b))
        return segs

    def _cp_scan(self, x: np.ndarray, stride: int):
        """Normalised cyclic-prefix correlation for each frame format, on a
        grid of `stride` samples: {format: (times, metric, cumulative sum)}.

        The metric is 1.0 where a burst with that symbol layout starts, small
        elsewhere, and does not depend on the carrier frequency offset. The
        window sums for the two CP lengths are computed once and shared."""
        n = self.num.nfft
        if len(x) <= n + self.longest:
            return {}
        p = x[:-n] * np.conj(x[n:])
        e = 0.5 * (np.abs(x[:-n]) ** 2 + np.abs(x[n:]) ** 2)
        cp_ = np.concatenate(([0], np.cumsum(p, dtype=np.complex128)))
        ce = np.concatenate(([0], np.cumsum(e, dtype=np.float64)))
        win = {}
        for cp in {self.num.cp_long, self.num.cp_short}:
            win[cp] = (np.abs(cp_[cp:] - cp_[:-cp]).astype(np.float32),
                       (ce[cp:] - ce[:-cp]).astype(np.float32))
        out = {}
        for fmt in FORMATS:
            span = len(p) - fmt.length(self.num)
            if span <= 0:
                continue
            m = span // stride
            num = np.zeros(m, dtype=np.float32)
            den = np.zeros(m, dtype=np.float32)
            for off, cp in zip(fmt.offsets(self.num), fmt.cps(self.num)):
                a, en = win[cp]
                num += a[off:off + m * stride:stride]
                den += en[off:off + m * stride:stride]
            out[fmt] = (np.arange(m) * stride, num / np.maximum(den, 1e-20), cp_)
        return out

    def _frac_cfo(self, cp_, t, fmt):
        acc = 0j
        for off, cp in zip(fmt.offsets(self.num), fmt.cps(self.num)):
            acc += cp_[t + off + cp] - cp_[t + off]
        return -np.angle(acc) * self.fs / (2 * np.pi * self.num.nfft)

    def _detect_segment(self, x: np.ndarray, base: int):
        stride = 8
        found = []
        for fmt, (times, m, cp_) in self._cp_scan(x, stride).items():
            # local maxima above threshold, at least half a symbol apart
            w = max(1, self.num.nfft // (2 * stride))
            if len(m) < 2 * w + 1:
                continue
            pad = np.pad(m, w, constant_values=0)
            win = np.lib.stride_tricks.sliding_window_view(pad, 2 * w + 1)
            peaks = np.flatnonzero((m > self.cp_threshold) & (m >= win.max(axis=1)))
            for k in peaks:
                found.append((fmt, int(times[k]), float(m[k]), cp_))
        out = []
        for fmt, t0, score, cp_ in found:
            # refine to the sample around the coarse peak
            n = self.num.nfft
            lo, hi = max(0, t0 - stride), min(len(x) - n - fmt.length(self.num), t0 + stride)
            best, bt = -1.0, t0
            for t in range(lo, hi + 1):
                num = den = 0.0
                for off, cp in zip(fmt.offsets(self.num), fmt.cps(self.num)):
                    a = t + off
                    num += abs(cp_[a + cp] - cp_[a])
                    seg = x[a:a + cp]
                    seg2 = x[a + n:a + n + cp]
                    den += 0.5 * (np.vdot(seg, seg).real + np.vdot(seg2, seg2).real)
                v = num / max(den, 1e-20)
                if v > best:
                    best, bt = v, t
            out.append((base + bt, fmt, self._frac_cfo(cp_, bt, fmt), best))
        return out

    def detect(self, x):
        """Candidate bursts, grouped: [[(start, format, frac CFO Hz, score), ...]].

        The 8-symbol layout is the 9-symbol one without its first symbol, so
        one burst scores almost equally under both, one symbol apart. Each
        group holds every reading of one stretch of signal, best first, and
        process() tries them in turn until one passes the CRC."""
        cands = []
        for a, b in self._active_segments(x):
            cands += self._detect_segment(x[a:b], a)
        cands.sort(key=lambda c: c[0])
        groups = []
        for c in cands:
            if groups and c[0] - groups[-1][0][0] < self.longest:
                groups[-1].append(c)
            else:
                groups.append([c])
        for g in groups:
            g.sort(key=lambda c: -c[3])
        self.stats["cp_candidates"] += len(groups)
        return groups

    # -- demodulation -----------------------------------------------------
    def _symbols(self, x, start, fmt):
        """FFT of every symbol, fftshift-ed, rows = symbols."""
        n = self.num.nfft
        rows = []
        for off, cp in zip(fmt.offsets(self.num), fmt.cps(self.num)):
            a = start + off + cp
            rows.append(np.fft.fftshift(np.fft.fft(x[a:a + n])))
        return np.array(rows)

    def _zc_present(self, y, fmt) -> float:
        """How much the first ZC symbol looks like the root-600 sequence, 0..1.

        Correlates the product of neighbouring carriers, which is immune to
        the linear phase a timing error puts across the band. It is also
        blind to a whole-carrier frequency shift (a ZC sequence's neighbour
        ratio is itself a linear phase), which is why _carrier_offset exists."""
        z = self.zc[0]
        ref = z[1:] * np.conj(z[:-1])
        v = y[fmt.zc_symbols[0]][self.bins]
        d = v[1:] * np.conj(v[:-1]) * np.conj(ref)
        d[N_DATA // 2 - 1] = 0          # carriers 299 and 300 sit either side of DC
        return float(abs(d.sum()) / max(np.sum(np.abs(v[1:]) * np.abs(v[:-1])), 1e-12))

    def _carrier_offset(self, y, fmt):
        """(coherence, whole-carrier offset): the shift at which the channel
        seen through ZC root 600 and the one seen through root 147 agree.

        A wrong shift leaves the two estimates with different phase slopes,
        because a frequency shift of a ZC sequence looks like a time shift
        that depends on the root, so only the right one makes them coherent."""
        best = (0.0, 0)
        s1, s2 = fmt.zc_symbols
        for q in range(-self.max_bins, self.max_bins + 1):
            h1 = y[s1][self.bins + q] / self.zc[0]
            h2 = y[s2][self.bins + q] / self.zc[1]
            c = abs(np.vdot(h1, h2)) / max(np.linalg.norm(h1) * np.linalg.norm(h2), 1e-12)
            if c > best[0]:
                best = (float(c), q)
        return best

    def _coarse_offset(self, burst) -> float:
        """Where the burst's 9 MHz sits in the band, in Hz from the centre.

        The ZC search only covers max_cfo_hz. A burst tuned a few hundred kHz
        off (an unexpected channel raster, or a coarse LO) is found here from
        its spectrum: the 600-carrier window holding the most energy. Returns
        0 when the burst is already within a few carriers of the centre."""
        if self.max_offset_hz <= 0:
            return 0.0
        nf = 1 << int(np.ceil(np.log2(len(burst))))
        p = np.abs(np.fft.fftshift(np.fft.fft(burst, nf))) ** 2
        bw = int(round(N_DATA * SCS / self.fs * nf))
        c = np.concatenate(([0.0], np.cumsum(p)))
        sums = c[bw:] - c[:-bw]
        centre = nf // 2 - bw // 2
        reach = int(self.max_offset_hz / self.fs * nf)
        lo, hi = max(0, centre - reach), min(len(sums) - 1, centre + reach)
        k = lo + int(np.argmax(sums[lo:hi + 1]))
        off = (k - centre) * self.fs / nf
        return off if abs(off) > 2 * SCS else 0.0

    def demodulate(self, x, start, fmt, frac_cfo):
        """Soft bits (7200 LLRs, descrambled) for a candidate, or None."""
        n = self.num.nfft
        blen = fmt.length(self.num)
        lo = max(0, start - n)
        seg = x[lo:start + blen + n].astype(np.complex128)
        start -= lo
        if start + blen > len(seg):
            return None
        tt = np.arange(len(seg))
        power = float(np.mean(np.abs(seg[start:start + blen]) ** 2))
        coarse = self._coarse_offset(seg[start:start + blen])
        if coarse:
            # whole carriers only: the fractional part is already measured
            coarse = round(coarse / SCS) * SCS
            seg = seg * np.exp(-2j * np.pi * coarse * tt / self.fs)
        seg = seg * np.exp(-2j * np.pi * frac_cfo * tt / self.fs)
        y = self._symbols(seg, start, fmt)
        if self._zc_present(y, fmt) < self.zc_threshold:
            return None
        coh, q = self._carrier_offset(y, fmt)
        if coh < self.zc_threshold:
            return None
        cfo = coarse + frac_cfo + q * SCS
        seg = seg * np.exp(-2j * np.pi * q * SCS * tt / self.fs)
        y = self._symbols(seg, start, fmt)[:, self.bins]
        # A timing error of d samples is a phase slope of -2 pi d / N per carrier.
        h = y[fmt.zc_symbols[0]] / self.zc[0]
        p = h[1:] * np.conj(h[:-1])
        p[N_DATA // 2 - 1] = 0
        shift = int(round(-np.angle(p.sum()) * n / (2 * np.pi)))
        if shift and 0 <= start + shift and start + shift + blen <= len(seg):
            start += shift
            y = self._symbols(seg, start, fmt)[:, self.bins]
        # channel from both ZC symbols, plus the common phase drift between them
        h = [y[s] / self.zc[i] for i, s in enumerate(fmt.zc_symbols)]
        gap = fmt.zc_symbols[1] - fmt.zc_symbols[0]
        drift = np.angle(np.sum(h[1] * np.conj(h[0]))) / gap
        h0 = 0.5 * (h[0] + h[1] * np.exp(-1j * drift * gap))
        llr, eqs = [], []
        for s in fmt.data_symbols:
            hs = h0 * np.exp(1j * drift * (s - fmt.zc_symbols[0]))
            w = np.abs(hs) ** 2
            eq = y[s] * np.conj(hs) / np.maximum(w, 1e-12)
            eqs.append(eq)
            # QPSK, TS 36.211 7.1.2: bit 0 on I, bit 1 on Q, 0 -> positive.
            # Weighting by |h|^2 makes a faded carrier count for less.
            l = np.empty(2 * N_DATA)
            l[0::2] = eq.real * w
            l[1::2] = eq.imag * w
            llr.append(l)
        llr = np.concatenate(llr)
        # SNR from the spread of the equalised constellation around its four points
        eqs = np.concatenate(eqs)
        a = np.r_[np.abs(eqs.real), np.abs(eqs.imag)]
        snr = 10 * np.log10(max(np.mean(a) ** 2 / max(np.var(a), 1e-12), 1e-12))
        llr = llr / max(np.mean(np.abs(llr)), 1e-12) * (1 - 2 * self.scr)
        return llr, cfo, snr, lo + start, power

    # -- decoding -----------------------------------------------------------
    def _dematch(self, llr):
        streams = np.zeros((3, D_LEN))
        np.add.at(streams, (self.rm_stream, self.rm_index), llr)
        return streams

    def decode_bits(self, llr):
        """(176-byte payload, method) or (None, None)."""
        streams = self._dematch(llr)

        def ok(bits):
            return crc24a_ok(np.packbits(bits[:K_INFO]).tobytes())

        hard = (streams[0][:K_INFO] < 0).astype(np.uint8)
        if ok(hard):
            return np.packbits(hard).tobytes(), "hard"
        if self.turbo:
            bits, good = turbo_decode(streams[0], streams[1], streams[2],
                                      self.iterations, check=ok)
            if good:
                return np.packbits(bits).tobytes(), "turbo"
        return None, None

    def process(self, x: np.ndarray, offset: int = 0, failed=None):
        """Decode every burst in x. Yields Frame objects.

        offset is added to sample_index, so a caller feeding chunks keeps
        absolute positions. If failed is a list, (start, format name) of each
        burst the ZC symbols confirmed but no decoder could recover is
        appended to it, for saving and later study."""
        x = np.asarray(x, dtype=np.complex64)
        for group in self.detect(x):
            confirmed = None
            for start, fmt, frac, _ in group:
                got = self.demodulate(x, start, fmt, frac)
                if got is None:
                    continue
                llr, cfo, snr, s0, power = got
                confirmed = confirmed or (s0, fmt.name)
                raw, method = self.decode_bits(llr)
                if raw is None:
                    continue
                self.stats["crc_ok"] += 1
                fields, rec_ok = parse_record(raw)
                yield Frame(raw, rec_ok, fields, int(offset + s0), float(cfo), float(snr),
                            method, fmt.name,
                            {"power_dbfs": round(float(10 * np.log10(max(power, 1e-20))), 1)})
                break
            else:
                if confirmed:
                    self.stats["crc_fail"] += 1
                    if failed is not None:
                        failed.append((int(confirmed[0]), confirmed[1]))
