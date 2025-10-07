from typing import Optional, Dict, List, Annotated
from fastapi import APIRouter, Query, Depends, HTTPException, status, Body
import psycopg2
from db import db_conn
from pydantic import BaseModel, Field
from psycopg2.extras import Json
import string, secrets
from datetime import datetime, timezone, timedelta

# --- secure password hashing ---
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHash
import os, hmac, hashlib

router = APIRouter(tags=["users"])

# -----------------------------
# Password hashing utilities
# -----------------------------
PH = PasswordHasher(              # tune if you want stronger/slower
    time_cost=3, memory_cost=64_000, parallelism=2, hash_len=32, salt_len=16
)
PEPPER = os.getenv("PASSWORD_PEPPER", "")  # optional server-side secret

def _pepper(pw: str) -> bytes:
    if not PEPPER:
        return pw.encode("utf-8")
    return hmac.new(PEPPER.encode(), pw.encode("utf-8"), hashlib.sha256).digest()

def hash_password(plain: str) -> str:
    return PH.hash(_pepper(plain))

def verify_password(plain: str, stored: str) -> bool:
    try:
        ok = PH.verify(stored, _pepper(plain))
        return bool(ok)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False

# -----------------------------
# ID generator
# -----------------------------
_ID_ALPHABET = string.ascii_letters + string.digits
def generate_id(n: int = 8) -> str:
    return ''.join(secrets.choice(_ID_ALPHABET) for _ in range(n))

# -----------------------------
# Thresholds: dict[str, list]
# -----------------------------
CATEGORY_KEYS: List[str] = [
    "gaming", "others", "videos", "articles",
    "messaging", "social_media", "text_editing",
]
def default_thresholds() -> Dict[str, List[str]]:
    return {k: [] for k in CATEGORY_KEYS}

# -----------------------------
# Schemas
# -----------------------------
class CreateUser(BaseModel):
    name: str = Field(min_length=1)
    password: str = Field(min_length=6, example="sTr0n&p4sSw0rd")
    status: str = Field(default="active", min_length=1)
    # Reworked: dict of arrays (defaults empty arrays)
    threshold: Dict[str, List[str]] = Field(
        default_factory=default_thresholds,
        example={k: [] for k in CATEGORY_KEYS}
    )
    email_address: Optional[str] = Field(default=None, example="user@example.com")
    is_admin: bool = Field(default=False, description="Whether the user is an admin account")

class LoginRequest(BaseModel):
    identifier: str = Field(..., description="Username or email address")
    password: str = Field(..., min_length=1)

class ThresholdsReplace(BaseModel):
    thresholds: Dict[str, List[str]] = Field(
        default_factory=default_thresholds,
        description="Replace entire thresholds map with arrays per category.",
        example={k: [] for k in CATEGORY_KEYS},
    )

class ThresholdsPatch(BaseModel):
    add: Optional[List[str]] = Field(default=None, description="Items to add to this category")
    remove: Optional[List[str]] = Field(default=None, description="Items to remove from this category")

# -----------------------------
# Endpoints
# -----------------------------

@router.get("/users/{user_id}",
            summary="Get a user by id",
            description="Retrieves a user by their unique ID.")
def get_user(user_id: str, conn: psycopg2.extensions.connection = Depends(db_conn)):
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, status, created_at, email_address, is_admin FROM users WHERE id = %s", (user_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")
        return {"id": r[0], "name": r[1], "status": r[2], "created_at": r[3].isoformat(), "email_address": r[4], "is_admin": r[5]}

@router.post("/users", status_code=status.HTTP_201_CREATED,
             summary="Create a new user",
             description=("Creates a new user with a generated 8-char id. "
                          "Stores a secure Argon2 hash of the password. "
                          "Thresholds are dict-of-arrays and default to empty arrays."))
def create_user(payload: CreateUser, conn: psycopg2.extensions.connection = Depends(db_conn)):
    sql = """
        INSERT INTO users (id, name, password, status, threshold, email_address, is_admin, invite_code)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, name, status, created_at, threshold, email_address, is_admin, invite_code
    """
    # ensure all categories exist as arrays; merge any provided keys
    merged = default_thresholds()
    for k, v in (payload.threshold or {}).items():
        merged[k] = list(dict.fromkeys(v or []))  # de-dup while preserving order

    pw_hash = hash_password(payload.password)

    for _ in range(5):
        new_id = generate_id(8)
        if not payload.is_admin:
            new_invite_code = generate_id(6)
        else:
            new_invite_code = None
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (new_id, payload.name, pw_hash, payload.status, Json(merged), payload.email_address, payload.is_admin, new_invite_code))
                row = cur.fetchone()
            conn.commit()
            return {
                "id": row[0],
                "name": row[1],
                "status": row[2],
                "created_at": row[3].isoformat() if row[3] else None,
                "threshold": row[4] or default_thresholds(),
                "email_address": row[5],
                "is_admin": row[6],
                "invite_code": row[7],
            }
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            continue
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Create failed: {e}")
    raise HTTPException(status_code=500, detail="Could not generate a unique user id.")

@router.post("/auth/login",
             summary="Login with user id or email + password",
             description="Verifies password against the stored Argon2 hash and returns a basic success result.")
