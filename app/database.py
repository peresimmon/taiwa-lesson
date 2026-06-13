"""データベース定義(デモはSQLite。本番は DATABASE_URL 環境変数でPostgreSQL等に差し替え可能)"""
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./videomatch.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Site(Base):
    """テナント(サイト)。メインサイト+企業ごとのサブサイト"""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(30), unique=True, index=True)  # ログインで使うサイトID
    name: Mapped[str] = mapped_column(String(100))
    is_main: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    # ユーザー名はサイト内で一意(別サイトには同名ユーザーが存在できる)
    __table_args__ = (UniqueConstraint("site_id", "username", name="uq_users_site_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    username: Mapped[str] = mapped_column(String(50), index=True)  # ログインID(変更不可)
    display_name: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 表示名(未設定はusername)
    password_hash: Mapped[str] = mapped_column(String(255))
    # "system_admin" | "site_admin" | "moderator" | "user"
    role: Mapped[str] = mapped_column(String(20), default="user")
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)  # メール連携用(任意)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # False=無効化(ログイン不可)
    # 初期パスワードのアカウントは初回ログイン時に変更を強制する
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Survey(Base):
    __tablename__ = "surveys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True)
    rating: Mapped[int] = mapped_column(Integer)  # 1〜5
    talk_again: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[str] = mapped_column(Text, default="")
    answers: Mapped[str] = mapped_column(Text, default="")  # 複数設問の回答(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Announcement(Base):
    """運営からのお知らせ(サイトごと)"""

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """イベントカレンダーの予定(ユーザーが登録可能。team_id付きはチーム限定)"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"), nullable=True)  # ルーム連携
    title: Mapped[str] = mapped_column(String(200))
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM"(任意)
    # 繰り返し: 同じシリーズの各回は同じseries_idでまとめる(単発はNULL)。
    # recurrenceは表示用ラベル("毎日"/"平日"/"毎週月"/"毎月15日"など)
    series_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    recurrence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EventAttendance(Base):
    """イベントへの参加可否(RSVP)。出欠制ルームのマッチング対象を決める"""

    __tablename__ = "event_attendance"
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_attendance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="yes")  # "yes" | "no"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Post(Base):
    """みんなの掲示板への投稿(team_id付きはチーム限定)"""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Team(Base):
    """サイト内のチーム。スケジュールや掲示板をチーム単位でも使える"""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")  # チームの説明
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TeamMember(Base):
    """チーム所属(複数チーム所属可)。is_leader=チームリーダー"""

    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_members"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    is_leader: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Room(Base):
    """ルーム(通常マッチングとは別の、ルームごとのマッチング場)

    通話設定(セッション時間・役割・表示モード)はサイト設定より優先される。
    NULLの項目はサイト設定に従う。
    """

    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)  # 見える範囲(NULL=全員)
    passphrase: Mapped[str] = mapped_column(String(100), default="")  # 入る際の合言葉(空=無し)
    capacity: Mapped[int] = mapped_column(Integer, default=0)  # 定員(0=無制限)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 存在期間(NULL=無期限)
    session_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role_matching: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    modes: Mapped[str | None] = mapped_column(String(100), nullable=True)  # "toon,real"等のCSV
    topic: Mapped[str] = mapped_column(String(200), default="")  # 話題カード(空=なし)
    # 出欠制マッチング(朝会など): ONだと、その日にこのルームへ紐づくイベントに
    # 「参加(yes)」とRSVPしたメンバーだけが待機列に入れる
    attendance_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RoomManager(Base):
    """ルーム管理者(ルーム設定を変更できるユーザー)"""

    __tablename__ = "room_managers"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_room_managers"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CallPair(Base):
    """通話セッションの参加者ペアの記録(通報・ブロック・再マッチ優先で使う)"""

    __tablename__ = "call_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    user_a: Mapped[int] = mapped_column(Integer, index=True)
    user_b: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    """通話相手の通報。サイト管理者とチームリーダーのみ閲覧可能"""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    reported_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    call_id: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="open")  # "open" | "resolved"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Block(Base):
    """ブロック(この相手とは二度とマッチしない)"""

    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("user_id", "blocked_id", name="uq_blocks"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    blocked_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Warning(Base):
    """特定ユーザーへの警告文。ログイン時にポップアップ表示し、確認で既読になる"""

    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # 警告対象
    issuer_name: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(String(500))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """管理操作の監査ログ(サイトごと)"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    """サイトごとの設定(キー・バリュー)"""

    __tablename__ = "settings"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))


def _migrate() -> None:
    """既存DBに後から追加したカラムを反映する簡易マイグレーション(SQLiteのみ)

    既存DBではusersテーブルにusernameのグローバル一意制約が残るが、
    デモ用途では制約が強い方向のずれなので許容する(新規DBは複合一意)。
    """
    if not DATABASE_URL.startswith("sqlite"):
        return

    def cols(conn, table):
        return [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]

    with engine.connect() as conn:
        user_cols = cols(conn, "users")
        if user_cols:
            if "role" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'"))
            if "site_id" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN site_id INTEGER NOT NULL DEFAULT 0"))
            if "must_change_password" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"))
            # 旧ロール名 "admin" は新体系のシステム管理者へ
            conn.execute(text("UPDATE users SET role='system_admin' WHERE role='admin'"))
            # 旧スキーマのグローバル一意インデックスを、サイト内一意に置き換える
            conn.execute(text("DROP INDEX IF EXISTS ix_users_username"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_username ON users (username)"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_site_username"
                    " ON users (site_id, username)"
                )
            )
        ann_cols = cols(conn, "announcements")
        if ann_cols and "site_id" not in ann_cols:
            conn.execute(text("ALTER TABLE announcements ADD COLUMN site_id INTEGER NOT NULL DEFAULT 0"))
        for table in ("events", "posts"):
            tcols = cols(conn, table)
            if tcols and "team_id" not in tcols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN team_id INTEGER"))
        ev_cols = cols(conn, "events")
        if ev_cols and "room_id" not in ev_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN room_id INTEGER"))
        if ev_cols and "start_time" not in ev_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN start_time VARCHAR(5)"))
        if ev_cols and "series_id" not in ev_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN series_id VARCHAR(32)"))
        if ev_cols and "recurrence" not in ev_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN recurrence VARCHAR(20)"))
        rm2_cols = cols(conn, "rooms")
        if rm2_cols and "attendance_required" not in rm2_cols:
            conn.execute(text("ALTER TABLE rooms ADD COLUMN attendance_required BOOLEAN NOT NULL DEFAULT 0"))
        if user_cols and "email" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(120)"))
        if user_cols and "display_name" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN display_name VARCHAR(50)"))
        if user_cols and "is_active" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"))
        sv_cols = cols(conn, "surveys")
        if sv_cols and "answers" not in sv_cols:
            conn.execute(text("ALTER TABLE surveys ADD COLUMN answers TEXT NOT NULL DEFAULT ''"))
        rm_cols = cols(conn, "rooms")
        if rm_cols and "topic" not in rm_cols:
            conn.execute(text("ALTER TABLE rooms ADD COLUMN topic VARCHAR(200) NOT NULL DEFAULT ''"))
        tm_cols = cols(conn, "teams")
        if tm_cols and "description" not in tm_cols:
            conn.execute(text("ALTER TABLE teams ADD COLUMN description VARCHAR(500) NOT NULL DEFAULT ''"))
        set_cols = cols(conn, "settings")
        if set_cols and "site_id" not in set_cols:
            # 旧スキーマ(キーのみ)は作り直す。設定はデフォルト値に戻る
            conn.execute(text("DROP TABLE settings"))
        conn.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate()
    Base.metadata.create_all(bind=engine)  # 落としたテーブルを新スキーマで再作成


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
