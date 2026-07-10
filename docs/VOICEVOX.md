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
- 音声は「文」単位で合成される。読点で分かれた字幕セグメントは1文にまとめて1回で合成する
  （ぶつ切り合成によるイントネーション崩れ=棒読みを防ぐ）。字幕タイミングは文字数比で自動配分
- 疑問文は語尾が自然に上がる（enable_interrogative_upspeak）
- 音声は文単位でキャッシュされる（wav名に読み上げテキスト+感情パラメータのハッシュ入り）。
  台本・読み辞書・感情設定を変えると、影響を受けた文だけ自動で再合成される（手動削除不要）
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

## 読み間違いの防止（3層構造）

1. **台本生成時**: script_prompt が制作メモに読み辞書JSONを出力させ、segmentステップが
   `output/epXXX/yomi.json` に自動取り込みする（人名の誤読を発生源で防ぐ）
2. **自動チェック**: `pip install pykakasi`(初回のみ)の上で
   `python pipeline/build_video.py output/epXXX --check-yomi`
   VOICEVOXの読みと形態素辞書の読みを自動照合し、**食い違う疑わしい語だけ**を
   `output/epXXX/yomi_review.json` に保存する。このファイルをClaudeに貼れば
   正しい読みの判定とyomi.jsonへの追記まで自動で行える（目視チェック不要）
3. **辞書の2層**: 人名など作品固有 → `output/epXXX/yomi.json` / 一般語 → リポジトリ直下 `yomi.json`
   （蓄積型。エピソード側が優先。キャッシュはハッシュで自動無効化されるので追記→再実行だけでよい）

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

## 字幕1枚単位の音声調整(overrides)と試聴
- セグメント番号の確認: output/epXXX/segments.json (字幕1枚=1セグメント。timeline.jsonで時刻も分かる)
- emotions.json に個別上書きを追加:
  "overrides": { "123": {"speedScale": 0.85, "pitchScale": -0.03, "intonationScale": 1.4, "pause_after": 1.5} }
  (pause_after=そのセグメントの後の間を秒指定。他はそのセグメントの声の調整)
- 1セグメントだけ試聴(全編を作り直さない):
  python pipeline/build_video.py output/epXXX --preview 123 --engine voicevox --voicevox-url http://127.0.0.1:10101 --speaker <ID>
  → output/epXXX/preview_seg123.wav ができるので再生して確認
- 納得したら --steps tts,srt,bgm,render で本番反映(上書きセグメントだけ再合成される)
