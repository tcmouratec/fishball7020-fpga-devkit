#!/usr/bin/env python3
"""Tests for ocusync.py: no radio, no captures, about a minute.

    # run from: the repo root
    python3 tools/droneid/test_ocusync.py
    python3 tools/droneid/test_ocusync.py --sweep      # also print the SNR sweep

Bursts are built here, in memory, by running the receiver's own chain
backwards: record, CRC-16, CRC-24A, turbo code, rate matching, scrambling,
QPSK, OFDM with the two Zadoff-Chu symbols. The receiver code was ALSO checked
against two real captures (a DJI Mini 2 and a Mavic Air 2, from the NDSS 2023
authors' repository): every field matched their decoder, the turbo parity
re-encoded bit-exact, and it decoded two Mavic Air 2 bursts theirs missed.
Those captures are AGPL-licensed and are not copied here.
"""
import struct
import sys
import os

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ocusync as o                                                    # noqa: E402

RNG = np.random.default_rng(7020)


def record(serial="1WNBH3900201N1", lat=51.4463, lon=7.2672, seq=591):
    v = [88, 16, 2, seq, 8183, serial.encode().ljust(16, b"\0"),
         int(round(lon * o.DEG)), int(round(lat * o.DEG)), 141, 37, -2, 2, 26, -13295,
         1650542026258, int(round(51.44621 * o.DEG)), int(round(7.26710 * o.DEG)),
         int(round(7.26735 * o.DEG)), int(round(51.44626 * o.DEG)), 58, 0, b"\0" * 20, 0]
    raw = bytearray(o.RECORD.pack(*v))
    struct.pack_into("<H", raw, o.RECORD.size - 2, o.crc16_droneid(bytes(raw[:-2])))
    return bytes(raw)


def logical(body: bytes) -> bytes:
    """A logical packet: length-3 byte, then body, then the DJI CRC-16."""
    pkt = bytes([len(body)]) + body        # total = 1 + body + 2, so byte 0 = total - 3
    return pkt + struct.pack("<H", o.crc16_droneid(pkt))


def o4_packet(marker: bytes, hashcode: bytes, total: int) -> bytes:
    """A synthetic O4 packet with the published layout: type, marker, session
    hashcode, an opaque (random) body, CRC-16; `total` bytes long."""
    t = 0x13 if marker == b"CRYP" else 0x10
    body = bytes([t]) + marker + hashcode + RNG.integers(0, 256, total - 12, dtype=np.uint8).tobytes()
    return logical(body)


def codeword(rec: bytes) -> bytes:
    body = rec.ljust(173, b"\0")
    c = o.crc24a(body)
    return body + c.to_bytes(3, "big")


def burst(payload: bytes, fs: float, fmt=o.FORMATS[0]) -> np.ndarray:
    num = o.Numerology.for_rate(fs)
    bits = np.unpackbits(np.frombuffer(payload, np.uint8))
    d = o.turbo_encode(bits)
    stream, idx = o.rate_match_map()
    e = np.array([d[s][i] for s, i in zip(stream, idx)], dtype=np.uint8)
    e ^= o.gold(o.E_BITS)
    per = e.reshape(6, 2 * o.N_DATA)
    bins = num.data_bins()
    out = []
    data_rows = iter(per)
    for k, cp in enumerate(fmt.cps(num)):
        f = np.zeros(num.nfft, dtype=np.complex128)
        if k in fmt.zc_symbols:
            f[bins] = o.zc_freq(o.ZC_ROOTS[fmt.zc_symbols.index(k)])
        else:
            b = next(data_rows) if k in fmt.data_symbols else o.gold(2 * o.N_DATA)
            f[bins] = ((1 - 2.0 * b[0::2]) + 1j * (1 - 2.0 * b[1::2])) / np.sqrt(2)
        t = np.fft.ifft(np.fft.ifftshift(f)) * np.sqrt(num.nfft)
        out.append(np.r_[t[-cp:], t])
    return np.concatenate(out)


def channel(sig, fs, snr_db, cfo_hz=0.0, lead=5000, tail=5000):
    """Bury a burst in noise at snr_db (signal power over noise in its band)."""
    x = np.r_[np.zeros(lead), sig, np.zeros(tail)].astype(np.complex128)
    x *= np.exp(2j * np.pi * cfo_hz * np.arange(len(x)) / fs)
    p = np.mean(np.abs(sig) ** 2)
    # noise measured over the whole band, signal over the 9 MHz it occupies
    n0 = p / 10 ** (snr_db / 10) * (fs / (o.N_DATA * o.SCS))
    x += np.sqrt(n0 / 2) * (RNG.standard_normal(len(x)) + 1j * RNG.standard_normal(len(x)))
    return x.astype(np.complex64)


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    return bool(cond)


