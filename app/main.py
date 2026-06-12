"""ビデオ通話マッチングアプリ バックエンド (FastAPI)

REST API + WebSocket の構成。将来のFlutterスマホアプリからも同じAPIを利用できる。
起動: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from pathlib import Path

import re
import secrets

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import create_token, decode_token, hash_password, verify_password
from .database import (
    Announcement,
    AuditLog,
    Event,
    Post,
    Room,
    RoomManager,
    SessionLocal,
    Setting,
    Site,
    Survey,
    Team,
    TeamMember,
    User,
    get_db,
    init_db,
    utcnow,
)
from .matching import Client, manager

MAIN_SITE_SLUG = "taiwa-lesson"  # メインサイトのサイトID(変更の可能性あり)
SITE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")

app = FastAPI(title="VideoMatch API")

# スマホアプリ等の別オリジンからの利用を想定してCORSを許可(本番では絞ること)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def cache_control(request, call_next):
    """HTML/JS/CSSは毎回サーバーに再検証させる。
    デプロイ後にブラウザキャッシュの古いコードが使われ続けるのを防ぐ
    (ETagにより未変更なら304で済むため通信量はほぼ増えない)"""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", f"/{MAIN_SITE_SLUG}", "/login") or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response

init_db()


# --- サイト設定 ----------------------------------------------------------------

DEFAULT_SETTINGS = {
    "session_minutes": "10",       # 対話セッションの長さ(分)
    "allow_registration": "true",  # 新規ユーザー登録を受け付けるか(メインサイトのみ有効)
    "role_matching": "true",       # true=「話し手」「聞き手」に分かれてマッチング / false=役割なし
    "anonymous_mode": "true",      # true=匿名(ランダムな呼び名) / false=実名(ユーザー名表示)
    "survey_enabled": "true",      # セッション後のアンケートを行うか
    "survey_question": "相手の話を「聴けた」と感じましたか？",  # アンケートの設問
    # 通話画面で利用できる表示モード
    "mode_toon": "true",     # デフォルメモードアバター
    "mode_real": "true",     # リアルモードアバター(3D VRM)
    "mode_still": "true",    # 静止画
    "mode_camera": "false",  # 実映像(カメラそのまま)
    "rooms_enabled": "true",  # ルーム作成機能のオンオフ
    "tagline": "「話す力」じゃなく、「聴く力」を鍛える。",  # ダッシュボードのキャッチコピー
}

VIDEO_MODES = ("toon", "real", "still", "camera")


def audit(db: Session, actor: User, action: str, detail: str = "") -> None:
    """管理操作の監査ログを記録する(呼び出し側のcommitで保存される)"""
    db.add(
        AuditLog(
            site_id=actor.site_id,
            user_id=actor.id,
            username=actor.username,
            action=action,
            detail=detail[:500],
        )
    )


def get_setting(db: Session, site_id: int, key: str) -> str:
    row = db.get(Setting, (site_id, key))
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(db: Session, site_id: int, key: str, value: str) -> None:
    row = db.get(Setting, (site_id, key))
    if row:
        row.value = value
    else:
        db.add(Setting(site_id=site_id, key=key, value=value))


def create_site_admin(db: Session, site: Site) -> tuple[User, str]:
    """サイト管理者を自動生成する。初期パスワードは初回ログイン時に変更を強制"""
    password = f"password@{site.slug}"
    user = User(
        site_id=site.id,
        username=f"{site.slug}_admin",
        password_hash=hash_password(password),
        role="site_admin",
        must_change_password=True,
    )
    db.add(user)
    return user, password


def seed_initial_data() -> None:
    """メインサイト・管理者アカウント・デモ用初期データを投入する"""
    db = SessionLocal()
    try:
        main = db.query(Site).filter(Site.slug == MAIN_SITE_SLUG).first()
        if main is None:
            main = Site(slug=MAIN_SITE_SLUG, name="対話のおけいこ", is_main=True)
            db.add(main)
            db.commit()
            db.refresh(main)

        # マイグレーション直後の既存データ(site_id=0)をメインサイトへ移行
        db.query(User).filter(User.site_id == 0).update({"site_id": main.id})
        db.query(Announcement).filter(Announcement.site_id == 0).update({"site_id": main.id})
        db.commit()

        # システム管理者。初期パスワードのままなら初回ログイン時に変更を強制する
        admin = (
            db.query(User)
            .filter(User.site_id == main.id, User.username == "administrator")
            .first()
        )
        if admin is None:
            db.add(
                User(
                    site_id=main.id,
                    username="administrator",
                    password_hash=hash_password("password"),
                    role="system_admin",
                    must_change_password=True,
                )
            )
        else:
            if admin.role != "system_admin":
                admin.role = "system_admin"
            if verify_password("password", admin.password_hash) and not admin.must_change_password:
                admin.must_change_password = True

        # メインサイトのサイト管理者
        if not db.query(User).filter(
            User.site_id == main.id, User.username == f"{MAIN_SITE_SLUG}_admin"
        ).first():
            create_site_admin(db, main)
        db.commit()

        if db.query(Announcement).filter(Announcement.site_id == main.id).count() == 0:
            db.add_all(
                [
                    Announcement(
                        site_id=main.id,
                        title="「対話のおけいこ」へようこそ!",
                        body="「セッション相手を探す」ボタンを押すと、匿名の相手と10分間の対話セッションが始まります。「聴く力」を鍛える毎日の習慣にしましょう。",
                    ),
                    Announcement(
                        site_id=main.id,
                        title="ベータ版として運用中です",
                        body="現在デモ運用中のため、予告なくデータがリセットされる場合があります。気づきや改善案があれば掲示板でシェアしてください。",
                    ),
                ]
            )
            db.commit()
    finally:
        db.close()


seed_initial_data()

bearer_scheme = HTTPBearer()


# --- スキーマ ----------------------------------------------------------------


class AuthIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=128)


class LoginIn(AuthIn):
    site: str | None = None  # サブサイトのログインで指定。省略時はメインサイト


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=6, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class TokenOut(BaseModel):
    token: str
    username: str
    role: str = "user"
    site: str = MAIN_SITE_SLUG
    site_name: str = ""
    must_change_password: bool = False


class SurveyIn(BaseModel):
    room_id: str = Field(max_length=64)
    rating: int = Field(ge=1, le=5)
    talk_again: bool = False
    comment: str = Field(default="", max_length=2000)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    team_id: int | None = None  # 指定するとチーム限定の予定
    room_id: int | None = None  # ルーム連携(予定からルームに参加できる)


class PostIn(BaseModel):
    body: str = Field(min_length=1, max_length=1000)
    team_id: int | None = None  # 指定するとチーム限定の投稿


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TeamMemberIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    is_leader: bool = False  # サイト管理者の追加時のみ有効


class LeaderIn(BaseModel):
    is_leader: bool


class RoleIn(BaseModel):
    role: str = Field(pattern=r"^(user|moderator)$")


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    team_id: int | None = None                       # 見える範囲(Noneで全員)
    passphrase: str = Field(default="", max_length=100)
    capacity: int = Field(default=0, ge=0, le=100)   # 0=無制限
    expires_hours: int | None = Field(default=None, ge=1, le=720)  # Noneで無期限
    session_minutes: int | None = Field(default=None, ge=1, le=60)
    role_matching: bool | None = None
    modes: list[str] | None = None  # Noneでサイト設定に従う


class RoomManagerIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)


class SettingsIn(BaseModel):
    session_minutes: int = Field(ge=1, le=60)
    allow_registration: bool
    role_matching: bool = True
    anonymous_mode: bool = True
    survey_enabled: bool = True
    survey_question: str = Field(default="", max_length=300)
    mode_toon: bool = True
    mode_real: bool = True
    mode_still: bool = True
    mode_camera: bool = False
    rooms_enabled: bool = True
    site_name: str = Field(default="", max_length=100)  # 空欄なら変更しない
    tagline: str = Field(default="", max_length=200)    # 空欄ならデフォルト


class BulkUsersIn(BaseModel):
    csv: str = Field(min_length=1, max_length=20000)  # 1行=「ユーザー名,初期パスワード」


class SiteIn(BaseModel):
    slug: str = Field(min_length=2, max_length=30)
    name: str = Field(min_length=1, max_length=100)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=128)


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


def active_user(user: User = Depends(current_user)) -> User:
    """通常APIで使う依存。初期パスワードのままのユーザーは変更が済むまで利用不可"""
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="パスワードの変更が必要です")
    return user


def site_admin_user(user: User = Depends(active_user)) -> User:
    if user.role not in ("site_admin", "system_admin"):
        raise HTTPException(status_code=403, detail="サイト管理者権限が必要です")
    return user


def system_admin_user(user: User = Depends(active_user)) -> User:
    if user.role != "system_admin":
        raise HTTPException(status_code=403, detail="システム管理者権限が必要です")
    return user


def get_main_site(db: Session) -> Site:
    site = db.query(Site).filter(Site.slug == MAIN_SITE_SLUG).first()
    if site is None:
        raise HTTPException(status_code=500, detail="メインサイトが初期化されていません")
    return site


def token_response(user: User, site: Site) -> TokenOut:
    return TokenOut(
        token=create_token(user.id),
        username=user.username,
        role=user.role,
        site=site.slug,
        site_name=site.name,
        must_change_password=user.must_change_password,
    )


@app.post("/api/register", response_model=TokenOut, status_code=201)
def register(body: AuthIn, db: Session = Depends(get_db)):
    """自己登録はメインサイトのみ(サブサイトは管理者がアカウントを作成する)"""
    site = get_main_site(db)
    if get_setting(db, site.id, "allow_registration") != "true":
        raise HTTPException(status_code=403, detail="現在、新規登録は受け付けていません")
    if db.query(User).filter(User.site_id == site.id, User.username == body.username).first():
        raise HTTPException(status_code=409, detail="このユーザー名は既に使われています")
    user = User(site_id=site.id, username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return token_response(user, site)


@app.post("/api/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    slug = body.site or MAIN_SITE_SLUG
    site = db.query(Site).filter(Site.slug == slug).first()
    user = None
    if site is not None:
        user = (
            db.query(User)
            .filter(User.site_id == site.id, User.username == body.username)
            .first()
        )
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="サイトID・ユーザー名・パスワードのいずれかが違います")
    return token_response(user, site)


@app.post("/api/password")
def change_password(
    body: PasswordIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="現在のパスワードが違います")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    site = db.get(Site, user.site_id)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "site": site.slug if site else "",
        "site_name": site.name if site else "",
        "must_change_password": user.must_change_password,
    }


@app.get("/api/config")
def app_config(user: User = Depends(active_user), db: Session = Depends(get_db)):
    """ログインユーザー向けのサイト設定"""
    sid = user.site_id
    site = db.get(Site, sid)
    return {
        "site_name": site.name if site else "",
        "tagline": get_setting(db, sid, "tagline") or DEFAULT_SETTINGS["tagline"],
        "session_minutes": int(get_setting(db, sid, "session_minutes")),
        "role_matching": get_setting(db, sid, "role_matching") == "true",
        "anonymous_mode": get_setting(db, sid, "anonymous_mode") == "true",
        "survey_enabled": get_setting(db, sid, "survey_enabled") == "true",
        "survey_question": get_setting(db, sid, "survey_question") or DEFAULT_SETTINGS["survey_question"],
        "rooms_enabled": get_setting(db, sid, "rooms_enabled") == "true",
        "can_create_rooms": _is_moderator(user),
        "modes": {
            "toon": get_setting(db, sid, "mode_toon") == "true",
            "real": get_setting(db, sid, "mode_real") == "true",
            "still": get_setting(db, sid, "mode_still") == "true",
            "camera": get_setting(db, sid, "mode_camera") == "true",
        },
    }


# --- アンケート ----------------------------------------------------------------


@app.post("/api/surveys", status_code=201)
def submit_survey(
    body: SurveyIn,
    user: User = Depends(active_user),
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
def my_surveys(user: User = Depends(active_user), db: Session = Depends(get_db)):
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


# --- チーム ---------------------------------------------------------------------


def _team_or_404(db: Session, user: User, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None or team.site_id != user.site_id:
        raise HTTPException(status_code=404, detail="チームが存在しません")
    return team


def _membership(db: Session, team_id: int, user_id: int) -> TeamMember | None:
    return (
        db.query(TeamMember)
        .filter(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        .first()
    )


def _is_site_admin(user: User) -> bool:
    return user.role in ("site_admin", "system_admin")


def _require_team_member(db: Session, user: User, team_id: int) -> Team:
    """チームのメンバー(またはサイト管理者)であることを要求する"""
    team = _team_or_404(db, user, team_id)
    if not _is_site_admin(user) and _membership(db, team_id, user.id) is None:
        raise HTTPException(status_code=403, detail="このチームのメンバーではありません")
    return team


def _require_team_leader(db: Session, user: User, team_id: int) -> Team:
    """チームリーダー(またはサイト管理者)であることを要求する"""
    team = _team_or_404(db, user, team_id)
    if not _is_site_admin(user):
        m = _membership(db, team_id, user.id)
        if m is None or not m.is_leader:
            raise HTTPException(status_code=403, detail="チームリーダー権限が必要です")
    return team


@app.get("/api/teams")
def my_teams(user: User = Depends(active_user), db: Session = Depends(get_db)):
    """自分の所属チーム一覧"""
    from sqlalchemy import func

    counts = dict(
        db.query(TeamMember.team_id, func.count(TeamMember.id))
        .group_by(TeamMember.team_id)
        .all()
    )
    rows = (
        db.query(Team, TeamMember)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .filter(Team.site_id == user.site_id, TeamMember.user_id == user.id)
        .order_by(Team.id)
        .all()
    )
    return [
        {
            "id": t.id,
            "name": t.name,
            "is_leader": m.is_leader,
            "members": counts.get(t.id, 0),
        }
        for t, m in rows
    ]


@app.get("/api/teams/{team_id}/members")
def team_members(
    team_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    team = _require_team_member(db, user, team_id)
    rows = (
        db.query(TeamMember, User.username)
        .join(User, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team_id)
        .order_by(TeamMember.id)
        .all()
    )
    return {
        "team": team.name,
        "members": [
            {"user_id": m.user_id, "username": name, "is_leader": m.is_leader}
            for m, name in rows
        ],
    }


@app.post("/api/teams/{team_id}/members", status_code=201)
def team_add_member(
    team_id: int,
    body: TeamMemberIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """メンバー追加。チームリーダーは自チームへ招待可。リーダー指定はサイト管理者のみ"""
    _require_team_leader(db, user, team_id)
    target = (
        db.query(User)
        .filter(User.site_id == user.site_id, User.username == body.username)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if _membership(db, team_id, target.id):
        raise HTTPException(status_code=409, detail="既にチームのメンバーです")
    is_leader = body.is_leader if _is_site_admin(user) else False
    db.add(TeamMember(team_id=team_id, user_id=target.id, is_leader=is_leader))
    audit(db, user, "team_member_add", f"{target.username}")
    db.commit()
    return {"ok": True}


@app.delete("/api/teams/{team_id}/members/{user_id}")
def team_remove_member(
    team_id: int,
    user_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    _require_team_leader(db, user, team_id)
    m = _membership(db, team_id, user_id)
    if m is None:
        raise HTTPException(status_code=404, detail="チームのメンバーではありません")
    if m.is_leader and not _is_site_admin(user):
        raise HTTPException(status_code=403, detail="リーダーの削除はサイト管理者のみ可能です")
    db.delete(m)
    db.commit()
    return {"ok": True}


@app.get("/api/teams/{team_id}/stats")
def team_stats(
    team_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """チーム単位の統計(メンバー数・メンバーのセッション数合計)"""
    _require_team_member(db, user, team_id)
    member_ids = [
        m.user_id
        for m in db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
    ]
    sessions = (
        db.query(Survey).filter(Survey.user_id.in_(member_ids)).count()
        if member_ids
        else 0
    )
    return {"members": len(member_ids), "sessions": sessions}


# --- ルーム ---------------------------------------------------------------------


def _is_moderator(user: User) -> bool:
    return user.role in ("moderator", "site_admin", "system_admin")


def _room_or_404(db: Session, user: User, room_id: int) -> Room:
    room = db.get(Room, room_id)
    if room is None or room.site_id != user.site_id or _room_expired(room):
        raise HTTPException(status_code=404, detail="ルームが存在しません")
    return room


def _room_expired(room: Room) -> bool:
    return room.expires_at is not None and room.expires_at < utcnow().replace(tzinfo=None)


def _can_manage_room(db: Session, user: User, room: Room) -> bool:
    if _is_site_admin(user) or room.creator_id == user.id:
        return True
    return (
        db.query(RoomManager)
        .filter(RoomManager.room_id == room.id, RoomManager.user_id == user.id)
        .first()
        is not None
    )


def _require_room_manager(db: Session, user: User, room_id: int) -> Room:
    room = _room_or_404(db, user, room_id)
    if not _can_manage_room(db, user, room):
        raise HTTPException(status_code=403, detail="このルームの管理権限がありません")
    return room


def _room_visible(db: Session, user: User, room: Room) -> bool:
    """チーム限定ルームはメンバー(とサイト管理者)にだけ見える"""
    if room.team_id is None or _is_site_admin(user):
        return True
    return _membership(db, room.team_id, user.id) is not None


def _site_modes(db: Session, site_id: int) -> list[str]:
    return [m for m in VIDEO_MODES if get_setting(db, site_id, f"mode_{m}") == "true"]


def _room_payload(db: Session, user: User, room: Room) -> dict:
    """効果的な設定(ルーム上書き>サイト設定)込みのルーム情報"""
    sid = user.site_id
    eff_rm = (
        room.role_matching
        if room.role_matching is not None
        else get_setting(db, sid, "role_matching") == "true"
    )
    eff_minutes = room.session_minutes or int(get_setting(db, sid, "session_minutes"))
    eff_modes = room.modes.split(",") if room.modes else _site_modes(db, sid)
    can_manage = _can_manage_room(db, user, room)
    creator = db.get(User, room.creator_id)
    team = db.get(Team, room.team_id) if room.team_id else None
    payload = {
        "id": room.id,
        "name": room.name,
        "creator": creator.username if creator else "?",
        "team_id": room.team_id,
        "team_name": team.name if team else None,
        "has_passphrase": bool(room.passphrase),
        "capacity": room.capacity,
        "participants": manager.room_participants(sid, room.id),
        "expires_at": room.expires_at.isoformat() if room.expires_at else None,
        "session_minutes": eff_minutes,
        "role_matching": eff_rm,
        "modes": {m: m in eff_modes for m in VIDEO_MODES},
        "can_manage": can_manage,
        # 編集フォーム用の生値(管理者のみ)
        "raw": {
            "passphrase": room.passphrase,
            "session_minutes": room.session_minutes,
            "role_matching": room.role_matching,
            "modes": room.modes.split(",") if room.modes else None,
        } if can_manage else None,
        "managers": [
            {"user_id": m.user_id, "username": name}
            for m, name in db.query(RoomManager, User.username)
            .join(User, RoomManager.user_id == User.id)
            .filter(RoomManager.room_id == room.id)
            .all()
        ] if can_manage else None,
    }
    return payload


def _apply_room_settings(db: Session, user: User, room: Room, body: RoomIn) -> None:
    if body.team_id:
        _team_or_404(db, user, body.team_id)
    if body.modes is not None:
        invalid = [m for m in body.modes if m not in VIDEO_MODES]
        if invalid or not body.modes:
            raise HTTPException(status_code=422, detail="表示モードの指定が不正です")
    room.name = body.name
    room.team_id = body.team_id
    room.passphrase = body.passphrase
    room.capacity = body.capacity
    if body.expires_hours is not None:
        from datetime import timedelta

        room.expires_at = utcnow().replace(tzinfo=None) + timedelta(hours=body.expires_hours)
    else:
        room.expires_at = None
    room.session_minutes = body.session_minutes
    room.role_matching = body.role_matching
    room.modes = ",".join(body.modes) if body.modes is not None else None


@app.get("/api/rooms")
def list_rooms(user: User = Depends(active_user), db: Session = Depends(get_db)):
    """見えるルーム一覧(期限切れは削除)。ルーム機能オフのサイトでは空"""
    if get_setting(db, user.site_id, "rooms_enabled") != "true":
        return []
    rooms = db.query(Room).filter(Room.site_id == user.site_id).order_by(Room.id).all()
    result = []
    for room in rooms:
        if _room_expired(room):
            db.query(RoomManager).filter(RoomManager.room_id == room.id).delete()
            db.delete(room)
            continue
        if _room_visible(db, user, room):
            result.append(_room_payload(db, user, room))
    db.commit()
    return result


@app.post("/api/rooms", status_code=201)
def create_room(
    body: RoomIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """ルーム作成(モデレータ以上)。作成者は自動的に管理権限を持つ"""
    if get_setting(db, user.site_id, "rooms_enabled") != "true":
        raise HTTPException(status_code=403, detail="このサイトではルーム機能は無効です")
    if not _is_moderator(user):
        raise HTTPException(status_code=403, detail="ルーム作成にはモデレータ以上の権限が必要です")
    room = Room(site_id=user.site_id, creator_id=user.id, name=body.name)
    _apply_room_settings(db, user, room, body)
    db.add(room)
    audit(db, user, "room_create", body.name)
    db.commit()
    return {"ok": True, "id": room.id}


@app.put("/api/rooms/{room_id}")
def update_room(
    room_id: int,
    body: RoomIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    room = _require_room_manager(db, user, room_id)
    _apply_room_settings(db, user, room, body)
    audit(db, user, "room_update", body.name)
    db.commit()
    return {"ok": True}


@app.delete("/api/rooms/{room_id}")
async def delete_room(
    room_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    room = _require_room_manager(db, user, room_id)
    site_id = user.site_id
    db.query(RoomManager).filter(RoomManager.room_id == room.id).delete()
    db.query(Event).filter(Event.room_id == room.id).update({"room_id": None})
    db.delete(room)
    audit(db, user, "room_delete", room.name)
    db.commit()
    await manager.kick_room_queue(site_id, room_id)
    return {"ok": True}


@app.post("/api/rooms/{room_id}/managers", status_code=201)
def add_room_manager(
    room_id: int,
    body: RoomManagerIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """ルーム管理者を追加(設定変更権限を付与)"""
    room = _require_room_manager(db, user, room_id)
    target = (
        db.query(User)
        .filter(User.site_id == user.site_id, User.username == body.username)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    exists = (
        db.query(RoomManager)
        .filter(RoomManager.room_id == room.id, RoomManager.user_id == target.id)
        .first()
    )
    if exists or target.id == room.creator_id:
        raise HTTPException(status_code=409, detail="既にルーム管理者です")
    db.add(RoomManager(room_id=room.id, user_id=target.id))
    db.commit()
    return {"ok": True}


@app.delete("/api/rooms/{room_id}/managers/{user_id}")
def remove_room_manager(
    room_id: int,
    user_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    room = _require_room_manager(db, user, room_id)
    m = (
        db.query(RoomManager)
        .filter(RoomManager.room_id == room.id, RoomManager.user_id == user_id)
        .first()
    )
    if m is None:
        raise HTTPException(status_code=404, detail="ルーム管理者ではありません")
    db.delete(m)
    db.commit()
    return {"ok": True}


# --- ダッシュボード(お知らせ・イベント・掲示板・統計) ---------------------------


@app.get("/api/announcements")
def announcements(user: User = Depends(active_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Announcement)
        .filter(Announcement.site_id == user.site_id)
        .order_by(Announcement.created_at.desc())
        .limit(20)
        .all()
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
    team_id: int | None = None,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """指定月(YYYY-MM)のイベント一覧。team_id指定でチーム限定の予定"""
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=422, detail="monthはYYYY-MM形式で指定してください")
    q = (
        db.query(Event, User.username)
        .join(User, Event.user_id == User.id)
        .filter(User.site_id == user.site_id, Event.date.like(f"{month}-%"))
    )
    if team_id:
        _require_team_member(db, user, team_id)
        q = q.filter(Event.team_id == team_id)
    else:
        q = q.filter(Event.team_id.is_(None))
    rows = q.order_by(Event.date).all()
    # ルーム連携している予定にはルーム名を添える
    room_ids = {e.room_id for e, _ in rows if e.room_id}
    room_names = {
        r.id: r.name
        for r in db.query(Room).filter(Room.id.in_(room_ids)).all()
    } if room_ids else {}
    return [
        {
            "id": e.id,
            "title": e.title,
            "date": e.date,
            "username": name,
            "room_id": e.room_id,
            "room_name": room_names.get(e.room_id),
        }
        for e, name in rows
    ]


@app.post("/api/events", status_code=201)
def create_event(
    body: EventIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    if body.team_id:
        _require_team_member(db, user, body.team_id)
    if body.room_id:
        room = _room_or_404(db, user, body.room_id)
        if not _room_visible(db, user, room):
            raise HTTPException(status_code=403, detail="このルームには参加できません")
    event = Event(
        user_id=user.id, title=body.title, date=body.date,
        team_id=body.team_id, room_id=body.room_id,
    )
    db.add(event)
    db.commit()
    return {"ok": True, "id": event.id}


@app.get("/api/posts")
def list_posts(
    team_id: int | None = None,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """掲示板の投稿一覧。team_id指定でチーム限定の掲示板"""
    q = (
        db.query(Post, User.username)
        .join(User, Post.user_id == User.id)
        .filter(User.site_id == user.site_id)
    )
    if team_id:
        _require_team_member(db, user, team_id)
        q = q.filter(Post.team_id == team_id)
    else:
        q = q.filter(Post.team_id.is_(None))
    rows = q.order_by(Post.created_at.desc()).limit(30).all()
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
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    if body.team_id:
        _require_team_member(db, user, body.team_id)
    post = Post(user_id=user.id, body=body.body, team_id=body.team_id)
    db.add(post)
    db.commit()
    return {"ok": True, "id": post.id}


@app.get("/api/stats")
def stats(user: User = Depends(active_user), db: Session = Depends(get_db)):
    """ダッシュボード表示用の統計(自サイトのみ。オンライン=WebSocket接続中)"""
    counts = manager.waiting_counts(user.site_id)
    return {
        "total_users": db.query(User).filter(User.site_id == user.site_id).count(),
        "online": manager.online_count(user.site_id),
        "waiting": counts["speakers"] + counts["listeners"],
        "waiting_speakers": counts["speakers"],
        "waiting_listeners": counts["listeners"],
    }


# --- 管理者API ------------------------------------------------------------------


def _user_rows(db: Session, site_id: int) -> list[dict]:
    """サイト内のユーザー一覧(セッション数つき)"""
    from sqlalchemy import func

    counts = dict(
        db.query(Survey.user_id, func.count(Survey.id))
        .join(User, Survey.user_id == User.id)
        .filter(User.site_id == site_id)
        .group_by(Survey.user_id)
        .all()
    )
    rows = db.query(User).filter(User.site_id == site_id).order_by(User.id).all()
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


@app.get("/api/admin/users")
def admin_list_users(admin: User = Depends(site_admin_user), db: Session = Depends(get_db)):
    """自サイトのユーザー一覧"""
    return _user_rows(db, admin.site_id)


@app.post("/api/admin/users", status_code=201)
def admin_create_user(
    body: UserCreateIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """自サイトにユーザーを作成(初回ログイン時にパスワード変更を強制)"""
    if db.query(User).filter(
        User.site_id == admin.site_id, User.username == body.username
    ).first():
        raise HTTPException(status_code=409, detail="このユーザー名は既に使われています")
    user = User(
        site_id=admin.site_id,
        username=body.username,
        password_hash=hash_password(body.password),
        must_change_password=True,
    )
    db.add(user)
    audit(db, admin, "user_create", body.username)
    db.commit()
    return {"ok": True, "id": user.id}


@app.post("/api/admin/users/bulk")
def admin_bulk_users(
    body: BulkUsersIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """CSVでユーザーを一括登録する。1行=「ユーザー名,初期パスワード」。
    パスワード省略時は自動生成し、レスポンスで一度だけ返す"""
    results = []
    created = 0
    for line in body.csv.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0]
        given_pw = parts[1] if len(parts) > 1 and parts[1] else ""
        password = given_pw or secrets.token_urlsafe(6)
        if not (2 <= len(name) <= 50):
            results.append({"username": name, "password": None, "status": "ユーザー名は2〜50文字"})
            continue
        if len(password) < 6:
            results.append({"username": name, "password": None, "status": "パスワードは6文字以上"})
            continue
        if db.query(User).filter(User.site_id == admin.site_id, User.username == name).first():
            results.append({"username": name, "password": None, "status": "既に存在します"})
            continue
        db.add(
            User(
                site_id=admin.site_id,
                username=name,
                password_hash=hash_password(password),
                must_change_password=True,
            )
        )
        created += 1
        results.append({"username": name, "password": password if not given_pw else None, "status": "ok"})
    audit(db, admin, "users_bulk", f"CSV一括登録 {created}件")
    db.commit()
    return {"created": created, "results": results}


@app.get("/api/admin/report")
def admin_report(admin: User = Depends(site_admin_user), db: Session = Depends(get_db)):
    """サイトの利用レポート(セッション数・評価・日別推移・チーム別)"""
    from datetime import timedelta

    from sqlalchemy import func

    sid = admin.site_id
    site_surveys = (
        db.query(Survey).join(User, Survey.user_id == User.id).filter(User.site_id == sid)
    )
    now = utcnow().replace(tzinfo=None)
    avg_rating = (
        db.query(func.avg(Survey.rating))
        .join(User, Survey.user_id == User.id)
        .filter(User.site_id == sid)
        .scalar()
    )
    # 直近14日の日別セッション数
    since = now - timedelta(days=13)
    daily_rows = dict(
        db.query(func.date(Survey.created_at), func.count(Survey.id))
        .join(User, Survey.user_id == User.id)
        .filter(User.site_id == sid, Survey.created_at >= since.replace(hour=0, minute=0, second=0))
        .group_by(func.date(Survey.created_at))
        .all()
    )
    daily = []
    for i in range(13, -1, -1):
        d = (now - timedelta(days=i)).date().isoformat()
        daily.append({"date": d, "count": daily_rows.get(d, 0)})
    # チーム別
    teams = []
    for team in db.query(Team).filter(Team.site_id == sid).order_by(Team.id).all():
        member_ids = [
            m.user_id for m in db.query(TeamMember).filter(TeamMember.team_id == team.id).all()
        ]
        sessions = (
            db.query(Survey).filter(Survey.user_id.in_(member_ids)).count() if member_ids else 0
        )
        teams.append({"name": team.name, "members": len(member_ids), "sessions": sessions})
    return {
        "total_users": db.query(User).filter(User.site_id == sid).count(),
        "total_sessions": site_surveys.count(),
        "sessions_7d": site_surveys.filter(Survey.created_at >= now - timedelta(days=7)).count(),
        "sessions_30d": site_surveys.filter(Survey.created_at >= now - timedelta(days=30)).count(),
        "avg_rating": round(avg_rating, 2) if avg_rating is not None else None,
        "daily": daily,
        "teams": teams,
    }


@app.get("/api/admin/audit")
def admin_audit_log(admin: User = Depends(site_admin_user), db: Session = Depends(get_db)):
    """監査ログ(直近100件)"""
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.site_id == admin.site_id)
        .order_by(AuditLog.id.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": a.id,
            "username": a.username,
            "action": a.action,
            "detail": a.detail,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@app.put("/api/admin/users/{user_id}/role")
def admin_set_role(
    user_id: int,
    body: RoleIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """一般ユーザー⇔モデレータの切替(サイト管理者のみ)"""
    target = db.get(User, user_id)
    if target is None or target.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if target.role in ("site_admin", "system_admin"):
        raise HTTPException(status_code=400, detail="管理者のロールは変更できません")
    target.role = body.role
    audit(db, admin, "role_change", f"{target.username} → {body.role}")
    db.commit()
    return {"ok": True}


@app.get("/api/admin/users/{user_id}/surveys")
def admin_user_surveys(
    user_id: int,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """指定ユーザーの通話(セッション)履歴(自サイトのみ)"""
    target = db.get(User, user_id)
    if target is None or target.site_id != admin.site_id:
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
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """自サイトのユーザーと関連データを削除(管理者は削除不可)"""
    target = db.get(User, user_id)
    if target is None or target.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if target.role in ("site_admin", "system_admin"):
        raise HTTPException(status_code=400, detail="管理者ユーザーは削除できません")
    db.query(Survey).filter(Survey.user_id == user_id).delete()
    db.query(Event).filter(Event.user_id == user_id).delete()
    db.query(Post).filter(Post.user_id == user_id).delete()
    db.query(TeamMember).filter(TeamMember.user_id == user_id).delete()
    db.delete(target)
    audit(db, admin, "user_delete", target.username)
    db.commit()
    return {"ok": True}


def _settings_payload(db: Session, site_id: int) -> dict:
    def flag(key):
        return get_setting(db, site_id, key) == "true"

    site = db.get(Site, site_id)
    return {
        "site_name": site.name if site else "",
        "tagline": get_setting(db, site_id, "tagline") or DEFAULT_SETTINGS["tagline"],
        "session_minutes": int(get_setting(db, site_id, "session_minutes")),
        "allow_registration": flag("allow_registration"),
        "role_matching": flag("role_matching"),
        "anonymous_mode": flag("anonymous_mode"),
        "survey_enabled": flag("survey_enabled"),
        "survey_question": get_setting(db, site_id, "survey_question") or DEFAULT_SETTINGS["survey_question"],
        "mode_toon": flag("mode_toon"),
        "mode_real": flag("mode_real"),
        "mode_still": flag("mode_still"),
        "mode_camera": flag("mode_camera"),
        "rooms_enabled": flag("rooms_enabled"),
    }


@app.get("/api/admin/settings")
def admin_get_settings(admin: User = Depends(site_admin_user), db: Session = Depends(get_db)):
    return _settings_payload(db, admin.site_id)


@app.put("/api/admin/settings")
def admin_put_settings(
    body: SettingsIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    if not (body.mode_toon or body.mode_real or body.mode_still or body.mode_camera):
        raise HTTPException(status_code=422, detail="表示モードは少なくとも1つ有効にしてください")
    sid = admin.site_id
    set_setting(db, sid, "session_minutes", str(body.session_minutes))
    for key in (
        "allow_registration", "role_matching", "anonymous_mode", "survey_enabled",
        "mode_toon", "mode_real", "mode_still", "mode_camera", "rooms_enabled",
    ):
        set_setting(db, sid, key, "true" if getattr(body, key) else "false")
    set_setting(db, sid, "survey_question",
                body.survey_question.strip() or DEFAULT_SETTINGS["survey_question"])
    set_setting(db, sid, "tagline", body.tagline.strip() or DEFAULT_SETTINGS["tagline"])
    if body.site_name.strip():
        site = db.get(Site, sid)
        if site:
            site.name = body.site_name.strip()
    audit(db, admin, "settings_update", "サイト設定を変更")
    db.commit()
    return {"ok": True}


@app.post("/api/admin/announcements", status_code=201)
def admin_create_announcement(
    body: AnnouncementIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    ann = Announcement(site_id=admin.site_id, title=body.title, body=body.body)
    db.add(ann)
    audit(db, admin, "announcement_create", body.title)
    db.commit()
    return {"ok": True, "id": ann.id}


@app.delete("/api/admin/announcements/{ann_id}")
def admin_delete_announcement(
    ann_id: int,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    ann = db.get(Announcement, ann_id)
    if ann is None or ann.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="お知らせが存在しません")
    db.delete(ann)
    audit(db, admin, "announcement_delete", ann.title)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/teams")
def admin_list_teams(admin: User = Depends(site_admin_user), db: Session = Depends(get_db)):
    """自サイトのチーム一覧(メンバー数・リーダー名つき)"""
    from sqlalchemy import func

    counts = dict(
        db.query(TeamMember.team_id, func.count(TeamMember.id))
        .group_by(TeamMember.team_id)
        .all()
    )
    leaders: dict[int, list[str]] = {}
    for m, name in (
        db.query(TeamMember, User.username)
        .join(User, TeamMember.user_id == User.id)
        .filter(TeamMember.is_leader.is_(True))
        .all()
    ):
        leaders.setdefault(m.team_id, []).append(name)
    rows = db.query(Team).filter(Team.site_id == admin.site_id).order_by(Team.id).all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "members": counts.get(t.id, 0),
            "leaders": leaders.get(t.id, []),
        }
        for t in rows
    ]


@app.post("/api/admin/teams", status_code=201)
def admin_create_team(
    body: TeamIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    team = Team(site_id=admin.site_id, name=body.name)
    db.add(team)
    audit(db, admin, "team_create", body.name)
    db.commit()
    return {"ok": True, "id": team.id}


@app.delete("/api/admin/teams/{team_id}")
def admin_delete_team(
    team_id: int,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """チームと所属情報・チーム限定の投稿/予定を削除する"""
    team = _team_or_404(db, admin, team_id)
    db.query(TeamMember).filter(TeamMember.team_id == team_id).delete(synchronize_session=False)
    db.query(Post).filter(Post.team_id == team_id).delete(synchronize_session=False)
    db.query(Event).filter(Event.team_id == team_id).delete(synchronize_session=False)
    db.delete(team)
    audit(db, admin, "team_delete", team.name)
    db.commit()
    return {"ok": True}


@app.put("/api/admin/teams/{team_id}/members/{user_id}")
def admin_set_team_leader(
    team_id: int,
    user_id: int,
    body: LeaderIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """チームリーダーの設定/解除(サイト管理者のみ)"""
    _team_or_404(db, admin, team_id)
    m = _membership(db, team_id, user_id)
    if m is None:
        raise HTTPException(status_code=404, detail="チームのメンバーではありません")
    m.is_leader = body.is_leader
    audit(db, admin, "team_leader_set", f"user_id={user_id} → {body.is_leader}")
    db.commit()
    return {"ok": True}


# --- システム管理者API -----------------------------------------------------------


@app.get("/api/sysadmin/sites")
def sysadmin_list_sites(
    admin: User = Depends(system_admin_user), db: Session = Depends(get_db)
):
    from sqlalchemy import func

    counts = dict(db.query(User.site_id, func.count(User.id)).group_by(User.site_id).all())
    rows = db.query(Site).order_by(Site.id).all()
    return [
        {
            "id": s.id,
            "slug": s.slug,
            "name": s.name,
            "is_main": s.is_main,
            "users": counts.get(s.id, 0),
            "created_at": s.created_at.isoformat(),
        }
        for s in rows
    ]


@app.post("/api/sysadmin/sites", status_code=201)
def sysadmin_create_site(
    body: SiteIn,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    """サイトを作成し、サイト管理者を自動生成する(初期パスワードは一度だけ返す)"""
    if not SITE_SLUG_RE.fullmatch(body.slug):
        raise HTTPException(status_code=422, detail="サイトIDは半角小文字英数とハイフン(2〜30文字)で指定してください")
    if db.query(Site).filter(Site.slug == body.slug).first():
        raise HTTPException(status_code=409, detail="このサイトIDは既に使われています")
    site = Site(slug=body.slug, name=body.name)
    db.add(site)
    db.commit()
    db.refresh(site)
    site_admin, initial_password = create_site_admin(db, site)
    audit(db, admin, "site_create", site.slug)
    db.commit()
    return {
        "ok": True,
        "id": site.id,
        "slug": site.slug,
        "name": site.name,
        "admin_username": site_admin.username,
        "initial_password": initial_password,
    }


@app.delete("/api/sysadmin/sites/{site_id}")
def sysadmin_delete_site(
    site_id: int,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    """サイトと所属ユーザー・関連データをすべて削除する"""
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="サイトが存在しません")
    if site.is_main:
        raise HTTPException(status_code=400, detail="メインサイトは削除できません")
    user_ids = [u.id for u in db.query(User).filter(User.site_id == site_id).all()]
    if user_ids:
        db.query(Survey).filter(Survey.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Event).filter(Event.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Post).filter(Post.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(TeamMember).filter(TeamMember.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.query(Team).filter(Team.site_id == site_id).delete(synchronize_session=False)
    db.query(Announcement).filter(Announcement.site_id == site_id).delete(synchronize_session=False)
    db.query(Setting).filter(Setting.site_id == site_id).delete(synchronize_session=False)
    db.delete(site)
    audit(db, admin, "site_delete", site.slug)
    db.commit()
    return {"ok": True}


@app.get("/api/sysadmin/sites/{site_id}/users")
def sysadmin_site_users(
    site_id: int,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    if db.get(Site, site_id) is None:
        raise HTTPException(status_code=404, detail="サイトが存在しません")
    return _user_rows(db, site_id)


@app.get("/api/sysadmin/sites/{site_id}/settings")
def sysadmin_site_settings(
    site_id: int,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    if db.get(Site, site_id) is None:
        raise HTTPException(status_code=404, detail="サイトが存在しません")
    return _settings_payload(db, site_id)


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
    if user.must_change_password:
        await ws.close(code=4003, reason="password change required")
        return

    await ws.accept()
    client = Client(user.id, user.username, user.site_id, ws)
    if not await manager.connect(client):
        await ws.send_json({"type": "error", "message": "別の端末で接続中です"})
        await ws.close(code=4002, reason="already connected")
        return

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")
            if msg_type == "join_queue":
                # サイト設定・ルーム設定を待機開始の度に反映する
                room_id = int(msg.get("room_id") or 0)
                join_error = None
                sdb = SessionLocal()
                try:
                    role_matching = get_setting(sdb, user.site_id, "role_matching") == "true"
                    client.anonymous = get_setting(sdb, user.site_id, "anonymous_mode") == "true"
                    if room_id:
                        room = sdb.get(Room, room_id)
                        if (
                            room is None
                            or room.site_id != user.site_id
                            or _room_expired(room)
                            or get_setting(sdb, user.site_id, "rooms_enabled") != "true"
                        ):
                            join_error = "ルームが見つかりません"
                        elif not _room_visible(sdb, user, room):
                            join_error = "このルームには参加できません"
                        elif room.passphrase and (msg.get("passphrase") or "") != room.passphrase:
                            join_error = "合言葉が違います"
                        elif room.capacity and manager.room_participants(user.site_id, room_id) >= room.capacity:
                            join_error = "このルームは満員です"
                        elif room.role_matching is not None:
                            role_matching = room.role_matching  # ルーム設定がサイト設定より優先
                finally:
                    sdb.close()
                if join_error:
                    await client.send({"type": "error", "message": join_error})
                    continue
                role = msg.get("role") or ""
                if not role_matching:
                    role = "any"
                elif role == "any":
                    role = ""  # 役割マッチング有効時にanyは指定できない
                await manager.join_queue(client, role, room_id)
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


@app.get("/")
def root_redirect():
    """ルートはメインサイトのログインページへ"""
    return RedirectResponse(f"/{MAIN_SITE_SLUG}")


@app.get(f"/{MAIN_SITE_SLUG}")
@app.get("/login")
def spa_pages():
    """メインサイト(/taiwa-lesson)とサブサイト(/login)のログインページ。
    どちらも同じSPAを返し、フロント側がURLで表示を切り替える"""
    return FileResponse(static_dir / "index.html")


app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
