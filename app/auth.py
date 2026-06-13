"""パスワードハッシュ(PBKDF2)とJWTトークンの発行・検証"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

# 本番では必ず環境変数 SECRET_KEY を設定すること
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return secrets.compare_digest(candidate, digest)


def create_token(user_id: int, token_version: str) -> str:
    """ユーザーID + そのユーザーのトークン世代(token_version)を署名して発行する。

    token_versionはユーザー行ごとの乱数で、DBが作り直されて連番IDが振り直されても
    新しいユーザーは別の値を持つ。これにより「IDだけ一致する別人のトークン」を弾ける。
    """
    payload = {
        "sub": str(user_id),
        "tv": token_version,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> tuple[int, str] | None:
    """トークンから (ユーザーID, token_version) を取り出す。無効なら None。

    token_versionの一致確認は呼び出し側(DBのユーザー行と照合)で行う。
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"]), str(payload.get("tv", ""))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
