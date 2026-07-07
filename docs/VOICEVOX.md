# VOICEVOXで朗読音声を出力する手順

## 初回セットアップ
1. https://voicevox.hiroshiba.jp/ から公式アプリをインストールして起動
   （起動中は合成エンジンが http://127.0.0.1:50021 で待ち受ける）
   - サーバー運用なら: `docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest`
2. 話者一覧の確認: `curl -s http://127.0.0.1:50021/speakers`
   - 朗読向け男性声のおすすめ: 青山龍星(13)、玄野武宏(11)

## 毎回の実行
```bash
rm -rf output/epXXX/audio output/epXXX/narration.wav  # 別エンジンの音声が残っている場合のみ
python3 pipeline/build_video.py output/epXXX --engine voicevox --speaker 13
```
- 音声はセグメント単位でキャッシュされる。台本の一部修正時は変わった部分だけ再合成される
- 字幕(SRT)は合成音声の実測時間から自動生成されるため、エンジンを替えてもズレない

## 注意
- クレジット表記必須: 動画概要欄に「VOICEVOX:青山龍星」等を記載（話者ごとの規約を一度確認）
- VOICEVOXが起動していないと `Connection refused` になる。先にアプリ/エンジンを起動すること

## 感情的な朗読（声はそのまま、感情スタイル切替）

VOICEVOXの多くの話者には同一声質の「感情スタイル」がある（青山龍星: 熱血/不機嫌/喜び/しっぽり/かなしみ/囁き）。
加えて audio_query の intonationScale(抑揚)/speedScale/pitchScale で温度を調整できる。

1. スタイルIDを確認: `curl -s http://127.0.0.1:50021/speakers` で「青山龍星」の各スタイルのidを調べる
2. リポジトリ直下の `voice_config.json` の emotions に、感情タグ→style/抑揚を設定（初期値は目安。idは必ず確認）
3. エピソードの `output/epXXX/emotions.json` で「セグメント範囲→感情タグ」を指定
   （範囲のindexは `output/epXXX/segments.json` のindex）
4. 通常どおり実行。感情指定セグメントだけ `segNNNN.感情.wav` として再合成される
   （既存キャッシュは無効化されない。感情の設定を変えたら該当の `segNNNN.感情.wav` を消して再実行）

## シーン画像（リアルドラマ風背景）

- `output/epXXX/scenes.json` に約15シーンを定義（開始セグメントindexと画像名）
- `output/epXXX/image_prompts.md` のプロンプトでMidjourney/DALL-E等から16:9画像を生成し、
  `output/epXXX/images/scene01.png` 〜に保存（jpgでも可）
- 画像が無いシーンは自動生成背景で代替されるので、途中から差し替えても動く
- アニメーション: ズームイン/アウト/左右パンをシーンごとに巡回＋シーン間0.8秒クロスフェード。
  ズームは尺で正規化され最大1.06倍で頭打ち（拡大しすぎない）

## BGM(自動生成・クレジット不要)
- `python3 pipeline/make_bgm.py` で assets/bgm/{warm,sad,tense,hope}.wav を生成(初回は自動実行される)
- `output/epXXX/bgm_map.json` で章ごとのムードを指定(warm/sad/tense/hope)
- `--steps bgm,render` でBGMトラック生成→動画に低音量ミックス(自動ダッキング+ラウドネス正規化)
- プログラム合成のオリジナル曲のため著作権フリー。市販曲に差し替える場合は assets/bgm/ のwavを置き換えるだけ

## AivisSpeech(より自然な発音・無料・VOICEVOX互換)
1. https://aivis-project.com/ からAivisSpeechをインストールして起動(エンジンは http://127.0.0.1:10101)
2. 話者ID確認: `curl http://127.0.0.1:10101/speakers`
3. 実行: `python pipeline/build_video.py output/epXXX --engine voicevox --voicevox-url http://127.0.0.1:10101 --speaker <ID>`
   - パイプラインはVOICEVOX互換APIなのでURL/話者IDを変えるだけ。クレジット表記は話者の規約に従う
- 声色を変えない方針: voice_config.jsonはスタイル切替なし(抑揚・速度のみ)

## 読み間違いの直し方(yomi.json)
- リポジトリ直下の yomi.json に「誤読される単語→ひらがな」を追記(音声のみ置換、字幕は漢字のまま)
- 追記後、`output/epXXX/audio/` の該当セグメントwav(または全部)を削除して再実行すると差分だけ再合成される

## 背景画像の2方式
- `output/epXXX/images/scene01.png` 等 … シーン番号に対応して「順番に」使う(従来方式・最優先)
- `output/epXXX/images_random/` … フォルダ内の画像から「ランダムに」選んでスライド表示
  (ファイル名は自由。シード固定で再実行しても同じ割当。直前と同じ画像は連続しない)
- 両方あるシーンは images/ が優先。どちらも無いシーンは自動生成背景

## BGMムード一覧(bgm_map.jsonでピックアップ)
warm sad tense hope nostalgic dark epic gentle suspense bittersweet uplifting calm dramatic mystery
(全14種。1本の動画では章に合わせて3〜5種を選ぶのがおすすめ)
