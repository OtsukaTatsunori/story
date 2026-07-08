#!/usr/bin/env python3
"""ストーリーディレクター・プロンプトの組み立てツール（マルチジャンル対応）。

役割は「決定」ではなく「素材の提示」。ジャンルごとの各バンクから候補を複数サンプリングし、
履歴DB由来の禁止リストとともに genres/<genre>/director_prompt.md に差し込む。
最終的な選択・組み合わせはLLM（ストーリーディレクター）が面白さ基準で行う。

ジャンルは genres/<genre>/ 配下に自己完結する:
  genre.json        カテゴリ定義(バンク・候補数・冷却期間)・禁止ルール
  banks/*.json      候補バンク
  director_prompt.md / master_plot_prompt.md / script_prompt.md / review_prompt.md

フロー（例: 家族ジャンル）:
  1. python pipeline/generate_prompt.py --genre family --out output/family/ep001/director_prompt.md
  2. 生成されたプロンプトをClaudeに投げ、物語設計書を得る → output/family/ep001/design.md
  3. 設計書末尾のJSONを履歴に記録:
     python pipeline/generate_prompt.py --genre family --record output/family/ep001/design.md
  4. master_plot_prompt.md の {{design}} に設計書を差し込んでプロット生成
     python pipeline/generate_prompt.py --genre family --plot output/family/ep001/design.md --out output/family/ep001/plot_prompt.md
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENRES_DIR = ROOT / "genres"
DEFAULT_GENRE = "japan_tech"


class Genre:
    def __init__(self, name: str):
        self.dir = GENRES_DIR / name
        if not self.dir.exists():
            avail = ", ".join(sorted(p.name for p in GENRES_DIR.iterdir() if p.is_dir()))
            sys.exit(f"ジャンル '{name}' がありません。利用可能: {avail}")
        self.name = name
        cfg = json.loads((self.dir / "genre.json").read_text(encoding="utf-8"))
        self.categories: dict = cfg["categories"]
        self.history_keys: dict = cfg.get("history_keys", {})
        self.banned_list_categories: list = cfg.get("banned_list_categories", [])
        self.combo_ban: dict | None = cfg.get("combo_ban")
        # 履歴内のフリーテキスト要約キー → {label, window}
        # (象徴アイテム等、IDではなく「具体の一文要約」で被り管理する軸)
        self.summary_keys: dict = cfg.get("summary_keys", {})
        self.history_path = ROOT / "db" / f"{name}.json"

    def bank(self, filename: str, key: str) -> list[dict]:
        return json.loads((self.dir / "banks" / filename).read_text(encoding="utf-8"))[key]

    def history(self) -> list[dict]:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        return []

    def category_of_key(self, key: str) -> str | None:
        """履歴JSONのキー → カテゴリ名。明示マップ優先、なければ同名カテゴリ。"""
        if key in self.history_keys:
            return self.history_keys[key]
        return key if key in self.categories else None


def used_ids(g: Genre, history: list[dict], category: str, window: int) -> set[str]:
    """直近window話でそのカテゴリとして使われたIDの集合。"""
    ids: set[str] = set()
    for ep in history[-window:] if window else []:
        for key, v in ep.get("choices", {}).items():
            if g.category_of_key(key) != category:
                continue
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


def build_director(g: Genre, seed, out: Path | None) -> None:
    rng = random.Random(seed)
    history = g.history()

    menu_parts: list[str] = []
    for cat, c in g.categories.items():
        items = g.bank(c["file"], c["key"])
        banned = used_ids(g, history, cat, c["window"])
        pool = [it for it in items if it["id"] not in banned]
        if len(pool) < c["n"]:
            pool = items
        picked = rng.sample(pool, min(c["n"], len(pool)))
        menu_parts.append(f"### {c['label']}\n" + "\n".join(item_label(it) for it in picked))
    candidates = "\n\n".join(menu_parts)

    # 禁止リスト: 直近使用の主要要素(ID管理) + 全期間の主軸コンボ + 具体の一文要約
    banned_lines: list[str] = []
    for cat in g.banned_list_categories:
        window = g.categories[cat]["window"]
        ids = used_ids(g, history, cat, window)
        if ids:
            banned_lines.append(f"- {g.categories[cat]['label']}: "
                                f"{', '.join(sorted(ids))}（直近{window}話で使用済み）")
    if g.combo_ban:
        keys = g.combo_ban["keys"]
        combos = sorted({"×".join(str(ep["choices"].get(k)) for k in keys)
                         for ep in history
                         if all(ep.get("choices", {}).get(k) for k in keys)})
        if combos:
            banned_lines.append(f"- {g.combo_ban['label']}: {', '.join(combos)}")
    for key, meta in g.summary_keys.items():
        vals = [ep["choices"].get(key) for ep in history[-meta["window"]:]
                if ep.get("choices", {}).get(key)]
        if vals:
            banned_lines.append(f"- {meta['label']}（直近{meta['window']}話の具体。"
                                f"近い具体は避けること）: " + " / ".join(vals))
    banned_text = "\n".join(banned_lines) if banned_lines else "（初回のため禁止事項なし）"

    template = (g.dir / "director_prompt.md").read_text(encoding="utf-8")
    body = template.split("---", 1)[1].lstrip() if "---" in template else template
    prompt = body.replace("{{candidates}}", candidates).replace("{{banned}}", banned_text)
    guide_path = g.dir / "foreshadowing_guide.md"
    if "{{foreshadow_guide}}" in prompt and guide_path.exists():
        prompt = prompt.replace("{{foreshadow_guide}}", guide_path.read_text(encoding="utf-8"))

    unresolved = re.findall(r"\{\{(\w+)\}\}", prompt)
    if unresolved:
        sys.exit(f"未解決のスロットがあります: {unresolved}")

    emit(prompt, out, "ディレクター・プロンプト")


def build_plot(g: Genre, design_path: Path, out: Path | None) -> None:
    design = design_path.read_text(encoding="utf-8")
    template = (g.dir / "master_plot_prompt.md").read_text(encoding="utf-8")
    body = template.split("---", 1)[1].lstrip() if "---" in template else template
    prompt = body.replace("{{design}}", design)
    emit(prompt, out, "プロット生成プロンプト")


def record(g: Genre, design_path: Path) -> None:
    """設計書末尾のJSONコードブロックを履歴に取り込む。"""
    text = design_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not blocks:
        sys.exit("設計書にJSONコードブロックが見つかりません")
    choices = json.loads(blocks[-1])
    history = g.history()
    history.append({"episode": len(history) + 1, "choices": choices, "design_file": str(design_path)})
    g.history_path.parent.mkdir(parents=True, exist_ok=True)
    g.history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"履歴に記録しました: {g.name} episode {len(history)} ({g.history_path})")


def emit(prompt: str, out: Path | None, label: str) -> None:
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(prompt, encoding="utf-8")
        print(f"{label}を書き出しました: {out}")
    else:
        print(prompt)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--genre", default=DEFAULT_GENRE,
                    help=f"ジャンル名(genres/配下のフォルダ名。既定: {DEFAULT_GENRE})")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--plot", type=Path, metavar="DESIGN_MD",
                    help="設計書からプロット生成プロンプトを組み立てる")
    ap.add_argument("--record", type=Path, metavar="DESIGN_MD",
                    help="設計書末尾のJSONを履歴DBに記録する")
    args = ap.parse_args()

    g = Genre(args.genre)
    if args.record:
        record(g, args.record)
    elif args.plot:
        build_plot(g, args.plot, args.out)
    else:
        build_director(g, args.seed, args.out)


if __name__ == "__main__":
    main()
