#!/usr/bin/env python3
"""Standalone song analysis — runs as subprocess, survives server restarts.

Requires: librosa, numpy, matplotlib (optional dependencies for audio analysis).
These are NOT required by the main eryu server.

Usage:
    python3 analyze_song.py <song_id> [song_name] [song_artist] [cache_dir]
"""
import json
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
        import librosa
        import numpy as np

        y, sr = librosa.load(str(audio_file), sr=22050)
        duration = librosa.get_duration(y=y, sr=sr)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        rms = librosa.feature.rms(y=y)[0]
        times_rms = librosa.times_like(rms, sr=sr)
        chroma = librosa.feature.chroma_stft(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)
        keys = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        dominant_key = keys[int(np.argmax(chroma_mean))]

        seg_count = 6
        seg_len = len(rms) // seg_count
        segments = []
        for i in range(seg_count):
            seg = rms[i * seg_len:(i + 1) * seg_len]
            t0 = float(times_rms[i * seg_len])
            t1 = float(times_rms[min((i + 1) * seg_len - 1, len(times_rms) - 1)])
            segments.append({
                "start": round(t0, 1), "end": round(t1, 1),
                "avgEnergy": round(float(np.mean(seg)), 4),
                "maxEnergy": round(float(np.max(seg)), 4),
            })

        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import librosa.display

        fig, axes = plt.subplots(3, 1, figsize=(14, 10))
        S = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128)
        S_dB = librosa.power_to_db(S, ref=np.max)
        librosa.display.specshow(S_dB, sr=sr, x_axis='time', y_axis='mel', ax=axes[0])
        axes[0].set_title('Mel Spectrogram')
        librosa.display.specshow(chroma, sr=sr, x_axis='time', y_axis='chroma', ax=axes[1])
        axes[1].set_title('Chromagram')
        axes[2].plot(times_rms, rms, color='#e74c3c', linewidth=0.8)
        axes[2].fill_between(times_rms, rms, alpha=0.3, color='#e74c3c')
        axes[2].set_title('Energy (RMS)')
        axes[2].set_xlabel('Time (s)')
        plt.tight_layout()
        img_path = cache_dir / f"{song_id}_analysis.png"
        plt.savefig(str(img_path), dpi=150)
        plt.close()

        result = {
            "songId": song_id, "name": song_name, "artist": song_artist,
            "duration": round(duration, 1), "bpm": round(float(tempo)),
            "key": dominant_key, "segments": segments,
            "spectrogram": str(img_path),
        }
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        marker_file.unlink(missing_ok=True)
    except Exception as e:
        marker_file.unlink(missing_ok=True)
        err_file.write_text(str(e))


if __name__ == "__main__":
    main()
