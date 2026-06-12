"""ビデオ通話マッチングアプリ バックエンド (FastAPI)

REST API + WebSocket の構成。将来のFlutterスマホアプリからも同じAPIを利用できる。
起動: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from pathlib import Path

import re

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
    Event,
    Post,
    SessionLocal,
    Setting,
    Site,
    Survey,
    User,
    get_db,
    init_db,
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
}


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


class PostIn(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


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
    return {
        "session_minutes": int(get_setting(db, sid, "session_minutes")),
        "role_matching": get_setting(db, sid, "role_matching") == "true",
        "anonymous_mode": get_setting(db, sid, "anonymous_mode") == "true",
        "survey_enabled": get_setting(db, sid, "survey_enabled") == "true",
        "survey_question": get_setting(db, sid, "survey_question") or DEFAULT_SETTINGS["survey_question"],
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
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """指定月(YYYY-MM)のイベント一覧(自サイトのみ)"""
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=422, detail="monthはYYYY-MM形式で指定してください")
    rows = (
        db.query(Event, User.username)
        .join(User, Event.user_id == User.id)
        .filter(User.site_id == user.site_id, Event.date.like(f"{month}-%"))
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
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    event = Event(user_id=user.id, title=body.title, date=body.date)
    db.add(event)
    db.commit()
    return {"ok": True, "id": event.id}


@app.get("/api/posts")
def list_posts(user: User = Depends(active_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Post, User.username)
        .join(User, Post.user_id == User.id)
        .filter(User.site_id == user.site_id)
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
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    post = Post(user_id=user.id, body=body.body)
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
    db.commit()
    return {"ok": True, "id": user.id}


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
    db.delete(target)
    db.commit()
    return {"ok": True}


def _settings_payload(db: Session, site_id: int) -> dict:
    def flag(key):
        return get_setting(db, site_id, key) == "true"

    return {
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
        "mode_toon", "mode_real", "mode_still", "mode_camera",
    ):
        set_setting(db, sid, key, "true" if getattr(body, key) else "false")
    set_setting(db, sid, "survey_question",
                body.survey_question.strip() or DEFAULT_SETTINGS["survey_question"])
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
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.query(Announcement).filter(Announcement.site_id == site_id).delete(synchronize_session=False)
    db.query(Setting).filter(Setting.site_id == site_id).delete(synchronize_session=False)
    db.delete(site)
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
                # サイト設定(役割マッチング・匿名)を待機開始の度に反映する
                sdb = SessionLocal()
                try:
                    role_matching = get_setting(sdb, user.site_id, "role_matching") == "true"
                    client.anonymous = get_setting(sdb, user.site_id, "anonymous_mode") == "true"
                finally:
                    sdb.close()
                role = msg.get("role") or ""
                if not role_matching:
                    role = "any"
                elif role == "any":
                    role = ""  # 役割マッチング有効時にanyは指定できない
                await manager.join_queue(client, role)
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
