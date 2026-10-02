#!/usr/bin/env python3
"""Run the DroneID decoder over recordings from the public drone RF datasets.

    # run from: the repo root (needs numpy and scipy)
    tools/droneid/bench.py --preset rfuav --center-mhz 2440 DJI_MINI4PRO/*.iq
    tools/droneid/bench.py --preset dronedetect DroneDetect_V2/CLEAN/AIR_ON/*.dat
    tools/droneid/bench.py --preset tampere24 drones_2G4/*.bin
    tools/droneid/bench.py --rate 20e6 --center-mhz 2429.5 --format ci8 hackrf.iq
    tools/droneid/bench.py capture.sigmf-meta                   # rate/centre/format from SigMF

WHY IT EXISTS. This board sees ~10 MHz at a time; the datasets were recorded
with 20-200 MHz of bandwidth. This extracts every DroneID channel that fits
inside a recording (mix to baseband, filter, resample to 15.36 MSPS) and runs
the same decoder droneid_rx.py runs live, so a dataset answers "which of these
drones does the receiver find, and as what" before anyone flies one.

What a recording yields per channel: plaintext O2/O3 frames (serial, model,
positions), O4 frames (encrypted; the session hashcode identifies the drone),
serial-number frames, and bursts that were found but did not decode. Non-DJI
drones produce no DroneID at all, which is itself the answer for them: this
decoder identifies DJI, other brands need Remote ID (WiFi/Bluetooth) or a
classifier, see docs/droneid-microphase.md.

Presets (format, sample rate, centre) for the datasets this was written for;
any of them can be overridden with --format/--rate/--center-mhz:

    rfuav        RFUAV (Zhejiang Univ., 2025): cf32 at 100 MSPS; centre varies
                 per drone, so give --center-mhz from the dataset's table
    dronedetect  DroneDetect V2 (Essex): cf32 at 60 MSPS, centre 2437.5 MHz
    tampere24    Tampere Univ. (Zenodo 4264467) 2.4 GHz: ci16 at 120 MSPS, 2440 MHz
    tampere58    the same at 5.8 GHz: ci16 at 200 MSPS, 5800 MHz
    hackrf       HackRF raw files: ci8, rate and centre must be given

--detect video,elrs runs fpv.py's analog-video and LoRa/ExpressLRS detectors
over the same recordings (RFUAV and DroneRFa contain FPV links).

Exit 0 when every file was read, 1 if one could not be, 2 on a usage error.
The exit status says nothing about how many drones were found: read the summary.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from fractions import Fraction

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fpv                                                           # noqa: E402
import ocusync                                                       # noqa: E402

PRESETS = {
    "rfuav": {"format": "cf32", "rate": 100e6, "center": None},
    "dronedetect": {"format": "cf32", "rate": 60e6, "center": 2437.5},
    "tampere24": {"format": "ci16", "rate": 120e6, "center": 2440.0},
    "tampere58": {"format": "ci16", "rate": 200e6, "center": 5800.0},
    "hackrf": {"format": "ci8", "rate": None, "center": None},
}
RASTER = [2399.5, 2414.5, 2429.5, 2444.5, 2459.5,
          5736.5, 5756.5, 5776.5, 5796.5, 5816.5]
OUT_RATE = 15.36e6
HALF_SPAN = 6.0e6            # a channel needs +-4.5 MHz plus room for the filter


def read_meta(path):
    """(data path, format, rate, centre MHz) from a SigMF pair, else Nones."""
    if not (path.endswith(".sigmf-meta") or path.endswith(".sigmf-data")):
        return path, None, None, None
    base = path.rsplit(".", 1)[0]
    with open(base + ".sigmf-meta") as f:
        meta = json.load(f)
    g = meta["global"]
    dt = g.get("core:datatype", "cf32_le")
    fmt = {"cf32": "cf32", "ci16": "ci16", "ci8": "ci8", "cu8": "cu8"}.get(dt.split("_")[0], "cf32")
    caps = meta.get("captures") or [{}]
    fc = caps[0].get("core:frequency")
    return base + ".sigmf-data", fmt, g.get("core:sample_rate"), fc / 1e6 if fc else None


def open_samples(path, fmt):
    """(memmap of raw values, values per complex sample, converter)."""
    if fmt == "cf32":
        m = np.memmap(path, dtype=np.complex64, mode="r")
        return m, 1, lambda a: np.asarray(a, dtype=np.complex64)
    dt = {"ci16": np.int16, "ci8": np.int8, "cu8": np.uint8}[fmt]
    m = np.memmap(path, dtype=dt, mode="r")
    scale = {"ci16": 1 / 32768.0, "ci8": 1 / 128.0, "cu8": 1 / 128.0}[fmt]
    off = 127.5 if fmt == "cu8" else 0.0

    def conv(a):
        f = (np.asarray(a, dtype=np.float32) - off) * scale
        return f.view(np.complex64)
    return m, 2, conv


def channels_in(center, rate, plan):
    """Raster channels whose +-HALF_SPAN fits inside the recording."""
    half = rate / 2
    return [f for f in plan if abs(f * 1e6 - center * 1e6) + HALF_SPAN <= half]


def other_detectors(raw, per, conv, n_total, rate, center, kinds, out, path):
    """Analog video and LoRa/ExpressLRS over a wideband recording.

    Video: every 10 MHz window across the recording is mixed down, resampled
    to 11.52 MSPS (what the board would deliver) and tested on 40 ms out of
    every 0.5 s. LoRa: dechirped at the recording's own rate, which sees every
    channel inside it at once, on 0.1 s out of every 0.5 s."""
    from scipy.signal import resample_poly
    res = {"video": {}, "lora": {}}
    step = int(0.5 * rate)
    half = rate / 2
    if "video" in kinds:
        ratio = Fraction(11.52e6 / rate).limit_denominator(4000)
        offs = np.arange(-half + 6e6, half - 6e6 + 1, 10e6)
        n = int(0.045 * rate)
        for start in range(0, max(1, n_total - n), step):
            x = conv(raw[start * per:(start + n) * per])
            t = (start + np.arange(len(x))) / rate
            for off in offs:
                y = x * np.exp(-2j * np.pi * off * t).astype(np.complex64)
                y = resample_poly(y, ratio.numerator, ratio.denominator).astype(np.complex64)
                v = fpv.detect_video(y, 11.52e6)
                if v:
                    f = center + (off + v["offset_hz"]) / 1e6
                    ch = fpv.nearest_video_channel(f, tol=6.0) or f"{f:.1f} MHz"
                    k = f"{v['standard']} {ch}"
                    res["video"][k] = res["video"].get(k, 0) + 1
                    if out:
                        out.write(json.dumps(dict(v, file=path, freq_mhz=round(f, 2), channel=ch,
                                                  time_s=round(start / rate, 3))) + "\n")
    if "elrs" in kinds:
        band = "2.4" if center > 1500 else "900"
        n = int(0.1 * rate)
        for start in range(0, max(1, n_total - n), step):
            x = conv(raw[start * per:(start + n) * per])
            for d in fpv.detect_lora(x, rate, band, seconds=0.1):
                k = f"{d['bw_hz'] / 1e3:g} kHz SF{d['sf']}" + (" hopping" if d["hopping"] else "")
                res["lora"][k] = res["lora"].get(k, 0) + 1
                if out:
                    out.write(json.dumps(dict(d, file=path, time_s=round(start / rate, 3),
                                              freqs_mhz=[round(center + o / 1e6, 3)
                                                         for o in d["offsets_hz"]])) + "\n")
    return res


def bench_file(path, args, out):
    data_path, fmt, rate, center = read_meta(path)
    fmt = args.format or fmt or "cf32"
    rate = args.rate or rate
    center = args.center_mhz if args.center_mhz is not None else center
    if not rate or center is None:
        print(f"{path}: need --rate and --center-mhz (no preset or SigMF value)", file=sys.stderr)
        return None
    from scipy.signal import resample_poly
    raw, per, conv = open_samples(data_path, fmt)
    n_total = len(raw) // per
    if args.max_seconds:
        n_total = min(n_total, int(args.max_seconds * rate))
    kinds = set(args.detect.split(","))
    extra = other_detectors(raw, per, conv, n_total, rate, center, kinds, out, path) \
        if kinds & {"video", "elrs"} else {"video": {}, "lora": {}}
    if "droneid" not in kinds:
        return {"file": path, "format": fmt, "rate": rate, "center_mhz": center,
                "seconds": round(n_total / rate, 3), "channels": [], "frames": 0,
                "o2o3": {}, "o4_sessions": {}, "serial_frames": {}, "other": 0,
                "candidates": 0, "found_not_decoded": 0, "cpu_s": 0, **extra}
    plan = [float(f) for f in args.freqs.split(",")] if args.freqs else RASTER
    chans = channels_in(center, rate, plan)
    if not chans and not extra["video"] and not extra["lora"]:
        print(f"{path}: no DroneID channel fits in {center:g} MHz +- {rate / 2e6:g} MHz",
              file=sys.stderr)
        return None
    ratio = Fraction(OUT_RATE / rate).limit_denominator(4000)
    up, down = ratio.numerator, ratio.denominator
    chunk = int(args.chunk_ms * 1e-3 * rate)
    overlap = int(2e-3 * rate)                     # longer than a burst
    rx = {f: ocusync.Receiver(OUT_RATE) for f in chans}
    seen = set()
    summary = {"file": path, "format": fmt, "rate": rate, "center_mhz": center,
               "seconds": round(n_total / rate, 3), "channels": chans, "frames": 0,
               "o2o3": {}, "o4_sessions": {}, "serial_frames": {}, "other": 0}
    t0 = time.time()
    start = 0
    while start < n_total:
        stop = min(n_total, start + chunk + overlap)
        x = conv(raw[start * per:stop * per])
        t = (start + np.arange(len(x))) / rate
        for f in chans:
            shift = (f - center) * 1e6
            y = x * np.exp(-2j * np.pi * shift * t).astype(np.complex64)
            y = resample_poly(y, up, down).astype(np.complex64)
            for fr in rx[f].process(y):
                at = start + int(fr.sample_index * rate / OUT_RATE)
                key = (f, at // int(rate * 1e-4))       # 0.1 ms: one burst
                if key in seen:
                    continue                            # found again in the overlap
                seen.add(key)
                d = fr.as_dict()
                d.update(file=path, channel_mhz=f, time_s=round(at / rate, 6),
                         freq_mhz=round(f + fr.cfo_hz / 1e6, 4))
                summary["frames"] += 1
                g = d.get("generation")
                if g == "O2/O3" and d.get("record_crc_ok"):
                    k = f"{d['product']} {d['serial_number']}"
                    summary["o2o3"][k] = summary["o2o3"].get(k, 0) + 1
                elif g == "O4":
                    summary["o4_sessions"][d["hashcode"]] = summary["o4_sessions"].get(d["hashcode"], 0) + 1
                elif d.get("content") == "serial number":
                    s = d.get("serial_number", "?")
                    summary["serial_frames"][s] = summary["serial_frames"].get(s, 0) + 1
                else:
                    summary["other"] += 1
                if out:
                    out.write(json.dumps(d) + "\n")
        start += chunk
    st = {"cp_candidates": 0, "crc_ok": 0, "crc_fail": 0}
    for r in rx.values():
        for k in st:
            st[k] += r.stats[k]
    summary.update(candidates=st["cp_candidates"], found_not_decoded=st["crc_fail"],
                   cpu_s=round(time.time() - t0, 1), **extra)
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--preset", choices=sorted(PRESETS))
    ap.add_argument("--format", choices=("cf32", "ci16", "ci8", "cu8"))
    ap.add_argument("--rate", type=float, help="sample rate of the recordings, S/s")
    ap.add_argument("--center-mhz", type=float, help="centre frequency of the recordings")
    ap.add_argument("--freqs", help="channel centres to try, MHz (default: the DroneID raster)")
    ap.add_argument("--max-seconds", type=float, help="read at most this much of each file")
    ap.add_argument("--chunk-ms", type=float, default=50.0)
    ap.add_argument("--jsonl", help="write every frame here as one JSON object per line")
    ap.add_argument("--detect", default="droneid",
                    help="droneid, video, elrs, comma-separated (default droneid)")
    args = ap.parse_args()
    if args.preset:
        p = PRESETS[args.preset]
        args.format = args.format or p["format"]
        args.rate = args.rate or p["rate"]
        if args.center_mhz is None:
            args.center_mhz = p["center"]
    import importlib.util
    if importlib.util.find_spec("scipy") is None:
        ap.error("needs scipy for resampling: pip install scipy")
    out = open(args.jsonl, "w") if args.jsonl else None
    bad = 0
    totals = []
    for path in args.files:
        if not os.path.exists(path):
            print(f"{path}: no such file", file=sys.stderr)
            bad += 1
            continue
        s = bench_file(path, args, out)
        if s is None:
            bad += 1
            continue
        totals.append(s)
        found = []
        found += [f"{k} x{v}" for k, v in s["o2o3"].items()]
        found += [f"O4 session {k} x{v}" for k, v in s["o4_sessions"].items()]
        found += [f"serial {k} x{v}" for k, v in s["serial_frames"].items()]
        found += [f"VIDEO {k} x{v}" for k, v in s["video"].items()]
        found += [f"LoRa {k} x{v}" for k, v in s["lora"].items()]
        print(f"{path}: {s['seconds']} s, channels {', '.join(f'{c:g}' for c in s['channels'])}"
              f" | {s['frames']} frames, {s['found_not_decoded']} found-not-decoded,"
              f" {s['candidates']} candidates | {'; '.join(found) or 'nothing found'}"
              f" ({s['cpu_s']} s CPU)")
    if out:
        out.close()
    if len(totals) > 1:
        print(f"\n{len(totals)} files: {sum(t['frames'] for t in totals)} frames, "
              f"{sum(1 for t in totals if t['frames'])} files with DroneID, "
              f"{sum(t['found_not_decoded'] for t in totals)} bursts found but not decoded")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
