#!/usr/bin/env python3
"""Standalone song analysis — runs as subprocess, survives server restarts.

2.0 (2026-09-01): rewritten to need only numpy + ffmpeg — no librosa/matplotlib.
(上一版要 librosa 全家桶,2G 小机器装不动;现在 ffmpeg 解码 + numpy 手搓频谱,
 输出同一份 preanalysis.json,外加鼓点密度/频段占比这些「听感」指标。)

Requires: numpy (pip install numpy) and ffmpeg on PATH.
Point the server at a python that has numpy via env MUSIC_ANALYZE_PYTHON.

Usage:
    python3 analyze_song.py <song_id> [song_name] [song_artist] [cache_dir]

Output: <cache_dir>/<song_id>_preanalysis.json
    {songId, name, artist, duration, bpm, key, rms, onsetRate,
     bands: {low, mid, high}, segments: [{start, end, avgEnergy, maxEnergy}]}
"""
import json
import subprocess
import sys
from pathlib import Path


def main():
    song_id = sys.argv[1]
    song_name = sys.argv[2] if len(sys.argv) > 2 else ""
    song_artist = sys.argv[3] if len(sys.argv) > 3 else ""
    cache_dir = Path(sys.argv[4]) if len(sys.argv) > 4 else Path(__file__).resolve().parent / "data" / "music_cache"

    audio_file = cache_dir / f"{song_id}.mp3"
    result_file = cache_dir / f"{song_id}_preanalysis.json"
    marker_file = cache_dir / f"{song_id}.analyzing"
    err_file = cache_dir / f"{song_id}_analyze_error.txt"

    try:
        import numpy as np

        sr = 22050
        raw = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(audio_file), "-ac", "1",
             "-ar", str(sr), "-f", "f32le", "-"],
            capture_output=True, check=True).stdout
        y = np.frombuffer(raw, dtype=np.float32)
        if len(y) < sr:
            raise RuntimeError("audio too short or decode failed")
        duration = len(y) / sr

        n, hop = 2048, 512
        nf = (len(y) - n) // hop
        idx = np.arange(n)[None, :] + hop * np.arange(nf)[:, None]
        frames = y[idx] * np.hanning(n)
        mag = np.abs(np.fft.rfft(frames, axis=1))
        freqs = np.fft.rfftfreq(n, 1 / sr)

        # ── 鼓点/onset:谱通量正差分,峰间至少 100ms ──
        flux = np.maximum(np.diff(mag, axis=0), 0).sum(axis=1)
        flux = flux / (flux.max() + 1e-9)
        thr = flux.mean() + 1.2 * flux.std()
        cand = np.where((flux[1:-1] > thr)
                        & (flux[1:-1] >= flux[:-2])
                        & (flux[1:-1] >= flux[2:]))[0] + 1
        onsets = []
        for t in cand:
            if not onsets or (t - onsets[-1]) * hop / sr > 0.1:
                onsets.append(int(t))
        onset_rate = len(onsets) / duration

        # ── 节奏:onset 包络自相关,60~200 BPM ──
        z = flux - flux.mean()
        ac = np.correlate(z, z, "full")[len(z) - 1:]
        lag_min = int(60 / 200 * sr / hop)
        lag_max = int(60 / 60 * sr / hop)
        bpm = 60 / ((lag_min + int(np.argmax(ac[lag_min:lag_max]))) * hop / sr)

        # ── 调性:能量折进 12 个音级(80~5000Hz),取最重的 ──
        E = mag ** 2
        band = (freqs >= 80) & (freqs < 5000)
        with np.errstate(divide="ignore"):
            midi = 69 + 12 * np.log2(freqs[band] / 440.0)
        pc = (np.round(midi).astype(int)) % 12
        chroma = np.zeros(12)
        col = E[:, band].sum(axis=0)
        for k in range(12):
            chroma[k] = col[pc == k].sum()
        keys = ["A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#"]
        dominant_key = keys[int(np.argmax(chroma))]

        # ── 频段能量占比(听感:鼓底/主体/镲光) ──
        tot = E.sum() + 1e-9
        def share(lo, hi):
            return round(float(E[:, (freqs >= lo) & (freqs < hi)].sum() / tot * 100), 1)
        bands = {"low": share(40, 130), "mid": share(130, 2000), "high": share(5000, sr / 2)}

        # ── 能量分段(与上一版同形:6 段 RMS 弧线) ──
        rms = np.sqrt((frames ** 2).mean(axis=1))
        seg_count = 6
        seg_len = max(1, len(rms) // seg_count)
        segments = []
        for i in range(seg_count):
            seg = rms[i * seg_len:(i + 1) * seg_len]
            if not len(seg):
                break
            segments.append({
                "start": round(i * seg_len * hop / sr, 1),
                "end": round(min((i + 1) * seg_len, len(rms)) * hop / sr, 1),
                "avgEnergy": round(float(seg.mean()), 4),
                "maxEnergy": round(float(seg.max()), 4),
            })

        result = {
            "songId": song_id, "name": song_name, "artist": song_artist,
            "duration": round(duration, 1), "bpm": round(float(bpm)),
            "key": dominant_key, "rms": round(float(np.sqrt((y ** 2).mean())), 3),
            "onsetRate": round(float(onset_rate), 2), "bands": bands,
            "segments": segments,
        }
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        marker_file.unlink(missing_ok=True)
    except Exception as e:
        marker_file.unlink(missing_ok=True)
        err_file.write_text(str(e))


if __name__ == "__main__":
    main()