def main(sweep=False):
    ok = True
    print("bit level")
    ok &= check(o.crc24a(b"123456789") == 0xCDE703, "CRC-24A check value 0xCDE703")
    ok &= check(o.crc24a_ok(codeword(record())), "a codeword ends in its own CRC-24A")
    stream, idx = o.rate_match_map()
    counts = np.zeros((3, o.D_LEN))
    np.add.at(counts, (stream, idx), 1)
    ok &= check(counts.min() >= 1 and counts.max() <= 2,
                "rate matching sends every coded bit at least once, at most twice")
    ok &= check(len(set(o.qpp())) == o.K_INFO, "the QPP interleaver is a permutation")

    print("round trip, clean")
    rec = record()
    for fs in (11.52e6, 15.36e6, 30.72e6):
        for fmt in o.FORMATS:
            for cfo in (0.0, 9_000.0, -37_000.0):
                x = channel(burst(codeword(rec), fs, fmt), fs, 30, cfo)
                frames = list(o.Receiver(fs).process(x))
                good = (len(frames) == 1 and frames[0].raw[:91] == rec
                        and frames[0].record_crc_ok and frames[0].fmt == fmt.name
                        and abs(frames[0].cfo_hz - cfo) < 500)
                ok &= check(good, f"{fs / 1e6:5.2f} MSPS {fmt.name} CFO {cfo / 1e3:+.0f} kHz")
    for off in (700e3, -1.05e6):
        fr = list(o.Receiver(11.52e6).process(channel(burst(codeword(rec), 11.52e6), 11.52e6, 20, off)))
        ok &= check(len(fr) == 1 and fr[0].raw[:91] == rec and abs(fr[0].cfo_hz - off) < 500,
                    f"11.52 MSPS, burst {off / 1e3:+.0f} kHz off the tuned centre")
    f = list(o.Receiver(11.52e6).process(channel(burst(codeword(rec), 11.52e6), 11.52e6, 30)))[0]
    d = f.as_dict()
    ok &= check(d["serial_number"] == "1WNBH3900201N1" and abs(d["latitude"] - 51.4463) < 1e-5
                and d["product"] == "Mavic Air 2" and d["altitude_m"] == 42.97,
                "fields: serial, latitude, product, altitude in feet -> metres")

    print("other message types")
    h = bytes.fromhex("1a2b3c4d")
    cases = [("CRYP", o4_packet(b"CRYP", h, 173)), ("INFP", o4_packet(b"INFP", h, 138)),
             ("serial", logical(b"\x11" + b"1WNBH3900201N1".ljust(16, b"\0") + bytes(124)))]
    ok &= check(cases[0][1][0] == 0xAA and cases[1][1][0] == 0x87,
                "CRYP and INFP start with 0xAA and 0x87, as the published captures do")
    for name, pkt in cases:
        fr = list(o.Receiver(11.52e6).process(channel(burst(codeword(pkt), 11.52e6), 11.52e6, 20)))
        d = fr[0].as_dict() if fr else {}
        if name == "serial":
            good = d.get("serial_number") == "1WNBH3900201N1" and d.get("content") == "serial number"
        else:
            good = (d.get("generation") == "O4" and d.get("marker") == name
                    and d.get("hashcode") == "1a2b3c4d")
        ok &= check(len(fr) == 1 and fr[0].record_crc_ok and good,
                    f"{name} ({len(pkt)} bytes): decoded, CRC-16 checked, identified")

    print("no false alarms")
    rx = o.Receiver(11.52e6)
    noise = (RNG.standard_normal(4_000_000) + 1j * RNG.standard_normal(4_000_000)).astype(np.complex64)
    n = sum(len(list(rx.process(noise[i:i + 1 << 20]))) for i in range(0, len(noise), 1 << 20))
    ok &= check(n == 0, "0.35 s of noise at 11.52 MSPS gives no frames")

    print("decoding gain")
    snrs = (-1, 0, 1, 2, 4, 8, 10) if sweep else (1,)
    trials = 12 if sweep else 8
    res = {}
    for snr in snrs:
        hard = turbo = 0
        for _ in range(trials):
            x = channel(burst(codeword(rec), 11.52e6), 11.52e6, snr, RNG.uniform(-20e3, 20e3))
            rx = o.Receiver(11.52e6, gate_db=None)
            frames = list(rx.process(x))
            if frames and frames[0].raw[:91] == rec:
                turbo += 1
                hard += frames[0].method == "hard"
        res[snr] = (hard, turbo)
        if sweep:
            print(f"    SNR {snr:+d} dB: hard decisions {hard}/{trials}, with turbo {turbo}/{trials}")
    h, t = res[1]
    ok &= check(t > h and t >= trials * 0.75,
                f"at +1 dB the turbo decoder recovers frames hard decisions lose ({h} -> {t} of {trials})")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main("--sweep" in sys.argv))
