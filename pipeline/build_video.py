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
import platform
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
# 二重縁取り: 同じSRTを2回焼く。1回目=外縁(太い白)、2回目=本文(白文字+黒縁)
# → 白文字/黒縁/白の外縁 のテレビ字幕風になり、どんな背景でも読める
# 字幕スタイルは step_srt が生成する subtitles.ass に定義される(二重縁取り+太字)


def run(cmd, check=True, **kw):
    # encoding指定: Windowsの既定(cp932)でffmpegのUTF-8出力を読むと
    # UnicodeDecodeErrorになるため、UTF-8+置換で読む
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if check and r.returncode != 0:
        sys.exit(f"コマンド失敗: {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
    return r


def find_jp_font() -> str:
    """PIL(背景の章タイトル)用の日本語太字フォントのファイルパスを返す。
    Noto Sans JP → 各OS標準の太字ゴシック の順で最初に見つかったものを使う。"""
    home = Path.home()
    win_user_fonts = home / "AppData/Local/Microsoft/Windows/Fonts"
    candidates = [
        # Linux (Noto)
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        # Windows: ユーザーがNoto Sans JPを入れていれば優先
        str(win_user_fonts / "NotoSansJP-Bold.ttf"),
        str(win_user_fonts / "NotoSansJP-SemiBold.ttf"),
        str(win_user_fonts / "NotoSansJP-Bold.otf"),
        str(win_user_fonts / "NotoSansJP-SemiBold.otf"),
        "C:/Windows/Fonts/NotoSansJP-Bold.otf",
        # Windows標準の太字ゴシック
        "C:/Windows/Fonts/YuGothB.ttc",   # 游ゴシック Bold
        "C:/Windows/Fonts/meiryob.ttc",   # メイリオ Bold
        "C:/Windows/Fonts/YuGothM.ttc",
        "C:/Windows/Fonts/meiryo.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
        # macOS
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    sys.exit("日本語フォントが見つかりません。Noto Sans JP等をインストールしてください。")


def sub_font_name() -> str:
    """ffmpeg字幕(libass)用のフォントファミリ名。fontconfigが名前解決する。"""
    if platform.system() == "Windows":
        home = Path.home()
        fonts_dir = home / "AppData/Local/Microsoft/Windows/Fonts"
        names = [p.name for d in (fonts_dir, Path("C:/Windows/Fonts"))
                 if d.exists() for p in d.glob("NotoSansJP*")]
        if any("SemiBold" in n for n in names):
            # SemiBoldは独立ファミリ名で登録される。Bold指定より確実に太くなる
            return "Noto Sans JP SemiBold"
        if names:
            return "Noto Sans JP"
        return "Yu Gothic"  # Windows標準。Bold指定で太字化される
    if platform.system() == "Darwin":
        return "Hiragino Sans"
    return "Noto Sans CJK JP"


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
    raw_text = (ep / "script.md").read_text(encoding="utf-8")
    text = raw_text.split("## 制作メモ")[0]
    # 制作メモ内のJSONコードブロック({"直人": "なおと", ...}形式)を
    # エピソード読み辞書(ep/yomi.json)に取り込む。人名の誤読防止。
    # 既存のep/yomi.json(手動追記)が優先される。
    if "## 制作メモ" in raw_text:
        memo = raw_text.split("## 制作メモ", 1)[1]
        for block in re.findall(r"```json\s*(\{.*?\})\s*```", memo, re.DOTALL):
            try:
                d = json.loads(block)
            except json.JSONDecodeError:
                continue
            if d and all(isinstance(v, str) and re.fullmatch(r"[ぁ-んー]+", v) for v in d.values()):
                yfile = ep / "yomi.json"
                cur = json.loads(yfile.read_text(encoding="utf-8")) if yfile.exists() else {}
                merged = {**d, **cur}
                yfile.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"segment: 読み辞書{len(d)}語を {yfile} に取り込み")
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

_YOMI_CACHE: dict | None = None


def init_yomi(ep: Path | None) -> None:
    """読み辞書を初期化する。リポジトリ直下の共通辞書(一般語)と、
    エピソード直下の辞書(人名など作品固有の読み)をマージ。エピソード側が優先。"""
    global _YOMI_CACHE
    _YOMI_CACHE = {}
    paths = [Path(__file__).resolve().parent.parent / "yomi.json"]
    if ep is not None:
        paths.append(ep / "yomi.json")
    for p in paths:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            _YOMI_CACHE.update({k: v for k, v in d.items() if not k.startswith("_")})


def load_yomi() -> dict:
    if _YOMI_CACHE is None:
        init_yomi(None)
    return _YOMI_CACHE


def spoken_text(t: str) -> str:
    """合成エンジンに渡す読み上げテキスト(記号の除去・言い換え・読み辞書)。
    ここでの置換は音声のみに影響し、字幕には影響しない。"""
    for word, yomi in sorted(load_yomi().items(), key=lambda kv: -len(kv[0])):
        t = t.replace(word, yomi)
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


def tts_voicevox(text: str, wav: Path, url: str, speaker: int, emotion: dict | None = None) -> None:
    """emotion: voice_config.jsonの感情パラメータ。
    style: 同一話者の感情スタイルID(あればspeakerを差し替え)
    speedScale/pitchScale/intonationScale/volumeScale: audio_queryを上書き"""
    import time
    import urllib.parse, urllib.request

    def post(req_url: str, data: bytes | None = None) -> bytes:
        # VOICEVOXは並列負荷で接続をリセットすることがあるためリトライする
        last: Exception | None = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(
                    req_url, data=data, method="POST",
                    headers={"Content-Type": "application/json"} if data else {})
                with urllib.request.urlopen(req, timeout=300) as res:
                    return res.read()
            except Exception as e:  # ConnectionResetError / URLError / timeout
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"VOICEVOX接続に5回失敗: {req_url.split('?')[0]} ({last})")

    emotion = emotion or {}
    spk = emotion.get("style", speaker)
    q = post(f"{url}/audio_query?speaker={spk}&text={urllib.parse.quote(text)}")
    query = json.loads(q)
    for key in ("speedScale", "pitchScale", "intonationScale", "volumeScale"):
        if key in emotion:
            query[key] = emotion[key]
    audio = post(f"{url}/synthesis?speaker={spk}&enable_interrogative_upspeak=true",
                 json.dumps(query).encode("utf-8"))
    wav.write_bytes(audio)


