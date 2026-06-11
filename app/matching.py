"""役割別マッチング(話し手×聞き手)とWebRTCシグナリングの管理

WebSocketメッセージ仕様(JSON):
  クライアント → サーバー
    {"type": "join_queue", "role": "speaker"|"listener"}  役割を指定して待機列に入る
    {"type": "cancel_queue"}                   待機をやめる
    {"type": "consent", "accept": true/false}  注意事項への同意/拒否
    {"type": "signal", "data": {...}}          WebRTCシグナリング(相手へ中継)
    {"type": "leave"}                          通話を終了する

  サーバー → クライアント
    {"type": "queued", "role": "speaker"|"listener"}    待機開始
    {"type": "matched", "room_id",
     "my_nickname", "my_role",
     "peer_nickname", "peer_role"}                      マッチ成立(同意画面へ)
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

ROLE_SPEAKER = "speaker"
ROLE_LISTENER = "listener"
ROLES = (ROLE_SPEAKER, ROLE_LISTENER)

# セッション内だけで使う呼び名の候補。本名・ユーザー名は相手に伝えない
NICKNAMES = [
    "こもれび", "やまびこ", "せせらぎ", "そよかぜ", "ひだまり",
    "あさつゆ", "ゆうなぎ", "しらかば", "つきかげ", "ほしあかり",
    "かざはな", "たんぽぽ", "すずらん", "さざなみ", "あまやどり",
    "ゆきどけ", "はるかぜ", "こがらし", "しおさい", "わたぐも",
]


class Client:
    def __init__(self, user_id: int, username: str, ws: WebSocket):
        self.user_id = user_id
        self.username = username
        self.ws = ws
        self.room: "Room | None" = None
        self.role: str | None = None      # 待機〜セッション中の役割
        self.nickname: str | None = None  # セッション限定のランダムな呼び名

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
        self.waiting: dict[str, list[Client]] = {ROLE_SPEAKER: [], ROLE_LISTENER: []}
        self.clients: dict[int, Client] = {}

    def waiting_counts(self) -> dict[str, int]:
        return {
            "speakers": len(self.waiting[ROLE_SPEAKER]),
            "listeners": len(self.waiting[ROLE_LISTENER]),
        }

    def _in_queue(self, client: Client) -> bool:
        return any(client in queue for queue in self.waiting.values())

    def _remove_from_queues(self, client: Client) -> None:
        for queue in self.waiting.values():
            if client in queue:
                queue.remove(client)

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
            self._remove_from_queues(client)
            await self._teardown_room(client, notify_type="peer_left")

    # --- マッチング ---------------------------------------------------------

    async def join_queue(self, client: Client, role: str) -> None:
        if role not in ROLES:
            await client.send({"type": "error", "message": "「話し手」か「聞き手」を選んでください"})
            return
        async with self.lock:
            if client.room is not None or self._in_queue(client):
                return
            client.role = role
            # 反対の役割で待っている人がいれば即マッチ。いなければ待機列へ
            opposite = ROLE_LISTENER if role == ROLE_SPEAKER else ROLE_SPEAKER
            if self.waiting[opposite]:
                partner = random.choice(self.waiting[opposite])
                self.waiting[opposite].remove(partner)
                await self._create_room(client, partner)
            else:
                self.waiting[role].append(client)
                await client.send({"type": "queued", "role": role})

    async def _create_room(self, a: Client, b: Client) -> None:
        """部屋を作り、セッション限定の呼び名を割り振って双方に通知する(lock取得済みで呼ぶこと)"""
        room = Room(secrets.token_hex(16), a, b)
        a.room = room
        b.room = room
        a.nickname, b.nickname = random.sample(NICKNAMES, 2)
        for member in (a, b):
            peer = room.peer_of(member)
            await member.send(
                {
                    "type": "matched",
                    "room_id": room.id,
                    "my_nickname": member.nickname,
                    "my_role": member.role,
                    "peer_nickname": peer.nickname,
                    "peer_role": peer.role,
                }
            )

    async def cancel_queue(self, client: Client) -> None:
        async with self.lock:
            self._remove_from_queues(client)

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