def login(payload: LoginRequest, conn: psycopg2.extensions.connection = Depends(db_conn)):
    # try id first, fall back to email
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, password, name
              FROM users
             WHERE name = %s OR email_address = %s
             LIMIT 1
        """, (payload.identifier, payload.identifier))
        r = cur.fetchone()
    if not r:
        # status code 401: login fail
        raise HTTPException(status_code=401, detail="Invalid credentials")
    user_id, stored_hash, name = r
    if not stored_hash or not verify_password(payload.password, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Here you would mint a session or JWT; for now, return minimal info
    return {"ok": True, "user_id": user_id, "name": name}

@router.get("/users",
            summary="List all users",
            description="Retrieves a list of all users, with optional search and pagination.")
def list_users(
    q: Optional[str] = Query(None, description="search by name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: psycopg2.extensions.connection = Depends(db_conn),
):
    sql = """
        SELECT id, name, status, created_at, email_address, is_admin
        FROM users
        WHERE (%s IS NULL OR name ILIKE '%%' || %s || '%%')
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (q, q, limit, offset))
        rows = cur.fetchall()
        return [{"id": r[0], "name": r[1], "status": r[2], "created_at": r[3].isoformat(), "email_address": r[4], "is_admin": r[5]} for r in rows]

@router.get("/users/{user_id}/is-admin",
            summary="Check if user is admin",
            description="Returns whether the specified user has admin privileges.")
def is_admin(user_id: str, conn: psycopg2.extensions.connection = Depends(db_conn)):
    with conn.cursor() as cur:
        cur.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")
        return {"is_admin": bool(r[0])}
    
# ---------- Presence / status ----------
@router.get("/users/{user_id}/presence",
            summary="Get presence & last activity",
            description=("Computes presence from the most recent screenshot. "
                         "Active: last ≤ 3 minutes; Recent: last ≤ 20 minutes; else Offline."))
def get_presence(user_id: str, conn: psycopg2.extensions.connection = Depends(db_conn)):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT capture_time, category
              FROM screenshots
             WHERE user_id = %s
             ORDER BY capture_time DESC
             LIMIT 1
        """, (user_id,))
        r = cur.fetchone()

    if not r:
        # no screenshots yet → offline unknown
        return {"status": "offline", "last_online": None, "last_category": None}

    last_ts, last_cat = r
    now = datetime.now(timezone.utc)
    delta = now - last_ts
    minutes = delta.total_seconds() / 60.0

    if minutes <= 3:
        status_str = "active"
    elif minutes <= 20:
        status_str = "recent"
    else:
        status_str = "offline"

    # human-ish "x minutes/hours/days"
    if minutes < 60:
        last_online = f"{int(round(minutes))} minutes"
    elif minutes < 60 * 24:
        last_online = f"{int(round(minutes/60))} hours"
    else:
        last_online = f"{int(round(minutes/1440))} days"

    return {
        "status": status_str,
        "last_online": last_online,
        "last_category": last_cat,
        "last_capture_time": last_ts.isoformat(),
    }

# ---------- Thresholds (dict of arrays) ----------
@router.get("/users/{user_id}/thresholds",
            summary="Get user thresholds (arrays)",
            description="Returns the thresholds (alarms) (alarms) map where each category is an array (defaults to empty arrays).")
def get_thresholds(user_id: str, conn: psycopg2.extensions.connection = Depends(db_conn)):
    with conn.cursor() as cur:
        cur.execute("SELECT threshold FROM users WHERE id = %s", (user_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")

    current = r[0] or {}
    # normalize to ensure all categories exist as arrays
    out = default_thresholds()
    for k, v in (current or {}).items():
        out[k] = list(v or [])
    return out

@router.put("/users/{user_id}/thresholds",
            summary="Replace thresholds (alarms) (arrays per category)",
            description="Replaces the entire thresholds (alarms) map with arrays per category.")
def replace_thresholds(user_id: str, payload: ThresholdsReplace, conn: psycopg2.extensions.connection = Depends(db_conn)):
    merged = default_thresholds()
    for k, v in (payload.thresholds or {}).items():
        merged[k] = list(dict.fromkeys(v or []))
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE users SET threshold = %s::jsonb WHERE id = %s
            RETURNING threshold
        """, (Json(merged), user_id))
        r = cur.fetchone()
        if not r:
            conn.rollback()
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()
        return r[0] or default_thresholds()

@router.get("/users/{user_id}/thresholds/{category}",
            summary="Get thresholds (alarms) for a category",
            description="Returns the array of thresholds (alarms) for a single category.")
def get_threshold_category(user_id: str, category: str, conn: psycopg2.extensions.connection = Depends(db_conn)):
    if category not in CATEGORY_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown category '{category}'")
    with conn.cursor() as cur:
        cur.execute("SELECT threshold -> %s FROM users WHERE id = %s", (category, user_id))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")
        arr = r[0] or []
        return {"category": category, "alarms": list(arr)}

@router.put("/users/{user_id}/thresholds/{category}",
            summary="Add/remove thresholds (alarms) in a category",
            description=("Patch the category array by adding/removing thresholds (alarms). "
                         "Duplicates are de-duplicated; order is preserved by first appearance."))
def patch_threshold_category(
    user_id: str,
    category: str,
    patch: ThresholdsPatch = Body(...),
    conn: psycopg2.extensions.connection = Depends(db_conn),
):
    if category not in CATEGORY_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown category '{category}'")

    with conn.cursor() as cur:
        cur.execute("SELECT threshold FROM users WHERE id = %s", (user_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="User not found")
        current = r[0] or default_thresholds()

        # normalize map
        for k in CATEGORY_KEYS:
            current.setdefault(k, [])

        base_list = list(current.get(category) or [])
        # remove
        if patch.remove:
            remove_set = set(patch.remove)
            base_list = [x for x in base_list if x not in remove_set]
        # add
        if patch.add:
            for x in patch.add:
                if x not in base_list:
                    base_list.append(x)

        current[category] = base_list
        cur.execute("UPDATE users SET threshold = %s::jsonb WHERE id = %s RETURNING threshold",
                    (Json(current), user_id))
        updated = cur.fetchone()[0]
        conn.commit()
    return {"category": category, "items": updated.get(category, [])}
