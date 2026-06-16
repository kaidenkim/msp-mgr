import hashlib
import json
import os
from pathlib import Path
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException
from config import SECRET_KEY

_signer = URLSafeTimedSerializer(SECRET_KEY)
COOKIE_NAME = "msp_mgr_admin"
SESSION_MAX_AGE = 60 * 60 * 8  # 8시간

DATA_DIR = Path(__file__).parent / "data"
CRED_FILE = DATA_DIR / "admin_credentials.json"
DEFAULT_PASSWORD = "1234"


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _load_credentials() -> dict:
    if CRED_FILE.exists():
        return json.loads(CRED_FILE.read_text())
    return {"password_hash": _hash(DEFAULT_PASSWORD)}


def _save_credentials(creds: dict):
    DATA_DIR.mkdir(exist_ok=True)
    CRED_FILE.write_text(json.dumps(creds))


def verify_admin_password(password: str) -> bool:
    creds = _load_credentials()
    return creds["password_hash"] == _hash(password)


def change_admin_password(new_password: str):
    _save_credentials({"password_hash": _hash(new_password)})


def make_session() -> str:
    return _signer.dumps({"role": "admin"})


def decode_session(token: str) -> dict:
    try:
        return _signer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return {}


def is_admin(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    return decode_session(token).get("role") == "admin"


def require_admin(request: Request) -> dict:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="관리자 로그인이 필요합니다")
    return {"role": "admin"}
