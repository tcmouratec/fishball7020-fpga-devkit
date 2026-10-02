"""Detect the links FPV and home-built drones use: analog video and ExpressLRS.

    from fpv import detect_video, detect_lora
    v = detect_video(iq, fs)       # None, or {"standard": "PAL", "score_db": ..., ...}
    l = detect_lora(iq, fs, "2.4") # [{"bw_hz": 812500, "sf": 6, ...}, ...]

These drones broadcast no identity, so this DETECTS, it does not identify: it
says "an analog video transmitter is on R4" or "an ExpressLRS-shaped control
link is hopping around 2.44 GHz". That is what can honestly be said from the
air about an FPV drone. Pure numpy.

ANALOG VIDEO. FPV cameras send PAL or NTSC composite video as wideband FM
(typically 5.8 GHz, also 1.2-1.3 and 3.3 GHz). Every video line starts with a
sync pulse, so the FM-demodulated signal repeats at the line rate: 15 625 Hz
(PAL, 64 us lines) or 15 734.26 Hz (NTSC). The detector demodulates with a
phase-difference discriminator and measures how far the spectrum of the result
stands up at the first 24 harmonics of each line rate, against the spectrum
around them. That comb is specific: OFDM (WiFi, DJI digital video, LTE),
LoRa and noise do not make one. The FM deviation of a typical VTX exceeds what
11.52 MSPS can represent unwrapped; the line periodicity survives the
wrapping, so detection still works at that rate.

EXPRESSLRS. Its LoRa modes are fixed by the ExpressLRS source
(src/src/common.cpp, ExpressLRS_AirRateConfig):

    2.4 GHz  SX1280/LR1121  bandwidth 812.5 kHz  SF5-SF8   80 channels 2400.4-2479.4 MHz
    900 MHz  SX127x/LR1121  bandwidth 500 kHz    SF5-SF9   FCC915 903.5-926.9 (40 ch),
                                                           EU868 863.275-869.575 (13 ch), ...

A LoRa symbol is a linear chirp across the bandwidth. Multiplying by the
conjugate of a reference chirp ("dechirping") turns a chirp of the same
bandwidth and spreading factor into a single tone, whatever its frequency
offset, and an FFT then concentrates its energy into one bin. The detector
does that for each bandwidth/SF pair, in windows one symbol long, and keeps
windows whose peak stands well above the window's mean, far more than when the
same window is dechirped with the opposite chirp (which rules out carriers and
wideband signals), and repeats in the next windows: a preamble is 8-14
identical chirps. ExpressLRS hops channel every
packet (25-500 packets/s), so several distinct frequencies within one buffer
mark a hopping link rather than a fixed LoRa device such as a LoRaWAN sensor.

Not covered: ExpressLRS's FLRC and FSK modes, TBS Crossfire's FSK mode, and
the frequency-hopping FSK links of FrSky/FlySky/Spektrum radios, which need a
different detector.
"""
from __future__ import annotations

import numpy as np

PAL_LINE_HZ = 15625.0
NTSC_LINE_HZ = 4.5e6 / 286          # 15 734.27 Hz

# 5.8 GHz analog FPV channel table (MHz): the bands every VTX and goggle use.
VIDEO_CHANNELS = {
    "A": [5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725],
    "B": [5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866],
    "E": [5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945],
    "F": [5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880],
    "R": [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917],
    "L": [5362, 5399, 5436, 5473, 5510, 5547, 5584, 5621],
}
# 1.2-1.3 GHz VTX channels (the common 8-channel set)
VIDEO_CHANNELS_1G3 = [1080, 1120, 1160, 1200, 1240, 1280, 1320, 1360]

# ExpressLRS LoRa air modes: (band, bandwidth Hz, spreading factors)
ELRS_LORA = {
    "2.4": (812500.0, (5, 6, 7, 8)),
    "900": (500000.0, (5, 6, 7, 8, 9)),
}
ELRS_DOMAINS_MHZ = {
    "ISM2400": (2400.4, 2479.4),
    "FCC915": (903.5, 926.9),
    "AU915": (915.5, 926.9),
    "EU868": (863.275, 869.575),
    "IN866": (865.375, 866.95),
}


