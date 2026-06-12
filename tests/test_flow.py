"""バックエンドの自動結合テスト

サーバー(http://127.0.0.1:8000)起動済みの状態で実行する:
    python tests/test_flow.py

検証内容:
  1. ユーザー登録 / 重複登録の拒否 / ログイン / 認証エラー
  2. 2クライアントのWebSocket接続 → マッチング成立
  3. 双方同意 → call_start(initiator振り分け)
  4. シグナリングメッセージの中継(offer/answer/ice)
  5. 片方が leave → 相手に peer_left
  6. アンケート送信と取得
  7. 同意拒否時に相手へ peer_declined が届く
"""
import asyncio
import json
import secrets
import sys

import requests
import websockets

BASE = "http://127.0.0.1:8000"
WS_BASE = "ws://127.0.0.1:8000"

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def setup_users():
    print("[1] 認証API")
    suffix = secrets.token_hex(4)
    users = []
    for i in (1, 2):
        name = f"testuser{i}_{suffix}"
        r = requests.post(f"{BASE}/api/register", json={"username": name, "password": "pass123"})
        check(f"登録 {name}", r.status_code == 201, r.text)
        users.append({"username": name, "token": r.json()["token"]})

    # 重複登録
    r = requests.post(f"{BASE}/api/register", json={"username": users[0]["username"], "password": "pass123"})
    check("重複ユーザー名は409", r.status_code == 409, r.text)

    # ログイン成功・失敗
    r = requests.post(f"{BASE}/api/login", json={"username": users[0]["username"], "password": "pass123"})
    check("ログイン成功", r.status_code == 200, r.text)
    r = requests.post(f"{BASE}/api/login", json={"username": users[0]["username"], "password": "wrongpw"})
    check("誤パスワードは401", r.status_code == 401, r.text)

    # 認証付きAPI
    r = requests.get(f"{BASE}/api/me", headers={"Authorization": f"Bearer {users[0]['token']}"})
    check("/api/me", r.status_code == 200 and r.json()["username"] == users[0]["username"], r.text)
    r = requests.get(f"{BASE}/api/me", headers={"Authorization": "Bearer invalid"})
    check("無効トークンは401", r.status_code == 401, r.text)
    return users


async def recv_type(ws, expected, timeout=5):
    """expectedのtypeのメッセージが来るまで受信する"""
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if msg["type"] == expected:
            return msg


async def test_matching_flow(users):
    print("[2] マッチング〜通話〜退出フロー")
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")

    await ws1.send(json.dumps({"type": "join_queue", "role": "speaker"}))
    q = await recv_type(ws1, "queued")
    check("queuedに役割が入る", q["role"] == "speaker", q)
    await ws2.send(json.dumps({"type": "join_queue", "role": "listener"}))

    m1 = await recv_type(ws1, "matched")
    m2 = await recv_type(ws2, "matched")
    check("両者にmatched", m1["room_id"] == m2["room_id"], f"{m1} / {m2}")
    check("役割が話し手×聞き手",
          m1["my_role"] == "speaker" and m1["peer_role"] == "listener"
          and m2["my_role"] == "listener" and m2["peer_role"] == "speaker", f"{m1} / {m2}")
    check("呼び名が交差して一致し、互いに異なる",
          m1["my_nickname"] == m2["peer_nickname"]
          and m2["my_nickname"] == m1["peer_nickname"]
          and m1["my_nickname"] != m1["peer_nickname"], f"{m1} / {m2}")
    room_id = m1["room_id"]

    # 双方同意
    await ws1.send(json.dumps({"type": "consent", "accept": True}))
    await ws2.send(json.dumps({"type": "consent", "accept": True}))
    c1 = await recv_type(ws1, "call_start")
    c2 = await recv_type(ws2, "call_start")
    check("initiatorは片方だけ", c1["initiator"] != c2["initiator"], f"{c1} / {c2}")

    # シグナリング中継
    caller, callee = (ws1, ws2) if c1["initiator"] else (ws2, ws1)
    await caller.send(json.dumps({"type": "signal", "data": {"kind": "offer", "sdp": "dummy-offer"}}))
    sig = await recv_type(callee, "signal")
    check("offerが中継される", sig["data"]["kind"] == "offer" and sig["data"]["sdp"] == "dummy-offer")
    await callee.send(json.dumps({"type": "signal", "data": {"kind": "answer", "sdp": "dummy-answer"}}))
    sig = await recv_type(caller, "signal")
    check("answerが中継される", sig["data"]["kind"] == "answer")
    await caller.send(json.dumps({"type": "signal", "data": {"kind": "ice", "candidate": {"a": 1}}}))
    sig = await recv_type(callee, "signal")
    check("ICE候補が中継される", sig["data"]["kind"] == "ice")

    # 退出 → 相手にpeer_left
    await ws1.send(json.dumps({"type": "leave"}))
    await recv_type(ws2, "peer_left")
    check("退出が相手に通知される", True)

    await ws1.close()
    await ws2.close()
    return room_id


