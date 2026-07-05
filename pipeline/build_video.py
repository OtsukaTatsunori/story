#!/usr/bin/env python3
"""台本(script.md) → YouTube動画(mp4) 生成パイプライン。

工程:
  1. segment : 台本を字幕単位のセグメントに分割 (segments.json)
  2. tts     : セグメントごとに音声合成し、実測時間からタイムライン生成
               エンジンはアダプタ式: openjtalk(ローカル/デモ用) | voicevox(ローカルAPI) | edge(edge-tts)
  3. srt     : タイムラインから字幕(SRT)生成
  4. bg      : 章ごとの背景画像を生成(PIL)。images/ に自作画像を置けばそちらを優先
  5. render  : ffmpegでKen Burns背景+朗読音声+BGM+焼き込み字幕のmp4を出力

使い方:
  python3 pipeline/build_video.py output/ep001 --engine openjtalk
  python3 pipeline/build_video.py output/ep001 --engine voicevox --voicevox-url http://127.0.0.1:50021 --speaker 13
  python3 pipeline/build_video.py output/ep001 --steps segment,tts,srt,bg,render
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SUB_MAX = 40          # 字幕1枚の最大文字数(20字×2行)
SUB_LINE = 20         # 字幕1行の最大文字数
PAUSE_SEG = 0.30      # セグメント間ポーズ(秒)
PAUSE_PARA = 0.65     # 段落間
PAUSE_CHAP = 1.60     # 章間
FPS = 24
W, H = 1280, 720

# 字幕スタイル(libassのPlayResY=288基準。MarginV=29 ≒ 画面下10%の余白)
# FontName: Noto Sans JP相当(Noto Sans CJK JP)。縁取り(Outline)で読みやすく
SUB_STYLE = ("FontName=Noto Sans CJK JP,Bold=1,FontSize=29,"
             "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
             "BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV=29")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f"コマンド失敗: {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
    return r


# ---------- 1. segment ----------

def split_sentence(s: str) -> list[str]:
    """字幕サイズに文を分割。句読点で細かく割ってからSUB_MAX以内に貪欲に結合する。
    文の途中でぶつ切りにしない(句読点のない超長文のみ最後の手段として強制分割)。"""
    if len(s) <= SUB_MAX:
        return [s]
    parts, buf = [], ""
    for ch in s:
        buf += ch
        if ch in "、。！？":
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    chunks, cur = [], ""
    for p in parts:
        if len(p) > SUB_MAX:  # 句読点なしの超長句のみ強制分割
            if cur:
                chunks.append(cur)
                cur = ""
            while len(p) > SUB_MAX:
                chunks.append(p[:SUB_MAX])
                p = p[SUB_MAX:]
            cur = p
        elif len(cur) + len(p) <= SUB_MAX:
            cur += p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def step_segment(ep: Path) -> None:
    text = (ep / "script.md").read_text(encoding="utf-8")
    text = text.split("## 制作メモ")[0]
    segments, chapter = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("# ") or line == "---":
            continue
        if line.startswith("## "):
            chapter += 1
            segments.append({"chapter": chapter, "type": "title", "text": line[3:].strip()})
            continue
        # 朗読用に整形: 記号をポーズ・読みに変換
        spoken_para = line
        for sent in split_sentence(line):
            segments.append({"chapter": chapter, "type": "body", "text": sent})
        segments[-1]["para_end"] = True
    (ep / "segments.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"segment: {len(segments)}セグメント / {chapter}章")


# ---------- 2. tts ----------

def spoken_text(t: str) -> str:
    """合成エンジンに渡す読み上げテキスト(記号の除去・言い換え)。"""
    t = re.sub(r"[「」『』【】]", "", t)
    t = t.replace("——", "、").replace("……", "、").replace("…", "、")
    t = t.replace("〇・〇三", "ゼロてんゼロさん").replace("〇・三", "ゼロてんさん").replace("〇・〇一", "ゼロてんゼロいち")
    t = re.sub(r"[※#*]", "", t)
    return t.strip("、 ")


def tts_openjtalk(text: str, wav: Path) -> None:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        tf = f.name
    run(["open_jtalk", "-x", "/var/lib/mecab/dic/open-jtalk/naist-jdic",
         "-m", "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice",
         "-r", "1.0", "-ow", str(wav), tf])


def tts_voicevox(text: str, wav: Path, url: str, speaker: int) -> None:
    import urllib.parse, urllib.request
    q = urllib.request.urlopen(urllib.request.Request(
        f"{url}/audio_query?speaker={speaker}&text={urllib.parse.quote(text)}", method="POST")).read()
    audio = urllib.request.urlopen(urllib.request.Request(
        f"{url}/synthesis?speaker={speaker}", data=q,
        headers={"Content-Type": "application/json"}, method="POST")).read()
    wav.write_bytes(audio)


def tts_edge(text: str, wav: Path, voice: str) -> None:
    import asyncio, edge_tts
    asyncio.run(edge_tts.Communicate(text, voice).save(str(wav)))


def wav_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def step_tts(ep: Path, engine: str, vv_url: str, speaker: int, edge_voice: str) -> None:
    segments = json.loads((ep / "segments.json").read_text(encoding="utf-8"))
    audio_dir = ep / "audio"
    audio_dir.mkdir(exist_ok=True)
    t = 0.0
    timeline = []
    for i, seg in enumerate(segments):
        wav = audio_dir / f"seg{i:04d}.wav"
        text = spoken_text(seg["text"])
        if not wav.exists():
            if not re.search(r"[ぁ-んァ-ヶ一-龠a-zA-Z0-9０-９]", text):
                run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                     "-t", "0.6", str(wav)])
            elif engine == "openjtalk":
                tts_openjtalk(text, wav)
            elif engine == "voicevox":
                tts_voicevox(text, wav, vv_url, speaker)
            elif engine == "edge":
                tts_edge(text, wav, edge_voice)
            else:
                sys.exit(f"未知のエンジン: {engine}")
        dur = wav_duration(wav)
        timeline.append({**seg, "wav": wav.name, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
        t += PAUSE_CHAP if seg["type"] == "title" else (PAUSE_PARA if seg.get("para_end") else PAUSE_SEG)
        if (i + 1) % 25 == 0:
            print(f"tts: {i+1}/{len(segments)}  {t/60:.1f}分")
    (ep / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")

    # 結合: 各セグメントを統一フォーマット(44100Hz/mono/16bit)+末尾無音に変換し、
    # concat demuxer(ファイルリスト経由)でcopy結合する。
    # これにより入力ファイル数が増えてもコマンドラインが長くならない
    # (Windowsのコマンドライン長制限 WinError 206 を回避)。
    pad_dir = ep / "audio_padded"
    pad_dir.mkdir(exist_ok=True)
    lines = []
    for i, seg in enumerate(timeline):
        src = audio_dir / seg["wav"]
        dst = pad_dir / f"p{i:04d}.wav"
        gap = PAUSE_CHAP if seg["type"] == "title" else (PAUSE_PARA if seg.get("para_end") else PAUSE_SEG)
        run(["ffmpeg", "-y", "-i", str(src),
             "-af", f"aresample=44100,apad=pad_dur={gap}",
             "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(dst)])
        lines.append(f"file '{dst.resolve().as_posix()}'")
        if (i + 1) % 100 == 0:
            print(f"  結合準備 {i+1}/{len(timeline)}")
    list_path = ep / "_concat.txt"
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(ep / "narration.wav")])
    total = wav_duration(ep / "narration.wav")
    print(f"tts: 完了 {total/60:.1f}分 → narration.wav")


# ---------- 3. srt ----------

def fmt_ts(s: float) -> str:
    ms = int(round(s * 1000))
    return f"{ms//3600000:02d}:{ms%3600000//60000:02d}:{ms%60000//1000:02d},{ms%1000:03d}"


def wrap_sub(text: str) -> str:
    """字幕テキストを最大2行(1行15〜20字)に折り返す。句読点優先で自然な位置で割る。"""
    if len(text) <= SUB_LINE:
        return text
    mid = len(text) / 2
    # 句読点・記号のうち中央に最も近い位置で割る
    breaks = [i + 1 for i, ch in enumerate(text[:-1]) if ch in "、。！？…」』——"]
    cand = [b for b in breaks if len(text) - b <= SUB_LINE and b <= SUB_LINE]
    if cand:
        pos = min(cand, key=lambda b: abs(b - mid))
    else:
        pos = min(int(mid + 0.5), SUB_LINE)
    return text[:pos] + "\n" + text[pos:]


def step_srt(ep: Path) -> None:
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    lines = []
    for n, seg in enumerate(timeline, 1):
        end = seg["end"] + (0.25 if not seg["type"] == "title" else 0.1)
        lines += [str(n), f"{fmt_ts(seg['start'])} --> {fmt_ts(end)}", wrap_sub(seg["text"]), ""]
    (ep / "subtitles.srt").write_text("\n".join(lines), encoding="utf-8")
    print(f"srt: {len(timeline)}枚 → subtitles.srt")


# ---------- 4. bg ----------

CHAPTER_PALETTES = [  # (上端, 下端) 章の感情に沿った暗色トーン
    ((24, 26, 34), (10, 10, 14)), ((30, 30, 26), (12, 12, 10)),
    ((34, 26, 24), (14, 10, 10)), ((26, 24, 34), (10, 10, 16)),
    ((20, 20, 28), (6, 6, 10)),  ((26, 30, 26), (10, 12, 10)),
    ((34, 24, 28), (14, 8, 10)), ((24, 28, 34), (8, 10, 14)),
    ((36, 30, 22), (14, 12, 8)), ((30, 32, 38), (12, 13, 16)),
]


def step_bg(ep: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    chapters = sorted({s["chapter"] for s in timeline})
    titles = {s["chapter"]: s["text"] for s in timeline if s["type"] == "title"}
    font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
    bg_dir = ep / "bg"
    bg_dir.mkdir(exist_ok=True)
    W2, H2 = W * 2, H * 2  # Ken Burns用に大きめ
    for ch in chapters:
        custom = ep / "images" / f"ch{ch:02d}.png"  # 自作画像があれば優先
        out = bg_dir / f"ch{ch:02d}.png"
        if custom.exists():
            Image.open(custom).convert("RGB").resize((W2, H2)).save(out)
            continue
        top, bottom = CHAPTER_PALETTES[(ch - 1) % len(CHAPTER_PALETTES)]
        img = Image.new("RGB", (W2, H2))
        px = img.load()
        for y in range(H2):
            r = y / H2
            col = tuple(int(t + (b - t) * r) for t, b in zip(top, bottom))
            for x in range(0, W2, 4):
                for dx in range(4):
                    if x + dx < W2:
                        px[x + dx, y] = col
        d = ImageDraw.Draw(img, "RGBA")
        # 斜光
        for i in range(60):
            a = int(18 * (1 - i / 60))
            d.polygon([(W2*0.55+i*8, 0), (W2*0.62+i*8, 0), (W2*0.30+i*8, H2), (W2*0.23+i*8, H2)],
                      fill=(255, 240, 210, a))
        # ビネット
        for i in range(120):
            a = int(80 * (i / 120) ** 2)
            d.rectangle([i*6, i*4, W2-i*6, H2-i*4], outline=(0, 0, 0, min(a, 4)), width=6)
        # 章タイトルは左上(下部の字幕域を避ける)
        title = titles.get(ch, "")
        name = title.split("　", 1)[1] if "　" in title else title
        d.text((W2*0.07, H2*0.10), title.split("　")[0] if "　" in title else f"第{ch}章",
               font=ImageFont.truetype(font_path, 40), fill=(200, 180, 140, 200))
        d.text((W2*0.07, H2*0.155), name, font=ImageFont.truetype(font_path, 84),
               fill=(235, 228, 214, 235))
        img.save(out)
    print(f"bg: {len(chapters)}枚 → bg/")


# ---------- 5. render ----------

def step_render(ep: Path) -> None:
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    chapters = sorted({s["chapter"] for s in timeline})
    spans = {}
    for ch in chapters:
        segs = [s for s in timeline if s["chapter"] == ch]
        spans[ch] = (segs[0]["start"], segs[-1]["end"])
    total = wav_duration(ep / "narration.wav")
    spans[chapters[-1]] = (spans[chapters[-1]][0], total)

    inputs, fparts, labels = [], [], []
    for k, ch in enumerate(chapters):
        start, end = spans[ch]
        end = spans[chapters[k+1]][0] if k + 1 < len(chapters) else total
        dur = max(end - start, 0.5)
        frames = int(dur * FPS) + 1
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(ep / "bg" / f"ch{ch:02d}.png")]
        # ゆっくりズームのKen Burns。章ごとにズーム方向を交互に
        z = f"1.0+0.00008*on" if k % 2 == 0 else f"1.08-0.00008*on"
        fparts.append(
            f"[{k}:v]fps={FPS},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={W}x{H}:fps={FPS},format=yuv420p[v{k}]")
        labels.append(f"[v{k}]")
    n = len(chapters)
    fc = ";".join(fparts) + ";" + "".join(labels) + f"concat=n={n}:v=1:a=0[vid]"
    # 字幕焼き込み
    srt = str(ep / "subtitles.srt").replace(":", "\\:")
    style = SUB_STYLE
    fc += f";[vid]subtitles='{srt}':force_style='{style}'[vout]"
    # BGM: 静かな環境音パッド(プレースホルダ)を生成してダッキング的に低音量で敷く
    bgm = (f"aevalsrc='0.02*sin(2*PI*110*t)+0.015*sin(2*PI*164.8*t)+0.012*sin(2*PI*220*t)"
           f"+0.006*sin(2*PI*329.6*t)':s=44100,tremolo=f=0.15:d=0.4,volume=0.5[bgm]")
    fc += f";{bgm};[{n}:a][bgm]amix=inputs=2:duration=first:weights='1 0.35'[aout]"
    cmd = (["ffmpeg", "-y"] + inputs + ["-i", str(ep / "narration.wav"),
           "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", str(ep / "video.mp4")])
    print("render: ffmpeg実行中(数分かかります)…")
    run(cmd)
    print(f"render: 完了 → {ep/'video.mp4'} ({total/60:.1f}分)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", type=Path, help="例: output/ep001")
    ap.add_argument("--engine", default="openjtalk", choices=["openjtalk", "voicevox", "edge"])
    ap.add_argument("--voicevox-url", default="http://127.0.0.1:50021")
    ap.add_argument("--speaker", type=int, default=13, help="VOICEVOX話者ID(13=青山龍星)")
    ap.add_argument("--edge-voice", default="ja-JP-KeitaNeural")
    ap.add_argument("--steps", default="segment,tts,srt,bg,render")
    args = ap.parse_args()
    steps = args.steps.split(",")
    ep = args.episode
    if "segment" in steps: step_segment(ep)
    if "tts" in steps: step_tts(ep, args.engine, args.voicevox_url, args.speaker, args.edge_voice)
    if "srt" in steps: step_srt(ep)
    if "bg" in steps: step_bg(ep)
    if "render" in steps: step_render(ep)


if __name__ == "__main__":
    main()
