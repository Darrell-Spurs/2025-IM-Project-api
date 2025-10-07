from datetime import datetime, timedelta, timezone, date as _date
from io import BytesIO
from typing import Optional, List, Dict
from urllib.parse import quote
import hashlib

import httpx
import psycopg2
from fastapi import APIRouter, Query, HTTPException, Depends, Body, File, UploadFile, Form
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse

import string, secrets
from psycopg2.extras import Json

from db import db_conn  # flat import if you run inside the `api/` dir
import os
from PIL import Image
import io

SCREENSHOTS_DIR = "../screenshots"

# helper to make 8-char id (A–Z a–z 0–9)
_ID_ALPHABET = string.ascii_letters + string.digits
def _gen_id(n: int = 8) -> str:
    return ''.join(secrets.choice(_ID_ALPHABET) for _ in range(n))


router = APIRouter(prefix="", tags=["screenshots"])

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

def _ensure_supabase_env():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(500, "Supabase env not configured on API server.")

def _row_to_dict(row) -> Dict:
    # Keep this order in SELECTs to match indexes below
    return {
        "id": row[0],
        "user_id": row[1],
        "category": row[2],
        "capture_time": row[3].isoformat() if row[3] else None,
        "storage_provider": row[4],
        "bucket": row[5],
        "object_key": row[6],
        "sha256": row[7],
        "mime": row[8],
        "bytes": row[9],
        "width": row[10],
        "height": row[11],
        "extra": row[12] or {},
        "created_at": row[13].isoformat() if row[13] else None,
        "deleted_at": row[14].isoformat() if row[14] else None,
    }

def _sign_url(bucket: str, object_key: str, expires_in: int = 300) -> str:
    """
    Create a short-lived signed URL for a private object.
    """
    _ensure_supabase_env()
    key_enc = quote(object_key.replace("\\", "/"), safe="/")
    endpoint = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{key_enc}"
    # Supabase expects JSON with expiresIn (seconds)
    with httpx.Client(timeout=10) as client:
        r = client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Content-Type": "application/json",
            },
            json={"expiresIn": expires_in},
        )
        if r.status_code // 100 != 2:
            raise HTTPException(502, f"Failed to sign URL: {r.text}")
        # Response body includes {"signedURL": "..."} in modern clients; REST returns path
        data = r.json()
        signed_path = data.get("signedURL") or data.get("url") or data  # handle variants
        if signed_path.startswith("http"):
            return signed_path
        return f"{SUPABASE_URL}{signed_path}"