def nearest_video_channel(mhz: float, tol: float = 4.0):
    """'R4 5769' for the nearest standard channel within tol MHz, else None."""
    best = None
    for band, chans in VIDEO_CHANNELS.items():
        for i, f in enumerate(chans):
            d = abs(f - mhz)
            if d <= tol and (best is None or d < best[0]):
                best = (d, f"{band}{i + 1} {f}")
    for i, f in enumerate(VIDEO_CHANNELS_1G3):
        d = abs(f - mhz)
        if d <= tol and (best is None or d < best[0]):
            best = (d, f"1G3-{i + 1} {f}")
    return best[1] if best else None


# --------------------------------------------------------------- analog video
def _comb_score(spec, df, line_hz, harmonics=24, half=3, guard=8):
    """Mean, over harmonics, of how far the line-rate harmonic stands above
    the spectrum around it (dB). Noise gives ~0-2 dB, video 10-30 dB."""
    scores = []
    for h in range(1, harmonics + 1):
        k = int(round(h * line_hz / df))
        if k + half + 6 * guard >= len(spec):
            break
        peak = spec[k - half:k + half + 1].max()
        around = np.r_[spec[k - 6 * guard:k - guard], spec[k + guard:k + 6 * guard]]
        scores.append(10 * np.log10(peak / max(np.median(around), 1e-30)))
    return float(np.mean(scores)) if scores else 0.0


