#!/usr/bin/env python3
"""シーン背景画像をAPIで一括並列生成する(ChatGPT UIで3枚ずつ手動生成する作業の置き換え)。

エピソードの image_prompts.md(共通スタイル+各シーンのENプロンプト)と scenes.json を読み、
未生成のシーンだけを並列でAPIに投げて images/sceneNN.png に保存する。

対応プロバイダ:
  openai    : gpt-image-1 (環境変数 OPENAI_API_KEY)
  stability : Stable Image Core (環境変数 STABILITY_API_KEY)

使い方:
  # ドライラン(プロンプトの解析結果だけ確認。API不要)
  python3 pipeline/make_images.py output/hidamari/ep001 --dry-run

  # 一括生成(未生成分のみ。--force で全再生成)
  export OPENAI_API_KEY=sk-...
  python3 pipeline/make_images.py output/hidamari/ep001 --workers 4

  # 特定シーンだけ再生成(気に入らなかった絵の差し替え)
  python3 pipeline/make_images.py output/hidamari/ep001 --only 5,9 --force

備考:
- 生成済み(images/にファイルがある)シーンはスキップするので、途中で止めても再開できる
- 失敗したシーンは2回までリトライし、それでも失敗したら一覧を最後に表示する
- サイズはgpt-image-1の16:9最近傍(1536x1024)で生成し、build_video.py側が16:9にクロップする
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def parse_prompts(md_path: Path) -> tuple[str, dict[int, str]]:
    """image_prompts.md から共通スタイルと {シーン番号: ENプロンプト} を取り出す。"""
    text = md_path.read_text(encoding="utf-8")
    m = re.search(r"```\n(.*?)\n```", text, re.DOTALL)
    style = " ".join(m.group(1).split()) if m else ""
    style = re.sub(r",?\s*--ar\s+\S+", "", style)  # アスペクト指定はMidjourney専用なので除去
    prompts = {}
    for sec in re.finditer(r"### scene(\d+)[^\n]*\nEN: (.+?)(?:\n和:|\n###|\Z)", text, re.DOTALL):
        prompts[int(sec.group(1))] = " ".join(sec.group(2).split())
    return style, prompts


def gen_openai(prompt: str, out: Path, size: str) -> None:
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps({"model": "gpt-image-1", "prompt": prompt,
                         "size": size, "quality": "medium", "n": 1}).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    out.write_bytes(base64.b64decode(data["data"][0]["b64_json"]))


def gen_stability(prompt: str, out: Path, size: str) -> None:
    import io
    boundary = "----sd" + str(int(time.time() * 1000))
    fields = {"prompt": prompt, "aspect_ratio": "16:9", "output_format": "png"}
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://api.stability.ai/v2beta/stable-image/generate/core",
        data=body.getvalue(),
        headers={"Authorization": f"Bearer {os.environ['STABILITY_API_KEY']}",
                 "Accept": "image/*",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())


PROVIDERS = {
    "openai":    {"fn": gen_openai,    "env": "OPENAI_API_KEY",    "size": "1536x1024"},
    "stability": {"fn": gen_stability, "env": "STABILITY_API_KEY", "size": "16:9"},
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode", type=Path, help="エピソードフォルダ(例: output/hidamari/ep001)")
    ap.add_argument("--provider", default="openai", choices=PROVIDERS)
    ap.add_argument("--workers", type=int, default=4, help="並列数(既定4)")
    ap.add_argument("--only", help="対象シーン番号をカンマ区切りで限定(例: 5,9)")
    ap.add_argument("--force", action="store_true", help="生成済みでも再生成する")
    ap.add_argument("--dry-run", action="store_true", help="APIを呼ばずプロンプトだけ表示")
    args = ap.parse_args()

    ep = args.episode
    style, prompts = parse_prompts(ep / "image_prompts.md")
    scenes = json.loads((ep / "scenes.json").read_text(encoding="utf-8"))["scenes"]
    img_dir = ep / "images"
    img_dir.mkdir(exist_ok=True)
    only = {int(x) for x in args.only.split(",")} if args.only else None

    jobs = []
    for sc in scenes:
        sid = sc["id"]
        if only and sid not in only:
            continue
        out = img_dir / sc["image"]
        if out.exists() and not args.force:
            print(f"scene{sid:02d}: 生成済みのためスキップ ({out.name})")
            continue
        if sid not in prompts:
            print(f"scene{sid:02d}: image_prompts.mdにENプロンプトが見つからないためスキップ")
            continue
        jobs.append((sid, f"{style} {prompts[sid]}", out, sc["title"]))

    if args.dry_run:
        for sid, prompt, out, title in jobs:
            print(f"\n--- scene{sid:02d} {title} → {out}\n{prompt}")
        print(f"\nドライラン: {len(jobs)}枚が生成対象")
        return
    if not jobs:
        print("生成対象がありません(すべて生成済み)")
        return

    p = PROVIDERS[args.provider]
    if not os.environ.get(p["env"]):
        sys.exit(f"環境変数 {p['env']} が設定されていません")

    failed = []
    def work(sid, prompt, out, title):
        for attempt in range(3):
            try:
                t0 = time.time()
                p["fn"](prompt, out, p["size"])
                return f"scene{sid:02d}: 完了 {time.time()-t0:.0f}秒 ({title})"
            except Exception as e:
                if attempt == 2:
                    failed.append((sid, str(e)[:200]))
                    return f"scene{sid:02d}: 失敗 ({e})"
                time.sleep(5 * (attempt + 1))

    print(f"{len(jobs)}枚を{args.workers}並列で生成中 ({args.provider})…")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, *j) for j in jobs]
        for f in as_completed(futs):
            print(f.result())
    if failed:
        print(f"\n失敗: {len(failed)}枚 → --only {','.join(str(s) for s,_ in failed)} で再実行してください")
        for sid, err in failed:
            print(f"  scene{sid:02d}: {err}")
    else:
        print("\n全シーン生成完了")


if __name__ == "__main__":
    main()
