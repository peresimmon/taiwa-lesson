# 対話のおけいこ — 匿名対話トレーニング(Web版)

「話す力」じゃなく、「聴く力」を鍛える。匿名の相手と1対1のビデオ通話で対話を練習するWebサービスです。
マルチテナント構成で、一般向けの**メインサイト**と企業向けの**サブサイト**(社内コミュニケーションツール)を1つのシステムで運営できます。

## 📚 ドキュメント

| 資料 | 内容 |
|---|---|
| [管理者向け機能ガイド](docs/ADMIN-GUIDE.md) | **機能仕様書**。各機能の説明と運用手順(技術知識は不要) |
| [機能追加の取りまとめ](docs/CHANGELOG.md) | これまでに実装した機能の経緯まとめ |
| [SPEC.md](docs/SPEC.md) | マルチテナント化の設計仕様(確定事項) |
| [TODO.md](docs/TODO.md) | 本番運用までに対応する技術課題 |

## 主な機能

- **マッチングと通話**: 「話し手×聞き手」の役割マッチング(役割なしも選択可) /
  双方同意で開始する1対1のWebRTC通話 / セッションタイマー / 役割交代つきセッション(10分×2回) /
  「また話したい」同士の再マッチ優先 / マッチ成立の通知音 / 話題カード
- **表示モード4種**: 顔トラッキング連動の2Dアバター(5種) / 3D VRMアバター(5種) / 静止画 / 実映像。
  アバター利用時、カメラの実映像はネットワークに流れません
- **通話機能**: ミュート / 映像オフ / 画面共有 / セッション内チャット(終了時にログ保存を促して破棄) /
  カメラ・マイクのデバイス選択と通話前テスト
- **マルチテナント**: サイトID単位の完全分離 / ログイン2系統(`/taiwa-lesson`・`/login`) /
  4段階ロール(システム管理者・サイト管理者・モデレータ・ユーザー) / 初期パスワードの初回変更強制
- **コミュニティ**: お知らせ / イベントカレンダー / 掲示板 / チーム(リーダー・チーム限定の掲示板/予定) /
  ルーム(合言葉・定員・期限・通話設定の上書き・カレンダー連携)
- **トラスト&セーフティ**: 通報(管理者・チームリーダーが確認) / ブロック / 警告文のポップアップ /
  パスワードリセット
- **管理**: タブ式の管理画面 / サイト設定(20項目超・5セクション) / ユーザー管理(ソート・ロール変更・
  有効化/無効化・CSV一括登録/エクスポート・メール案内) / 利用レポート(CSVエクスポート) /
  監査ログ / アンケートの設問カスタマイズ(複数設問対応) / サイト別ブランディング /
  デモデータのワンクリック生成・削除(検証用。本番では撤去)
- **PWA対応**: スマホのホーム画面に追加してアプリとして起動可能

## 構成

```
videomatch/
├── app/                    # バックエンド (Python + FastAPI)
│   ├── main.py             # REST API + WebSocketエンドポイント + サイト設定
│   ├── matching.py         # マッチング・同意フロー・シグナリング・チャット中継・役割交代
│   ├── auth.py             # パスワードハッシュ(PBKDF2) + JWT
│   └── database.py         # DBモデル + 簡易マイグレーション (SQLite / PostgreSQL差し替え可)
├── static/                 # フロントエンド (素のHTML/CSS/JSのSPA)
│   ├── index.html / app.js / style.css
│   ├── manifest.json / sw.js   # PWA
│   ├── icons/              # PWAアイコン
│   └── models/             # 3D VRMアバター(ライセンスは models/README.md 参照)
├── docs/                   # ドキュメント一式
├── tests/
│   ├── test_flow.py        # 自動結合テスト(196項目)
│   └── fake_peer.py        # 動作確認用の疑似ユーザー
├── Dockerfile              # Cloud Run用
└── requirements.txt
```

## 技術構成

| 項目 | 採用 | 補足 |
|---|---|---|
| バックエンド | Python + FastAPI | REST + WebSocket。将来のFlutterアプリからも同じAPIを利用可能(CORS設定済み) |
| ビデオ通話 | ブラウザ標準WebRTC | シグナリングは自前のWebSocket。本番はTURNサーバー追加を推奨([TODO.md](docs/TODO.md)) |
| 顔トラッキング | MediaPipe Face Landmarker | ブラウザ内で完結。表情・頭の動きをアバターに反映 |
| 3Dアバター | three.js + @pixiv/three-vrm | VRM 0.x / 1.0 両対応 |
| DB | SQLite | デモ用。環境変数 `DATABASE_URL` でPostgreSQL等に差し替え可能 |
| ホスティング | Google Cloud Run | GitHubのmainブランチへのpushで自動デプロイ |

