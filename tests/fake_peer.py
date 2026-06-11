"""ブラウザ動作確認用の疑似ユーザー。役割を指定してキューに入り、マッチしたら同意して待つ

使い方: python tests/fake_peer.py [speaker|listener]  (省略時はlistener)
"""
import asyncio
import json
import secrets
import sys

import requests
import websockets

BASE = "http://127.0.0.1:8000"


async def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "listener"
    name = f"hanako_{secrets.token_hex(3)}"
    r = requests.post(f"{BASE}/api/register", json={"username": name, "password": "pass123"})
    token = r.json()["token"]
    print(f"registered: {name} (role={role})")

    async with websockets.connect(f"{BASE.replace('http', 'ws')}/ws?token={token}") as ws:
        await ws.send(json.dumps({"type": "join_queue", "role": role}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            print("recv:", msg)
            if msg["type"] == "matched":
                await ws.send(json.dumps({"type": "consent", "accept": True}))
                print("sent consent")
            elif msg["type"] in ("peer_left", "peer_declined"):
                print("done")
                return


asyncio.run(main())
