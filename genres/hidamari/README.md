# 陽だまり家族エッセイ（hidamari ジャンル）

家族の日常に流れる時間を主役にした、ほんわか常時感動系の15〜20分朗読エッセイ動画ジャンル。
「家族の感動話（family）」が起承転結の転で泣かせるのに対し、こちらは**ピークを作らず
さざ波のように小さな感動を置き続ける**。悪役ゼロ・死別ゼロ・確執ゼロ。
設計思想の全文は [GUIDE.md](GUIDE.md)（量産指示書v1）を参照。

## 1本の制作フロー

```bash
# 1. ディレクター・プロンプト生成（候補メニュー＋直近被り除外を自動注入）
python pipeline/generate_prompt.py --genre hidamari --out output/hidamari/ep001/director_prompt.md

# 2. 生成されたプロンプトをClaudeに投げて「物語設計書」を得る
#    → output/hidamari/ep001/design.md に保存（ここで人間が選別。
#      判断基準:「冒頭150字で微笑めるか」「自分の家族を思い出すか」「さざ波になっているか」）

# 3. 設計書末尾のJSONを履歴DB(db/hidamari.json)に記録（被り防止の中核。サボると収束が始まる）
python pipeline/generate_prompt.py --genre hidamari --record output/hidamari/ep001/design.md

# 4. プロット生成プロンプトを組み立ててClaudeへ → plot.md
python pipeline/generate_prompt.py --genre hidamari --plot output/hidamari/ep001/design.md --out output/hidamari/ep001/plot_prompt.md

# 5. review_prompt.md でプロットを検品(レビューA) → script_prompt.md で本文（楽章①〜③→④〜⑥の2分割）
#    → review_prompt.md で本文を検品(レビューB) → output/hidamari/ep001/script.md

# 6. 以降は共通パイプライン（他ジャンルと同一）
#    シーン画像 → images/ へ、emotions.json / bgm_map.json を設定して:
python pipeline/build_video.py output/hidamari/ep001 --engine voicevox --speaker 13 --workers 6 --encoder nvenc
```

## このジャンル固有の要点

- **さざ波×6楽章構成**（幸せ事件15%→ふくらむ時間10%→しみじみの瞬間10%→思い出の満ち潮30%→受けとめ15%→それからの日々20%）。
  起承転結ではない。気づきは全体の1/3地点に置き、残りを思い出と受けとめに使う
- **リテンションは謎ではなく共感**: オープンループ（未回収の謎）は使わない。
  「1分1あるある」「笑いの等間隔配置」「エコー台詞の再会」で聴かせ続ける
- **禁止物**: 死・病気・事故・確執・悪役・重い罪悪感（それらは family ジャンルの領分）。
  涙のトリガーは「あっという間に過ぎる時間」だけ
- **元エッセイの固有設定は流用禁止**（子どもだけで祭りへ/かき揚げ/跳躍表現/光る玩具/短歌締め）。
  継承するのは構造とトーンのみ。詳細は GUIDE.md 第0部
- **被り管理は「具体の一文要約」**: 設計書JSONの `trigger_summary` / `anchor_summary` /
  `item_summary` / `phrase_summary` / `opening_summary` が次回以降の禁止リストに反映される
- **量産運用ルール**: 源泉sp01（成長のうれしさびしさ）＋sp02（気づかない最後）は合計3割以下。
  視点は母:父:祖父母その他=5:3:2目安
- **BGMは自動適用**: エピソードに `bgm_map.json` を置かなければ、`bgm_defaults.json` の既定
  （明るい10曲: hanauta→sanpo→komorebi→engawa→yuuyake→hidamari→pokapoka を章の進行に均等割り当て、
  既定ムードは hidamari）が自動で使われる。個別調整したい回だけ ep フォルダに `bgm_map.json` を置けば上書きされる。
  曲はすべて `pipeline/make_bgm.py` によるプログラム合成（著作権フリー・クレジット不要）
