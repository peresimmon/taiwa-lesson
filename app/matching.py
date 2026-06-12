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
ROLE_ANY = "any"  # 役割なしマッチング(サイト設定で役割マッチングを無効にした場合)
ROLES = (ROLE_SPEAKER, ROLE_LISTENER, ROLE_ANY)

# セッション内だけで使う呼び名の候補。本名・ユーザー名は相手に伝えない
NICKNAMES = [
    "こもれび", "やまびこ", "せせらぎ", "そよかぜ", "ひだまり",
    "あさつゆ", "ゆうなぎ", "しらかば", "つきかげ", "ほしあかり",
    "かざはな", "たんぽぽ", "すずらん", "さざなみ", "あまやどり",
    "ゆきどけ", "はるかぜ", "こがらし", "しおさい", "わたぐも",
]


class Client:
    def __init__(self, user_id: int, username: str, site_id: int, ws: WebSocket):
        self.user_id = user_id
        self.username = username
        self.site_id = site_id            # マッチングは同一サイト内でのみ行う
        self.ws = ws
        self.room: "Room | None" = None
        self.role: str | None = None      # 待機〜セッション中の役割
        self.nickname: str | None = None  # セッション限定のランダムな呼び名
        self.anonymous = True             # False=実名(ユーザー名)で表示するサイト

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
        # (site_id, role) -> 待機中クライアント。サイトをまたいだマッチングはしない
        self.waiting: dict[tuple[int, str], list[Client]] = {}
        self.clients: dict[int, Client] = {}

    def _queue(self, site_id: int, role: str) -> list[Client]:
        return self.waiting.setdefault((site_id, role), [])

    def waiting_counts(self, site_id: int) -> dict[str, int]:
        return {
            "speakers": len(self._queue(site_id, ROLE_SPEAKER)),
            "listeners": len(self._queue(site_id, ROLE_LISTENER)),
            "any": len(self._queue(site_id, ROLE_ANY)),
        }

    def online_count(self, site_id: int) -> int:
        return sum(1 for c in self.clients.values() if c.site_id == site_id)

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
            # 相手となる待機列: 役割ありなら反対の役割、役割なしなら同じ"any"の列
            if role == ROLE_ANY:
                partner_queue = self._queue(client.site_id, ROLE_ANY)
            else:
                opposite = ROLE_LISTENER if role == ROLE_SPEAKER else ROLE_SPEAKER
                partner_queue = self._queue(client.site_id, opposite)
            if partner_queue:
                partner = random.choice(partner_queue)
                partner_queue.remove(partner)
                await self._create_room(client, partner)
            else:
                self._queue(client.site_id, role).append(client)
                await client.send({"type": "queued", "role": role})

    async def _create_room(self, a: Client, b: Client) -> None:
        """部屋を作り、セッション限定の呼び名を割り振って双方に通知する(lock取得済みで呼ぶこと)"""
        room = Room(secrets.token_hex(16), a, b)
        a.room = room
        b.room = room
        if a.anonymous:
            # 匿名サイト: セッション限定のランダムな呼び名
            a.nickname, b.nickname = random.sample(NICKNAMES, 2)
        else:
            # 実名サイト(社内利用など): ユーザー名をそのまま表示
            a.nickname, b.nickname = a.username, b.username
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