def _stream_authenticated(bucket: str, object_key: str):
    """
    Stream a private object via the API server (used for zips).
    """
    _ensure_supabase_env()
    key_enc = quote(object_key.replace("\\", "/"), safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/authenticated/{bucket}/{key_enc}"
    client = httpx.Client(timeout=None)
    r = client.stream(
        "GET",
        url,
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    if r.status_code // 100 != 2:
        client.close()
        raise HTTPException(502, f"Object download failed: {r.text}")
    return client, r

CATEGORY_KEYS: List[str] = [
        "gaming", "others", "videos", "articles",
        "messaging", "social_media", "text_editing",
    ]
def _zero_fill_categories(summary: Dict[str, int]) -> Dict[str, int]:
    # ensure every CATEGORY_KEYS is present, default 0
    out = {k: 0 for k in CATEGORY_KEYS}
    out.update({k: int(v or 0) for k, v in (summary or {}).items()})
    return out

# To be updated
@router.get("/users/{user_id}/screenshots")
def list_screenshots_for_user(
        user_id: str,
        category: Optional[str] = None,
        start: Optional[datetime] = Query(None, description="ISO8601 start time (inclusive)"),
        end: Optional[datetime] = Query(None, description="ISO8601 end time (exclusive)"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Returns screenshot metadata for a user, filterable by time range and category.
    Uses capture_time (new schema) and storage-key fields.
    """
    direction = "ASC" if order == "asc" else "DESC"
    sql = f"""
        SELECT
            id, user_id, category, capture_time,
            storage_provider, bucket, object_key,
            sha256, mime, bytes, width, height,
            extra, created_at, deleted_at
        FROM screenshots
        WHERE user_id = %s
          AND (%s IS NULL OR category = %s)
          AND (%s IS NULL OR capture_time >= %s)
          AND (%s IS NULL OR capture_time < %s)
        ORDER BY capture_time {direction}
        LIMIT %s OFFSET %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, category, category, start, start, end, end, limit, offset))
        rows = cur.fetchall()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}

# To be updated
@router.get("/screenshots/{screenshot_id}")
def get_screenshot_metadata(
        screenshot_id: str,
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Return a single screenshot's metadata by its screenshot ID.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id, user_id, category, capture_time,
                storage_provider, bucket, object_key,
                sha256, mime, bytes, width, height,
                extra, created_at, deleted_at
            FROM screenshots
            WHERE id = %s
            """,
            (screenshot_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    return _row_to_dict(row)

# To be updated
@router.get("/screenshots/{screenshot_id}/download")
def download_screenshot(
        screenshot_id: str,
        expires_in: int = Query(300, ge=30, le=3600),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Return a redirect to a short-lived signed URL for the screenshot in Supabase Storage.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT bucket, object_key FROM screenshots WHERE id = %s", (screenshot_id,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "Screenshot not found.")
    bucket, object_key = r
    url = _sign_url(bucket, object_key, expires_in)
    return RedirectResponse(url, status_code=307)

# To be updated
@router.get("/users/{user_id}/screenshots/within")
def list_screenshots_within_hours(
        user_id: str,
        hours: int = Query(6, ge=1, le=24*365*100, description="Look back hours"),
        category: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Recent screenshots for a user (uses capture_time).
    """
    start_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    direction = "ASC" if order == "asc" else "DESC"
    sql = f"""
        SELECT
            id, user_id, category, capture_time,
            storage_provider, bucket, object_key,
            sha256, mime, bytes, width, height,
            extra, created_at, deleted_at
        FROM screenshots
        WHERE user_id = %s
          AND capture_time >= %s
          AND (%s IS NULL OR category = %s)
        ORDER BY capture_time {direction}
        LIMIT %s OFFSET %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, start_dt, category, category, limit, offset))
        rows = cur.fetchall()
    return {"items": [_row_to_dict(r) for r in rows], "count": len(rows)}

@router.get("/users/{user_id}/screenshots/within/summary")
def list_screenshots_within_hours_summary(
        user_id: str,
        hours: int = Query(6, ge=1, le=24*365*100, description="Look back hours"),
        record_seconds: int = Query(10, ge=1, le=300, description="Seconds per record"),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Returns a dict: {category: total_seconds, ...} for the last X hours.
    Each record in `screenshots` is treated as `record_seconds`.
    """
    start_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    sql = """
        SELECT COALESCE(category, '(uncategorized)') AS category, COUNT(*)::int AS n
        FROM screenshots
        WHERE user_id = %s
          AND capture_time >= %s
        GROUP BY 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, start_dt))
        rows = cur.fetchall()
    result = _zero_fill_categories({row[0]: row[1] for row in rows})
    result = {k: v * record_seconds for k, v in result.items()}
    return {
        "user_id": user_id,
        "lookback_hours": hours,
        "summary": result,
    }

@router.get("/users/{user_id}/screenshots/category-summary-by-range",
            summary="Get category usage summary with configurable time granularity",
            description="Returns seconds spent in each category between start and end times, grouped by time granularity.")
def get_category_summary_by_range(
        user_id: str,
        start: datetime = Query(..., description="Start time (inclusive)"),
        end: datetime = Query(..., description="End time (exclusive)"),
        granularity: str = Query(None, description="Time granularity: hour, day, week, month, year. If none, treats whole period as one."),
        category: Optional[str] = Query(None, description="Optional: filter to specific category"),
        record_seconds: int = Query(10, ge=1, le=300, description="Seconds per record"),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    # Ensure timestamps are UTC
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    # Validate granularity
    valid_granularities = {'hour', 'day', 'week', 'month', 'year'}
    if granularity and granularity not in valid_granularities:
        raise HTTPException(400, f"Invalid granularity. Must be one of: {', '.join(valid_granularities)}")
    
    result = {}
    if granularity:
        current = start
        while current < end:
            period_key = current.isoformat()
            result[period_key] = _zero_fill_categories({})
            
            # Increment based on granularity
            if granularity == 'hour':
                current += timedelta(hours=1)
            elif granularity == 'day':
                current += timedelta(days=1)
            elif granularity == 'week':
                current += timedelta(weeks=1)
            elif granularity == 'month':
                # Handle month increment
                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1)
                else:
                    current = current.replace(month=current.month + 1)
            elif granularity == 'year':
                current = current.replace(year=current.year + 1)
    else:
        # No granularity - just one period
        result[start.isoformat()] = _zero_fill_categories({})

    # SQL with date_trunc if granularity specified
    if granularity:
        sql = """
            SELECT 
                date_trunc(%s, capture_time) as period_start,
                category,
                COUNT(*)::int AS count
            FROM screenshots
            WHERE user_id = %s
              AND capture_time >= %s
              AND capture_time < %s
              AND (%s IS NULL OR category = %s)
            GROUP BY period_start, category
            ORDER BY period_start
        """
        params = (granularity, user_id, start, end, category, category)
    else:
        # No granularity - treat whole period as one
        sql = """
            SELECT 
                %s as period_start,
                category,
                COUNT(*)::int AS count
            FROM screenshots
            WHERE user_id = %s
              AND capture_time >= %s
              AND capture_time < %s
              AND (%s IS NULL OR category = %s)
            GROUP BY category
        """
        params = (start, user_id, start, end, category, category)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    # Fill in actual data where it exists
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for period_start, cat, count in cur.fetchall():
            period_key = period_start.isoformat()
            result[period_key][cat] = count * record_seconds

    return {
        "user_id": user_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "granularity": granularity,
        "periods": result
    }

# To be updated
@router.get("/users/{user_id}/screenshots.zip")
def download_screenshots_zip(
    user_id: str,
    category: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    max_screenshots: int = Query(200, ge=1, le=2000),
    conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Bundle matched screenshots into a ZIP by streaming from Supabase Storage (authenticated).
    """
    sql = """
        SELECT bucket, object_key
        FROM screenshots
        WHERE user_id = %s
          AND (%s IS NULL OR category = %s)
          AND (%s IS NULL OR capture_time >= %s)
          AND (%s IS NULL OR capture_time < %s)
        ORDER BY capture_time DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, category, category, start, start, end, end, max_screenshots))
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(404, "No screenshots match the criteria.")

    def zip_stream():
        import zipfile
        mem = BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
            for (bucket, object_key) in rows:
                client, r = _stream_authenticated(bucket, object_key)
                # Use the tail of the key as filename inside the zip
                arcname = object_key.rsplit("/", 1)[-1]
                with zf.open(arcname, "w") as zinfo:
                    for chunk in r.iter_bytes():
                        zinfo.write(chunk)
                r.close()
                client.close()
        mem.seek(0)
        yield from mem.getbuffer()

    filename = f"{user_id}_screenshots.zip"
    return StreamingResponse(
        zip_stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# To be updated
@router.post(
    "/users/{user_id}/screenshots/test-generate",
    summary="Generate placeholder screenshots (test data)",
    description=(
        "Inserts `count` screenshots for a user using a fixed placeholder image, "
        "spaced 10 seconds apart starting from `start`. "
        "Only `user_id`, `category`, `start`, and `count` are variable; all storage fields are fixed."
    ),
    )
def generate_test_screenshots(
        user_id: str,
        category: str = Query(..., min_length=1, description="Category to assign to all generated screenshots."),
        start: Optional[datetime] = Query(None, description="Start time (UTC). Defaults to now if omitted."),
        count: int = Query(10, ge=1, le=1000, description="How many screenshots to create."),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Generates `count` screenshots at 10s intervals using a fixed placeholder asset.
    Variable fields: user_id, category, capture_time, created_at.
    """
    # Placeholder asset (fixed)
    storage_provider = "supabase"
    bucket = "screenshots"
    object_key = "CAs8FbMG/2025/09/18/09b5830c62ec5f6a8d854e9c1307754287a937efb55f4e213863b44e574bdd44.webp"
    sha256 = "09b5830c62ec5f6a8d854e9c1307754287a937efb55f4e213863b44e574bdd44"
    mime = "image/webp"
    bytes_ = 40848
    width = 1600
    height = 900
    extra = {
        "compress": {
            "codec": "webp",
            "max_dim": 1600,
            "min_dim": 640,
            "target_kb": 50,
            "min_quality": 30
        },
        "original": {
            "mime": "image/png",
            "bytes": 488229,
            "width": 3840,
            "height": 2160,
            "sha256": "6e506a9599647f0e90944b35b1ee7171310a003285a0fce15f47b88b48a96876"
        }
    }

    base = start or datetime.now(timezone.utc)
    # Ensure timezone-aware UTC
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    else:
        base = base.astimezone(timezone.utc)

    # Prepare batch values
    rows = []
    now_utc = datetime.now(timezone.utc)
    for i in range(count):
        sid = _gen_id(8)
        cap_time = base + timedelta(seconds=10 * i)
        created_at = now_utc  # or cap_time; choose one. Using "now" for create timestamp.
        rows.append((
            sid, user_id, category, cap_time,
            storage_provider, bucket, object_key,
            sha256, mime, bytes_, width, height,
            Json(extra), created_at
        ))

    sql = """
        INSERT INTO screenshots (
            id, user_id, category, capture_time,
            storage_provider, bucket, object_key,
            sha256, mime, bytes, width, height,
            extra, created_at
        )
        VALUES %s
        RETURNING id, capture_time
    """

    # Use mogrify-friendly bulk insert
    try:
        with conn.cursor() as cur:
            # Build VALUES (...) list safely
            args_str_list = []
            for r in rows:
                args_str_list.append(cur.mogrify("(" + ",".join(["%s"]*len(r)) + ")", r).decode())
            cur.execute(sql.replace("VALUES %s", "VALUES " + ",".join(args_str_list)))
            created = cur.fetchall()
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")

    # Return a small summary
    return {
        "ok": True,
        "user_id": user_id,
        "category": category,
        "start": base.isoformat(),
        "count": count,
        "ids": [c[0] for c in created],
        "first_capture_time": created[0][1].isoformat() if created else None,
        "last_capture_time": created[-1][1].isoformat() if created else None,
    }


@router.post("/screenshots/upload-screenshot")
async def upload_screenshot(
        screenshot_id: str = Form(...),
        user_id: str = Form(...),
        category: str = Form(...),
        file: UploadFile = File(...),
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Upload a screenshot to local storage and save metadata to database.
    All screenshots are saved as PNG format for optimal quality.
    """
    try:
        # Validate file type
        if not file.content_type or not file.content_type.startswith('image/'):
            raise HTTPException(status_code=400, detail="File must be an image")
        
        # Read and validate file size (max 10MB)
        file_content = await file.read()
        if len(file_content) > 10 * 1024 * 1024:  # 10MB
            raise HTTPException(status_code=400, detail="File too large (max 10MB)")
        
        # Generate filename with sanitized screenshot_id (always PNG)
        safe_id = "".join(c for c in screenshot_id if c.isalnum() or c in "_-")
        filename = f"screenshot_{safe_id}.png"
        file_path = os.path.join(SCREENSHOTS_DIR, filename)
        
        # Process and save the file as PNG
        image = Image.open(io.BytesIO(file_content))
        width, height = image.size
        
        # Save as PNG (lossless, universal compatibility)
        image.save(file_path, 'PNG', optimize=True)
        mime_type = 'image/png'
        
        # Get file size
        file_size = os.path.getsize(file_path)
        
        # Calculate SHA256 hash
        with open(file_path, 'rb') as f:
            sha256_hash = hashlib.sha256(f.read()).hexdigest()
        
        # Save metadata to database
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO screenshots (
                    id, user_id, category, capture_time,
                    storage_provider, bucket, object_key,
                    sha256, mime, bytes, width, height,
                    extra, created_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s
                )
            """, (
                screenshot_id,
                user_id,
                category,
                datetime.now(timezone.utc),  # capture_time
                'local',  # storage_provider
                None,  # bucket (not used for local storage)
                filename,  # object_key (relative path)
                sha256_hash,
                mime_type,
                file_size,
                width,
                height,
                Json({}),  # extra metadata
                datetime.now(timezone.utc)  # created_at
            ))
            conn.commit()
        
        return {
            'success': True,
            'screenshot_id': screenshot_id,
            'file_path': filename,
            'file_size': file_size,
            'width': width,
            'height': height,
            'mime_type': mime_type,
            'format': 'png'
        }
        
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.get("/screenshots/{screenshot_id}/file")
async def get_screenshot_file(
        screenshot_id: str,
        conn: psycopg2.extensions.connection = Depends(db_conn),
    ):
    """
    Serve screenshot file by screenshot ID with database validation.
    """
    # Get screenshot metadata from database
    with conn.cursor() as cur:
        cur.execute("""
            SELECT storage_provider, object_key, mime, user_id
            FROM screenshots 
            WHERE id = %s
        """, (screenshot_id,))
        row = cur.fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    
    storage_provider, object_key, mime_type, user_id = row
    
    # Only serve local storage files
    if storage_provider != 'local':
        raise HTTPException(status_code=400, detail="Screenshot not stored locally")
    
    # Build file path
    file_path = os.path.join(SCREENSHOTS_DIR, object_key)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Screenshot file not found on disk")
    
    return FileResponse(
        path=file_path,
        media_type=mime_type,
        filename=object_key
    )

# Removed unsafe direct file serving endpoint for security reasons
# Use /screenshots/{screenshot_id}/file instead which validates through database