def detect_video(x: np.ndarray, fs: float, threshold_db: float = 9.0, seconds: float = 0.04):
    """Look for analog PAL/NTSC FM video in x (complex baseband at fs).

    Uses up to `seconds` of signal (40 ms: 600+ lines, ~25 Hz resolution, which
    separates PAL from NTSC's 109 Hz difference at the first harmonic and
    ~2.6 kHz at the 24th). Returns None, or a dict with the standard, the
    comb score, the carrier's offset from the centre (the mean instantaneous
    frequency), and the power."""
    n = min(len(x), int(seconds * fs))
    if n < int(0.01 * fs):
        return None
    x = np.asarray(x[:n], dtype=np.complex64)
    prod = x[1:] * np.conj(x[:-1])
    d = np.angle(prod).astype(np.float32)                        # rad/sample
    offset_hz = float(np.angle(np.sum(prod))) * fs / (2 * np.pi)
    # Only the first ~24 line harmonics (< 400 kHz) matter: average blocks of
    # samples down to >= 1 MSPS before the FFT. It costs nothing in the comb
    # and makes the FFT 8-16x shorter, which is what a Pi needs.
    dec = max(1, int(fs // 1.0e6))
    m = len(d) // dec
    d = d[:m * dec].reshape(m, dec).mean(axis=1)
    fd = fs / dec
    d -= d.mean()
    w = np.hanning(len(d)).astype(np.float32)
    nfft = 1 << int(np.ceil(np.log2(len(d))))
    spec = np.abs(np.fft.rfft(d * w, nfft)) ** 2
    df = fd / nfft
    pal = _comb_score(spec, df, PAL_LINE_HZ)
    ntsc = _comb_score(spec, df, NTSC_LINE_HZ)
    std, score = ("PAL", pal) if pal >= ntsc else ("NTSC", ntsc)
    if score < threshold_db:
        return None
    return {"kind": "analog_video", "standard": std, "score_db": round(score, 1),
            "other_db": round(min(pal, ntsc), 1), "offset_hz": round(offset_hz),
            "power_dbfs": round(float(10 * np.log10(np.mean(np.abs(x) ** 2) + 1e-30)), 1)}


# --------------------------------------------------------------- LoRa / ELRS
def _upchirp(n, fs, bw, sf):
    """One LoRa base upchirp (symbol 0) sampled at fs: -bw/2 -> +bw/2 in 2^sf/bw s."""
    t = np.arange(n) / fs
    k = bw * bw / (2 ** sf)                    # sweep rate, Hz/s
    return np.exp(1j * 2 * np.pi * (-bw / 2 * t + 0.5 * k * t * t)).astype(np.complex64)


def detect_lora(x: np.ndarray, fs: float, band: str = "2.4", par_db: float = 12.0,
                contrast_db: float = 6.0, min_repeat: int = 3, seconds: float = 0.1):
    """Find LoRa chirps with ExpressLRS's parameters for `band` ("2.4"/"900").

    Returns one dict per (bandwidth, SF) that shows up: how many preamble-like
    runs were seen, at which offsets from the centre (Hz), and whether they
    hop. An empty list means nothing with those parameters was transmitting
    in this stretch."""
    bw, sfs = ELRS_LORA[band]
    if bw >= fs:
        return []
    n_max = min(len(x), int(seconds * fs))
    x = np.asarray(x[:n_max], dtype=np.complex64)
    found = []
    for sf in sfs:
        n = int(round((2 ** sf) / bw * fs))      # samples per symbol
        if n < 16 or n * (min_repeat + 1) > len(x):
            continue
        up = _upchirp(n, fs, bw, sf)
        m = len(x) // n
        win = x[:m * n].reshape(m, n)
        nfft = 1 << int(np.ceil(np.log2(n)))      # zero-padded: a fast FFT length
        spec = np.abs(np.fft.fft(win * np.conj(up)[None, :], nfft, axis=1)) ** 2
        peak_bin = spec.argmax(axis=1)
        par = 10 * np.log10(spec.max(axis=1) / np.maximum(spec.mean(axis=1), 1e-30))
        hit = par > par_db
        # The candidates dechirped the wrong way round. A CW carrier, noise or
        # a wideband signal looks alike either way (with fs >> bw a carrier
        # alone reaches ~10 log10(fs/bw) dB); an upchirp preamble collapses to
        # one bin only the right way, so demand a clear contrast.
        idx = np.flatnonzero(hit)
        if len(idx):
            spec_d = np.abs(np.fft.fft(win[idx] * up[None, :], nfft, axis=1)) ** 2
            par_d = 10 * np.log10(spec_d.max(axis=1) / np.maximum(spec_d.mean(axis=1), 1e-30))
            hit[idx] = par[idx] - par_d > contrast_db
        # a preamble: min_repeat consecutive windows peaking at (nearly) the same bin
        runs = []
        i = 0
        while i < m:
            if not hit[i]:
                i += 1
                continue
            j = i
            while (j + 1 < m and hit[j + 1]
                   and min(abs(int(peak_bin[j + 1]) - int(peak_bin[i])),
                           nfft - abs(int(peak_bin[j + 1]) - int(peak_bin[i]))) <= 3):
                j += 1
            if j - i + 1 >= min_repeat:
                f = np.fft.fftfreq(nfft, 1 / fs)[peak_bin[i]]
                runs.append({"t_s": round(i * n / fs, 5), "symbols": j - i + 1,
                             "offset_hz": round(float(f)),
                             "par_db": round(float(par[i:j + 1].mean()), 1)})
            i = j + 1
        if runs:
            offs = sorted({round(r["offset_hz"] / bw) for r in runs})
            gaps = np.diff([r["t_s"] for r in runs])
            found.append({
                "kind": "lora", "band": band, "bw_hz": bw, "sf": sf,
                "preambles": len(runs), "distinct_channels": len(offs),
                "hopping": len(offs) >= 3,
                "packet_interval_ms": round(float(np.median(gaps)) * 1e3, 2) if len(gaps) else None,
                "par_db": round(float(np.mean([r["par_db"] for r in runs])), 1),
                "offsets_hz": sorted({r["offset_hz"] for r in runs})[:12],
            })
    # The same chirps also excite the neighbouring SFs a little; keep the best.
    if found:
        best = max(found, key=lambda f: (f["preambles"], f["par_db"]))
        found = [f for f in found if f is best or f["preambles"] >= best["preambles"] // 2
                 and f["par_db"] >= best["par_db"] - 3]
    return found


def describe_lora(d: dict, centre_mhz: float) -> str:
    what = "ExpressLRS-like hopping LoRa" if d["hopping"] else "LoRa"
    return (f"{what} {d['bw_hz'] / 1e3:g} kHz SF{d['sf']} near {centre_mhz:.1f} MHz:"
            f" {d['preambles']} packets on {d['distinct_channels']} channels"
            + (f", every {d['packet_interval_ms']} ms" if d.get("packet_interval_ms") else ""))