def test_survey(users, room_id):
    print("[3] アンケートAPI")
    headers = {"Authorization": f"Bearer {users[0]['token']}"}
    r = requests.post(f"{BASE}/api/surveys", headers=headers,
                      json={"room_id": room_id, "rating": 4, "talk_again": True, "comment": "テストコメント"})
    check("アンケート送信", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/surveys", headers=headers,
                      json={"room_id": room_id, "rating": 9, "talk_again": False, "comment": ""})
    check("評価6以上は422", r.status_code == 422, r.text)
    r = requests.get(f"{BASE}/api/surveys/mine", headers=headers)
    rows = r.json()
    check("自分の回答を取得", r.status_code == 200 and len(rows) == 1 and rows[0]["rating"] == 4, r.text)


async def test_same_role_no_match(users):
    print("[4] 同じ役割同士はマッチしない")
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws1.send(json.dumps({"type": "join_queue", "role": "listener"}))
    await recv_type(ws1, "queued")
    await ws2.send(json.dumps({"type": "join_queue", "role": "listener"}))
    await recv_type(ws2, "queued")
    try:
        await recv_type(ws1, "matched", timeout=1.5)
        check("聞き手同士はマッチしない", False, "matchedが届いてしまった")
    except TimeoutError:
        check("聞き手同士はマッチしない", True)

    # 役割未指定はエラー
    ws1b = ws1
    await ws1b.send(json.dumps({"type": "cancel_queue"}))
    await ws1b.send(json.dumps({"type": "join_queue"}))
    err = await recv_type(ws1b, "error")
    check("役割未指定はエラー", "選んで" in err["message"], err)
    await ws1.close()
    await ws2.close()


async def test_decline_flow(users):
    print("[5] 同意拒否フロー")
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws1.send(json.dumps({"type": "join_queue", "role": "speaker"}))
    await ws2.send(json.dumps({"type": "join_queue", "role": "listener"}))
    await recv_type(ws1, "matched")
    await recv_type(ws2, "matched")

    await ws1.send(json.dumps({"type": "consent", "accept": False}))
    await recv_type(ws2, "peer_declined")
    check("拒否が相手に通知される", True)
    await ws1.close()
    await ws2.close()


async def test_disconnect_during_wait(users):
    print("[6] 待機中の切断")
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    await ws1.send(json.dumps({"type": "join_queue", "role": "speaker"}))
    await recv_type(ws1, "queued")
    await ws1.close()
    # 切断後に別の2人が正常にマッチできること(キューに残骸が残らない)
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws3 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws2.send(json.dumps({"type": "join_queue", "role": "speaker"}))
    await recv_type(ws2, "queued")
    await ws3.send(json.dumps({"type": "join_queue", "role": "listener"}))
    m = await recv_type(ws3, "matched")
    check("切断後も正常にマッチング", bool(m["room_id"]))
    await ws2.close()
    await ws3.close()


