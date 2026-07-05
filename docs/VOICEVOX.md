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