def tts_edge(text: str, wav: Path, voice: str) -> None:
    import asyncio, edge_tts
    asyncio.run(edge_tts.Communicate(text, voice).save(str(wav)))


def wav_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def load_emotions(ep: Path):
    """emotions.json(任意)とvoice_config.json(任意)を読み、
    セグメントindex→感情タグ と タグ→VOICEVOXパラメータ を返す。"""
    tag_of = {}
    default = "narration"
    efile = ep / "emotions.json"
    if efile.exists():
        e = json.loads(efile.read_text(encoding="utf-8"))
        default = e.get("default", "narration")
        for span in e.get("spans", []):
            for i in range(span["from"], span["to"] + 1):
                tag_of[i] = span["emotion"]
    vfile = Path(__file__).resolve().parent.parent / "voice_config.json"
    params = {}
    if vfile.exists():
        params = json.loads(vfile.read_text(encoding="utf-8")).get("emotions", {})
    # セグメント個別の上書き(emotions.jsonの"overrides")。字幕1枚単位のピンポイント調整。
    # 例: "overrides": {"123": {"speedScale": 0.85, "pitchScale": -0.03, "pause_after": 1.5}}
    overrides = {}
    if efile.exists():
        e = json.loads(efile.read_text(encoding="utf-8"))
        overrides = {int(k): v for k, v in e.get("overrides", {}).items()}
    return default, tag_of, params, overrides


def synth_one(i: int, seg: dict, wav: Path, engine: str, vv_url: str, speaker: int,
              edge_voice: str, emotion: dict) -> None:
    text = spoken_text(seg["text"])
    if not re.search(r"[ぁ-んァ-ヶ一-龠a-zA-Z0-9０-９]", text):
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.6", str(wav)])
    elif engine == "openjtalk":
        tts_openjtalk(text, wav)
    elif engine == "voicevox":
        tts_voicevox(text, wav, vv_url, speaker, emotion)
    elif engine == "edge":
        tts_edge(text, wav, edge_voice)
    else:
        sys.exit(f"未知のエンジン: {engine}")


def group_segments(segments: list, tag_of: dict, default_tag: str,
                   emo_params: dict, overrides: dict) -> list:
    """読点で分割された字幕セグメントを「文」単位にまとめる。
    文の途中でぶつ切りに合成するとイントネーションが不自然になる(棒読みの主因)ため、
    1文を1回のTTS呼び出しで合成し、字幕タイミングは後で文字数比で配分する。
    感情タグ/上書きが変わる位置では文をまたがずに区切る。"""
    groups = []
    cur = None
    for i, seg in enumerate(segments):
        tag = tag_of.get(i, default_tag)
        ov = overrides.get(i, {})
        params = dict(emo_params.get(tag, {}))
        params.update({k: v for k, v in ov.items() if k != "pause_after"})
        key = (tag, json.dumps(params, sort_keys=True))
        if (cur is not None and seg["type"] == "body" and not cur["break"]
                and key == cur["key"] and len(cur["text"]) + len(seg["text"]) <= 160):
            cur["idx"].append(i)
            cur["text"] += seg["text"]
        else:
            cur = {"idx": [i], "text": seg["text"], "key": key, "tag": tag,
                   "params": params, "ov": bool(ov)}
            groups.append(cur)
        # この文がここで終わるか(読点で終わる=まだ続く)
        cur["break"] = (seg["type"] == "title" or bool(seg.get("para_end"))
                        or not seg["text"].rstrip().endswith("、"))
    return groups


def _norm_kana(s: str) -> str:
    """読み比較用の正規化。ひらがな→カタカナ、長音表記のゆれ(オウ/オー等)を吸収。"""
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
    s = re.sub(r"[^ァ-ヶー]", "", s)
    out = []
    for c in s:
        if out and ((c == "ウ" and out[-1] in "オコソトノホモヨロヲゴゾドボポョ")
                    or (c == "イ" and out[-1] in "エケセテネヘメレゲゼデベペ")
                    or (c == "ー")):
            out.append("ー")
        else:
            out.append(c.replace("ヅ", "ズ").replace("ヂ", "ジ"))
    return "".join(out)


