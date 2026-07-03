#!/usr/bin/env python3
"""プロット生成用プロンプトの組み立てツール。

prompts/banks/*.json から各要素をランダム選択し、
prompts/master_plot_prompt.md のスロットに差し込んで完成プロンプトを出力する。

多様性の担保:
- db/history.json に過去に使った要素IDを記録し、直近 N 話で使った要素は選択候補から除外する
- 感動パターン×スカッとパターンの「組み合わせ」も直近の重複を避ける

使い方:
    python pipeline/generate_prompt.py                # 新しい組み合わせで生成
    python pipeline/generate_prompt.py --seed 42      # 再現可能な生成
    python pipeline/generate_prompt.py --dry-run      # 選択結果だけ表示(履歴に記録しない)
    python pipeline/generate_prompt.py --out output/ep001/plot_prompt.md
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANKS = ROOT / "prompts" / "banks"
TEMPLATE = ROOT / "prompts" / "master_plot_prompt.md"
HISTORY = ROOT / "db" / "history.json"

# 各スロットについて「直近何話ぶんの使用済みIDを除外するか」
# バンクが小さいものはウィンドウも小さくする(候補が尽きないように)
SLOTS = {
    # slot名: (バンクファイル, JSON内のキー, 除外ウィンドウ)
    "protagonist": ("protagonists.json", "items", 8),
    "stage":       ("stages.json", "items", 5),
    "villain":     ("villains.json", "items", 6),
    "heroine":     ("relations.json", "heroines", 4),
    "ally":        ("relations.json", "allies", 4),
    "betrayal":    ("relations.json", "betrayals", 3),
    "turn1":       ("relations.json", "turns1", 3),
    "turn2":       ("relations.json", "turns2", 3),
    "reversal":    ("reversals.json", "items", 7),
    "crowd":       ("relations.json", "crowds", 3),
    "ending":      ("endings.json", "items", 5),
    "hook":        ("hooks.json", "items", 4),
    "kando":       ("kando_patterns.json", "items", 6),
    "sukatto":     ("sukatto_patterns.json", "items", 6),
}
FORESHADOW_COUNT = 4  # 伏線の型は毎回4種類指定
FORESHADOW_WINDOW = 2  # 直近2話で使った型は避ける(4個ずつ使うのでウィンドウは浅く)


def load_bank(filename: str, key: str) -> list[dict]:
    data = json.loads((BANKS / filename).read_text(encoding="utf-8"))
    return data[key]


def load_history() -> list[dict]:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return []


def recent_ids(history: list[dict], slot: str, window: int) -> set[str]:
    ids: set[str] = set()
    for ep in history[-window:] if window else []:
        v = ep.get("choices", {}).get(slot)
        if isinstance(v, list):
            ids.update(v)
        elif v:
            ids.add(v)
    return ids


def pick(rng: random.Random, items: list[dict], excluded: set[str]) -> dict:
    pool = [it for it in items if it["id"] not in excluded]
    if not pool:  # 除外で候補が尽きたら全体から選ぶ
        pool = items
    return rng.choice(pool)


def format_item(it: dict) -> str:
    name = it.get("name")
    return f"【{name}】{it['text']}" if name else it["text"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="選択結果のみ表示、履歴に記録しない")
    ap.add_argument("--out", type=Path, default=None, help="完成プロンプトの出力先")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    history = load_history()

    choices: dict = {}
    rendered: dict[str, str] = {}
    for slot, (filename, key, window) in SLOTS.items():
        items = load_bank(filename, key)
        excluded = recent_ids(history, slot, window)
        it = pick(rng, items, excluded)
        choices[slot] = it["id"]
        rendered[slot] = format_item(it)

    # 感動×スカッとの組み合わせ重複チェック(全期間)
    used_combos = {(ep["choices"].get("kando"), ep["choices"].get("sukatto")) for ep in history}
    if (choices["kando"], choices["sukatto"]) in used_combos:
        sukatto_items = load_bank("sukatto_patterns.json", "items")
        alt = [s for s in sukatto_items
               if (choices["kando"], s["id"]) not in used_combos
               and s["id"] not in recent_ids(history, "sukatto", SLOTS["sukatto"][2])]
        if alt:
            it = rng.choice(alt)
            choices["sukatto"] = it["id"]
            rendered["sukatto"] = format_item(it)

    # 伏線: 4種類を重複なしで選ぶ
    fs_items = load_bank("foreshadowing.json", "items")
    fs_excluded = recent_ids(history, "foreshadow", FORESHADOW_WINDOW)
    fs_pool = [it for it in fs_items if it["id"] not in fs_excluded]
    if len(fs_pool) < FORESHADOW_COUNT:
        fs_pool = fs_items
    fs_picked = rng.sample(fs_pool, FORESHADOW_COUNT)
    choices["foreshadow"] = [it["id"] for it in fs_picked]
    rendered["foreshadow"] = "\n" + "\n".join(f"  - {format_item(it)}" for it in fs_picked)

    # テンプレートに差し込み
    template = TEMPLATE.read_text(encoding="utf-8")
    # テンプレート冒頭の説明部(--- より前)を除去
    body = template.split("---", 1)[1].lstrip() if "---" in template else template
    prompt = re.sub(r"\{\{(\w+)\}\}", lambda m: rendered.get(m.group(1), m.group(0)), body)

    unresolved = re.findall(r"\{\{(\w+)\}\}", prompt)
    if unresolved:
        sys.exit(f"未解決のスロットがあります: {unresolved}")

    print("== 今回の選択 ==")
    for slot in list(SLOTS) + ["foreshadow"]:
        print(f"  {slot:12s}: {choices[slot]}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(prompt, encoding="utf-8")
        print(f"\nプロンプトを書き出しました: {args.out}")
    else:
        print("\n" + "=" * 60 + "\n")
        print(prompt)

    if not args.dry_run:
        episode = {"episode": len(history) + 1, "choices": choices}
        history.append(episode)
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"履歴を記録しました: episode {episode['episode']} ({HISTORY})")


if __name__ == "__main__":
    main()
