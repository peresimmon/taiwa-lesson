# 本番運用TODO

デモでは不要だが、本番運用までに必ず対応する項目。

## アーキテクチャ前提（重要）

- **本サービスは「単一プロセス・単一インスタンス」での稼働を前提**にしている。
  以下の状態を**プロセス内メモリ**で保持しているため、ワーカーやインスタンスを増やすと
  待機列・通話・統計・設定が分断して破綻する:
  - マッチングの待機列・WebSocket接続・通話セッション（`app/matching.py` の `MatchingManager`）
  - リアルタイム統計のブロードキャスト（同じく `MatchingManager`。配信先は自プロセスの接続のみ）
  - サイト設定のキャッシュ（`app/main.py` の `_settings_cache`）
- **当面の運用ルール**: Render/Cloud Run などのスケール設定は **インスタンス数=1・ワーカー数=1** に固定する。
  現状の起動コマンド（`render.yaml` / `Dockerfile`）は uvicorn 1ワーカーで、この前提を満たしている。
- **水平スケールが必要になったら**: 待機列・WS・統計を **Redis（pub/sub＋共有状態）** に外部化し、
  設定キャッシュも Redis か短TTLに置き換える。それまでは1インスタンス運用を厳守する。

## インフラ

- [ ] **TURNサーバーの導入** — 現在はGoogle STUNのみ。企業ネットワークやモバイル回線同士では
      P2P接続に失敗するため、本番では必須。候補: Metered.ca(無料枠あり) / Twilio TURN / 自前coturn。
      `static/app.js` の `RTC_CONFIG.iceServers` に追加するだけで適用できる
- [ ] **データベースの永続化** — Cloud RunのSQLiteは再デプロイ/インスタンスごとに消える。
      Cloud SQL(PostgreSQL)へ移行する。`DATABASE_URL` 環境変数対応は実装済みのため、
      Cloud Run側で `DATABASE_URL=postgresql://...` を設定し `psycopg2-binary` を requirements に追加する。
      ※永続化しないと連番IDが振り直される。現在は `token_version`(ユーザー行ごとの乱数)で
      「IDだけ一致する別人のトークン」を弾いているため*別人として勝手にログインする事故は防げている*が、
      正規ユーザーも再デプロイのたびにログアウトされる。恒久対応にはDB永続化が必要
- [x] **SECRET_KEYの設定** — Renderデプロイでは `render.yaml` の `generateValue: true` で
      ランダムな安定値が自動設定される（既定値 `dev-secret-change-me` はローカル開発時のみ）。
      他環境（Cloud Run等）に移す場合は環境変数 `SECRET_KEY` に
      32バイト以上の乱数(`python -c "import secrets; print(secrets.token_urlsafe(48))"`)を設定すること

## セキュリティ

- [ ] **administratorの初回パスワード変更強制を復活させる** — デモ版では利便性のため
      無効化している(`app/main.py` の `seed_initial_data` 内、コメント箇所)。
      本番では `must_change_password=True` に戻し、既定パスワード`password`も廃止する
- [ ] **ログイン試行回数制限(ブルートフォース対策)** — slowapi等でIP/ユーザー単位のレート制限を導入
- [ ] CORSの `allow_origins` を本番ドメインに絞る
- [ ] administrator・サイト管理者の初期パスワード運用ルールの整備(現状は初回ログイン時の変更強制のみ)
- [ ] 自己登録機能の廃止(本番では管理者作成のみの想定)

## デモ機能の撤去

- [ ] **デモデータの生成・削除機能を削除する** — 開発者ページ(🛠 開発者)の「デモデータ」パネルと、
      対応するAPI(`POST/DELETE /api/dev/demo-data`、`app/main.py` の `create_demo_data`/
      `delete_demo_data`)を本番運用では丸ごと削除する。
      ※現在は実APIルート経由で生成・削除している(DB直挿入はマッチング記録の `CallPair` のみ)

## メール

- [ ] SMTP設定(環境変数 `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM`)。
      未設定の間、招待・リセットメールは送信されず画面表示のみになる