def check_yomi(ep: Path, url: str, speaker: int) -> None:
    """台本中の漢字語の読みを自動チェックする(合成はしない)。
    VOICEVOXの読みと形態素辞書(pykakasi)の読みを突き合わせ、
    食い違う「疑わしい語」だけを表示し ep/yomi_review.json に保存する。
    誤読は ep/yomi.json (人名等) か リポジトリ直下 yomi.json (一般語) に追記する。
    読み辞書適用後のテキストで判定するため、辞書で解決済みの語は表示されない。"""
    import urllib.parse, urllib.request
    from collections import OrderedDict
    try:
        import pykakasi
        kk = pykakasi.kakasi()
    except ImportError:
        kk = None
        print("check-yomi: pip install pykakasi を実行すると照合読みが有効になり、"
              "疑わしい語だけに自動で絞り込めます(今回は全語を表示)")
    init_yomi(ep)
    segments = json.loads((ep / "segments.json").read_text(encoding="utf-8"))
    words: OrderedDict[str, None] = OrderedDict()
    for seg in segments:
        for w in re.findall(r"[一-龠]{2,}", spoken_text(seg["text"])):
            words.setdefault(w)
    print(f"check-yomi: {len(words)}語を照合中…")
    suspects = {}
    for w in words:
        try:
            q = urllib.request.urlopen(urllib.request.Request(
                f"{url}/audio_query?speaker={speaker}&text={urllib.parse.quote(w)}",
                method="POST"), timeout=30).read()
            vv = re.sub(r"[^ァ-ヶー]", "", json.loads(q).get("kana", ""))
        except Exception as e:
            sys.exit(f"check-yomi: VOICEVOXに接続できません({e})。先にVOICEVOXを起動してください")
        if kk is None:
            print(f"  {w} → {vv}")
            continue
        ref = "".join(item["kana"] for item in kk.convert(w))
        if _norm_kana(vv) != _norm_kana(ref):
            suspects[w] = {"voicevox": vv, "reference": ref}
            print(f"  疑: {w} → VOICEVOX:{vv} / 照合:{ref}")
    if kk is not None:
        out = ep / "yomi_review.json"
        out.write_text(json.dumps(suspects, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"check-yomi: 疑わしい語 {len(suspects)}/{len(words)} → {out}")
        print("  このファイルをClaudeに貼れば、正しい読みを判定してyomi.jsonへの追記まで行えます")


def step_tts(ep: Path, engine: str, vv_url: str, speaker: int, edge_voice: str,
             workers: int = 4) -> None:
    import hashlib
    from concurrent.futures import ThreadPoolExecutor, as_completed
    segments = json.loads((ep / "segments.json").read_text(encoding="utf-8"))
    audio_dir = ep / "audio"
    audio_dir.mkdir(exist_ok=True)
    init_yomi(ep)
    default_tag, tag_of, emo_params, overrides = load_emotions(ep)

    # 文単位にまとめて合成対象の一覧(キャッシュ済みはスキップ)を作り、並列合成する
    groups = group_segments(segments, tag_of, default_tag, emo_params, overrides)
    jobs = []
    for g in groups:
        i0 = g["idx"][0]
        # キャッシュ名に「実際に読み上げるテキスト(読み辞書適用後)+感情パラメータ」の
        # ハッシュを含める。yomi.json/emotions.json/voice_config.jsonを変えると
        # 影響を受けた文だけ自動で再合成される(手動でwavを消す必要なし)
        h = hashlib.md5((spoken_text(g["text"]) + json.dumps(g["params"], sort_keys=True))
                        .encode("utf-8")).hexdigest()[:8]
        base = f"seg{i0:04d}" if len(g["idx"]) == 1 else f"sen{i0:04d}_{len(g['idx'])}"
        g["wav"] = audio_dir / f"{base}.{h}.wav"
        if not g["wav"].exists():
            jobs.append(g)
    if jobs:
        print(f"tts: {len(segments)}セグメント→{len(groups)}文 / 未合成{len(jobs)}文を{workers}並列で合成中…")
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(synth_one, g["idx"][0], {"text": g["text"]}, g["wav"],
                              engine, vv_url, speaker, edge_voice, g["params"]): g["idx"][0]
                    for g in jobs}
            for f in as_completed(futs):
                f.result()  # 例外があればここで送出
                done += 1
                if done % 50 == 0:
                    print(f"tts: {done}/{len(jobs)}")

    # 実測時間でタイムラインを構築(文の長さを字幕ごとに文字数比で配分)
    t = 0.0
    timeline = []
    for g in groups:
        dur = wav_duration(g["wav"])
        weights = [max(1, len(spoken_text(segments[i]["text"]))) for i in g["idx"]]
        wsum = sum(weights)
        last = g["idx"][-1]
        seg_last = segments[last]
        gap = overrides.get(last, {}).get(
            "pause_after",
            PAUSE_CHAP if seg_last["type"] == "title" else (PAUSE_PARA if seg_last.get("para_end") else PAUSE_SEG))
        g["gap"] = gap
        off = 0.0
        for i, w in zip(g["idx"], weights):
            d = dur * w / wsum
            timeline.append({**segments[i], "wav": g["wav"].name,
                             "start": round(t + off, 3), "end": round(t + off + d, 3),
                             "gap": gap if i == last else 0.0})
            off += d
        t += dur + gap
    (ep / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")

    # 結合: 各セグメントを統一フォーマット(44100Hz/mono/16bit)+末尾無音に変換し、
    # concat demuxer(ファイルリスト経由)でcopy結合する。
    # これにより入力ファイル数が増えてもコマンドラインが長くならない
    # (Windowsのコマンドライン長制限 WinError 206 を回避)。
    pad_dir = ep / "audio_padded"
    pad_dir.mkdir(exist_ok=True)

    def pad_one(k: int, g: dict) -> None:
        dst = pad_dir / f"p{k:04d}.wav"
        run(["ffmpeg", "-y", "-i", str(g["wav"]),
             "-af", f"aresample=44100,apad=pad_dur={g['gap']}",
             "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(dst)])

    print(f"tts: 結合準備({len(groups)}件・並列)…")
    with ThreadPoolExecutor(max_workers=max(workers, 4)) as ex:
        list(ex.map(lambda a: pad_one(*a), enumerate(groups)))
    lines = [f"file '{(pad_dir / f'p{k:04d}.wav').resolve().as_posix()}'"
             for k in range(len(groups))]
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


def fmt_ass(s: float) -> str:
    cs = int(round(s * 100))
    return f"{cs//360000}:{cs%360000//6000:02d}:{cs%6000//100:02d}.{cs%100:02d}"


def step_srt(ep: Path) -> None:
    """字幕を2形式で出力する。
    subtitles.srt : 確認用・YouTubeアップロード用
    subtitles.ass : 動画焼き込み用。二重縁取り(外=白/内=黒)と太字をスタイルとして持ち、
      外枠(Layer0)と内枠(Layer1)を同座標に重ねるため位置ズレが起きない。
      字幕同士の時間の重なりは次の字幕開始でクランプする
      (重なると字幕エンジンの衝突回避で2枚目が画面上部に押し出されてしまうため)。"""
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    # 章タイトルは画面左上にdrawtextで固定表示されるため字幕には含めない(二重表示防止)
    # 表示時刻: 終了に余韻を足しつつ、次の字幕の開始とは絶対に重ねない
    events = []
    for i, seg in enumerate(timeline):
        if seg["type"] == "title":
            continue
        end = seg["end"] + 0.25
        if i + 1 < len(timeline):
            end = min(end, timeline[i + 1]["start"])
        end = max(end, seg["start"] + 0.1)
        events.append((seg["start"], end, wrap_sub(seg["text"])))

    lines = []
    for n, (st, en, text) in enumerate(events, 1):
        lines += [str(n), f"{fmt_ts(st)} --> {fmt_ts(en)}", text, ""]
    (ep / "subtitles.srt").write_text("\n".join(lines), encoding="utf-8")

    font = sub_font_name()
    # サイズ・縁取り・下余白は720p基準のピクセル値
    # (旧force_style方式は288p基準の29が2.5倍に拡大されて約72px相当だった)
    common = "&H00FFFFFF,&H00FFFFFF,{outline_col},&H00000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,40,40,72,1"
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Outer,{font},72," + common.format(outline_col="&H00FFFFFF", outline=8.8) + "\n"
        f"Style: Inner,{font},72," + common.format(outline_col="&H00000000", outline=6.2) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev_lines = []
    for st, en, text in events:
        t = text.replace("{", "").replace("}", "").replace("\n", "\\N")
        ev_lines.append(f"Dialogue: 0,{fmt_ass(st)},{fmt_ass(en)},Outer,,0,0,0,,{t}")
        ev_lines.append(f"Dialogue: 1,{fmt_ass(st)},{fmt_ass(en)},Inner,,0,0,0,,{t}")
    (ep / "subtitles.ass").write_text(header + "\n".join(ev_lines) + "\n", encoding="utf-8")
    print(f"srt: {len(timeline)}枚 → subtitles.srt / subtitles.ass")


# ---------- 4. bg ----------

CHAPTER_PALETTES = [  # (上端, 下端) 章の感情に沿った暗色トーン
    ((24, 26, 34), (10, 10, 14)), ((30, 30, 26), (12, 12, 10)),
    ((34, 26, 24), (14, 10, 10)), ((26, 24, 34), (10, 10, 16)),
    ((20, 20, 28), (6, 6, 10)),  ((26, 30, 26), (10, 12, 10)),
    ((34, 24, 28), (14, 8, 10)), ((24, 28, 34), (8, 10, 14)),
    ((36, 30, 22), (14, 12, 8)), ((30, 32, 38), (12, 13, 16)),
]


def load_units(ep: Path) -> list[dict]:
    """映像の単位(シーン)リストを返す。
    scenes.json があればシーン単位(約15枚・実写風画像)、なければ章単位(後方互換)。
    各unit = {key, start, image(images/内の候補名), chapter}"""
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    total_end = timeline[-1]["end"]
    sfile = ep / "scenes.json"
    units = []
    if sfile.exists():
        scenes = json.loads(sfile.read_text(encoding="utf-8"))["scenes"]
        for sc in scenes:
            seg = timeline[min(sc["from_seg"], len(timeline) - 1)]
            units.append({"key": f"scene{sc['id']:02d}",
                          "start": seg["start"],
                          "image": sc.get("image", f"scene{sc['id']:02d}.png"),
                          "chapter": seg["chapter"]})
        units.sort(key=lambda u: u["start"])
        if units[0]["start"] > 0:
            units[0]["start"] = 0.0
    else:
        for ch in sorted({s["chapter"] for s in timeline}):
            segs = [s for s in timeline if s["chapter"] == ch]
            units.append({"key": f"ch{ch:02d}", "start": segs[0]["start"],
                          "image": f"ch{ch:02d}.png", "chapter": ch})
    for i, u in enumerate(units):
        u["end"] = units[i + 1]["start"] if i + 1 < len(units) else total_end
    return units


IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def fit_169(src: Path, out: Path, W2: int, H2: int) -> None:
    """画像をアスペクト比を保って16:9にセンタークロップし、2倍解像度で保存。"""
    from PIL import Image
    img = Image.open(src).convert("RGB")
    scale = max(W2 / img.width, H2 / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)))
    left, top = (img.width - W2) // 2, (img.height - H2) // 2
    img.crop((left, top, left + W2, top + H2)).save(out)


def step_bg(ep: Path) -> None:
    """背景画像の準備。優先順:
    ① images/  : シーン番号・章番号に対応した名前の画像を順番に使う(scene01.png等)
    ② images_random/ : プール内の画像からシーンごとにランダムに選ぶ
       (再現性のためシード固定。同じ画像が連続しないように選ぶ)
    ③ どちらも無ければ自動生成の暗色背景
    """
    import random as _random
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    units = load_units(ep)
    titles = {s["chapter"]: s["text"] for s in timeline if s["type"] == "title"}
    font_path = find_jp_font()
    bg_dir = ep / "bg"
    bg_dir.mkdir(exist_ok=True)
    W2, H2 = W * 2, H * 2  # Ken Burns用に大きめ

    pool = sorted(p for p in (ep / "images_random").glob("*")
                  if p.suffix.lower() in IMG_EXTS) if (ep / "images_random").exists() else []
    rng = _random.Random(len(units))  # シード固定(再実行しても同じ割当)
    prev = None
    used = {"seq": 0, "rand": 0, "auto": 0}
    for unit in units:
        ch = unit["chapter"]
        out = bg_dir / f"{unit['key']}.png"
        # ① 順番指定の画像(images/)
        seq = next((ep / "images" / (Path(unit["image"]).stem + ext)
                    for ext in IMG_EXTS if (ep / "images" / (Path(unit["image"]).stem + ext)).exists()), None)
        if seq:
            fit_169(seq, out, W2, H2)
            used["seq"] += 1
        elif pool:
            # ② ランダムプール(images_random/)。直前と同じ画像は避ける
            cand = [p for p in pool if p != prev] or pool
            pick = rng.choice(cand)
            prev = pick
            fit_169(pick, out, W2, H2)
            used["rand"] += 1
        else:
            step_bg_procedural(out, ch, titles, font_path, W2, H2)
            used["auto"] += 1
    mode = "シーン単位" if (ep / "scenes.json").exists() else "章単位"
    print(f"bg: {len(units)}枚 → bg/ ({mode} / 順番:{used['seq']} ランダム:{used['rand']} 自動:{used['auto']})")


def step_bg_procedural(out: Path, ch: int, titles: dict, font_path: str, W2: int, H2: int) -> None:
    """自作画像がない場合のフォールバック背景(暗色グラデ+章タイトル)。"""
    from PIL import Image, ImageDraw, ImageFont
    if True:
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
        # 章タイトルは背景に焼き込まない(パンで動いてちらつくため)。
        # renderのdrawtextで画面に固定描画する
        img.save(out)


# ---------- 4.5 bgm ----------

def step_bgm(ep: Path) -> None:
    """章→ムードの対応表(bgm_map.json)に従い、生成済みBGM(assets/bgm/*.wav)を
    つないで動画全体のBGMトラック(ep/bgm.wav)を作る。
    assetsが無ければ pipeline/make_bgm.py で自動生成する。"""
    root = Path(__file__).resolve().parent.parent
    bgm_dir = root / "assets" / "bgm"
    if not (bgm_dir / "warm.wav").exists():
        run([sys.executable, str(root / "pipeline" / "make_bgm.py")])
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    total = timeline[-1]["end"]
    mfile = ep / "bgm_map.json"
    default = "warm"
    ch_mood = {}
    if mfile.exists():
        m = json.loads(mfile.read_text(encoding="utf-8"))
        default = m.get("default", "warm")
        ch_mood = {int(k): v for k, v in m.get("chapters", {}).items()}
    # 章ごとの区間を作る(同ムードが続く場合は結合)
    chapters = sorted({s["chapter"] for s in timeline})
    spans = []
    for ch in chapters:
        segs = [s for s in timeline if s["chapter"] == ch]
        mood = ch_mood.get(ch, default)
        start = segs[0]["start"]
        if spans and spans[-1][2] == mood:
            spans[-1][1] = None  # 後で次の開始まで伸ばす
        else:
            spans.append([start, None, mood])
    for i, sp in enumerate(spans):
        sp[1] = spans[i + 1][0] if i + 1 < len(spans) else total + 1
    # 各区間: ムードwavをループで必要秒数切り出し、両端をフェード
    tmp = ep / "audio_padded"
    tmp.mkdir(exist_ok=True)
    parts = []
    for i, (s, e, mood) in enumerate(spans):
        dur = e - s
        p = tmp / f"bgm{i:02d}.wav"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(bgm_dir / f"{mood}.wav"),
             "-t", f"{dur:.3f}",
             "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={max(dur-2,0):.3f}:d=2",
             "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(p)])
        parts.append(f"file '{p.resolve().as_posix()}'")
    lst = ep / "_bgm_concat.txt"
    lst.write_text("\n".join(parts) + "\n", encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(ep / "bgm.wav")])
    print(f"bgm: {len(spans)}区間 ({', '.join(sp[2] for sp in spans)}) → bgm.wav")


