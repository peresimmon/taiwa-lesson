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

ROLE_SPEAKER = "speaker"   # 話し手
ROLE_LISTENER = "listener" # 聞き手
ROLE_ANY = "any"  # 役割なしマッチング(サイト設定で役割マッチングを無効にした場合)
ROLES = (ROLE_SPEAKER, ROLE_LISTENER, ROLE_ANY)  # join_queueで受け付ける役割の一覧

# セッション内だけで使う呼び名の候補。本名・ユーザー名は相手に伝えない
NICKNAMES = [
    "こもれび", "やまびこ", "せせらぎ", "そよかぜ", "ひだまり",
    "あさつゆ", "ゆうなぎ", "しらかば", "つきかげ", "ほしあかり",
    "かざはな", "たんぽぽ", "すずらん", "さざなみ", "あまやどり",
    "ゆきどけ", "はるかぜ", "こがらし", "しおさい", "わたぐも",
]


class Client:
    """1つのWebSocket接続(=ログイン中の1ユーザー)。待機〜通話中の状態を保持する"""

    def __init__(self, user_id: int, username: str, site_id: int, ws: WebSocket):
        self.user_id = user_id            # ユーザーの内部ID
        self.username = username          # 表示名(相手に見せる呼び名のベース)
        self.site_id = site_id            # マッチングは同一サイト内でのみ行う
        self.ws = ws                      # このクライアントのWebSocket
        self.room: "Room | None" = None   # 参加中の通話ルーム(待機中・未通話はNone)
        self.role: str | None = None      # 待機〜セッション中の役割
        self.nickname: str | None = None  # セッション限定のランダムな呼び名
        self.anonymous = True             # False=実名(ユーザー名)で表示するサイト
        self.queue_room = 0               # 待機中のルームID(0=ロビー通話)
        self.active_room_id = 0           # 通話中のルームID(定員カウント用)
        self.blocked: set[int] = set()    # 相互ブロック済みユーザーID(マッチさせない)
        self.preferred: set[int] = set()  # 「また話したい」が相互一致したユーザーID(優先)
        self.base_topic = ""              # ルーム/サイト設定由来の話題カード

    async def send(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:
            pass  # 切断済みの相手への送信は無視(切断処理はdisconnectで行う)


class Room:
    """マッチした2人の通話セッション(WebRTCのルーム)。1対1で固定"""

    def __init__(self, room_id: str, a: Client, b: Client):
        self.id = room_id                                  # セッションID(WebRTCのroom_id)
        self.members: dict[int, Client] = {a.user_id: a, b.user_id: b}  # user_id -> 参加者
        self.consents: set[int] = set()    # 同意済みユーザーID(2人そろうと通話開始)
        self.started = False               # 通話が開始済みか
        self.speaker_topic = ""            # 話し手が同意画面で設定した話題
        self.swap_requests: set[int] = set()  # 役割交代に同意したユーザー

    def peer_of(self, client: Client) -> Client | None:
        for uid, member in self.members.items():
            if uid != client.user_id:
                return member
        return None


class MatchingManager:
    """全接続クライアントの待機列・マッチング・シグナリング中継を司る(プロセス内シングルトン)"""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()  # 待機列・部屋の更新を直列化する排他ロック
        # (site_id, room_id, role) -> 待機中クライアント。サイト・ルームをまたいだマッチングはしない
        self.waiting: dict[tuple[int, int, str], list[Client]] = {}
        self.clients: dict[int, Client] = {}  # 接続中の全クライアント(user_id -> Client)
        self.on_match = None  # マッチ成立時のフック(通話ペアのDB記録に使う)

    def _queue(self, site_id: int, room_id: int, role: str) -> list[Client]:
        return self.waiting.setdefault((site_id, room_id, role), [])

    def waiting_counts(self, site_id: int, room_id: int = 0) -> dict[str, int]:
        return {
            "speakers": len(self._queue(site_id, room_id, ROLE_SPEAKER)),
            "listeners": len(self._queue(site_id, room_id, ROLE_LISTENER)),
            "any": len(self._queue(site_id, room_id, ROLE_ANY)),
        }

    def online_count(self, site_id: int) -> int:
        return sum(1 for c in self.clients.values() if c.site_id == site_id)

    def room_participants(self, site_id: int, room_id: int) -> int:
        """ルームの現在人数(待機中+そのルーム発の通話中)。定員チェック用"""
        waiting = sum(
            len(q) for (sid, rid, _), q in self.waiting.items()
            if sid == site_id and rid == room_id
        )
        active = sum(
            1 for c in self.clients.values()
            if c.site_id == site_id and c.room is not None and c.active_room_id == room_id
        )
        return waiting + active

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

    async def join_queue(self, client: Client, role: str, room_id: int = 0) -> None:
        if role not in ROLES:
            await client.send({"type": "error", "message": "「話し手」か「聞き手」を選んでください"})
            return
        async with self.lock:
            if client.room is not None or self._in_queue(client):
                return
            client.role = role
            client.queue_room = room_id
            # 相手となる待機列: 役割ありなら反対の役割、役割なしなら同じ"any"の列
            if role == ROLE_ANY:
                partner_queue = self._queue(client.site_id, room_id, ROLE_ANY)
            else:
                opposite = ROLE_LISTENER if role == ROLE_SPEAKER else ROLE_SPEAKER
                partner_queue = self._queue(client.site_id, room_id, opposite)
            # ブロック相手(相互)を除外し、「また話したい」相互一致を優先する
            candidates = [c for c in partner_queue if c.user_id not in client.blocked]
            if candidates:
                preferred = [c for c in candidates if c.user_id in client.preferred]
                partner = random.choice(preferred or candidates)
                partner_queue.remove(partner)
                await self._create_room(client, partner)
            else:
                self._queue(client.site_id, room_id, role).append(client)
                await client.send({"type": "queued", "role": role, "room_id": room_id})

    async def kick_room_queue(self, site_id: int, room_id: int) -> None:
        """ルーム削除時に待機中のクライアントを待機解除する"""
        async with self.lock:
            for (sid, rid, _), queue in list(self.waiting.items()):
                if sid != site_id or rid != room_id:
                    continue
                for client in list(queue):
                    queue.remove(client)
                    await client.send({"type": "error", "message": "ルームが削除されました"})

    async def _create_room(self, a: Client, b: Client) -> None:
        """部屋を作り、セッション限定の呼び名を割り振って双方に通知する(lock取得済みで呼ぶこと)"""
        room = Room(secrets.token_hex(16), a, b)
        a.room = room
        b.room = room
        a.active_room_id = a.queue_room
        b.active_room_id = b.queue_room
        if a.anonymous:
            # 匿名サイト: セッション限定のランダムな呼び名
            a.nickname, b.nickname = random.sample(NICKNAMES, 2)
        else:
            # 実名サイト(社内利用など): ユーザー名をそのまま表示
            a.nickname, b.nickname = a.username, b.username
        if self.on_match:
            self.on_match(a, b, room.id)  # 通話ペアを記録(通報・ブロック用)
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

    async def handle_consent(self, client: Client, accept: bool, topic: str = "") -> None:
        async with self.lock:
            room = client.room
            if room is None or room.started:
                return
            if not accept:
                # 同意しなかった → 部屋を解散し、相手に通知
                await self._teardown_room(client, notify_type="peer_declined")
                return
            # 話し手は同意画面で話題カードを設定できる(ロビー通話)
            if topic and client.role == ROLE_SPEAKER:
                room.speaker_topic = topic[:200]
            room.consents.add(client.user_id)
            if len(room.consents) == 2:
                # 双方が同意 → 通話開始。先にマッチした側がWebRTCのofferを作る
                room.started = True
                members = list(room.members.values())
                # 話題カード: 話し手の設定 > ルーム/サイト設定
                final_topic = room.speaker_topic or next(
                    (m.base_topic for m in members if m.base_topic), ""
                )
                for i, member in enumerate(members):
                    await member.send(
                        {"type": "call_start", "initiator": i == 0, "topic": final_topic}
                    )

    # --- 役割交代(10分×2回) ---------------------------------------------------

    async def handle_swap(self, client: Client) -> None:
        """双方が希望したら役割を交代してセッションを続ける"""
        async with self.lock:
            room = client.room
            if room is None or not room.started:
                return
            room.swap_requests.add(client.user_id)
            peer = room.peer_of(client)
            if peer is None:
                return
            if len(room.swap_requests) < 2:
                await peer.send({"type": "swap_offer"})  # 相手に交代の希望を通知
                return
            room.swap_requests.clear()
            client.role, peer.role = peer.role, client.role
            for member in room.members.values():
                other = room.peer_of(member)
                await member.send(
                    {"type": "swap_start", "my_role": member.role, "peer_role": other.role}
                )

    # --- セッション内チャット ---------------------------------------------------

    async def relay_chat(self, client: Client, text: str) -> None:
        room = client.room
        if room is None or not room.started or not text:
            return
        peer = room.peer_of(client)
        if peer is not None:
            await peer.send({"type": "chat", "text": text, "sender": client.nickname})

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
            member.active_room_id = 0
        if peer is not None:
            await peer.send({"type": notify_type})


manager = MatchingManager()
