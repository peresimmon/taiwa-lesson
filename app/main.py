"""ビデオ通話マッチングアプリ バックエンド (FastAPI)

REST API + WebSocket の構成。将来のFlutterスマホアプリからも同じAPIを利用できる。
起動: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import create_token, decode_token, hash_password, verify_password
from .database import (
    Announcement,
    Event,
    Post,
    SessionLocal,
    Setting,
    Survey,
    User,
    get_db,
    init_db,
)
from .matching import Client, manager

app = FastAPI(title="VideoMatch API")

# スマホアプリ等の別オリジンからの利用を想定してCORSを許可(本番では絞ること)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# --- サイト設定 ----------------------------------------------------------------

DEFAULT_SETTINGS = {
    "session_minutes": "10",     # 対話セッションの長さ(分)
    "allow_registration": "true",  # 新規ユーザー登録を受け付けるか
}


def get_setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def seed_demo_data() -> None:
    """管理者アカウントと、お知らせが空ならデモ用の初期データを投入する"""
    db = SessionLocal()
    try:
        # 起動直後から使える管理者アカウント(本番ではパスワードを必ず変更すること)
        if not db.query(User).filter(User.username == "administrator").first():
            db.add(
                User(
                    username="administrator",
                    password_hash=hash_password("password"),
                    role="admin",
                )
            )
            db.commit()
        if db.query(Announcement).count() == 0:
            db.add_all(
                [
                    Announcement(
                        title="「対話のおけいこ」へようこそ!",
                        body="「セッション相手を探す」ボタンを押すと、匿名の相手と10分間の対話セッションが始まります。「聴く力」を鍛える毎日の習慣にしましょう。",
                    ),
                    Announcement(
                        title="ベータ版として運用中です",
                        body="現在デモ運用中のため、予告なくデータがリセットされる場合があります。気づきや改善案があれば掲示板でシェアしてください。",
                    ),
                ]
            )
            db.commit()
    finally:
        db.close()


seed_demo_data()

bearer_scheme = HTTPBearer()


# --- スキーマ ----------------------------------------------------------------


class AuthIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=128)


class TokenOut(BaseModel):
    token: str
    username: str
    role: str = "user"


class SurveyIn(BaseModel):
    room_id: str = Field(max_length=64)
    rating: int = Field(ge=1, le=5)
    talk_again: bool = False
    comment: str = Field(default="", max_length=2000)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PostIn(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)


class SettingsIn(BaseModel):
    session_minutes: int = Field(ge=1, le=60)
    allow_registration: bool


# --- 認証 ---------------------------------------------------------------------


def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    user_id = decode_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="トークンが無効です")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="ユーザーが存在しません")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return user


@app.post("/api/register", response_model=TokenOut, status_code=201)
def register(body: AuthIn, db: Session = Depends(get_db)):
    if get_setting(db, "allow_registration") != "true":
        raise HTTPException(status_code=403, detail="現在、新規登録は受け付けていません")
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="このユーザー名は既に使われています")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(token=create_token(user.id), username=user.username, role=user.role)


@app.post("/api/login", response_model=TokenOut)
def login(body: AuthIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが違います")
    return TokenOut(token=create_token(user.id), username=user.username, role=user.role)


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "role": user.role}


@app.get("/api/config")
def app_config(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ログインユーザー向けのサイト設定(セッション時間など)"""
    return {"session_minutes": int(get_setting(db, "session_minutes"))}


# --- アンケート ----------------------------------------------------------------


@app.post("/api/surveys", status_code=201)
def submit_survey(
    body: SurveyIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    survey = Survey(
        user_id=user.id,
        room_id=body.room_id,
        rating=body.rating,
        talk_again=body.talk_again,
        comment=body.comment,
    )
    db.add(survey)
    db.commit()
    return {"ok": True}


@app.get("/api/surveys/mine")
def my_surveys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Survey)
        .filter(Survey.user_id == user.id)
        .order_by(Survey.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "room_id": s.room_id,
            "rating": s.rating,
            "talk_again": s.talk_again,
            "comment": s.comment,
            "created_at": s.created_at.isoformat(),
        }
        for s in rows
    ]


# --- ダッシュボード(お知らせ・イベント・掲示板・統計) ---------------------------


