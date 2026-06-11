"""ブラウザ動作確認用の疑似ユーザー。キューに入り、マッチしたら同意して待つ"""
import asyncio
import json
import secrets

import requests
import websockets

BASE = "http://127.0.0.1:8000"


async def main():
    name = f"hanako_{secrets.token_hex(3)}"
    r = requests.post(f"{BASE}/api/register", json={"username": name, "password": "pass123"})
    token = r.json()["token"]
    print(f"registered: {name}")

    async with websockets.connect(f"ws://127.0.0.1:8000/ws?token={token}") as ws:
        await ws.send(json.dumps({"type": "join_queue"}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            print("recv:", msg["type"])
            if msg["type"] == "matched":
                await ws.send(json.dumps({"type": "consent", "accept": True}))
                print("sent consent")
            elif msg["type"] in ("peer_left", "peer_declined"):
                print("done")
                return


asyncio.run(main())