## ローカルでの起動方法

```bash
cd videomatch
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

ブラウザで http://localhost:8000 を開くと、メインサイト(`/taiwa-lesson`)にリダイレクトされます。
サブサイトのログインページは http://localhost:8000/login です。

- 初回起動時に管理者 `administrator` / `password` が自動作成されます
  (デモ版のため初回パスワード変更の強制はなし。本番では復活させます → [docs/TODO.md](docs/TODO.md))
- メインサイトでは新規登録タブから自分でユーザーを作成できます
- システム管理画面の「デモデータを生成」で、デモ用のユーザー・チーム・履歴・サブサイトを一括投入できます

### 1人で2ユーザー分の動作確認をする方法

ログイン情報がブラウザに保存されるため、**通常ウィンドウ + シークレットウィンドウ**
(または別ブラウザ・別端末)で2ユーザーとしてログインしてください。
もう1人をスクリプトで用意することもできます:

```bash
python tests/fake_peer.py speaker   # 話し手として待機する疑似ユーザー
```

### 自動テスト

```bash
# サーバー起動中に別ターミナルで
pip install websockets requests
python tests/test_flow.py   # 196項目の結合テスト
```

別ポートで起動している場合はテスト内の `BASE` / `WS_BASE` を合わせてください。

## デプロイ(Google Cloud Run)

GitHubの `main` ブランチへpushすると、Cloud Buildが自動でビルド・デプロイします(継続的デプロイ設定済み)。

```bash
git push origin main   # これだけで本番に反映される
```

### 環境変数

| 変数 | 内容 |
|---|---|
| `SECRET_KEY` | JWT署名キー。**本番では必ず長い乱数を設定**(未設定だと開発用の既定値) |
| `DATABASE_URL` | 例: `postgresql://...`。未設定ならSQLite(**Cloud Runでは再デプロイで消える**) |
| `PORT` | Cloud Runが自動設定 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | メール連携(招待・パスワードリセット)。未設定の間は画面表示のみ |

本番運用前のチェックリストは [docs/TODO.md](docs/TODO.md) を参照してください
(TURNサーバー・DB永続化・レート制限など)。

## APIリファレンス(抜粋)

認証が必要なAPIは `Authorization: Bearer <token>` ヘッダーを付けます。

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/register` | ユーザー登録(メインサイトのみ) |
| POST | `/api/login` | ログイン。サブサイトは `site` にサイトIDを指定 |
| POST | `/api/password` | パスワード変更(初回変更強制にも使用) |
| GET | `/api/me` / `/api/config` | 自分の情報 / サイト設定(クライアント向け) |
| GET/POST | `/api/posts` `/api/events` | 掲示板・カレンダー(`team_id` でチーム限定) |
| GET | `/api/teams` ほか | チーム・メンバー管理 |
| GET/POST/PUT/DELETE | `/api/rooms` ほか | ルームの一覧・作成・設定・管理者 |
| POST | `/api/surveys` | アンケート送信(複数設問対応) |
| POST | `/api/reports` `/api/blocks` `/api/warnings` | 通報 / ブロック / 警告 |
| GET/PUT/POST | `/api/admin/...` | サイト管理(ユーザー・設定・お知らせ・チーム・レポート・監査・CSV) |
| GET/POST/DELETE | `/api/sysadmin/sites...` | システム管理(サイトの作成・削除・確認) |
| WS | `/ws?token=...` | マッチング+通話シグナリング+チャット+役割交代 |

WebSocketメッセージ仕様は [app/matching.py](app/matching.py) の冒頭コメントを参照してください。

## スマホでの利用

- **PWA**: サイトをスマホで開き、Android(Chrome)は「アプリをインストール」、
  iPhone(Safari)は共有→「ホーム画面に追加」でアプリとして起動できます(HTTPS必須)
- **Flutter版(将来)**: バックエンドはそのまま流用できます。通話部分は `flutter_webrtc` で
  同じシグナリング(`/ws`)に接続するか、Agora SDKへの差し替えを想定しています