# ---------- 5. render ----------

XFADE = 0.8   # シーン間クロスフェード(秒)
ZMAX = 1.06   # Ken Burnsの最大ズーム(尺に関係なくここで頭打ち)

# 映像エンコーダ(--encoder)。GPUがあればnvenc/qsv/amfで数倍速(画質は同等ビットレート帯)
VIDEO_CODECS = {
    "cpu":   ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"],
    "qsv":   ["-c:v", "h264_qsv", "-global_quality", "23"],
    "amf":   ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"],
}


def motion_expr(k: int, frames: int) -> str:
    """背景をゆっくり動かす(ズームなし・等倍パンのみ)。
    表示倍率はZMAX固定なので『だんだん拡大される』ことは一切ない。
    パン方向(右/左/下/上)を巡回して単調さを防ぐ。"""
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    # 移動量は余白(iw-iw/zoom)の範囲。等速でゆっくり流れる
    mode = k % 4
    if mode == 0:    # 左→右パン
        return f"zoompan=z='{ZMAX}':x='(iw-iw/zoom)*on/{frames}':y='{cy}'"
    if mode == 1:    # 右→左パン
        return f"zoompan=z='{ZMAX}':x='(iw-iw/zoom)*(1-on/{frames})':y='{cy}'"
    if mode == 2:    # 上→下パン
        return f"zoompan=z='{ZMAX}':x='{cx}':y='(ih-ih/zoom)*on/{frames}'"
    # 下→上パン
    return f"zoompan=z='{ZMAX}':x='{cx}':y='(ih-ih/zoom)*(1-on/{frames})'"


