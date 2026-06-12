# 対話のおけいこ — 匿名対話トレーニング(Web版)

## 📚 ドキュメント

| 資料 | 内容 |
|---|---|
| [管理者向け機能ガイド](docs/ADMIN-GUIDE.md) | **機能仕様書**。各機能の説明と運用手順(技術知識は不要) |
| [機能追加の取りまとめ](docs/CHANGELOG.md) | これまでに実装した機能の経緯まとめ |
| [SPEC.md](docs/SPEC.md) | マルチテナント化の設計仕様(確定事項) |
| [TODO.md](docs/TODO.md) | 本番運用までに対応する技術課題 |

---

企画レポートの要件を実装したWebアプリです。

- ✅ ユーザー登録・ログイン(JWT認証)
- ✅ ログイン後のダッシュボード(お知らせ / イベントカレンダー / みんなの掲示板 / 通話履歴 / オンライン状況)
- ✅ 待機中ユーザーからランダムで2人を抽出してマッチング
- ✅ 通話前に注意事項を表示し、**双方が同意した場合のみ**通話開始
- ✅ 1対1ビデオ通話(WebRTC)
- ✅ 通話終了後のアンケート表示・保存

## 構成

```
videomatch/
├── app/                  # バックエンド (Python + FastAPI)
│   ├── main.py           # REST API + WebSocketエンドポイント
│   ├── matching.py       # ランダムマッチング・同意フロー・シグナリング
│   ├── auth.py           # パスワードハッシュ + JWT
│   └── database.py       # DBモデル (SQLite / 本番はPostgreSQL等に差し替え可)
├── static/               # フロントエンド (ブラウザSPA)
│   ├── index.html / app.js / style.css
├── tests/
│   ├── test_flow.py      # 自動結合テスト(19項目)
│   └── fake_peer.py      # 動作確認用の疑似ユーザー
└── requirements.txt
```

### 技術選定のポイント

| 項目 | 採用 | 理由 |
|---|---|---|
| バックエンド | Python + FastAPI | レポート推奨どおり。REST+WebSocket構成なので**将来のFlutterアプリからそのまま同じAPIを利用可能** |
| ビデオ通話 | ブラウザ標準WebRTC | 完全無料・アカウント不要でデモ運用に最適。シグナリングは自前のWebSocketで実施 |
| DB | SQLite | デモ用。環境変数 `DATABASE_URL` でPostgreSQL等に差し替え可能 |

> **Agoraについて**: レポートで推奨されていたAgora(月10,000分無料)は、Flutterスマホアプリ対応時に通話部分だけ差し替えられる構成にしています(マッチング・同意・アンケートのAPIはそのまま使えます)。AgoraはNAT越えに強くスマホSDKが充実しているため、スマホ版開発のタイミングでの導入がおすすめです。

## ローカルでの起動方法

```bash
cd videomatch
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

ブラウザで http://localhost:8000 を開きます。

### 1人で2ユーザー分の動作確認をする方法

ログイン情報がブラウザに保存されるため、**通常ウィンドウ + シークレットウィンドウ**(または別ブラウザ・別端末)で2ユーザーとしてログインしてください。

1. 両方のウィンドウでそれぞれ別ユーザーを登録
2. 両方で「マッチング開始」→ 自動でマッチング
3. 両方で「同意して通話する」→ ビデオ通話開始
4. 「通話を終了する」→ アンケート回答

### 自動テスト

```bash
# サーバー起動中に別ターミナルで
pip install websockets requests
python tests/test_flow.py   # 19項目の結合テスト
```

## AWS無料枠でのデモ運用

**重要な前提**: カメラ・マイク(getUserMedia)は `localhost` 以外では **HTTPSが必須** です。
そのためデモ公開には「ドメイン(またはDuckDNS等の無料サブドメイン)+ HTTPS化」が必要になります。

### 推奨構成(無料枠内)

- **EC2 t2.micro / t3.micro**(無料枠: 12ヶ月・月750時間)1台
- **Caddy** をリバースプロキシに使用(Let's Encrypt証明書を自動取得・更新)
- DBはひとまず同居SQLiteでOK(本格運用時にRDSへ)

### 手順

```bash
# 1. EC2 (Amazon Linux 2023) を起動し、セキュリティグループで 80, 443 を開放

# 2. サーバーにログインして環境構築
sudo dnf install -y python3.11 python3.11-pip
# プロジェクト一式を scp や git でアップロードして…
cd videomatch
pip3.11 install -r requirements.txt

# 3. アプリを起動(本番はsystemd化を推奨)
SECRET_KEY="ランダムな長い文字列" python3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. Caddyをインストールして HTTPS リバースプロキシ
sudo dnf install -y caddy
# /etc/caddy/Caddyfile に以下を記述(ドメインは自分のものに)
#   yourdomain.example.com {
#       reverse_proxy 127.0.0.1:8000
#   }
sudo systemctl enable --now caddy
```

CaddyはWebSocketも自動で中継するため追加設定は不要です。

### 注意点

- `SECRET_KEY` 環境変数を必ず設定してください(JWT署名キー)
- `app/main.py` のCORS設定(`allow_origins=["*"]`)は、本番では自ドメインに絞ってください
- WebRTCはP2P通信のため、一部の厳しいネットワーク(企業内など)ではTURNサーバーが必要になる場合があります。デモ段階ではGoogleの無料STUNのみで概ね動作します

## 今後の拡張(スマホアプリ化)

バックエンドはそのまま流用できます。

1. **Flutter版の作成**: `flutter_webrtc` パッケージで同じシグナリング(WS `/ws`)に接続するか、Agora SDKに切り替え
2. **API**: 認証(`/api/register`, `/api/login`)・アンケート(`/api/surveys`)はHTTPなのでFlutterの`http`パッケージでそのまま呼べます(CORS設定済み)
3. **ストア審査対策**(レポート記載どおり): 通報ボタン・ブロック機能の追加が必要になる見込みです

## APIリファレンス(抜粋)

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/register` | ユーザー登録 → JWTトークン返却 |
| POST | `/api/login` | ログイン → JWTトークン返却 |
| GET | `/api/me` | 自分の情報(要Bearerトークン) |
| POST | `/api/surveys` | アンケート送信(要Bearerトークン) |
| GET | `/api/surveys/mine` | 自分の回答履歴 |
| GET | `/api/announcements` | お知らせ一覧 |
| GET / POST | `/api/events?month=YYYY-MM` | イベントの取得 / 登録 |
| GET / POST | `/api/posts` | 掲示板の取得 / 投稿 |
| GET | `/api/stats` | オンライン人数・待機人数・登録者数 |
| WS | `/ws?token=...` | マッチング+通話シグナリング |

※ お知らせは現状DBに直接登録する想定です(初回起動時にデモ用データを自動投入)。管理画面が必要になったら追加してください。

WebSocketメッセージ仕様は [app/matching.py](app/matching.py) の冒頭コメントを参照してください。
