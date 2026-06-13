"""ビデオ通話マッチングアプリ バックエンド (FastAPI)

REST API + WebSocket の構成。将来のFlutterスマホアプリからも同じAPIを利用できる。
起動: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from pathlib import Path

import json
import os
import random
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
    Block,
    CallPair,
    Event,
    Post,
    Report,
    Room,
    RoomManager,
    SessionLocal,
    Setting,
    Site,
    Survey,
    Team,
    TeamMember,
    User,
    Warning,
    get_db,
    init_db,
    utcnow,
)
from .matching import Client, manager


def _record_call_pair(a: Client, b: Client, call_id: str) -> None:
    """マッチ成立時に通話ペアを記録する(通報・ブロック・再マッチ優先で使う)"""
    db = SessionLocal()
    try:
        db.add(CallPair(call_id=call_id, site_id=a.site_id, user_a=a.user_id, user_b=b.user_id))
        db.commit()
    finally:
        db.close()


manager.on_match = _record_call_pair

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
    if path in ("/", f"/{MAIN_SITE_SLUG}", "/login", "/manifest.json") or path.endswith(
        (".html", ".js", ".css")
    ):
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
    # 通話機能の利用可否
    "feature_mute": "true",          # ミュート
    "feature_camera_toggle": "true", # 映像オフ
    "feature_screenshare": "true",   # 画面共有
    "feature_chat": "true",          # セッション内チャット
    "role_swap_enabled": "true",     # 役割交代つきセッション(10分×2回)
    # ロビー通話(役割なし)の話題カード: none / random / fixed
    "lobby_topic_mode": "none",
    "lobby_topic_text": "",   # fixedのときの話題
    "topic_pool": "",         # randomで使う独自アセット(1行1話題。空なら内蔵アセット)
    "rematch_priority": "true",  # 「また話したい」同士の再マッチ優先
    "survey_questions": "",   # アンケート設問(1行1問。空なら単一設問survey_questionを使用)
}

VIDEO_MODES = ("toon", "real", "still", "camera")

# ランダム話題カードの内蔵アセット
TOPICS = [
    "最近うれしかったこと", "ハマっている食べもの", "子どものころの夢",
    "最近ちょっと頑張ったこと", "行ってみたい場所", "好きな季節とその理由",
    "最近見た映画やドラマ", "休日の過ごし方", "今年挑戦したいこと",
    "自分のちょっとした自慢", "最近気になっているニュース", "好きな音楽の話",
    "もし1週間休みがあったら", "最近笑ったできごと", "大切にしている習慣",
    "学生時代の思い出", "おすすめの本やマンガ", "朝型? 夜型?",
    "最近買ってよかったもの", "ストレス解消法", "もし宝くじが当たったら",
    "今いちばん欲しいスキル", "ペットや動物の話", "明日が楽しみになる予定",
]


def send_mail(to: str | None, subject: str, body: str) -> bool:
    """SMTP設定(環境変数)があればメールを送る。未設定・失敗時はFalse"""
    host = os.environ.get("SMTP_HOST")
    if not to or not host:
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "noreply@example.com")
        msg["To"] = to
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as s:
            s.starttls()
            smtp_user = os.environ.get("SMTP_USER")
            smtp_pass = os.environ.get("SMTP_PASS")
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        return True
    except Exception:
        return False


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

        # システム管理者。
        # デモ版に限り初回ログイン時のパスワード変更強制を行わない
        # (本番では必ず復活させること → docs/TODO.md)
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
                    must_change_password=False,
                )
            )
        else:
            if admin.role != "system_admin":
                admin.role = "system_admin"
            if admin.must_change_password:
                admin.must_change_password = False

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
    display_name: str = ""  # 表示名(未設定はusernameと同じ値)
    role: str = "user"
    site: str = MAIN_SITE_SLUG
    site_name: str = ""
    must_change_password: bool = False


class SurveyIn(BaseModel):
    room_id: str = Field(max_length=64)
    rating: int = Field(ge=1, le=5)
    talk_again: bool = False
    comment: str = Field(default="", max_length=2000)
    answers: list[int] = Field(default_factory=list, max_length=20)  # 複数設問の評価(1〜5)


class ReportIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)


class ReportStatusIn(BaseModel):
    status: str = Field(pattern=r"^(open|resolved)$")


class BlockIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=64)


class WarningIn(BaseModel):
    user_id: int
    message: str = Field(min_length=1, max_length=500)


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
    description: str = Field(default="", max_length=500)


class MeUpdateIn(BaseModel):
    """自分の設定変更。送られてきたフィールドのみ更新する"""
    email: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=50)  # 空文字でusernameに戻す


class TeamMemberIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    is_leader: bool = False  # サイト管理者の追加時のみ有効


class LeaderIn(BaseModel):
    is_leader: bool


class RoleIn(BaseModel):
    role: str = Field(pattern=r"^(user|moderator|site_admin)$")


class ActiveIn(BaseModel):
    is_active: bool


class UserEditIn(BaseModel):
    """ユーザー編集(管理者用)。送られてきたフィールドのみ更新する"""
    display_name: str | None = Field(default=None, max_length=50)  # 空文字でusernameに戻す
    role: str | None = Field(default=None, pattern=r"^(user|moderator|site_admin)$")
    is_active: bool | None = None


class SiteEditIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    team_id: int | None = None                       # 見える範囲(Noneで全員)
    passphrase: str = Field(default="", max_length=100)
    capacity: int = Field(default=0, ge=0, le=100)   # 0=無制限
    expires_hours: int | None = Field(default=None, ge=1, le=720)  # Noneで無期限
    session_minutes: int | None = Field(default=None, ge=1, le=60)
    role_matching: bool | None = None
    modes: list[str] | None = None  # Noneでサイト設定に従う
    topic: str = Field(default="", max_length=200)  # 話題カード(空=なし)


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
    feature_mute: bool = True
    feature_camera_toggle: bool = True
    feature_screenshare: bool = True
    feature_chat: bool = True
    role_swap_enabled: bool = True
    lobby_topic_mode: str = Field(default="none", pattern=r"^(none|random|fixed)$")
    lobby_topic_text: str = Field(default="", max_length=200)
    topic_pool: str = Field(default="", max_length=4000)
    rematch_priority: bool = True
    survey_questions: str = Field(default="", max_length=2000)


class BulkUsersIn(BaseModel):
    csv: str = Field(min_length=1, max_length=20000)  # 1行=「ユーザー名,初期パスワード」


class SiteIn(BaseModel):
    slug: str = Field(min_length=2, max_length=30)
    name: str = Field(min_length=1, max_length=100)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=128)
    email: str | None = Field(default=None, max_length=120)  # 任意。設定すると案内メールを送る


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
    if not user.is_active:
        raise HTTPException(status_code=403, detail="このアカウントは無効化されています")
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
        display_name=user.display_name or user.username,
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
    if not user.is_active:
        raise HTTPException(status_code=403, detail="このアカウントは無効化されています。管理者にお問い合わせください")
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
        "display_name": user.display_name or "",
        "role": user.role,
        "email": user.email or "",
        "created_at": user.created_at.isoformat(),
        "site": site.slug if site else "",
        "site_name": site.name if site else "",
        "must_change_password": user.must_change_password,
    }


@app.put("/api/me")
def update_me(
    body: MeUpdateIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """自分の設定変更(メールアドレス・表示名)。送られてきた項目のみ更新する"""
    if "email" in body.model_fields_set:
        user.email = (body.email or "").strip() or None
    if "display_name" in body.model_fields_set:
        user.display_name = (body.display_name or "").strip() or None
    db.commit()
    return {
        "ok": True,
        "email": user.email or "",
        "display_name": user.display_name or "",
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
        "role_swap_enabled": get_setting(db, sid, "role_swap_enabled") == "true",
        "features": {
            "mute": get_setting(db, sid, "feature_mute") == "true",
            "camera_toggle": get_setting(db, sid, "feature_camera_toggle") == "true",
            "screenshare": get_setting(db, sid, "feature_screenshare") == "true",
            "chat": get_setting(db, sid, "feature_chat") == "true",
        },
        "survey_questions": [
            q.strip() for q in get_setting(db, sid, "survey_questions").splitlines() if q.strip()
        ] or [get_setting(db, sid, "survey_question") or DEFAULT_SETTINGS["survey_question"]],
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
    # 複数設問の回答は設問文とセットでJSON保存する(設問は後から変更されうるため)
    answers_json = ""
    if body.answers:
        questions = [
            q.strip() for q in get_setting(db, user.site_id, "survey_questions").splitlines() if q.strip()
        ] or [get_setting(db, user.site_id, "survey_question") or DEFAULT_SETTINGS["survey_question"]]
        pairs = [
            {"question": q, "rating": max(1, min(5, r))}
            for q, r in zip(questions, body.answers)
        ]
        answers_json = json.dumps(pairs, ensure_ascii=False)
    survey = Survey(
        user_id=user.id,
        room_id=body.room_id,
        rating=body.rating,
        talk_again=body.talk_again,
        comment=body.comment,
        answers=answers_json,
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


# --- 通報・ブロック・警告(トラスト&セーフティ) -----------------------------------


def _call_partner(db: Session, user: User, call_id: str) -> User:
    """通話IDから自分の相手を特定する(参加者本人のみ)"""
    cp = db.query(CallPair).filter(CallPair.call_id == call_id).first()
    if cp is None or user.id not in (cp.user_a, cp.user_b):
        raise HTTPException(status_code=404, detail="通話の記録が見つかりません")
    partner = db.get(User, cp.user_b if cp.user_a == user.id else cp.user_a)
    if partner is None:
        raise HTTPException(status_code=404, detail="相手のユーザーが見つかりません")
    return partner


def _led_team_member_ids(db: Session, user: User) -> set[int]:
    """自分がリーダーを務めるチームの全メンバーのユーザーID"""
    lead_ids = [
        m.team_id
        for m in db.query(TeamMember).filter(
            TeamMember.user_id == user.id, TeamMember.is_leader.is_(True)
        )
    ]
    if not lead_ids:
        return set()
    return {
        m.user_id
        for m in db.query(TeamMember).filter(TeamMember.team_id.in_(lead_ids))
    }


@app.post("/api/reports", status_code=201)
def create_report(
    body: ReportIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """通話相手を通報する(ユーザー情報と紐づけて保存)"""
    partner = _call_partner(db, user, body.call_id)
    db.add(
        Report(
            site_id=user.site_id,
            reporter_id=user.id,
            reported_id=partner.id,
            call_id=body.call_id,
            reason=body.reason,
        )
    )
    db.commit()
    return {"ok": True}


@app.get("/api/reports")
def list_reports(
    team_id: int | None = None,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """通報一覧。サイト管理者は全件、チームリーダーは自チームのメンバーが対象の件のみ"""
    q = db.query(Report).filter(Report.site_id == user.site_id)
    if _is_site_admin(user):
        if team_id:
            _team_or_404(db, user, team_id)
            ids = {m.user_id for m in db.query(TeamMember).filter(TeamMember.team_id == team_id)}
            q = q.filter(Report.reported_id.in_(ids))
    else:
        led_ids = _led_team_member_ids(db, user)
        if not led_ids:
            raise HTTPException(status_code=403, detail="通報の閲覧権限がありません")
        if team_id:
            team = _team_or_404(db, user, team_id)
            m = _membership(db, team.id, user.id)
            if m is None or not m.is_leader:
                raise HTTPException(status_code=403, detail="このチームのリーダーではありません")
            ids = {x.user_id for x in db.query(TeamMember).filter(TeamMember.team_id == team_id)}
            q = q.filter(Report.reported_id.in_(ids))
        else:
            q = q.filter(Report.reported_id.in_(led_ids))
    rows = q.order_by(Report.id.desc()).limit(100).all()
    user_ids = {r.reporter_id for r in rows} | {r.reported_id for r in rows}
    names = {
        u.id: u.display_name or u.username
        for u in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    return [
        {
            "id": r.id,
            "reporter": names.get(r.reporter_id, "(削除済み)"),
            "reported": names.get(r.reported_id, "(削除済み)"),
            "reported_id": r.reported_id,
            "reason": r.reason,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.put("/api/reports/{report_id}")
def update_report(
    report_id: int,
    body: ReportStatusIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if report is None or report.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="通報が存在しません")
    report.status = body.status
    audit(db, admin, "report_resolve", f"通報#{report.id} → {body.status}")
    db.commit()
    return {"ok": True}


@app.post("/api/blocks", status_code=201)
def create_block(
    body: BlockIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """通話相手をブロックする(以後マッチングされない)"""
    partner = _call_partner(db, user, body.call_id)
    exists = (
        db.query(Block)
        .filter(Block.user_id == user.id, Block.blocked_id == partner.id)
        .first()
    )
    if not exists:
        db.add(Block(user_id=user.id, blocked_id=partner.id))
        db.commit()
    return {"ok": True}


@app.post("/api/warnings", status_code=201)
def create_warning(
    body: WarningIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """警告文を発令する(サイト管理者と、対象が所属するチームのリーダーのみ)"""
    target = db.get(User, body.user_id)
    if target is None or target.site_id != user.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if not (_is_site_admin(user) or target.id in _led_team_member_ids(db, user)):
        raise HTTPException(status_code=403, detail="警告を発令する権限がありません")
    db.add(
        Warning(
            site_id=user.site_id,
            user_id=target.id,
            issuer_name=user.username,
            message=body.message,
        )
    )
    audit(db, user, "warning_issue", target.username)
    db.commit()
    return {"ok": True}


@app.get("/api/warnings/pending")
def my_pending_warnings(user: User = Depends(active_user), db: Session = Depends(get_db)):
    """自分宛ての未確認警告(ログイン時にポップアップ表示する)"""
    rows = (
        db.query(Warning)
        .filter(Warning.user_id == user.id, Warning.acknowledged.is_(False))
        .order_by(Warning.id)
        .all()
    )
    return [
        {"id": w.id, "message": w.message, "created_at": w.created_at.isoformat()}
        for w in rows
    ]


@app.post("/api/warnings/{warning_id}/ack")
def ack_warning(
    warning_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    w = db.get(Warning, warning_id)
    if w is None or w.user_id != user.id:
        raise HTTPException(status_code=404, detail="警告が存在しません")
    w.acknowledged = True
    db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset_password")
def reset_user_password(
    user_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """新しい初期パスワードを発行する(サイト管理者・モデレータ)。
    対象は初回ログイン時に変更を強制される"""
    if not _is_moderator(user):
        raise HTTPException(status_code=403, detail="パスワードリセットの権限がありません")
    target = db.get(User, user_id)
    if target is None or target.site_id != user.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if target.role in ("site_admin", "system_admin") and target.id != user.id:
        raise HTTPException(status_code=400, detail="管理者のパスワードはリセットできません")
    new_password = secrets.token_urlsafe(6)
    target.password_hash = hash_password(new_password)
    target.must_change_password = True
    audit(db, user, "password_reset", target.username)
    mailed = send_mail(
        target.email,
        "パスワードリセットのお知らせ",
        f"{target.username} さん\n\n新しい初期パスワード: {new_password}\n"
        "ログイン後にパスワードの変更が必要です。",
    )
    db.commit()
    return {"ok": True, "password": new_password, "mailed": mailed}


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
            "description": t.description,
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
        db.query(TeamMember, User.username, User.display_name)
        .join(User, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team_id)
        .order_by(TeamMember.id)
        .all()
    )
    return {
        "team": team.name,
        "members": [
            {
                "user_id": m.user_id,
                "username": name,
                "display_name": dn or name,
                "is_leader": m.is_leader,
            }
            for m, name, dn in rows
        ],
    }


@app.get("/api/teams/{team_id}")
def team_detail(
    team_id: int,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """チーム画面用の詳細(メンバー・統計込み)。メンバーとサイト管理者のみ"""
    team = _require_team_member(db, user, team_id)
    rows = (
        db.query(TeamMember, User.username, User.display_name)
        .join(User, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team_id)
        .order_by(TeamMember.id)
        .all()
    )
    member_ids = [m.user_id for m, _, _ in rows]
    sessions = (
        db.query(Survey).filter(Survey.user_id.in_(member_ids)).count() if member_ids else 0
    )
    my_membership = _membership(db, team_id, user.id)
    return {
        "id": team.id,
        "name": team.name,
        "description": team.description,
        "is_leader": bool(my_membership and my_membership.is_leader) or _is_site_admin(user),
        "my_user_id": user.id,
        "members": [
            {
                "user_id": m.user_id,
                "username": name,
                "display_name": dn or name,
                "is_leader": m.is_leader,
            }
            for m, name, dn in rows
        ],
        "stats": {"members": len(member_ids), "sessions": sessions},
    }


@app.put("/api/teams/{team_id}")
def team_update(
    team_id: int,
    body: TeamIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """チーム設定(名前・説明)の変更。チームリーダーとサイト管理者のみ"""
    team = _require_team_leader(db, user, team_id)
    team.name = body.name
    team.description = body.description.strip()
    audit(db, user, "team_update", body.name)
    db.commit()
    return {"ok": True}


@app.put("/api/teams/{team_id}/members/{user_id}/leader")
def team_set_leader(
    team_id: int,
    user_id: int,
    body: LeaderIn,
    user: User = Depends(active_user),
    db: Session = Depends(get_db),
):
    """リーダーの任命/解除。リーダーは他メンバーを共同リーダーにできる(複数人可)。
    自分自身のリーダー権限はサイト管理者のみ変更できる"""
    _require_team_leader(db, user, team_id)
    if user_id == user.id and not _is_site_admin(user):
        raise HTTPException(status_code=400, detail="自分のリーダー権限は変更できません")
    m = _membership(db, team_id, user_id)
    if m is None:
        raise HTTPException(status_code=404, detail="チームのメンバーではありません")
    m.is_leader = body.is_leader
    audit(db, user, "team_leader_set", f"user_id={user_id} → {body.is_leader}")
    db.commit()
    return {"ok": True}


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
    """メンバーの削除。自分自身なら誰でも脱退できる(最後のリーダーを除く)"""
    if user_id == user.id:
        # 脱退(セルフ削除)
        _team_or_404(db, user, team_id)
        m = _membership(db, team_id, user.id)
        if m is None:
            raise HTTPException(status_code=404, detail="チームのメンバーではありません")
        if m.is_leader:
            others = (
                db.query(TeamMember)
                .filter(
                    TeamMember.team_id == team_id,
                    TeamMember.is_leader.is_(True),
                    TeamMember.user_id != user.id,
                )
                .count()
            )
            if others == 0:
                raise HTTPException(
                    status_code=400, detail="他のリーダーを任命してから脱退してください"
                )
        db.delete(m)
        db.commit()
        return {"ok": True}
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
        "creator": (creator.display_name or creator.username) if creator else "?",
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
        "topic": room.topic,
        "raw": {
            "passphrase": room.passphrase,
            "session_minutes": room.session_minutes,
            "role_matching": room.role_matching,
            "modes": room.modes.split(",") if room.modes else None,
            "topic": room.topic,
        } if can_manage else None,
        "managers": [
            {"user_id": m.user_id, "username": name, "display_name": dn or name}
            for m, name, dn in db.query(RoomManager, User.username, User.display_name)
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
    room.topic = body.topic.strip()


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
        db.query(Event, User.username, User.display_name)
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
    room_ids = {e.room_id for e, _, _ in rows if e.room_id}
    room_names = {
        r.id: r.name
        for r in db.query(Room).filter(Room.id.in_(room_ids)).all()
    } if room_ids else {}
    return [
        {
            "id": e.id,
            "title": e.title,
            "date": e.date,
            "username": dn or name,
            "room_id": e.room_id,
            "room_name": room_names.get(e.room_id),
        }
        for e, name, dn in rows
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
        db.query(Post, User.username, User.display_name)
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
            "username": dn or name,
            "created_at": p.created_at.isoformat(),
        }
        for p, name, dn in rows
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
            "display_name": u.display_name or "",
            "role": u.role,
            "is_active": u.is_active,
            "email": u.email or "",
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
        email=(body.email or "").strip() or None,
        must_change_password=True,
    )
    db.add(user)
    audit(db, admin, "user_create", body.username)
    # メールアドレスがあれば案内メール(招待)を送る
    mailed = send_mail(
        user.email,
        "アカウントのご案内",
        f"{user.username} さん\n\nアカウントが作成されました。\n"
        f"ユーザー名: {user.username}\n初期パスワード: {body.password}\n"
        "初回ログイン時にパスワードの変更が必要です。",
    )
    db.commit()
    return {"ok": True, "id": user.id, "mailed": mailed}


@app.post("/api/admin/users/bulk")
def admin_bulk_users(
    body: BulkUsersIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """CSVでユーザーを一括登録する。1行=「ユーザー名,初期パスワード,メール(任意)」。
    パスワード省略時は自動生成し、レスポンスで一度だけ返す。メールがあれば案内を送る"""
    results = []
    created = 0
    for line in body.csv.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0]
        given_pw = parts[1] if len(parts) > 1 and parts[1] else ""
        email = parts[2] if len(parts) > 2 and parts[2] else None
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
                email=email,
                must_change_password=True,
            )
        )
        created += 1
        mailed = send_mail(
            email,
            "アカウントのご案内",
            f"{name} さん\n\nアカウントが作成されました。\n"
            f"ユーザー名: {name}\n初期パスワード: {password}\n"
            "初回ログイン時にパスワードの変更が必要です。",
        )
        results.append({
            "username": name,
            "password": password if not given_pw else None,
            "status": "ok" + ("(メール送信済み)" if mailed else ""),
        })
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


@app.get("/api/admin/report/export")
def admin_report_export(
    admin: User = Depends(site_admin_user), db: Session = Depends(get_db)
):
    """セッション(アンケート)データのCSVエクスポート"""
    from fastapi.responses import Response

    rows = (
        db.query(Survey, User.username)
        .join(User, Survey.user_id == User.id)
        .filter(User.site_id == admin.site_id)
        .order_by(Survey.id)
        .all()
    )

    def esc(v) -> str:
        s = str(v if v is not None else "")
        return '"' + s.replace('"', '""') + '"'

    lines = ["日時,ユーザー名,評価,また話したい,コメント,設問別回答"]
    for s, name in rows:
        answers = ""
        if s.answers:
            try:
                answers = " / ".join(
                    f"{a['question']}: {a['rating']}" for a in json.loads(s.answers)
                )
            except Exception:
                answers = ""
        lines.append(",".join([
            esc(s.created_at.isoformat()), esc(name), esc(s.rating),
            esc("はい" if s.talk_again else "いいえ"), esc(s.comment), esc(answers),
        ]))
    csv_data = "﻿" + "\n".join(lines)  # BOM付きでExcelの文字化けを防ぐ
    audit(db, admin, "report_export", f"{len(rows)}件")
    db.commit()
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sessions.csv"},
    )


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
    """ロール変更(一般・モデレータ・サイト管理者)。サイト管理者のみ"""
    target = db.get(User, user_id)
    if target is None or target.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if target.role == "system_admin":
        raise HTTPException(status_code=400, detail="システム管理者のロールは変更できません")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="自分のロールは変更できません")
    target.role = body.role
    audit(db, admin, "role_change", f"{target.username} → {body.role}")
    db.commit()
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/active")
def admin_set_active(
    user_id: int,
    body: ActiveIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """ユーザーの有効化/無効化。無効化中はログイン・API利用ができない"""
    target = db.get(User, user_id)
    if target is None or target.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    if target.role in ("site_admin", "system_admin"):
        raise HTTPException(status_code=400, detail="管理者は無効化できません")
    target.is_active = body.is_active
    audit(db, admin, "user_enable" if body.is_active else "user_disable", target.username)
    db.commit()
    return {"ok": True}


def _apply_user_edit(db: Session, actor: User, target: User, body: UserEditIn) -> None:
    """ユーザー編集の共通処理(表示名・ロール・有効/無効)。commitは呼び出し側"""
    fields = body.model_fields_set
    if "display_name" in fields:
        new_name = (body.display_name or "").strip() or None
        if new_name != target.display_name:
            target.display_name = new_name
            audit(db, actor, "user_update", f"{target.username} 表示名 → {new_name or '(ユーザー名に戻す)'}")
    if "role" in fields and body.role is not None and body.role != target.role:
        if target.role == "system_admin":
            raise HTTPException(status_code=400, detail="システム管理者のロールは変更できません")
        if target.id == actor.id:
            raise HTTPException(status_code=400, detail="自分のロールは変更できません")
        target.role = body.role
        audit(db, actor, "role_change", f"{target.username} → {body.role}")
    if "is_active" in fields and body.is_active is not None and body.is_active != target.is_active:
        if target.role in ("site_admin", "system_admin"):
            raise HTTPException(status_code=400, detail="管理者は無効化できません")
        target.is_active = body.is_active
        audit(db, actor, "user_enable" if body.is_active else "user_disable", target.username)


@app.put("/api/admin/users/{user_id}")
def admin_edit_user(
    user_id: int,
    body: UserEditIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    """ユーザー編集(表示名・ロール・有効/無効をまとめて変更)。サイト管理者のみ"""
    target = db.get(User, user_id)
    if target is None or target.site_id != admin.site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    _apply_user_edit(db, admin, target, body)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/users/export")
def admin_users_export(
    admin: User = Depends(site_admin_user), db: Session = Depends(get_db)
):
    """ユーザー一覧のCSVエクスポート"""
    from fastapi.responses import Response

    def esc(v) -> str:
        s = str(v if v is not None else "")
        return '"' + s.replace('"', '""') + '"'

    lines = ["ID,ユーザー名,表示名,権限,状態,メール,登録日,セッション数"]
    role_names = {
        "system_admin": "システム管理者", "site_admin": "サイト管理者",
        "moderator": "モデレータ", "user": "一般",
    }
    for u in _user_rows(db, admin.site_id):
        lines.append(",".join([
            esc(u["id"]), esc(u["username"]), esc(u["display_name"]),
            esc(role_names.get(u["role"], u["role"])),
            esc("有効" if u["is_active"] else "無効"), esc(u["email"]),
            esc(u["created_at"]), esc(u["session_count"]),
        ]))
    csv_data = "﻿" + "\n".join(lines)  # BOM付きでExcelの文字化けを防ぐ
    audit(db, admin, "users_export", f"{len(lines) - 1}件")
    db.commit()
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


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
        "username": target.display_name or target.username,
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
        "feature_mute": flag("feature_mute"),
        "feature_camera_toggle": flag("feature_camera_toggle"),
        "feature_screenshare": flag("feature_screenshare"),
        "feature_chat": flag("feature_chat"),
        "role_swap_enabled": flag("role_swap_enabled"),
        "lobby_topic_mode": get_setting(db, site_id, "lobby_topic_mode"),
        "lobby_topic_text": get_setting(db, site_id, "lobby_topic_text"),
        "topic_pool": get_setting(db, site_id, "topic_pool"),
        "rematch_priority": flag("rematch_priority"),
        "survey_questions": get_setting(db, site_id, "survey_questions"),
    }


@app.get("/api/admin/settings")
def admin_get_settings(admin: User = Depends(site_admin_user), db: Session = Depends(get_db)):
    return _settings_payload(db, admin.site_id)


def _apply_settings(db: Session, sid: int, body: SettingsIn) -> None:
    """サイト設定の保存処理(管理画面・システム管理画面で共用)。commitは呼び出し側"""
    if not (body.mode_toon or body.mode_real or body.mode_still or body.mode_camera):
        raise HTTPException(status_code=422, detail="表示モードは少なくとも1つ有効にしてください")
    set_setting(db, sid, "session_minutes", str(body.session_minutes))
    for key in (
        "allow_registration", "role_matching", "anonymous_mode", "survey_enabled",
        "mode_toon", "mode_real", "mode_still", "mode_camera", "rooms_enabled",
        "feature_mute", "feature_camera_toggle", "feature_screenshare", "feature_chat",
        "role_swap_enabled", "rematch_priority",
    ):
        set_setting(db, sid, key, "true" if getattr(body, key) else "false")
    set_setting(db, sid, "survey_question",
                body.survey_question.strip() or DEFAULT_SETTINGS["survey_question"])
    set_setting(db, sid, "tagline", body.tagline.strip() or DEFAULT_SETTINGS["tagline"])
    set_setting(db, sid, "lobby_topic_mode", body.lobby_topic_mode)
    set_setting(db, sid, "lobby_topic_text", body.lobby_topic_text.strip())
    set_setting(db, sid, "topic_pool", body.topic_pool.strip())
    set_setting(db, sid, "survey_questions", body.survey_questions.strip())
    if body.site_name.strip():
        site = db.get(Site, sid)
        if site:
            site.name = body.site_name.strip()


@app.put("/api/admin/settings")
def admin_put_settings(
    body: SettingsIn,
    admin: User = Depends(site_admin_user),
    db: Session = Depends(get_db),
):
    _apply_settings(db, admin.site_id, body)
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
    for m, name, dn in (
        db.query(TeamMember, User.username, User.display_name)
        .join(User, TeamMember.user_id == User.id)
        .filter(TeamMember.is_leader.is_(True))
        .all()
    ):
        leaders.setdefault(m.team_id, []).append(dn or name)
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
    team = Team(site_id=admin.site_id, name=body.name, description=body.description.strip())
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
    _delete_site_data(db, site)
    audit(db, admin, "site_delete", site.slug)
    db.commit()
    return {"ok": True}


def _delete_site_data(db: Session, site: Site) -> None:
    """サイトと所属ユーザー・関連データをすべて削除する(commitは呼び出し側)"""
    from sqlalchemy import or_

    site_id = site.id
    user_ids = [u.id for u in db.query(User).filter(User.site_id == site_id).all()]
    if user_ids:
        db.query(Survey).filter(Survey.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Event).filter(Event.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Post).filter(Post.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(TeamMember).filter(TeamMember.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Block).filter(
            or_(Block.user_id.in_(user_ids), Block.blocked_id.in_(user_ids))
        ).delete(synchronize_session=False)
        db.query(RoomManager).filter(RoomManager.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.query(CallPair).filter(CallPair.site_id == site_id).delete(synchronize_session=False)
    db.query(Report).filter(Report.site_id == site_id).delete(synchronize_session=False)
    db.query(Warning).filter(Warning.site_id == site_id).delete(synchronize_session=False)
    db.query(Room).filter(Room.site_id == site_id).delete(synchronize_session=False)
    db.query(Team).filter(Team.site_id == site_id).delete(synchronize_session=False)
    db.query(Announcement).filter(Announcement.site_id == site_id).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.site_id == site_id).delete(synchronize_session=False)
    db.query(Setting).filter(Setting.site_id == site_id).delete(synchronize_session=False)
    db.delete(site)


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


@app.get("/api/sysadmin/sites-export")
def sysadmin_sites_export(
    admin: User = Depends(system_admin_user), db: Session = Depends(get_db)
):
    """サイト一覧のCSVエクスポート"""
    from sqlalchemy import func

    from fastapi.responses import Response

    def esc(v) -> str:
        s = str(v if v is not None else "")
        return '"' + s.replace('"', '""') + '"'

    counts = dict(db.query(User.site_id, func.count(User.id)).group_by(User.site_id).all())
    lines = ["ID,サイトID,サイト名,種別,ユーザー数,作成日"]
    for s in db.query(Site).order_by(Site.id).all():
        lines.append(",".join([
            esc(s.id), esc(s.slug), esc(s.name),
            esc("メイン" if s.is_main else "サブ"),
            esc(counts.get(s.id, 0)), esc(s.created_at.isoformat()),
        ]))
    csv_data = "﻿" + "\n".join(lines)  # BOM付きでExcelの文字化けを防ぐ
    audit(db, admin, "sites_export", f"{len(lines) - 1}件")
    db.commit()
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sites.csv"},
    )


@app.put("/api/sysadmin/sites/{site_id}")
def sysadmin_edit_site(
    site_id: int,
    body: SiteEditIn,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    """サイトの表示名を変更する(サイトID=slugは内部の紐付けに使うため変更不可)"""
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="サイトが存在しません")
    site.name = body.name.strip()
    audit(db, admin, "site_update", f"{site.slug} 名前 → {site.name}")
    db.commit()
    return {"ok": True, "id": site.id, "name": site.name}


@app.put("/api/sysadmin/sites/{site_id}/users/{user_id}")
def sysadmin_edit_user(
    site_id: int,
    user_id: int,
    body: UserEditIn,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    """任意サイトのユーザー編集(表示名・ロール・有効/無効)"""
    if db.get(Site, site_id) is None:
        raise HTTPException(status_code=404, detail="サイトが存在しません")
    target = db.get(User, user_id)
    if target is None or target.site_id != site_id:
        raise HTTPException(status_code=404, detail="ユーザーが存在しません")
    _apply_user_edit(db, admin, target, body)
    db.commit()
    return {"ok": True}


@app.put("/api/sysadmin/sites/{site_id}/settings")
def sysadmin_put_site_settings(
    site_id: int,
    body: SettingsIn,
    admin: User = Depends(system_admin_user),
    db: Session = Depends(get_db),
):
    """任意サイトの設定変更(管理画面のサイト設定と同じ項目)"""
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="サイトが存在しません")
    _apply_settings(db, site_id, body)
    audit(db, admin, "settings_update", f"サイト設定を変更({site.slug})")
    db.commit()
    return {"ok": True}


# --- デモデータ(本番運用では削除する → docs/TODO.md) ------------------------------

DEMO_USER_PREFIX = "デモ_"  # 既存ユーザーと衝突しない識別子(削除時の目印にもなる)
DEMO_SITE_SLUG = "demo-corp"  # デモ用サブサイトのサイトID
DEMO_SUB_NAMES = ["こうじ", "みさき", "しゅん", "なな", "だいち", "りん"]
DEMO_NAMES = [
    "さくら", "たろう", "ひかり", "けんた", "ゆい", "そうた",
    "あおい", "りく", "めい", "はると", "ことは", "ゆうま",
]
DEMO_COMMENTS = [
    "相手の話を最後まで聴けた気がします",
    "途中で口を挟んでしまった。次は最後まで聴きたい",
    "あいづちを意識したら会話が続いた",
    "沈黙が怖くなくなってきた",
    "質問のバリエーションを増やしたい",
    "相手の表情をよく見て話せた",
    "",
]
DEMO_POSTS = [
    "今日の対話、とても勉強になりました!",
    "「聴く」って意外と難しいですね…",
    "あいづちのコツ、誰か教えてください",
    "10分があっという間でした",
    "役割交代してみると、話し手の気持ちがよく分かります",
    "今週3回目のセッション完了!",
]
DEMO_EVENTS = ["みんなで傾聴会", "ふりかえり共有会", "対話のコツ勉強会", "新メンバー歓迎会"]


@app.post("/api/sysadmin/demo-data", status_code=201)
def create_demo_data(
    admin: User = Depends(system_admin_user), db: Session = Depends(get_db)
):
    """デモ用のユーザー・チーム・履歴などを一括生成する(管理者のサイトに作成)"""
    from datetime import timedelta

    sid = admin.site_id
    if (
        db.query(User)
        .filter(User.site_id == sid, User.username.startswith(DEMO_USER_PREFIX, autoescape=True))
        .first()
        or db.query(Site).filter(Site.slug == DEMO_SITE_SLUG).first()
    ):
        raise HTTPException(status_code=409, detail="デモデータは既に存在します。先に削除してください")

    now = utcnow().replace(tzinfo=None)

    # ユーザー(先頭2人はモデレータ)。パスワードは全員 demo1234
    demo_users: list[User] = []
    pw_hash = hash_password("demo1234")
    for i, name in enumerate(DEMO_NAMES):
        u = User(
            site_id=sid,
            username=f"{DEMO_USER_PREFIX}{name}",
            password_hash=pw_hash,
            role="moderator" if i < 2 else "user",
            created_at=now - timedelta(days=random.randint(3, 30)),
        )
        db.add(u)
        demo_users.append(u)
    db.flush()

    # チーム(先頭メンバーがリーダー)
    team_defs = [
        ("デモ_営業チーム", demo_users[0:5]),
        ("デモ_開発チーム", demo_users[4:9]),
        ("デモ_人事チーム", demo_users[9:12]),
    ]
    teams: list[Team] = []
    for tname, members in team_defs:
        team = Team(site_id=sid, name=tname)
        db.add(team)
        db.flush()
        for j, m in enumerate(members):
            db.add(TeamMember(team_id=team.id, user_id=m.id, is_leader=(j == 0)))
        teams.append(team)

    # 通話履歴(直近14日に分散。両者分のアンケートつき)
    session_count = 30
    for _ in range(session_count):
        a, b = random.sample(demo_users, 2)
        when = now - timedelta(days=random.randint(0, 13), hours=random.randint(0, 12))
        call_id = "demo" + secrets.token_hex(14)
        db.add(CallPair(call_id=call_id, site_id=sid, user_a=a.id, user_b=b.id, created_at=when))
        for u in (a, b):
            db.add(
                Survey(
                    user_id=u.id,
                    room_id=call_id,
                    rating=random.randint(3, 5),
                    talk_again=random.random() < 0.5,
                    comment=random.choice(DEMO_COMMENTS),
                    created_at=when,
                )
            )

    # 掲示板(サイト全体+チーム限定)
    for body_text in DEMO_POSTS:
        db.add(
            Post(
                user_id=random.choice(demo_users).id,
                body=body_text,
                created_at=now - timedelta(days=random.randint(0, 10), hours=random.randint(0, 12)),
            )
        )
    db.add(Post(user_id=demo_users[0].id, team_id=teams[0].id, body="(チーム限定)今月の目標は「最後まで聴く」です"))

    # イベント(今月の予定)
    for i, title in enumerate(DEMO_EVENTS):
        date = (now + timedelta(days=2 + i * 5)).date().isoformat()
        db.add(Event(user_id=random.choice(demo_users).id, title=title, date=date))

    # ルームとお知らせ
    db.add(
        Room(
            site_id=sid,
            creator_id=demo_users[0].id,
            name="デモ_雑談ルーム",
            topic="最近うれしかったこと",
        )
    )
    db.add(
        Announcement(
            site_id=sid,
            title="【デモ】サンプルのお知らせ",
            body="これはデモデータです。システム管理画面の「デモデータを削除」でまとめて削除できます。",
        )
    )

    # --- デモ用サブサイト(企業向けの見本) ---
    sub_site = Site(slug=DEMO_SITE_SLUG, name="デモ株式会社")
    db.add(sub_site)
    db.flush()
    sub_admin, _ = create_site_admin(db, sub_site)
    sub_admin.must_change_password = False  # デモなのでそのままログイン可能にする
    # 社内ツールらしい設定(実名表示・実映像あり)
    set_setting(db, sub_site.id, "anonymous_mode", "false")
    set_setting(db, sub_site.id, "mode_camera", "true")
    sub_users: list[User] = []
    for i, name in enumerate(DEMO_SUB_NAMES):
        u = User(
            site_id=sub_site.id,
            username=f"{DEMO_USER_PREFIX}{name}",
            password_hash=pw_hash,
            role="moderator" if i == 0 else "user",
            created_at=now - timedelta(days=random.randint(3, 20)),
        )
        db.add(u)
        sub_users.append(u)
    db.flush()
    sub_team = Team(site_id=sub_site.id, name="デモ_総務チーム")
    db.add(sub_team)
    db.flush()
    for j, m in enumerate(sub_users[:4]):
        db.add(TeamMember(team_id=sub_team.id, user_id=m.id, is_leader=(j == 0)))
    sub_sessions = 10
    for _ in range(sub_sessions):
        a, b = random.sample(sub_users, 2)
        when = now - timedelta(days=random.randint(0, 13), hours=random.randint(0, 12))
        call_id = "demo" + secrets.token_hex(14)
        db.add(CallPair(call_id=call_id, site_id=sub_site.id, user_a=a.id, user_b=b.id, created_at=when))
        for u in (a, b):
            db.add(
                Survey(
                    user_id=u.id, room_id=call_id, rating=random.randint(3, 5),
                    talk_again=random.random() < 0.5,
                    comment=random.choice(DEMO_COMMENTS), created_at=when,
                )
            )
    db.add(Post(user_id=sub_users[0].id, body="社内の1on1代わりに使ってみています"))
    db.add(Event(user_id=sub_users[0].id, title="部署横断 雑談会",
                 date=(now + timedelta(days=4)).date().isoformat()))
    db.add(
        Announcement(
            site_id=sub_site.id,
            title="【デモ】デモ株式会社のサイトです",
            body="サブサイトのデモです。実名表示・実映像ありの社内向け設定になっています。",
        )
    )

    audit(db, admin, "demo_create",
          f"ユーザー{len(demo_users)}件・セッション{session_count}件・サブサイト{DEMO_SITE_SLUG}")
    db.commit()
    return {
        "ok": True,
        "users": len(demo_users),
        "teams": len(teams),
        "sessions": session_count,
        "posts": len(DEMO_POSTS) + 1,
        "events": len(DEMO_EVENTS),
        "rooms": 1,
        "subsite": DEMO_SITE_SLUG,
        "subsite_users": len(sub_users) + 1,  # サイト管理者を含む
        "subsite_sessions": sub_sessions,
    }


@app.delete("/api/sysadmin/demo-data")
def delete_demo_data(
    admin: User = Depends(system_admin_user), db: Session = Depends(get_db)
):
    """生成したデモデータをまとめて削除する"""
    from sqlalchemy import or_

    sid = admin.site_id
    demo_users = (
        db.query(User)
        .filter(User.site_id == sid, User.username.startswith(DEMO_USER_PREFIX, autoescape=True))
        .all()
    )
    user_ids = [u.id for u in demo_users]
    if user_ids:
        db.query(Survey).filter(Survey.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(CallPair).filter(
            or_(CallPair.user_a.in_(user_ids), CallPair.user_b.in_(user_ids))
        ).delete(synchronize_session=False)
        db.query(Post).filter(Post.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Event).filter(Event.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(TeamMember).filter(TeamMember.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Block).filter(
            or_(Block.user_id.in_(user_ids), Block.blocked_id.in_(user_ids))
        ).delete(synchronize_session=False)
        db.query(Warning).filter(Warning.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Report).filter(
            or_(Report.reporter_id.in_(user_ids), Report.reported_id.in_(user_ids))
        ).delete(synchronize_session=False)
        db.query(RoomManager).filter(RoomManager.user_id.in_(user_ids)).delete(synchronize_session=False)

    # デモのチーム・ルーム・お知らせ(名前のプレフィックスで判定)
    demo_teams = db.query(Team).filter(
        Team.site_id == sid, Team.name.startswith("デモ_", autoescape=True)
    ).all()
    for team in demo_teams:
        db.query(TeamMember).filter(TeamMember.team_id == team.id).delete(synchronize_session=False)
        db.query(Post).filter(Post.team_id == team.id).delete(synchronize_session=False)
        db.query(Event).filter(Event.team_id == team.id).delete(synchronize_session=False)
        db.delete(team)
    demo_rooms = db.query(Room).filter(
        Room.site_id == sid, Room.name.startswith("デモ_", autoescape=True)
    ).all()
    for room in demo_rooms:
        db.query(RoomManager).filter(RoomManager.room_id == room.id).delete(synchronize_session=False)
        db.query(Event).filter(Event.room_id == room.id).update({"room_id": None})
        db.delete(room)
    db.query(Announcement).filter(
        Announcement.site_id == sid, Announcement.title.startswith("【デモ】", autoescape=True)
    ).delete(synchronize_session=False)
    if user_ids:
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)

    # デモ用サブサイトを丸ごと削除
    sub_site = db.query(Site).filter(Site.slug == DEMO_SITE_SLUG).first()
    if sub_site:
        _delete_site_data(db, sub_site)

    audit(db, admin, "demo_delete", f"ユーザー{len(user_ids)}件ほか")
    db.commit()
    return {
        "ok": True,
        "users": len(user_ids),
        "teams": len(demo_teams),
        "rooms": len(demo_rooms),
        "subsite": DEMO_SITE_SLUG if sub_site else None,
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
    if user is None or not user.is_active:
        await ws.close(code=4001, reason="unknown user")
        return
    if user.must_change_password:
        await ws.close(code=4003, reason="password change required")
        return

    await ws.accept()
    client = Client(user.id, user.display_name or user.username, user.site_id, ws)
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
                    client.base_topic = ""
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
                        else:
                            if room.role_matching is not None:
                                role_matching = room.role_matching  # ルーム設定がサイト設定より優先
                            client.base_topic = room.topic  # ルーム通話の話題カード
                    elif not role_matching:
                        # ロビー通話(役割なし)の話題カードはサイト設定に従う
                        mode = get_setting(sdb, user.site_id, "lobby_topic_mode")
                        if mode == "fixed":
                            client.base_topic = get_setting(sdb, user.site_id, "lobby_topic_text")
                        elif mode == "random":
                            pool = [
                                t.strip()
                                for t in get_setting(sdb, user.site_id, "topic_pool").splitlines()
                                if t.strip()
                            ] or TOPICS
                            client.base_topic = random.choice(pool)
                    # ブロック(相互)と「また話したい」相互一致を読み込む
                    client.blocked = {
                        b.blocked_id for b in sdb.query(Block).filter(Block.user_id == user.id)
                    } | {
                        b.user_id for b in sdb.query(Block).filter(Block.blocked_id == user.id)
                    }
                    client.preferred = set()
                    if get_setting(sdb, user.site_id, "rematch_priority") == "true":
                        my_likes = {
                            s.room_id
                            for s in sdb.query(Survey).filter(
                                Survey.user_id == user.id, Survey.talk_again.is_(True)
                            )
                        }
                        if my_likes:
                            for cp in sdb.query(CallPair).filter(CallPair.call_id.in_(my_likes)):
                                partner_id = cp.user_b if cp.user_a == user.id else cp.user_a
                                mutual = (
                                    sdb.query(Survey)
                                    .filter(
                                        Survey.room_id == cp.call_id,
                                        Survey.user_id == partner_id,
                                        Survey.talk_again.is_(True),
                                    )
                                    .first()
                                )
                                if mutual:
                                    client.preferred.add(partner_id)
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
                await manager.handle_consent(
                    client, bool(msg.get("accept")), str(msg.get("topic") or "")
                )
            elif msg_type == "signal":
                await manager.relay_signal(client, msg.get("data") or {})
            elif msg_type == "chat":
                sdb = SessionLocal()
                try:
                    chat_ok = get_setting(sdb, user.site_id, "feature_chat") == "true"
                finally:
                    sdb.close()
                if chat_ok:
                    await manager.relay_chat(client, str(msg.get("text") or "")[:500])
            elif msg_type == "swap_request":
                sdb = SessionLocal()
                try:
                    swap_ok = get_setting(sdb, user.site_id, "role_swap_enabled") == "true"
                finally:
                    sdb.close()
                if swap_ok:
                    await manager.handle_swap(client)
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
