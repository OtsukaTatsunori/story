# 台本生成システムの使い方

ゴールは要素の消化ではなく**「今回作れる中で最も面白い一本」**を作ること。
抽選機は素材の提示と重複回避だけを担当し、組み合わせの決定はLLM（ストーリーディレクター）が
面白さ基準で行う2段構成。

## 生成フロー

```
① 候補メニュー生成（機械）
   banks/*.json から候補を複数サンプリング＋履歴由来の禁止リストを添付
      ↓
② ストーリーディレクター（LLM）… director_prompt.md
   候補から噛み合う要素だけを選択・組み合わせ・融合し「物語設計書」を作る
   （感動は主軸1+副軸0〜2、スカッとは中盤小→終盤大の段階設計、候補外の創作も可）
      ↓
③ プロット生成（LLM）… master_plot_prompt.md + 設計書 → プロット約6000字
      ↓
④ レビューA（LLM）… review_prompt.md
   プロ放送作家＋想定読者(58歳・製造業40年)の2視点で検品→改稿版まで出す
      ↓
⑤ 本文生成（LLM）… script_prompt.md → 朗読本文約10,000字（4分割生成）
      ↓
⑥ レビューB（LLM）… review_prompt.md（本文用）
   「一度聴いて分かるか」最優先で検品→改稿版まで出す
```

## コマンド

```bash
# ① ディレクター・プロンプト生成
python3 pipeline/generate_prompt.py --out output/ep001/director_prompt.md

# ② これをClaudeに投げて物語設計書を得る → output/ep001/design.md に保存

# ②' 設計書の選択要素を履歴DBに記録（次回以降の重複回避に使われる）
python3 pipeline/generate_prompt.py --record output/ep001/design.md

# ③ 設計書からプロット生成プロンプトを組み立て
python3 pipeline/generate_prompt.py --plot output/ep001/design.md --out output/ep001/plot_prompt.md

# ④〜⑥ review_prompt.md / script_prompt.md に成果物を添付してClaudeで実行
```

## 多様性の担保方法

- **候補の直近除外**: カテゴリごとのウィンドウ（対立軸3話、主人公6話、逆転5話…）で
  使用済みIDを候補メニューから外す（`genres/japan_tech/genre.json` の categories で調整可）
- **禁止リストの明示**: 主要カテゴリの直近使用と「感動主軸×スカッと主軸」の全期間使用済みコンボを
  ディレクターに禁止事項として渡す
- **組み合わせ爆発**: 対立軸20 × 悪役30 × 感動40 × スカッと40 × 逆転30 …
  同じ要素でも組み合わせが変われば別の話になる。ディレクターが化学反応を狙って選ぶ
- **候補外の創作**: 候補が弱ければディレクターは独自展開を作ってよい（id: "original" で記録）
- **バンクの育成**: 反応の良い型を残し、飽きられた型を入れ替える。JSONに追記するだけで拡張できる

## ファイル構成

| ファイル | 役割 |
|---|---|
| `director_prompt.md` | ストーリーディレクター（物語設計書の生成。今回の核） |
| `master_plot_prompt.md` | 設計書→プロット6000字 |
| `review_prompt.md` | レビューA（プロット）/ B（本文）。2視点検品＋改稿 |
| `script_prompt.md` | プロット→朗読本文。セリフ極端化ルール入り |
| `banks/conflicts.json` | 対立軸＝悪役の価値観・動機（20種） |
| `banks/villains.json` | 悪役の立場と手口（30種） |
| `banks/protagonists.json` | 主人公の職業・技能（25種） |
| `banks/stages.json` | 舞台（15種） |
| `banks/kando_patterns.json` | 感動パターン（40種） |
| `banks/sukatto_patterns.json` | スカッとパターン（40種） |
| `banks/reversals.json` | 逆転のきっかけと手段（30種） |
| `banks/endings.json` | 結末の形（12種） |
| `banks/relations.json` | ヒロイン/味方/裏切り/大衆反転/転機①②（各7〜10種） |
| `banks/foreshadowing.json` | 伏線の型（18種） |
| `banks/hooks.json` | 冒頭フックの型（8種）＋タイトル文法 |
| `../db/history.json` | 使用済み要素の履歴（--record で自動更新） |

## 人間の承認ポイント

最低限、②の設計書と④の改稿版プロットの2箇所は人間が目を通すこと。
設計書の段階なら方向転換のコストが最小で済む。
