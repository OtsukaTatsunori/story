#!/usr/bin/env python3
"""感情別のオリジナルBGMをプログラム合成する(著作権フリー・クレジット不要)。

エレピ風の柔らかい音色でコード進行をゆっくり鳴らす。ムードは4種:
  warm    : 温かい日常・結末 (メジャー系の王道進行)
  sad     : 不遇・喪失 (マイナー系)
  tense   : 危機・対決 (低音ドローン+不協和のうねり)
  hope    : 逆転・再起 (上昇感のある進行)

使い方:
  python3 pipeline/make_bgm.py            # assets/bgm/{mood}.wav を生成(各60秒ループ)
"""
import math
import struct
import wave
from pathlib import Path

import numpy as np

SR = 44100
LOOP_SEC = 64.0   # 1ループ64秒(8秒×8コード)
CHORD_SEC = 8.0

NOTE = {n: 440.0 * 2 ** ((i - 9) / 12) for i, n in enumerate(
    ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}


def freq(name: str, octave: int) -> float:
    return NOTE[name] * 2 ** (octave - 4)


# コード進行(各ムード8コード)。(ルート,種類,オクターブ)
PROGRESSIONS = {
    # 基本4ムード
    "warm":  [("C", "maj", 3), ("G", "maj", 3), ("A", "min", 3), ("F", "maj", 3),
              ("C", "maj", 3), ("E", "min", 3), ("F", "maj", 3), ("G", "maj", 3)],
    "sad":   [("A", "min", 3), ("F", "maj", 3), ("C", "maj", 3), ("G", "maj", 3),
              ("A", "min", 3), ("D", "min", 3), ("E", "min", 3), ("E", "min", 3)],
    "tense": [("D", "min", 2), ("D", "min", 2), ("A#", "maj", 2), ("D", "min", 2),
              ("C", "min", 2), ("D", "min", 2), ("D#", "maj", 2), ("D", "min", 2)],
    "hope":  [("F", "maj", 3), ("G", "maj", 3), ("A", "min", 3), ("C", "maj", 3),
              ("F", "maj", 3), ("G", "maj", 3), ("C", "maj", 4), ("C", "maj", 4)],
    # 拡張10ムード(動画ごとにbgm_map.jsonでピックアップして使う)
    "nostalgic":   [("F", "maj", 3), ("E", "min", 3), ("D", "min", 3), ("C", "maj", 3),
                    ("F", "maj", 3), ("C", "maj", 3), ("D", "min", 3), ("G", "maj", 3)],
    "dark":        [("A", "min", 2), ("A", "min", 2), ("F", "maj", 2), ("E", "min", 2),
                    ("A", "min", 2), ("G", "min", 2), ("F", "maj", 2), ("E", "min", 2)],
    "epic":        [("C", "min", 2), ("G#", "maj", 2), ("D#", "maj", 3), ("A#", "maj", 2),
                    ("C", "min", 2), ("G#", "maj", 2), ("A#", "maj", 2), ("C", "min", 3)],
    "gentle":      [("G", "maj", 3), ("D", "maj", 3), ("E", "min", 3), ("C", "maj", 3),
                    ("G", "maj", 3), ("C", "maj", 3), ("D", "maj", 3), ("G", "maj", 3)],
    "suspense":    [("E", "min", 2), ("F", "maj", 2), ("E", "min", 2), ("F", "maj", 2),
                    ("E", "min", 2), ("D#", "maj", 2), ("E", "min", 2), ("B", "maj", 1)],
    "bittersweet": [("C", "maj", 3), ("E", "min", 3), ("F", "maj", 3), ("F", "min", 3),
                    ("C", "maj", 3), ("A", "min", 3), ("F", "min", 3), ("C", "maj", 3)],
    "uplifting":   [("D", "maj", 3), ("A", "maj", 3), ("B", "min", 3), ("G", "maj", 3),
                    ("D", "maj", 3), ("A", "maj", 3), ("G", "maj", 3), ("D", "maj", 4)],
    "calm":        [("A", "min", 3), ("G", "maj", 3), ("F", "maj", 3), ("G", "maj", 3),
                    ("A", "min", 3), ("F", "maj", 3), ("G", "maj", 3), ("A", "min", 3)],
    "dramatic":    [("B", "min", 2), ("G", "maj", 2), ("D", "maj", 3), ("A", "maj", 2),
                    ("B", "min", 2), ("G", "maj", 2), ("A", "maj", 2), ("B", "min", 2)],
    "mystery":     [("D", "min", 3), ("D#", "maj", 3), ("D", "min", 3), ("C", "min", 3),
                    ("D", "min", 3), ("G", "min", 3), ("D#", "maj", 3), ("D", "min", 3)],
    # さらに拡張10ムード(計24)
    "serene":      [("C", "maj", 3), ("F", "maj", 3), ("C", "maj", 3), ("G", "maj", 3),
                    ("C", "maj", 3), ("F", "maj", 3), ("G", "maj", 3), ("C", "maj", 3)],
    "melancholy":  [("E", "min", 3), ("C", "maj", 3), ("D", "maj", 3), ("B", "min", 3),
                    ("E", "min", 3), ("A", "min", 3), ("B", "min", 3), ("E", "min", 3)],
    "heroic":      [("G", "maj", 2), ("C", "maj", 3), ("D", "maj", 3), ("G", "maj", 3),
                    ("E", "min", 3), ("C", "maj", 3), ("D", "maj", 3), ("G", "maj", 3)],
    "tender":      [("F", "maj", 3), ("A", "min", 3), ("A#", "maj", 3), ("F", "maj", 3),
                    ("D", "min", 3), ("A#", "maj", 3), ("C", "maj", 3), ("F", "maj", 3)],
    "anxious":     [("F#", "min", 2), ("G", "maj", 2), ("F#", "min", 2), ("A", "min", 2),
                    ("F#", "min", 2), ("C", "maj", 2), ("G", "maj", 2), ("F#", "min", 2)],
    "triumphant":  [("A#", "maj", 2), ("D#", "maj", 3), ("F", "maj", 3), ("A#", "maj", 3),
                    ("G", "min", 3), ("D#", "maj", 3), ("F", "maj", 3), ("A#", "maj", 3)],
    "lonely":      [("B", "min", 2), ("B", "min", 2), ("G", "maj", 2), ("B", "min", 2),
                    ("F#", "min", 2), ("G", "maj", 2), ("B", "min", 2), ("B", "min", 2)],
    "playful":     [("C", "maj", 4), ("A", "min", 4), ("F", "maj", 3), ("G", "maj", 3),
                    ("C", "maj", 4), ("E", "min", 4), ("F", "maj", 3), ("G", "maj", 3)],
    "solemn":      [("D", "min", 2), ("A", "min", 2), ("A#", "maj", 2), ("F", "maj", 2),
                    ("D", "min", 2), ("G", "min", 2), ("A", "min", 2), ("D", "min", 2)],
    "healing":     [("G", "maj", 3), ("B", "min", 3), ("C", "maj", 3), ("G", "maj", 3),
                    ("A", "min", 3), ("C", "maj", 3), ("D", "maj", 3), ("G", "maj", 3)],
}
INTERVALS = {"maj": (0, 4, 7), "min": (0, 3, 7)}


def tone(f: float, dur: float, vol: float, attack: float = 0.8, decay: float | None = None) -> np.ndarray:
    """エレピ風: 基音+弱い倍音、ゆっくり立ち上がってゆっくり減衰。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    decay = decay or dur
    env = np.minimum(t / attack, 1.0) * np.exp(-t / decay)
    y = (np.sin(2 * np.pi * f * t)
         + 0.35 * np.sin(2 * np.pi * 2 * f * t)
         + 0.12 * np.sin(2 * np.pi * 3 * f * t))
    # ほんのわずかなビブラート
    y *= 1.0 + 0.015 * np.sin(2 * np.pi * 0.7 * t)
    return (vol * env * y).astype(np.float64)


def render_mood(mood: str) -> np.ndarray:
    prog = PROGRESSIONS[mood]
    total = np.zeros(int(LOOP_SEC * SR))
    for ci, (root, kind, octv) in enumerate(prog):
        start = int(ci * CHORD_SEC * SR)
        base = freq(root, octv)
        # コード(パッド)
        for k, semi in enumerate(INTERVALS[kind]):
            f = base * 2 ** (semi / 12)
            v = 0.10 if k == 0 else 0.07
            seg = tone(f, CHORD_SEC * 1.6, v, attack=1.2, decay=6.0)
            end = min(start + len(seg), len(total))
            total[start:end] += seg[: end - start]
        # ベース(1オクターブ下)
        segb = tone(base / 2, CHORD_SEC * 1.4, 0.09, attack=0.5, decay=5.0)
        end = min(start + len(segb), len(total))
        total[start:end] += segb[: end - start]
        # tenseは半音上の音を薄く重ねて不穏さを出す
        if mood in ("tense", "suspense", "dark", "mystery", "anxious", "solemn", "lonely"):
            f2 = base * 2 ** (1 / 12)
            seg2 = tone(f2, CHORD_SEC, 0.03, attack=2.0, decay=4.0)
            end = min(start + len(seg2), len(total))
            total[start:end] += seg2[: end - start]
        # warm/hopeは軽いアルペジオを散らす
        if mood in ("warm", "hope", "gentle", "uplifting", "nostalgic", "bittersweet", "calm",
                    "serene", "tender", "healing", "playful", "heroic", "triumphant"):
            for j, semi in enumerate(INTERVALS[kind] + (12,)):
                f = base * 2 ** (semi / 12) * 2
                at = start + int((j * 1.9 + 0.4) * SR)
                seg3 = tone(f, 3.0, 0.045, attack=0.02, decay=1.8)
                end = min(at + len(seg3), len(total))
                if at < len(total):
                    total[at:end] += seg3[: end - at]
    # ループのつなぎ目を滑らかに(先頭と末尾をクロスフェード)
    xf = int(2.0 * SR)
    fade = np.linspace(0, 1, xf)
    total[:xf] = total[:xf] * fade + total[-xf:] * (1 - fade)
    total = total[: int((LOOP_SEC - 2.0) * SR)]
    # 正規化(ピーク-6dB)
    total *= 0.5 / (np.abs(total).max() + 1e-9)
    return total


def to_stereo(mono: np.ndarray) -> np.ndarray:
    """高音質化: 左右にわずかな時間差+短いエコーで広がりと空気感を出す。"""
    delay = int(0.012 * SR)          # 左右差12ms
    left = mono
    right = np.concatenate([np.zeros(delay), mono[:-delay]])
    # 柔らかい残響(シンプルなマルチタップエコー)
    out_l, out_r = left.copy(), right.copy()
    for tap, gain in ((0.09, 0.22), (0.17, 0.13), (0.29, 0.07)):
        d = int(tap * SR)
        out_l[d:] += gain * right[:-d]
        out_r[d:] += gain * left[:-d]
    st = np.stack([out_l, out_r], axis=1)
    st *= 0.5 / (np.abs(st).max() + 1e-9)
    return st


def write_wav(path: Path, data: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    st = to_stereo(data)
    pcm = (np.clip(st, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "assets" / "bgm"
    for mood in PROGRESSIONS:
        path = out_dir / f"{mood}.wav"
        write_wav(path, render_mood(mood))
        print(f"BGM生成: {path}")


if __name__ == "__main__":
    main()
