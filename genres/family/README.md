# 家族・親子 感動ストーリー（family ジャンル）

50〜60代向け、家族の関係性を主題にした20〜30分の朗読動画ジャンル。
設計思想の全文は [GUIDE.md](GUIDE.md)（量産指示書v2）を参照。

## 1本の制作フロー

```bash
# 1. ディレクター・プロンプト生成（候補メニュー＋直近被り除外を自動注入）
python pipeline/generate_prompt.py --genre family --out output/family/ep001/director_prompt.md

# 2. 生成されたプロンプトをClaudeに投げて「物語設計書」を得る
#    → output/family/ep001/design.md に保存（ここで人間が選別。承認率6〜7割想定）

# 3. 設計書末尾のJSONを履歴DB(db/family.json)に記録（被り防止の中核。サボると収束が始まる）
python pipeline/generate_prompt.py --genre family --record output/family/ep001/design.md

# 4. プロット生成プロンプトを組み立ててClaudeへ → plot.md
python pipeline/generate_prompt.py --genre family --plot output/family/ep001/design.md --out output/family/ep001/plot_prompt.md

# 5. review_prompt.md でプロットを検品(レビューA) → script_prompt.md で本文（幕①〜③→④〜⑥の2分割）
#    → review_prompt.md で本文を検品(レビューB) → output/family/ep001/script.md

# 6. 以降は共通パイプライン（日本技術ジャンルと同一）
#    シーン画像15枚 → images/ へ、emotions.json / bgm_map.json を設定して:
python pipeline/build_video.py output/family/ep001 --engine voicevox --speaker 13 --workers 6 --encoder nvenc
```

## このジャンル固有の要点

- **6幕構成**（コールドオープン5%→日常15%→亀裂20%→深化20%→転25%→結15%）。
  script.md は章見出しを付けず、ひと続きの朗読本文として書く（章立てはしない方針）。
  動画のシーン区切り・BGMは章見出しではなく scenes.json / bgm_map.json 側で設定する
- **被り管理は「具体の一文要約」**: 設計書JSONの `item_summary` / `twist_summary` / `opening_summary` が
  次回以降の禁止リストに反映される。カテゴリ名でなく具体で書くこと
- **量産運用ルール**: 関係性の配分は 親子:夫婦:きょうだい:祖父母孫・姻族 = 4:3:2:1 目安。
  エンジン1(遅効性の気づき)とエンジン6(余命・死別)は全体の2割以下
- ナレーター話者・BGMムード・字幕仕様はパイプライン共通（必要ならジャンルごとに変えられる）