@app.get("/api/announcements")
def announcements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Announcement).order_by(Announcement.created_at.desc()).limit(20).all()
    )
    return [
        {
            "id": a.id,
            "title": a.title,
            "body": a.body,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@app.get("/api/events")
def list_events(
    month: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """指定月(YYYY-MM)のイベント一覧"""
    import re

    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=422, detail="monthはYYYY-MM形式で指定してください")
    rows = (
        db.query(Event, User.username)
        .join(User, Event.user_id == User.id)
        .filter(Event.date.like(f"{month}-%"))
        .order_by(Event.date)
        .all()
    )
    return [
        {"id": e.id, "title": e.title, "date": e.date, "username": name}
        for e, name in rows
    ]


@app.post("/api/events", status_code=201)
def create_event(
    body: EventIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    event = Event(user_id=user.id, title=body.title, date=body.date)
    db.add(event)
    db.commit()
    return {"ok": True, "id": event.id}


@app.get("/api/posts")
def list_posts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Post, User.username)
        .join(User, Post.user_id == User.id)
        .order_by(Post.created_at.desc())
        .limit(30)
        .all()
    )
    return [
        {
            "id": p.id,
            "body": p.body,
            "username": name,
            "created_at": p.created_at.isoformat(),
        }
        for p, name in rows
    ]


@app.post("/api/posts", status_code=201)
def create_post(
    body: PostIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    post = Post(user_id=user.id, body=body.body)
    db.add(post)
    db.commit()
    return {"ok": True, "id": post.id}


@app.get("/api/stats")
def stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ダッシュボード表示用の統計(オンライン=WebSocket接続中)"""
    counts = manager.waiting_counts()
    return {
        "total_users": db.query(User).count(),
        "online": len(manager.clients),
        "waiting": counts["speakers"] + counts["listeners"],
        "waiting_speakers": counts["speakers"],
        "waiting_listeners": counts["listeners"],
    }


# --- 管理者API ------------------------------------------------------------------


@app.get("/api/admin/users")
def admin_list_users(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    """登録ユーザー一覧(セッション数つき)"""
    from sqlalchemy import func

    counts = dict(
        db.query(Survey.user_id, func.count(Survey.id)).group_by(Survey.user_id).all()
    )
    rows = db.query(User).order_by(User.id).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "created_at": u.created_at.isoformat(),
            "session_count": counts.get(u.id, 0),
        }
        for u in rows
    ]


@app.get("/api/admin/users/{user_id}/surveys")
def admin_user_surveys(
    user_id: int,
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    """指定ユーザーの通話(セッション)履歴"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    rows = (
        db.query(Survey)
        .filter(Survey.user_id == user_id)
        .order_by(Survey.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "username": target.username,
        "surveys": [
            {
                "room_id": s.room_id,
                "rating": s.rating,
                "talk_again": s.talk_again,
                "comment": s.comment,
                "created_at": s.created_at.isoformat(),
            }
            for s in rows
        ],
    }


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    """ユーザーと関連データを削除(管理者は削除不可)"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if target.role == "admin":
        raise HTTPException(status_code=400, detail="管理者ユーザーは削除できません")
    db.query(Survey).filter(Survey.user_id == user_id).delete()
    db.query(Event).filter(Event.user_id == user_id).delete()
    db.query(Post).filter(Post.user_id == user_id).delete()
    db.delete(target)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_get_settings(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {
        "session_minutes": int(get_setting(db, "session_minutes")),
        "allow_registration": get_setting(db, "allow_registration") == "true",
    }


@app.put("/api/admin/settings")
def admin_put_settings(
    body: SettingsIn,
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    set_setting(db, "session_minutes", str(body.session_minutes))
    set_setting(db, "allow_registration", "true" if body.allow_registration else "false")
    db.commit()
    return {"ok": True}


@app.post("/api/admin/announcements", status_code=201)
def admin_create_announcement(
    body: AnnouncementIn,
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    ann = Announcement(title=body.title, body=body.body)
    db.add(ann)
    db.commit()
    return {"ok": True, "id": ann.id}


@app.delete("/api/admin/announcements/{ann_id}")
def admin_delete_announcement(
    ann_id: int,
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    ann = db.get(Announcement, ann_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="お知らせが存在しません")
    db.delete(ann)
    db.commit()
    return {"ok": True}


# --- WebSocket(マッチング+シグナリング) -------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    user_id = decode_token(token)
    if user_id is None:
        await ws.close(code=4001, reason="invalid token")
        return

    # ユーザー名をDBから取得
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
    finally:
        db.close()
    if user is None:
        await ws.close(code=4001, reason="unknown user")
        return

    await ws.accept()
    client = Client(user.id, user.username, ws)
    if not await manager.connect(client):
        await ws.send_json({"type": "error", "message": "別の端末で接続中です"})
        await ws.close(code=4002, reason="already connected")
        return

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")
            if msg_type == "join_queue":
                await manager.join_queue(client, msg.get("role") or "")
            elif msg_type == "cancel_queue":
                await manager.cancel_queue(client)
            elif msg_type == "consent":
                await manager.handle_consent(client, bool(msg.get("accept")))
            elif msg_type == "signal":
                await manager.relay_signal(client, msg.get("data") or {})
            elif msg_type == "leave":
                await manager.leave_call(client)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(client)


# --- フロントエンド配信(最後にマウント) ----------------------------------------

static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