def make_light_sweep(path: Path, w: int, h: int) -> None:
    """画面を斜めに横切る、柔らかい光の帯(半透明白)のレイヤーを1枚生成する。
    画像本体は一切動かさず、この光だけをoverlayで流すことで
    章タイトル・被写体を固定したまま『生きた映像感』を出す。"""
    import math
    from PIL import Image
    band_w = int(w * 0.5)
    cx = band_w / 2
    sigma = band_w * 0.22
    col_alpha = [int(85 * math.exp(-((x - cx) / sigma) ** 2)) for x in range(band_w)]
    row = [(255, 246, 228, a) for a in col_alpha]
    band = Image.new("RGBA", (band_w, h), (0, 0, 0, 0))
    band.putdata(row * h)
    # 斜めに傾け、画面幅のキャンバスに配置
    band = band.rotate(18, expand=True, resample=Image.BICUBIC)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.paste(band, ((w - band.width) // 2, (h - band.height) // 2), band)
    path.parent.mkdir(parents=True, exist_ok=True)
    layer.save(path)


def render_scene_clip(k: int, bg: Path, clip_len: float, light: Path | None,
                      grain: bool, out: Path) -> None:
    """1シーン分の背景クリップ(パン+光の帯)を中間ファイルに前処理する。
    ここが最も重い処理なので、シーンごとに別プロセスで並列実行する。
    中間ファイルはほぼ可逆(crf 10)なので画質は落ちない。"""
    frames = int(clip_len * FPS) + 1
    grain_f = ",noise=alls=6:allf=t" if grain else ""
    LIGHT_T = 11.0  # 光が画面を1往復する周期(秒)。ゆっくり流す
    # 滑らかなパンのため4倍解像度に拡大してからzoompanし、2倍で切り出して縮小する。
    # (パンは整数ピクセル単位でしか動けないため、解像度が低いとカクつく。
    #  シーン並列化で速度に余裕ができたため4倍に戻した)
    base = (f"[0:v]fps={FPS},scale={W*4}:{H*4}:flags=lanczos,"
            f"{motion_expr(k, frames)}:d={frames}:s={W*2}x{H*2}:fps={FPS},"
            f"scale={W}:{H}:flags=lanczos")
    inputs = ["-loop", "1", "-t", f"{clip_len:.3f}", "-i", str(bg)]
    if light is not None:
        inputs += ["-loop", "1", "-t", f"{clip_len:.3f}", "-i", str(light)]
        sweep = f"[b][l]overlay=x='(W+w)*mod(t\\,{LIGHT_T})/{LIGHT_T}-w':y=0:eof_action=pass"
        fc = (f"{base}[b];[1:v]format=rgba,fps={FPS}[l];"
              f"{sweep}{grain_f},format=yuv420p[v]")
    else:
        fc = f"{base}{grain_f},format=yuv420p[v]"
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", fc, "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10",
         "-t", f"{clip_len:.3f}", str(out)])


