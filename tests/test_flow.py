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

    await ws1.send(json.dumps({"type": "join_queue"}))
    await recv_type(ws1, "queued")
    await ws2.send(json.dumps({"type": "join_queue"}))
    await recv_type(ws2, "queued")

    m1 = await recv_type(ws1, "matched")
    m2 = await recv_type(ws2, "matched")
    check("両者にmatched", m1["room_id"] == m2["room_id"], f"{m1} / {m2}")
    check("相手の名前が正しい",
          m1["peer_name"] == users[1]["username"] and m2["peer_name"] == users[0]["username"])
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


async def test_decline_flow(users):
    print("[4] 同意拒否フロー")
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws1.send(json.dumps({"type": "join_queue"}))
    await ws2.send(json.dumps({"type": "join_queue"}))
    await recv_type(ws1, "matched")
    await recv_type(ws2, "matched")

    await ws1.send(json.dumps({"type": "consent", "accept": False}))
    await recv_type(ws2, "peer_declined")
    check("拒否が相手に通知される", True)
    await ws1.close()
    await ws2.close()


async def test_disconnect_during_wait(users):
    print("[5] 待機中の切断")
    ws1 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    await ws1.send(json.dumps({"type": "join_queue"}))
    await recv_type(ws1, "queued")
    await ws1.close()
    # 切断後に別の2人が正常にマッチできること(キューに残骸が残らない)
    ws2 = await websockets.connect(f"{WS_BASE}/ws?token={users[0]['token']}")
    ws3 = await websockets.connect(f"{WS_BASE}/ws?token={users[1]['token']}")
    await ws2.send(json.dumps({"type": "join_queue"}))
    await recv_type(ws2, "queued")
    await ws3.send(json.dumps({"type": "join_queue"}))
    m = await recv_type(ws3, "matched")
    check("切断後も正常にマッチング", bool(m["room_id"]))
    await ws2.close()
    await ws3.close()


def test_dashboard(users):
    print("[6] ダッシュボードAPI")
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
          and "online" in s and "waiting" in s, r.text)


async def main():
    users = setup_users()
    room_id = await test_matching_flow(users)
    test_survey(users, room_id)
    await test_decline_flow(users)
    await test_disconnect_during_wait(users)
    test_dashboard(users)
    print(f"\n結果: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