def test_dashboard(users):
    print("[7] ダッシュボードAPI")
    headers = {"Authorization": f"Bearer {users[0]['token']}"}

    # お知らせ(初期データが入っている)
    r = requests.get(f"{BASE}/api/announcements", headers=headers)
    check("お知らせ一覧", r.status_code == 200 and len(r.json()) >= 1, r.text)
    r = requests.get(f"{BASE}/api/announcements")
    check("未認証は403/401", r.status_code in (401, 403), r.text)

    # イベント
    r = requests.post(f"{BASE}/api/events", headers=headers,
                      json={"title": "テスト交流会", "date": "2026-06-20"})
    check("イベント作成", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/events", headers=headers,
                      json={"title": "不正な日付", "date": "2026/06/20"})
    check("日付形式エラーは422", r.status_code == 422, r.text)
    r = requests.get(f"{BASE}/api/events?month=2026-06", headers=headers)
    events = r.json()
    check("月別イベント取得", r.status_code == 200 and
          any(e["title"] == "テスト交流会" and e["username"] == users[0]["username"] for e in events), r.text)
    r = requests.get(f"{BASE}/api/events?month=2026-07", headers=headers)
    check("別の月には含まれない",
          all(e["title"] != "テスト交流会" for e in r.json()), r.text)
    r = requests.get(f"{BASE}/api/events?month=bad", headers=headers)
    check("month形式エラーは422", r.status_code == 422, r.text)

    # 掲示板
    r = requests.post(f"{BASE}/api/posts", headers=headers, json={"body": "こんにちは!"})
    check("掲示板投稿", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/posts", headers=headers, json={"body": ""})
    check("空投稿は422", r.status_code == 422, r.text)
    r = requests.get(f"{BASE}/api/posts", headers=headers)
    posts = r.json()
    check("投稿一覧(新しい順・投稿者名つき)",
          r.status_code == 200 and posts[0]["body"] == "こんにちは!"
          and posts[0]["username"] == users[0]["username"], r.text)

    # 統計
    r = requests.get(f"{BASE}/api/stats", headers=headers)
    s = r.json()
    check("統計取得", r.status_code == 200 and s["total_users"] >= 2
          and "online" in s and "waiting" in s
          and "waiting_speakers" in s and "waiting_listeners" in s, r.text)


ADMIN_TEST_PW = "password123!"  # テスト中に一時的に使う管理者パスワード


def admin_login():
    """administratorでログインする。初期パスワードなら強制変更フローを通す"""
    r = requests.post(f"{BASE}/api/login", json={"username": "administrator", "password": "password"})
    if r.status_code == 401:
        # 既に変更済み(同一プロセスでの再実行)
        return requests.post(f"{BASE}/api/login",
                             json={"username": "administrator", "password": ADMIN_TEST_PW})
    data = r.json()
    if data.get("must_change_password"):
        # 変更が済むまで他のAPIは403になる
        h = {"Authorization": f"Bearer {data['token']}"}
        blocked = requests.get(f"{BASE}/api/stats", headers=h)
        check("変更前は他APIが403", blocked.status_code == 403, blocked.text)
        rc = requests.post(f"{BASE}/api/password", headers=h,
                           json={"current_password": "password", "new_password": ADMIN_TEST_PW})
        check("初回パスワード変更", rc.status_code == 200, rc.text)
        r = requests.post(f"{BASE}/api/login",
                          json={"username": "administrator", "password": ADMIN_TEST_PW})
    return r


def restore_admin_password(headers):
    """ローカルDBを administrator/password に戻しておく(次回起動時に再び変更強制になる)"""
    requests.post(f"{BASE}/api/password", headers=headers,
                  json={"current_password": ADMIN_TEST_PW, "new_password": "password"})


def test_admin(users):
    print("[8] 管理者API")
    user_headers = {"Authorization": f"Bearer {users[0]['token']}"}

    # 一般ユーザーは管理APIにアクセスできない
    r = requests.get(f"{BASE}/api/admin/users", headers=user_headers)
    check("一般ユーザーは403", r.status_code == 403, r.text)

    # 起動時にシードされた管理者でログイン(初回はパスワード変更を強制される)
    r = admin_login()
    check("administratorでログイン", r.status_code == 200 and r.json().get("role") == "system_admin", r.text)
    headers = {"Authorization": f"Bearer {r.json()['token']}"}

    # ユーザー一覧
    r = requests.get(f"{BASE}/api/admin/users", headers=headers)
    rows = r.json()
    check("ユーザー一覧取得", r.status_code == 200 and
          any(u["username"] == users[0]["username"] for u in rows), r.text)
    target = next(u for u in rows if u["username"] == users[0]["username"])
    check("セッション数が記録される", target["session_count"] >= 1, target)

    # 他ユーザーの通話履歴
    r = requests.get(f"{BASE}/api/admin/users/{target['id']}/surveys", headers=headers)
    data = r.json()
    check("他ユーザーの履歴取得", r.status_code == 200 and
          data["username"] == users[0]["username"] and len(data["surveys"]) >= 1, r.text)

    # サイト設定の取得・変更
    r = requests.get(f"{BASE}/api/admin/settings", headers=headers)
    orig = r.json()
    check("設定取得", r.status_code == 200 and "session_minutes" in orig, r.text)
    r = requests.put(f"{BASE}/api/admin/settings", headers=headers,
                     json={"session_minutes": 5, "allow_registration": False})
    check("設定変更", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/config", headers=user_headers)
    check("設定が/api/configに反映される", r.json()["session_minutes"] == 5, r.text)
    r = requests.post(f"{BASE}/api/register", json={"username": "blocked_user_x", "password": "pass123"})
    check("登録停止中は403", r.status_code == 403, r.text)
    r = requests.put(f"{BASE}/api/admin/settings", headers=headers,
                     json={"session_minutes": orig["session_minutes"], "allow_registration": True})
    check("設定を元に戻す", r.status_code == 200, r.text)

    # お知らせの作成・削除
    r = requests.post(f"{BASE}/api/admin/announcements", headers=headers,
                      json={"title": "テストのお知らせ", "body": "本文です"})
    check("お知らせ作成", r.status_code == 201, r.text)
    ann_id = r.json()["id"]
    r = requests.get(f"{BASE}/api/announcements", headers=user_headers)
    check("お知らせが一覧に出る", any(a["id"] == ann_id for a in r.json()), r.text)
    r = requests.delete(f"{BASE}/api/admin/announcements/{ann_id}", headers=headers)
    check("お知らせ削除", r.status_code == 200, r.text)

    # ユーザー削除(管理者は削除不可)
    admin_row = next(u for u in rows if u["role"] == "system_admin")
    r = requests.delete(f"{BASE}/api/admin/users/{admin_row['id']}", headers=headers)
    check("管理者は削除できない", r.status_code == 400, r.text)
    suffix = secrets.token_hex(4)
    r = requests.post(f"{BASE}/api/register", json={"username": f"victim_{suffix}", "password": "pass123"})
    vid_token = r.json()["token"]
    r = requests.get(f"{BASE}/api/me", headers={"Authorization": f"Bearer {vid_token}"})
    vid = r.json()["id"]
    r = requests.delete(f"{BASE}/api/admin/users/{vid}", headers=headers)
    check("一般ユーザーを削除できる", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/me", headers={"Authorization": f"Bearer {vid_token}"})
    check("削除済みユーザーのトークンは無効", r.status_code == 401, r.text)
    return headers


def test_multitenant(users, admin_headers):
    print("[9] マルチテナント(サイト分離)")
    slug = f"corp-{secrets.token_hex(3)}"

    # 一般ユーザーはシステム管理APIにアクセスできない
    r = requests.get(f"{BASE}/api/sysadmin/sites",
                     headers={"Authorization": f"Bearer {users[0]['token']}"})
    check("一般ユーザーはsysadmin不可", r.status_code == 403, r.text)

    # サイト作成 → サイト管理者が自動生成される
    r = requests.post(f"{BASE}/api/sysadmin/sites", headers=admin_headers,
                      json={"slug": slug, "name": "テスト株式会社"})
    check("サイト作成", r.status_code == 201, r.text)
    created = r.json()
    check("サイト管理者が自動生成される",
          created["admin_username"] == f"{slug}_admin"
          and created["initial_password"] == f"password@{slug}", created)
    site_id = created["id"]
    r = requests.post(f"{BASE}/api/sysadmin/sites", headers=admin_headers,
                      json={"slug": slug, "name": "重複"})
    check("サイトID重複は409", r.status_code == 409, r.text)

    # サイト管理者ログイン: サイトID無し(メインサイト)では入れない
    r = requests.post(f"{BASE}/api/login",
                      json={"username": f"{slug}_admin", "password": f"password@{slug}"})
    check("サイトID無しではログイン不可", r.status_code == 401, r.text)
    # サイトID付きでログイン → 初回パスワード変更を強制される
    r = requests.post(f"{BASE}/api/login",
                      json={"username": f"{slug}_admin", "password": f"password@{slug}", "site": slug})
    check("サイトID付きでログイン", r.status_code == 200 and r.json()["must_change_password"], r.text)
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    r = requests.post(f"{BASE}/api/password", headers=h,
                      json={"current_password": f"password@{slug}", "new_password": "newpass123"})
    check("サイト管理者のパスワード変更", r.status_code == 200, r.text)
    r = requests.post(f"{BASE}/api/login",
                      json={"username": f"{slug}_admin", "password": "newpass123", "site": slug})
    check("変更後ログイン", r.status_code == 200 and r.json()["role"] == "site_admin"
          and not r.json()["must_change_password"], r.text)
    sub_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    # サイト分離: サブサイトのユーザー一覧にメインサイトのユーザーは出ない
    r = requests.get(f"{BASE}/api/admin/users", headers=sub_headers)
    names = [u["username"] for u in r.json()]
    check("ユーザー一覧がサイト内に限定される",
          f"{slug}_admin" in names and users[0]["username"] not in names, names)

    # サイト管理者がユーザーを作成 → サイトID付きでのみログイン可
    member = f"member_{secrets.token_hex(3)}"
    r = requests.post(f"{BASE}/api/admin/users", headers=sub_headers,
                      json={"username": member, "password": "member123"})
    check("サイト管理者がユーザー作成", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/login", json={"username": member, "password": "member123"})
    check("作成ユーザーはメインサイトに入れない", r.status_code == 401, r.text)
    r = requests.post(f"{BASE}/api/login",
                      json={"username": member, "password": "member123", "site": slug})
    check("作成ユーザーはサブサイトに入れる(要パスワード変更)",
          r.status_code == 200 and r.json()["must_change_password"], r.text)

    # 同名ユーザーがサイトごとに共存できる
    r = requests.post(f"{BASE}/api/register", json={"username": member, "password": "other123"})
    check("同名ユーザーを別サイト(メイン)に登録できる", r.status_code == 201, r.text)

    # 統計・設定の分離
    r = requests.get(f"{BASE}/api/stats", headers=sub_headers)
    check("統計がサイト内のみ", r.json()["total_users"] == 2, r.text)  # 管理者+member1
    r = requests.put(f"{BASE}/api/admin/settings", headers=sub_headers,
                     json={"session_minutes": 15, "allow_registration": False})
    check("サブサイトの設定変更", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/config",
                     headers={"Authorization": f"Bearer {users[0]['token']}"})
    check("メインサイトの設定には影響しない", r.json()["session_minutes"] != 15, r.text)

    # システム管理者は各サイトのユーザー・設定を確認できる
    r = requests.get(f"{BASE}/api/sysadmin/sites/{site_id}/users", headers=admin_headers)
    check("sysadmin: サイトのユーザー確認", r.status_code == 200 and len(r.json()) == 2, r.text)
    r = requests.get(f"{BASE}/api/sysadmin/sites/{site_id}/settings", headers=admin_headers)
    check("sysadmin: サイトの設定確認", r.status_code == 200 and r.json()["session_minutes"] == 15, r.text)

    # サイト削除 → 所属ユーザーもログイン不可に
    sites = requests.get(f"{BASE}/api/sysadmin/sites", headers=admin_headers).json()
    main_site = next(s for s in sites if s["is_main"])
    r = requests.delete(f"{BASE}/api/sysadmin/sites/{main_site['id']}", headers=admin_headers)
    check("メインサイトは削除できない", r.status_code == 400, r.text)
    r = requests.delete(f"{BASE}/api/sysadmin/sites/{site_id}", headers=admin_headers)
    check("サイト削除", r.status_code == 200, r.text)
    r = requests.post(f"{BASE}/api/login",
                      json={"username": f"{slug}_admin", "password": "newpass123", "site": slug})
    check("削除後はログイン不可", r.status_code == 401, r.text)


DEFAULT_PUT = {
    "session_minutes": 10, "allow_registration": True,
    "role_matching": True, "anonymous_mode": True,
    "survey_enabled": True, "survey_question": "",
    "mode_toon": True, "mode_real": True, "mode_still": True, "mode_camera": False,
}


async def test_phase2_settings(users, admin_headers):
    print("[10] サイト設定(役割なし・実名・表示モード・アンケート)")
    user_headers = {"Authorization": f"Bearer {users[0]['token']}"}

    # 役割なし+実名+アンケートなし+実映像許可 に変更
    r = requests.put(f"{BASE}/api/admin/settings", headers=admin_headers,
                     json={**DEFAULT_PUT, "role_matching": False, "anonymous_mode": False,
                           "survey_enabled": False, "mode_camera": True,
                           "survey_question": "今日の対話はどうでしたか？"})
    check("設定変更", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/config", headers=user_headers)
    c = r.json()
    check("configに反映",
          c["role_matching"] is False and c["anonymous_mode"] is False
          and c["survey_enabled"] is False and c["modes"]["camera"] is True
          and c["survey_question"] == "今日の対話はどうでしたか？", c)

    # 役割なしマッチング: roleを指定しなくてもマッチし、実名(ユーザー名)が表示される
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws1.send(json.dumps({"type": "join_queue"}))
    q = await recv_type(ws1, "queued")
    check("役割なしで待機できる", q["role"] == "any", q)
    await ws2.send(json.dumps({"type": "join_queue"}))
    m1 = await recv_type(ws1, "matched")
    m2 = await recv_type(ws2, "matched")
    check("役割なしでマッチング", m1["my_role"] == "any" and m2["my_role"] == "any", f"{m1} / {m2}")
    check("実名モードではユーザー名が表示される",
          m1["my_nickname"] == users[0]["username"] and m1["peer_nickname"] == users[1]["username"],
          f"{m1}")
    await ws1.close()
    await ws2.close()

    # 表示モードをすべてオフにはできない
    r = requests.put(f"{BASE}/api/admin/settings", headers=admin_headers,
                     json={**DEFAULT_PUT, "mode_toon": False, "mode_real": False,
                           "mode_still": False, "mode_camera": False})
    check("表示モード全オフは422", r.status_code == 422, r.text)

    # デフォルトに戻す
    r = requests.put(f"{BASE}/api/admin/settings", headers=admin_headers, json=DEFAULT_PUT)
    check("設定をデフォルトに戻す", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/config", headers=user_headers)
    check("デフォルト設問に戻る",
          r.json()["role_matching"] is True and "聴けた" in r.json()["survey_question"], r.text)


def test_teams(users, admin_headers):
    print("[11] チーム")
    h0 = {"Authorization": f"Bearer {users[0]['token']}"}  # リーダーにするユーザー
    h1 = {"Authorization": f"Bearer {users[1]['token']}"}  # 一般メンバー

    # 部外者用ユーザー
    suffix = secrets.token_hex(3)
    r = requests.post(f"{BASE}/api/register", json={"username": f"outsider_{suffix}", "password": "pass123"})
    h_out = {"Authorization": f"Bearer {r.json()['token']}"}

    # チーム作成はサイト管理者のみ
    r = requests.post(f"{BASE}/api/admin/teams", headers=h0, json={"name": "もぐり"})
    check("一般ユーザーはチーム作成不可", r.status_code == 403, r.text)
    r = requests.post(f"{BASE}/api/admin/teams", headers=admin_headers, json={"name": f"開発チーム_{suffix}"})
    check("チーム作成", r.status_code == 201, r.text)
    tid = r.json()["id"]

    # メンバー追加(管理者はリーダー指定可)
    r = requests.post(f"{BASE}/api/teams/{tid}/members", headers=admin_headers,
                      json={"username": users[0]["username"], "is_leader": True})
    check("リーダーを追加", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/teams/{tid}/members", headers=admin_headers,
                      json={"username": users[0]["username"]})
    check("重複追加は409", r.status_code == 409, r.text)

    # 所属チーム一覧
    r = requests.get(f"{BASE}/api/teams", headers=h0)
    teams = r.json()
    check("所属チームが見える", any(t["id"] == tid and t["is_leader"] for t in teams), teams)
    r = requests.get(f"{BASE}/api/teams", headers=h_out)
    check("部外者の一覧には出ない", all(t["id"] != tid for t in r.json()), r.text)

    # リーダーが招待できる(リーダー指定は無視される)
    r = requests.post(f"{BASE}/api/teams/{tid}/members", headers=h0,
                      json={"username": users[1]["username"], "is_leader": True})
    check("リーダーが招待できる", r.status_code == 201, r.text)
    r = requests.get(f"{BASE}/api/teams/{tid}/members", headers=h1)
    members = r.json()["members"]
    m1 = next(m for m in members if m["username"] == users[1]["username"])
    check("リーダー指定は管理者のみ有効", m1["is_leader"] is False, members)

    # 一般メンバーは招待できない / 部外者は閲覧できない
    r = requests.post(f"{BASE}/api/teams/{tid}/members", headers=h1,
                      json={"username": f"outsider_{suffix}"})
    check("一般メンバーは招待不可", r.status_code == 403, r.text)
    r = requests.get(f"{BASE}/api/teams/{tid}/members", headers=h_out)
    check("部外者はメンバー一覧を見られない", r.status_code == 403, r.text)

    # チーム限定の掲示板
    r = requests.post(f"{BASE}/api/posts", headers=h0, json={"body": "チームだけの連絡", "team_id": tid})
    check("チーム投稿", r.status_code == 201, r.text)
    r = requests.get(f"{BASE}/api/posts", headers=h0)
    check("サイト全体には出ない", all(p["body"] != "チームだけの連絡" for p in r.json()), r.text)
    r = requests.get(f"{BASE}/api/posts?team_id={tid}", headers=h1)
    check("チームの掲示板に出る", any(p["body"] == "チームだけの連絡" for p in r.json()), r.text)
    r = requests.get(f"{BASE}/api/posts?team_id={tid}", headers=h_out)
    check("部外者はチーム掲示板を見られない", r.status_code == 403, r.text)
    r = requests.post(f"{BASE}/api/posts", headers=h_out, json={"body": "侵入", "team_id": tid})
    check("部外者はチーム投稿できない", r.status_code == 403, r.text)

    # チーム限定のイベント
    r = requests.post(f"{BASE}/api/events", headers=h1,
                      json={"title": "チーム定例", "date": "2026-07-01", "team_id": tid})
    check("チームイベント作成", r.status_code == 201, r.text)
    r = requests.get(f"{BASE}/api/events?month=2026-07", headers=h0)
    check("サイト全体のカレンダーには出ない", all(e["title"] != "チーム定例" for e in r.json()), r.text)
    r = requests.get(f"{BASE}/api/events?month=2026-07&team_id={tid}", headers=h0)
    check("チームのカレンダーに出る", any(e["title"] == "チーム定例" for e in r.json()), r.text)

    # チーム統計
    r = requests.get(f"{BASE}/api/teams/{tid}/stats", headers=h0)
    check("チーム統計", r.status_code == 200 and r.json()["members"] == 2, r.text)

    # リーダーの解除/設定(管理者のみ) と メンバー削除
    uid1 = m1["user_id"]
    r = requests.put(f"{BASE}/api/admin/teams/{tid}/members/{uid1}", headers=h0, json={"is_leader": True})
    check("リーダー設定は管理者のみ", r.status_code == 403, r.text)
    r = requests.put(f"{BASE}/api/admin/teams/{tid}/members/{uid1}", headers=admin_headers, json={"is_leader": True})
    check("管理者がリーダー設定", r.status_code == 200, r.text)
    r = requests.delete(f"{BASE}/api/teams/{tid}/members/{uid1}", headers=h0)
    check("リーダーはリーダーを外せない", r.status_code == 403, r.text)
    r = requests.delete(f"{BASE}/api/teams/{tid}/members/{uid1}", headers=admin_headers)
    check("管理者はリーダーも外せる", r.status_code == 200, r.text)

    # チーム削除でチーム限定データも消える
    r = requests.delete(f"{BASE}/api/admin/teams/{tid}", headers=admin_headers)
    check("チーム削除", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/teams", headers=h0)
    check("削除後は一覧から消える", all(t["id"] != tid for t in r.json()), r.text)


async def test_rooms(users, admin_headers):
    print("[12] ルーム・モデレータ")
    h0 = {"Authorization": f"Bearer {users[0]['token']}"}
    h1 = {"Authorization": f"Bearer {users[1]['token']}"}
    suffix = secrets.token_hex(3)

    # 一般ユーザーはルーム作成不可 → モデレータに昇格すると可能
    r = requests.post(f"{BASE}/api/rooms", headers=h0, json={"name": "もぐりルーム"})
    check("一般ユーザーはルーム作成不可", r.status_code == 403, r.text)
    r = requests.get(f"{BASE}/api/me", headers=h0)
    uid0 = r.json()["id"]
    r = requests.put(f"{BASE}/api/admin/users/{uid0}/role", headers=admin_headers, json={"role": "moderator"})
    check("モデレータに昇格", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/me", headers=h0)
    check("ロールがmoderatorになる", r.json()["role"] == "moderator", r.text)

    # ルーム作成(合言葉+定員2+役割なし上書き+セッション5分+表示モード上書き)
    r = requests.post(f"{BASE}/api/rooms", headers=h0, json={
        "name": f"雑談ルーム_{suffix}", "passphrase": "himitsu", "capacity": 2,
        "session_minutes": 5, "role_matching": False, "modes": ["toon", "still"],
    })
    check("モデレータがルーム作成", r.status_code == 201, r.text)
    rid = r.json()["id"]

    r = requests.get(f"{BASE}/api/rooms", headers=h1)
    rooms = r.json()
    room = next((x for x in rooms if x["id"] == rid), None)
    check("ルーム一覧に出る", room is not None, rooms)
    check("有効設定が返る(上書き反映)",
          room["has_passphrase"] and room["session_minutes"] == 5
          and room["role_matching"] is False and room["modes"]["toon"] and not room["modes"]["real"], room)
    check("一般ユーザーに管理権限はない", room["can_manage"] is False, room)

    # 参加: 合言葉が必要。役割なし上書きなのでroleなしで参加できる
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws1.send(json.dumps({"type": "join_queue", "room_id": rid}))
    err = await recv_type(ws1, "error")
    check("合言葉なしは拒否", "合言葉" in err["message"], err)
    await ws1.send(json.dumps({"type": "join_queue", "room_id": rid, "passphrase": "himitsu"}))
    q = await recv_type(ws1, "queued")
    check("ルームで待機できる(役割なし上書き)", q["role"] == "any" and q["room_id"] == rid, q)

    # ルーム外の通常マッチングとは混ざらない(別ユーザーが通常キューで待機)
    r = requests.post(f"{BASE}/api/register", json={"username": f"third_{suffix}", "password": "pass123"})
    third_token = r.json()["token"]
    ws_norm = await websockets.connect(f"{WS_BASE}/ws?token={third_token}")
    await ws_norm.send(json.dumps({"type": "join_queue", "role": "listener"}))
    await recv_type(ws_norm, "queued")
    try:
        await recv_type(ws1, "matched", timeout=1.5)
        check("通常マッチングとは混ざらない", False)
    except TimeoutError:
        check("通常マッチングとは混ざらない", True)
    await ws_norm.close()

    # 同じルームに入った相手とマッチ
    await ws2.send(json.dumps({"type": "join_queue", "room_id": rid, "passphrase": "himitsu"}))
    m1 = await recv_type(ws1, "matched")
    m2 = await recv_type(ws2, "matched")
    check("ルーム内でマッチング", m1["my_role"] == "any" and m2["my_role"] == "any", f"{m1} / {m2}")

    # 定員2(通話中2人)なので3人目は満員
    ws3 = await websockets.connect(f"{WS_BASE}/ws?token={third_token}")
    await ws3.send(json.dumps({"type": "join_queue", "room_id": rid, "passphrase": "himitsu"}))
    err = await recv_type(ws3, "error")
    check("定員オーバーは満員", "満員" in err["message"], err)
    await ws3.close()
    await ws1.close()
    await ws2.close()

    # ルーム更新は管理者(作成者)のみ。ルーム管理者を追加すると更新できる
    upd = {"name": f"雑談ルーム_{suffix}", "passphrase": "", "capacity": 0,
           "session_minutes": None, "role_matching": None, "modes": None}
    r = requests.put(f"{BASE}/api/rooms/{rid}", headers=h1, json=upd)
    check("非管理者は更新不可", r.status_code == 403, r.text)
    r = requests.post(f"{BASE}/api/rooms/{rid}/managers", headers=h0,
                      json={"username": users[1]["username"]})
    check("ルーム管理者を追加", r.status_code == 201, r.text)
    r = requests.put(f"{BASE}/api/rooms/{rid}", headers=h1, json=upd)
    check("ルーム管理者は更新できる", r.status_code == 200, r.text)

    # チーム限定ルームは部外者から見えない・入れない
    r = requests.post(f"{BASE}/api/admin/teams", headers=admin_headers, json={"name": f"限定_{suffix}"})
    tid = r.json()["id"]
    requests.post(f"{BASE}/api/teams/{tid}/members", headers=admin_headers,
                  json={"username": users[0]["username"]})
    r = requests.post(f"{BASE}/api/rooms", headers=h0, json={"name": "チーム部屋", "team_id": tid})
    trid = r.json()["id"]
    r = requests.get(f"{BASE}/api/rooms", headers=h1)
    check("チーム限定ルームは部外者に見えない", all(x["id"] != trid for x in r.json()), r.text)
    ws4 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws4.send(json.dumps({"type": "join_queue", "room_id": trid}))
    err = await recv_type(ws4, "error")
    check("チーム限定ルームに部外者は入れない", "参加できません" in err["message"], err)
    await ws4.close()

    # ルーム機能オフ
    r = requests.put(f"{BASE}/api/admin/settings", headers=admin_headers,
                     json={**DEFAULT_PUT, "rooms_enabled": False})
    check("ルーム機能オフ", r.status_code == 200, r.text)
    r = requests.get(f"{BASE}/api/rooms", headers=h0)
    check("オフ時は一覧が空", r.json() == [], r.text)
    r = requests.post(f"{BASE}/api/rooms", headers=h0, json={"name": "作れない"})
    check("オフ時は作成不可", r.status_code == 403, r.text)
    requests.put(f"{BASE}/api/admin/settings", headers=admin_headers, json=DEFAULT_PUT)

    # 後始末: ルーム削除・チーム削除・ロールを戻す
    r = requests.delete(f"{BASE}/api/rooms/{rid}", headers=h0)
    check("ルーム削除", r.status_code == 200, r.text)
    requests.delete(f"{BASE}/api/rooms/{trid}", headers=h0)
    requests.delete(f"{BASE}/api/admin/teams/{tid}", headers=admin_headers)
    r = requests.put(f"{BASE}/api/admin/users/{uid0}/role", headers=admin_headers, json={"role": "user"})
    check("ロールを戻す", r.status_code == 200, r.text)


async def main():
    users = setup_users()
    room_id = await test_matching_flow(users)
    test_survey(users, room_id)
    await test_same_role_no_match(users)
    await test_decline_flow(users)
    await test_disconnect_during_wait(users)
    test_dashboard(users)
    admin_headers = test_admin(users)
    test_multitenant(users, admin_headers)
    await test_phase2_settings(users, admin_headers)
    test_teams(users, admin_headers)
    await test_rooms(users, admin_headers)
    restore_admin_password(admin_headers)
    print(f"\n結果: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
