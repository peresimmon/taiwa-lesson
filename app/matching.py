"""ランダムマッチングとWebRTCシグナリングの管理

WebSocketメッセージ仕様(JSON):
  クライアント → サーバー
    {"type": "join_queue"}                     待機列に入る
    {"type": "cancel_queue"}                   待機をやめる
    {"type": "consent", "accept": true/false}  注意事項への同意/拒否
    {"type": "signal", "data": {...}}          WebRTCシグナリング(相手へ中継)
    {"type": "leave"}                          通話を終了する

  サーバー → クライアント
    {"type": "queued"}                                  待機開始
    {"type": "matched", "room_id", "peer_name"}         マッチ成立(同意画面へ)
    {"type": "call_start", "initiator": true/false}     双方同意、通話開始
    {"type": "peer_declined"}                           相手が同意しなかった
    {"type": "signal", "data": {...}}                   シグナリング中継
    {"type": "peer_left"}                               相手が退出した
    {"type": "error", "message": "..."}
"""
import asyncio
import random
import secrets

from fastapi import WebSocket


class Client:
    def __init__(self, user_id: int, username: str, ws: WebSocket):
        self.user_id = user_id
        self.username = username
        self.ws = ws
        self.room: "Room | None" = None

    async def send(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:
            pass  # 切断済みの相手への送信は無視(切断処理はdisconnectで行う)


class Room:
    def __init__(self, room_id: str, a: Client, b: Client):
        self.id = room_id
        self.members: dict[int, Client] = {a.user_id: a, b.user_id: b}
        self.consents: set[int] = set()
        self.started = False

    def peer_of(self, client: Client) -> Client | None:
        for uid, member in self.members.items():
            if uid != client.user_id:
                return member
        return None


class MatchingManager:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiting: list[Client] = []
        self.clients: dict[int, Client] = {}

    # --- 接続管理 -----------------------------------------------------------

    async def connect(self, client: Client) -> bool:
        """接続を登録する。同一ユーザーの二重接続は拒否"""
        async with self.lock:
            if client.user_id in self.clients:
                return False
            self.clients[client.user_id] = client
            return True

    async def disconnect(self, client: Client) -> None:
        async with self.lock:
            self.clients.pop(client.user_id, None)
            if client in self.waiting:
                self.waiting.remove(client)
            await self._teardown_room(client, notify_type="peer_left")

    # --- マッチング ---------------------------------------------------------

    async def join_queue(self, client: Client) -> None:
        async with self.lock:
            if client.room is not None or client in self.waiting:
                return
            self.waiting.append(client)
            await client.send({"type": "queued"})
            if len(self.waiting) >= 2:
                # 待機中からランダムに2人を抽出してペアにする
                pair = random.sample(self.waiting, 2)
                for member in pair:
                    self.waiting.remove(member)
                room = Room(secrets.token_hex(16), pair[0], pair[1])
                pair[0].room = room
                pair[1].room = room
                for member in pair:
                    peer = room.peer_of(member)
                    await member.send(
                        {
                            "type": "matched",
                            "room_id": room.id,
                            "peer_name": peer.username,
                        }
                    )

    async def cancel_queue(self, client: Client) -> None:
        async with self.lock:
            if client in self.waiting:
                self.waiting.remove(client)

    # --- 同意フロー ---------------------------------------------------------

    async def handle_consent(self, client: Client, accept: bool) -> None:
        async with self.lock:
            room = client.room
            if room is None or room.started:
                return
            if not accept:
                # 同意しなかった → 部屋を解散し、相手に通知
                await self._teardown_room(client, notify_type="peer_declined")
                return
            room.consents.add(client.user_id)
            if len(room.consents) == 2:
                # 双方が同意 → 通話開始。先にマッチした側がWebRTCのofferを作る
                room.started = True
                members = list(room.members.values())
                for i, member in enumerate(members):
                    await member.send({"type": "call_start", "initiator": i == 0})

    # --- シグナリング中継 ----------------------------------------------------

    async def relay_signal(self, client: Client, data: dict) -> None:
        room = client.room
        if room is None or not room.started:
            return
        peer = room.peer_of(client)
        if peer is not None:
            await peer.send({"type": "signal", "data": data})

    # --- 退出 ---------------------------------------------------------------

    async def leave_call(self, client: Client) -> None:
        async with self.lock:
            await self._teardown_room(client, notify_type="peer_left")

    async def _teardown_room(self, client: Client, notify_type: str) -> None:
        """部屋を解散する(lock取得済みで呼ぶこと)"""
        room = client.room
        if room is None:
            return
        peer = room.peer_of(client)
        for member in room.members.values():
            member.room = None
        if peer is not None:
            await peer.send({"type": notify_type})


manager = MatchingManager()
