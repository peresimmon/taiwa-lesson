# 本番運用TODO

デモでは不要だが、本番運用までに必ず対応する項目。

## インフラ

- [ ] **TURNサーバーの導入** — 現在はGoogle STUNのみ。企業ネットワークやモバイル回線同士では
      P2P接続に失敗するため、本番では必須。候補: Metered.ca(無料枠あり) / Twilio TURN / 自前coturn。
      `static/app.js` の `RTC_CONFIG.iceServers` に追加するだけで適用できる
- [ ] **データベースの永続化** — Cloud RunのSQLiteは再デプロイで消える。
      Cloud SQL(PostgreSQL)へ移行する。`DATABASE_URL` 環境変数対応は実装済みのため、
      Cloud Run側で `DATABASE_URL=postgresql://...` を設定し `psycopg2-binary` を requirements に追加する
- [ ] **SECRET_KEYの設定** — JWT署名キーが既定値のまま。Cloud Runの環境変数に
      32バイト以上の乱数(`python -c "import secrets; print(secrets.token_urlsafe(48))"`)を設定する

## セキュリティ

- [ ] **administratorの初回パスワード変更強制を復活させる** — デモ版では利便性のため
      無効化している(`app/main.py` の `seed_initial_data` 内、コメント箇所)。
      本番では `must_change_password=True` に戻し、既定パスワード`password`も廃止する
- [ ] **ログイン試行回数制限(ブルートフォース対策)** — slowapi等でIP/ユーザー単位のレート制限を導入
- [ ] CORSの `allow_origins` を本番ドメインに絞る
- [ ] administrator・サイト管理者の初期パスワード運用ルールの整備(現状は初回ログイン時の変更強制のみ)
- [ ] 自己登録機能の廃止(本番では管理者作成のみの想定)

## デモ機能の撤去

- [ ] **デモデータの生成・削除機能を削除する** — システム管理画面の「デモデータ」パネルと、
      対応するAPI(`POST/DELETE /api/sysadmin/demo-data`、`app/main.py` の「デモデータ」セクション)を
      本番運用では丸ごと削除する

## メール

- [ ] SMTP設定(環境変数 `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM`)。
      未設定の間、招待・リセットメールは送信されず画面表示のみになる
