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


def seed_demo_data() -> None:
    """お知らせが空ならデモ用の初期データを投入する"""
    db = SessionLocal()
    try:
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


@app.post("/api/register", response_model=TokenOut, status_code=201)
def register(body: AuthIn, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="このユーザー名は既に使われています")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(token=create_token(user.id), username=user.username)


@app.post("/api/login", response_model=TokenOut)
def login(body: AuthIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが違います")
    return TokenOut(token=create_token(user.id), username=user.username)


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username}


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
