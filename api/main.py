import os
import re
import time
import unicodedata
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from nameparser import HumanName
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
app = FastAPI(title="RateMyDawg API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=50,
            dsn=os.environ["DATABASE_URL"],
            sslmode="require",
        )
    return _pool


@contextmanager
def db_cursor():
    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()
    finally:
        pool.putconn(conn)


def normalize_initials(s):
    s = re.sub(r'([A-Za-z]\.)(?=[A-Za-z])', r'\1 ', s)
    parts = s.split()
    return ' '.join(p.rstrip('.') + '.' if re.fullmatch(r'[A-Za-z]\.?', p) else p for p in parts)


def parse_name(full_name):
    n = HumanName(full_name or "")
    middle = normalize_initials(n.middle) if n.middle else None
    return n.first or None, middle, n.last or None


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFD', (s or "").lower().strip()) if unicodedata.category(c) != 'Mn')


@app.get("/health", tags=["Meta"])
@limiter.limit("30/minute")
def health(request: Request):
    start = time.monotonic()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "db_latency_ms": round((time.monotonic() - start) * 1000, 2),
    }


def _match_one(name: str) -> list:
    first, middle, last = parse_name(name)
    if not first or not last:
        return []

    base_filters = ["unaccent(lower(first_name)) = %s", "unaccent(lower(last_name)) = %s"]
    base_params = [norm(first), norm(last)]

    def fetch(extra_filters=None, extra_params=None):
        filters = base_filters + (extra_filters or [])
        params = base_params + (extra_params or [])
        with db_cursor() as cur:
            cur.execute(
                f"SELECT * FROM professors WHERE {' AND '.join(filters)} ORDER BY rmp_rating_count DESC NULLS LAST",
                params,
            )
            return cur.fetchall()

    if middle:
        results = fetch()
        if len(results) <= 1:
            return results
        query_middle = norm(middle)
        for char_count in range(1, len(query_middle) + 1):
            prefix = query_middle[:char_count]
            filtered = [r for r in results if r["middle_name"] and norm(r["middle_name"]).startswith(prefix)]
            if len(filtered) == 1:
                return filtered
            if filtered:
                results = filtered
        return results
    else:
        results = fetch(["middle_name IS NULL"])
        return results if results else fetch()


class BatchMatchRequest(BaseModel):
    names: list[str]


@app.post("/professors/match/batch", tags=["Professors"])
@limiter.limit("30/minute")
def match_professors_batch(request: Request, body: BatchMatchRequest):
    return {name: _match_one(name) for name in body.names}


@app.get("/professors/match", tags=["Professors"])
@limiter.limit("30/minute")
def match_professors(
    request: Request,
    name: str = Query(..., min_length=2),
):
    first, middle, last = parse_name(name)
    if not first or not last:
        raise HTTPException(status_code=400, detail="Could not parse a first and last name from the provided name")

    return _match_one(name)