def step_render(ep: Path, motion: bool = True, grain: bool = False,
                encoder: str = "cpu", workers: int = 4) -> None:
    from concurrent.futures import ThreadPoolExecutor
    units = load_units(ep)
    total = wav_duration(ep / "narration.wav")
    units[-1]["end"] = max(units[-1]["end"], total)

    n = len(units)
    durs = [max(u["end"] - u["start"], XFADE + 0.2) for u in units]
    light = ep / "bg" / "_light.png"
    if motion:
        make_light_sweep(light, W, H)

    # パス1: 重いシーン前処理(パン+光)をシーンごとに並列レンダリング
    clip_dir = ep / "bg" / "_clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    jobs = []
    for k, u in enumerate(units):
        clip_len = durs[k] + (XFADE if k < n - 1 else 0)
        out = clip_dir / f"c{k:03d}.mp4"
        clips.append(out)
        jobs.append((k, ep / "bg" / f"{u['key']}.png", clip_len,
                     light if motion else None, grain, out))
    print(f"render: パス1/2 {n}シーンを{workers}並列で前処理中…")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda a: render_scene_clip(*a), jobs))

    # パス2: クロスフェード結合+グレーディング+字幕+音声ミックス+最終エンコード
    inputs, fparts = [], []
    for k, c in enumerate(clips):
        inputs += ["-i", str(c)]
        fparts.append(f"[{k}:v]settb=AVTB[v{k}]")
    # ナレーション音声の入力index(クリップn本の後)
    n_light = n
    # シーン切替はクロスフェード。1枚ならそのまま
    if n == 1:
        fc = ";".join(fparts) + ";[v0]null[vid]"
    else:
        fc = ";".join(fparts)
        prev = "[v0]"
        boundary = 0.0
        for k in range(1, n):
            boundary += durs[k - 1]
            out_label = "[vid]" if k == n - 1 else f"[x{k}]"
            fc += (f";{prev}[v{k}]xfade=transition=fade:duration={XFADE}"
                   f":offset={max(boundary - XFADE, 0):.3f}{out_label}")
            prev = out_label
    # 字幕焼き込み
    # subtitlesフィルタ用にパスを正規化: Windowsの \ はエスケープ文字として
    # 食われるためフォワードスラッシュに統一し、ドライブレターの : をエスケープする
    ass = (ep / "subtitles.ass").as_posix().replace(":", "\\:")
    # 軽いシネマ調グレーディング(コントラスト/彩度を微調整+うっすらビネット)
    # → 字幕はグレーディングの後に焼くので文字はくっきりしたまま
    fc += (f";[vid]eq=contrast=1.04:saturation=1.06,vignette=PI/24,"
           f"fade=t=in:st=0:d=1.0,fade=t=out:st={total - 2.5:.3f}:d=2.5[graded]")
    # 章タイトルを左上に固定描画(背景のパンと独立なので動かない・ちらつかない)
    timeline = json.loads((ep / "timeline.json").read_text(encoding="utf-8"))
    font_arg = Path(find_jp_font()).as_posix().replace(":", "\\:")
    label = "[graded]"
    chapters = sorted({s["chapter"] for s in timeline})
    for i, ch in enumerate(chapters):
        segs = [s for s in timeline if s["chapter"] == ch]
        t0, t1 = segs[0]["start"], (timeline[[s["chapter"] for s in timeline].index(ch + 1)]["start"]
                                    if ch + 1 in chapters else total)
        title = next((s["text"] for s in segs if s["type"] == "title"), "")
        small = title.split("　")[0] if "　" in title else f"第{ch}章"
        name = title.split("　", 1)[1] if "　" in title else title
        for text, size, y in ((small, 20, 0.075), (name, 40, 0.115)):
            text = text.replace("\\", "").replace("'", "").replace(":", "：").replace(",", "，")
            nxt = "[vt]" if (i == len(chapters) - 1 and (text, size) == (name, 40)) else f"[t{i}{size}]"
            fc += (f";{label}drawtext=fontfile='{font_arg}':text='{text}'"
                   f":fontsize={size}:fontcolor=0xEDE4D6@0.92:borderw=2:bordercolor=0x000000@0.55"
                   f":x=w*0.06:y=h*{y}:enable='between(t,{t0 + 0.5:.2f},{t1:.2f})'{nxt}")
            label = nxt
    # 最終段でyuv420pに固定: 字幕・drawtext後に4:4:4へ昇格すると
    # H.264 High 4:4:4になり、Windows標準プレイヤー等で再生できなくなる
    fc += f";[vt]subtitles='{ass}',format=yuv420p[vout]"
    # BGM: 生成済みbgm.wav(感情別トラック)があればそれを、なければ環境音パッドを敷く
    bgm_wav = ep / "bgm.wav"
    if bgm_wav.exists():
        inputs2 = ["-i", str(bgm_wav)]
        bgm_src = f"[{n_light + 1}:a]volume=1.0[bgm]"
    else:
        inputs2 = []
        bgm_src = (f"aevalsrc='0.02*sin(2*PI*110*t)+0.015*sin(2*PI*164.8*t)+0.012*sin(2*PI*220*t)"
                   f"+0.006*sin(2*PI*329.6*t)':s=44100,tremolo=f=0.15:d=0.4,volume=0.5[bgm]")
    # ラウドネスをYouTube標準(-14LUFS)に正規化し、終端をフェードアウト
    # 朗読がメイン。BGMは薄く敷く程度(比率を上げたい場合はここを調整)
    fc += (f";{bgm_src};[{n_light}:a][bgm]amix=inputs=2:duration=first:weights='1 0.18',"
           f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={total - 2.5:.3f}:d=2.5[aout]")

    # 一時ファイルに書き出し、完了後にvideo.mp4へ置き換える。
    # (書き込み途中のmp4は再生情報が末尾に無く「サポートされていない」エラーになるため、
    #  レンダリング中にユーザーがvideo.mp4を開いても壊れたファイルを見ずに済む)
    tmp_out = ep / "_render_tmp.mp4"
    cmd = (["ffmpeg", "-y"] + inputs + ["-i", str(ep / "narration.wav")] + inputs2 + [
           "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
           ] + VIDEO_CODECS[encoder] + [
           "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-t", f"{total:.3f}", str(tmp_out)])
    def run_render(c) -> int:
        # 進捗(time=..., speed=...)をそのまま画面に流す
        c = c[:1] + ["-v", "error", "-stats"] + c[1:]
        return subprocess.run(c).returncode

    print(f"render: パス2/2 結合+字幕+音声(encoder={encoder})。下のtime=が動画内の処理済み時刻です…")
    if run_render(cmd) != 0:
        if encoder != "cpu":
            # GPUエンコーダが使えない環境(ドライバ古い等)はCPUに自動フォールバック
            print(f"\nrender: {encoder} が使えないためCPU(libx264)で再実行します")
            print("  ヒント: NVIDIAドライバを最新に更新するとnvencが使えます")
            idx = cmd.index(VIDEO_CODECS[encoder][1])
            cmd2 = cmd[:idx - 1] + VIDEO_CODECS["cpu"] + cmd[idx - 1 + len(VIDEO_CODECS[encoder]):]
            if run_render(cmd2) != 0:
                sys.exit("render失敗(CPU)。上のffmpegエラーを確認してください")
        else:
            sys.exit("render失敗。上のffmpegエラーを確認してください")
    import os
    os.replace(tmp_out, ep / "video.mp4")
    print(f"render: 完了 → {ep/'video.mp4'} ({total/60:.1f}分)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", type=Path, help="例: output/ep001")
    ap.add_argument("--engine", default="openjtalk", choices=["openjtalk", "voicevox", "edge"])
    ap.add_argument("--voicevox-url", default="http://127.0.0.1:50021")
    ap.add_argument("--speaker", type=int, default=13, help="VOICEVOX話者ID(13=青山龍星)")
    ap.add_argument("--edge-voice", default="ja-JP-KeitaNeural")
    ap.add_argument("--steps", default="segment,tts,srt,bg,bgm,render")
    ap.add_argument("--no-motion", action="store_true",
                    help="手ぶれ微振動(擬似モーション)を無効化する")
    ap.add_argument("--grain", action="store_true",
                    help="フィルムグレインを追加(空気感が増すがレンダリングが重くなる)")
    ap.add_argument("--workers", type=int, default=4,
                    help="TTS合成・音声変換の並列数(既定4)")
    ap.add_argument("--check-yomi", action="store_true",
                    help="台本中の漢字語の読みをVOICEVOXに問い合わせて一覧表示(誤読の事前発見用)")
    ap.add_argument("--preview", type=int, metavar="N",
                    help="セグメントNだけ合成して preview_segN.wav を出力(全編を作り直さず試聴)")
    ap.add_argument("--encoder", default="cpu", choices=list(VIDEO_CODECS),
                    help="映像エンコーダ。NVIDIA GPUならnvenc、Intel内蔵ならqsv、AMDならamfで高速化")
    args = ap.parse_args()
    steps = args.steps.split(",")
    ep = args.episode
    if args.check_yomi:
        check_yomi(ep, args.voicevox_url, args.speaker)
        return
    if args.preview is not None:
        init_yomi(ep)
        segments = json.loads((ep / "segments.json").read_text(encoding="utf-8"))
        i = args.preview
        seg = segments[i]
        default_tag, tag_of, emo_params, overrides = load_emotions(ep)
        tag = tag_of.get(i, default_tag)
        params = dict(emo_params.get(tag, {}))
        params.update({k: v for k, v in overrides.get(i, {}).items() if k != "pause_after"})
        out = ep / f"preview_seg{i}.wav"
        synth_one(i, seg, out, args.engine, args.voicevox_url, args.speaker,
                  args.edge_voice, params)
        print(f"セグメント{i} [{tag}] {params}")
        print(f"テキスト: {seg['text']}")
        print(f"試聴ファイル: {out}")
        return
    if "segment" in steps: step_segment(ep)
    if "tts" in steps: step_tts(ep, args.engine, args.voicevox_url, args.speaker,
                                args.edge_voice, args.workers)
    if "srt" in steps: step_srt(ep)
    if "bg" in steps: step_bg(ep)
    if "bgm" in steps: step_bgm(ep)
    if "render" in steps: step_render(ep, motion=not args.no_motion, grain=args.grain,
                                      encoder=args.encoder, workers=args.workers)


if __name__ == "__main__":
    main()
