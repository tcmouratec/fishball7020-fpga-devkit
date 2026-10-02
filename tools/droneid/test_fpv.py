#!/usr/bin/env python3
"""Tests for fpv.py: analog video and ExpressLRS detection, no radio needed.

    # run from: the repo root
    python3 tools/droneid/test_fpv.py

Signals are synthesised here: PAL and NTSC composite video (sync pulses,
porches, active lines with changing picture content) frequency-modulated at a
VTX-like deviation, and ExpressLRS-shaped LoRa packets (an n-symbol preamble of
base upchirps, two sync symbols, 2.25 downchirps, random payload symbols)
hopping between channels. And things that must NOT trigger either detector:
noise, OFDM, a CW carrier, FM carrying something that is not video.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fpv                                                            # noqa: E402

RNG = np.random.default_rng(58)
FS = 11.52e6


def composite(fs, standard, seconds):
    """Composite video, volts: sync tip -0.3, blanking 0, white 0.7."""
    line = 64e-6 if standard == "PAL" else 1 / fpv.NTSC_LINE_HZ
    n_line = line * fs
    nlines = int(seconds / line) + 1
    out = np.empty(int(nlines * n_line) + 1, dtype=np.float32)
    t_line = np.arange(int(np.ceil(n_line)) + 1) / fs
    base = RNG.uniform(0.1, 0.6, 64)
    for k in range(nlines):
        a = int(round(k * n_line))
        b = int(round((k + 1) * n_line))
        t = t_line[:b - a]
        v = np.zeros(b - a, dtype=np.float32)
        v[t < 4.7e-6] = -0.3                                   # sync
        act = (t >= 10.4e-6) & (t < line - 1.6e-6)             # active picture
        u = (t[act] - 10.4e-6) / (line - 12e-6)
        pic = np.interp(u * 63, np.arange(64), np.roll(base, k // 40))   # slowly changing scene
        v[act] = np.clip(pic + 0.05 * RNG.standard_normal(act.sum()), 0, 0.7)
        out[a:b] = v
    return out[:int(seconds * fs)]


def fm(v, fs, dev_hz, offset_hz=0.0, cnr_db=15.0):
    f = offset_hz + dev_hz * (v - 0.2) / 0.5
    x = np.exp(1j * 2 * np.pi * np.cumsum(f) / fs)
    nz = 10 ** (-cnr_db / 20) / np.sqrt(2)
    return (x + nz * (RNG.standard_normal(len(x)) + 1j * RNG.standard_normal(len(x)))).astype(np.complex64)


def lora_packets(fs, bw, sf, seconds, interval_s, offsets_hz, preamble=12, payload=20, snr_db=10):
    n = int(round(2 ** sf / bw * fs))
    t = np.arange(n) / fs
    k = bw * bw / 2 ** sf

    def chirp(sym=0, down=False):
        f0 = -bw / 2 + sym * bw / 2 ** sf
        ph = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
        # wrap the frequency at +bw/2 the way a LoRa modulator does
        tw = (2 ** sf - sym) / bw
        ph = np.where(t < tw, ph, ph - 2 * np.pi * bw * (t - tw))
        return np.exp(-1j * ph if down else 1j * ph)

    x = np.zeros(int(seconds * fs), dtype=np.complex128)
    at, i = int(0.003 * fs), 0
    while at + (preamble + payload + 5) * n < len(x):
        syms = [chirp() for _ in range(preamble)] + [chirp(24), chirp(32)] + \
               [chirp(down=True)] * 2 + [chirp(down=True)[:n // 4]] + \
               [chirp(int(s)) for s in RNG.integers(0, 2 ** sf, payload)]
        p = np.concatenate(syms)
        off = offsets_hz[i % len(offsets_hz)]
        tt = (at + np.arange(len(p))) / fs
        x[at:at + len(p)] += p * np.exp(2j * np.pi * off * tt)
        at += int(interval_s * fs)
        i += 1
    nz = 10 ** (-snr_db / 20) * np.sqrt(fs / bw) / np.sqrt(2)     # SNR in the LoRa bandwidth
    x += nz * (RNG.standard_normal(len(x)) + 1j * RNG.standard_normal(len(x)))
    return x.astype(np.complex64)


def ofdm(fs, seconds):
    nfft, cp = 768, 54
    syms = []
    for _ in range(int(seconds * fs / (nfft + cp))):
        f = np.zeros(nfft, complex)
        f[84:684] = (RNG.choice([-1, 1], 600) + 1j * RNG.choice([-1, 1], 600)) / np.sqrt(2)
        f[384] = 0
        s = np.fft.ifft(np.fft.ifftshift(f)) * np.sqrt(nfft)
        syms.append(np.r_[s[-cp:], s])
    x = np.concatenate(syms)
    return (x + 0.1 * (RNG.standard_normal(len(x)) + 1j * RNG.standard_normal(len(x)))).astype(np.complex64)


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    return bool(cond)


def main():
    ok = True
    print("analog video")
    for std in ("PAL", "NTSC"):
        for dev in (3e6, 7e6):
            v = composite(FS, std, 0.05)
            d = fpv.detect_video(fm(v, FS, dev, 0.8e6), FS)
            ok &= check(d is not None and d["standard"] == std,
                        f"{std}, deviation +-{dev / 1e6:g} MHz at 11.52 MSPS: "
                        f"{d['standard'] + ' %.1f dB' % d['score_db'] if d else 'missed'}")
    v = composite(FS, "PAL", 0.05)
    d = fpv.detect_video(fm(v, FS, 3e6, 0, cnr_db=3), FS)
    ok &= check(d is not None, f"PAL at 3 dB carrier-to-noise: {d['score_db'] if d else 'missed'} dB")
    ok &= check(fpv.nearest_video_channel(5768.6) == "R4 5769"
                and fpv.nearest_video_channel(5770.6) == "B3 5771"
                and fpv.nearest_video_channel(5500) is None,
                "channel naming: 5768.6 MHz -> R4 5769, 5770.6 -> B3 5771")

    print("analog video: must not trigger")
    noise = ((RNG.standard_normal(600000) + 1j * RNG.standard_normal(600000)) / np.sqrt(2)).astype(np.complex64)
    cw = (np.exp(2j * np.pi * 1.3e6 * np.arange(600000) / FS) + 0.05 * noise).astype(np.complex64)
    voice = fm(np.cumsum(RNG.standard_normal(600000)).astype(np.float32) / 300, FS, 2e6)
    for name, sig in (("noise", noise), ("OFDM", ofdm(FS, 0.05)), ("CW carrier", cw),
                      ("FM, no video", voice),
                      ("ExpressLRS LoRa", lora_packets(FS, 812500, 6, 0.05, 0.004, [0, 2e6, -3e6]))):
        d = fpv.detect_video(sig, FS)
        ok &= check(d is None, f"{name}: {'nothing' if d is None else 'FALSE ALARM %s' % d}")

    print("ExpressLRS LoRa")
    hops = [-4.1e6, -1.3e6, 0.9e6, 2.6e6, 4.4e6]
    for sf, interval in ((5, 0.002), (6, 0.004), (8, 0.02)):
        x = lora_packets(FS, 812500, sf, 0.12, interval, hops)
        r = fpv.detect_lora(x, FS, "2.4")
        best = max(r, key=lambda f: f["preambles"]) if r else None
        ok &= check(best is not None and best["sf"] == sf and best["hopping"],
                    f"2.4 GHz 812.5 kHz SF{sf}, a packet every {interval * 1e3:g} ms, hopping: "
                    + (fpv.describe_lora(best, 2440.0) if best else "missed"))
    x = lora_packets(FS, 500000, 9, 0.2, 0.04, [0.3e6])
    r = fpv.detect_lora(x, FS, "900")
    best = max(r, key=lambda f: f["preambles"]) if r else None
    ok &= check(best is not None and best["sf"] == 9 and not best["hopping"],
                "900 MHz 500 kHz SF9 on one channel: found, and NOT called hopping")
    x = lora_packets(FS, 812500, 7, 0.12, 0.007, hops, snr_db=0)
    r = fpv.detect_lora(x, FS, "2.4")
    ok &= check(any(f["sf"] == 7 for f in r), "SF7 at 0 dB SNR in its bandwidth")

    print("ExpressLRS LoRa: must not trigger")
    v = composite(FS, "PAL", 0.12)
    for name, sig in (("noise", np.tile(noise, 3)), ("OFDM", ofdm(FS, 0.12)),
                      ("CW carrier", np.tile(cw, 3)), ("analog video", fm(v, FS, 3e6))):
        r = fpv.detect_lora(sig, FS, "2.4") + fpv.detect_lora(sig, FS, "900")
        ok &= check(not r, f"{name}: {'nothing' if not r else 'FALSE ALARM %s' % r[0]}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
