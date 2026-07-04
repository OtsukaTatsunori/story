#!/usr/bin/env python3
"""ストーリーディレクター・プロンプトの組み立てツール。

役割は「決定」ではなく「素材の提示」。各バンクから候補を複数サンプリングし、
履歴DB由来の禁止リストとともに prompts/director_prompt.md に差し込む。
最終的な選択・組み合わせはLLM（ストーリーディレクター）が面白さ基準で行う。

フロー:
  1. python pipeline/generate_prompt.py --out output/ep001/director_prompt.md
  2. 生成されたプロンプトをClaudeに投げ、物語設計書を得る → output/ep001/design.md
  3. 設計書末尾のJSONを履歴に記録:
     python pipeline/generate_prompt.py --record output/ep001/design.md
  4. master_plot_prompt.md の {{design}} に設計書を差し込んでプロット生成
     python pipeline/generate_prompt.py --plot output/ep001/design.md --out output/ep001/plot_prompt.md
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANKS = ROOT / "prompts" / "banks"
DIRECTOR_TEMPLATE = ROOT / "prompts" / "director_prompt.md"
FORESHADOW_GUIDE = ROOT / "prompts" / "foreshadowing_guide.md"
PLOT_TEMPLATE = ROOT / "prompts" / "master_plot_prompt.md"
HISTORY = ROOT / "db" / "history.json"

# カテゴリ定義: (バンクファイル, JSON内キー, 候補提示数, 禁止ウィンドウ[直近n話])
CATEGORIES = {
    "conflict":    ("conflicts.json", "items", 6, 3),
    "villain":     ("villains.json", "items", 6, 5),
    "protagonist": ("protagonists.json", "items", 6, 6),
    "stage":       ("stages.json", "items", 5, 4),
    "kando":       ("kando_patterns.json", "items", 8, 4),
    "sukatto":     ("sukatto_patterns.json", "items", 8, 4),
    "turn1":       ("relations.json", "turns1", 4, 2),
    "turn2":       ("relations.json", "turns2", 4, 2),
    "reversal":    ("reversals.json", "items", 6, 5),
    "heroine":     ("relations.json", "heroines", 4, 3),
    "ally":        ("relations.json", "allies", 4, 3),
    "betrayal":    ("relations.json", "betrayals", 3, 2),
    "crowd":       ("relations.json", "crowds", 3, 2),
    "ending":      ("endings.json", "items", 4, 3),
    "hook":        ("hooks.json", "items", 4, 3),
    "foreshadow":  ("foreshadowing.json", "items", 8, 2),
}

CATEGORY_LABELS = {
    "conflict": "対立軸（悪役の価値観・動機。物語の背骨）",
    "villain": "悪役の立場と手口（対立軸と組み合わせて人格を設計する）",
    "protagonist": "主人公の職業・技能",
    "stage": "舞台",
    "kando": "感動パターン（主軸1＋副軸0〜2を選ぶ）",
    "sukatto": "スカッとパターン（中盤の小＋終盤の大、と段階設計する）",
    "turn1": "一つ目の転（主人公を沈める）",
    "turn2": "二つ目の転（空気を変える）",
    "reversal": "逆転のきっかけと手段",
    "heroine": "ヒロインとの関係",
    "ally": "途中から味方になる人物",
    "betrayal": "裏切りの扱い",
    "crowd": "大衆が味方に変わる理由",
    "ending": "結末の形",
    "hook": "序盤のつかみ・冒頭30秒フックの型",
    "foreshadow": "伏線の型（3〜5個選ぶ）",
}

# 履歴JSONのキー → 禁止判定に使うカテゴリのマッピング
HISTORY_KEY_TO_CATEGORY = {
    "conflict": "conflict", "villain": "villain", "protagonist": "protagonist",
    "stage": "stage", "kando_main": "kando", "kando_sub": "kando",
    "sukatto_main": "sukatto", "sukatto_sub": "sukatto",
    "turn1": "turn1", "turn2": "turn2", "reversal": "reversal",
    "heroine": "heroine", "ally": "ally", "betrayal": "betrayal",
    "crowd": "crowd", "ending": "ending", "hook": "hook", "foreshadow": "foreshadow",
}


def load_bank(filename: str, key: str) -> list[dict]:
    return json.loads((BANKS / filename).read_text(encoding="utf-8"))[key]


def load_history() -> list[dict]:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return []


def used_ids(history: list[dict], category: str, window: int) -> set[str]:
    """直近window話でそのカテゴリとして使われたIDの集合。"""
    ids: set[str] = set()
    for ep in history[-window:] if window else []:
        for key, cat in HISTORY_KEY_TO_CATEGORY.items():
            if cat != category:
                continue
            v = ep.get("choices", {}).get(key)
            if isinstance(v, list):
                ids.update(x for x in v if isinstance(x, str))
            elif isinstance(v, str):
                ids.add(v)
    ids.discard("original")
    return ids


def item_label(it: dict) -> str:
    name = f"【{it['name']}】" if it.get("name") else ""
    tone = f"（悪役の物言い: {it['villain_tone']}）" if it.get("villain_tone") else ""
    return f"- `{it['id']}` {name}{it['text']}{tone}"


def build_director(seed, out: Path | None) -> None:
    rng = random.Random(seed)
    history = load_history()

    menu_parts: list[str] = []
    for cat, (filename, key, n, window) in CATEGORIES.items():
        items = load_bank(filename, key)
        banned = used_ids(history, cat, window)
        pool = [it for it in items if it["id"] not in banned]
        if len(pool) < n:
            pool = items
        picked = rng.sample(pool, min(n, len(pool)))
        menu_parts.append(f"### {CATEGORY_LABELS[cat]}\n" + "\n".join(item_label(it) for it in picked))
    candidates = "\n\n".join(menu_parts)

    # 禁止リスト: 直近使用の主要要素 + 全期間の感動×スカッと主軸コンボ
    banned_lines: list[str] = []
    for cat in ("conflict", "kando", "sukatto", "reversal", "protagonist"):
        window = CATEGORIES[cat][3]
        ids = used_ids(history, cat, window)
        if ids:
            banned_lines.append(f"- {CATEGORY_LABELS[cat]}: {', '.join(sorted(ids))}（直近{window}話で使用済み）")
    combos = sorted({f"{ep['choices'].get('kando_main')}×{ep['choices'].get('sukatto_main')}"
                     for ep in history
                     if ep.get("choices", {}).get("kando_main") and ep.get("choices", {}).get("sukatto_main")})
    if combos:
        banned_lines.append(f"- 感動主軸×スカッと主軸の組み合わせ（全期間で再使用禁止）: {', '.join(combos)}")
    banned_text = "\n".join(banned_lines) if banned_lines else "（初回のため禁止事項なし）"

    template = DIRECTOR_TEMPLATE.read_text(encoding="utf-8")
    body = template.split("---", 1)[1].lstrip() if "---" in template else template
    guide = FORESHADOW_GUIDE.read_text(encoding="utf-8")
    prompt = (body.replace("{{candidates}}", candidates)
                  .replace("{{banned}}", banned_text)
                  .replace("{{foreshadow_guide}}", guide))

    unresolved = re.findall(r"\{\{(\w+)\}\}", prompt)
    if unresolved:
        sys.exit(f"未解決のスロットがあります: {unresolved}")

    emit(prompt, out, "ディレクター・プロンプト")


def build_plot(design_path: Path, out: Path | None) -> None:
    design = design_path.read_text(encoding="utf-8")
    template = PLOT_TEMPLATE.read_text(encoding="utf-8")
    body = template.split("---", 1)[1].lstrip() if "---" in template else template
    prompt = body.replace("{{design}}", design)
    emit(prompt, out, "プロット生成プロンプト")


def record(design_path: Path) -> None:
    """設計書末尾のJSONコードブロックを履歴に取り込む。"""
    text = design_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not blocks:
        sys.exit("設計書にJSONコードブロックが見つかりません")
    choices = json.loads(blocks[-1])
    history = load_history()
    history.append({"episode": len(history) + 1, "choices": choices, "design_file": str(design_path)})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"履歴に記録しました: episode {len(history)} ({HISTORY})")


def emit(prompt: str, out: Path | None, label: str) -> None:
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(prompt, encoding="utf-8")
        print(f"{label}を書き出しました: {out}")
    else:
        print(prompt)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--plot", type=Path, metavar="DESIGN_MD",
                    help="設計書からプロット生成プロンプトを組み立てる")
    ap.add_argument("--record", type=Path, metavar="DESIGN_MD",
                    help="設計書末尾のJSONを履歴DBに記録する")
    args = ap.parse_args()

    if args.record:
        record(args.record)
    elif args.plot:
        build_plot(args.plot, args.out)
    else:
        build_director(args.seed, args.out)


if __name__ == "__main__":
    main()
