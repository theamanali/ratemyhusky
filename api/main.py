from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import time

def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)

limiter = Limiter(key_func=get_client_ip)
app = FastAPI(
    title="RateMyHusky API",
    description=(
        "Professor ratings data for University of Washington campuses "
        "(Seattle, Bothell, Tacoma), sourced from RateMyProfessors.\n\n"
        "**Rate limits:** Search endpoints are limited to 30 requests/minute per IP. "
        "All other endpoints are limited to 60 requests/minute per IP.\n\n"
        "**Note:** `avg_rating` and `avg_difficulty` are `-1` for professors with no ratings."
    ),
    version="1.0.0",
)
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
            maxconn=10,
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
        yield cur
    finally:
        pool.putconn(conn)

def paginate(data, total, limit, offset):
    return {
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_next": offset + limit < total,
    }

VALID_SORT_COLS = {"avg_rating", "avg_difficulty", "num_ratings"}
VALID_SORT_ORDERS = {"asc", "desc"}


@app.get("/health", tags=["Meta"], summary="Health check")
def health(request: Request):
    """Returns API and database status with DB response latency."""
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


@app.get("/schools", tags=["Schools"], summary="List all schools")
@limiter.limit("60/minute")
def get_schools(request: Request):
    """Returns all UW campuses. Use the returned `id` as `school_id` in other endpoints."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM schools ORDER BY name")
        return cur.fetchall()


@app.get("/departments", tags=["Schools"], summary="List departments")
@limiter.limit("60/minute")
def get_departments(
    request: Request,
    school_id: str = Query(None, description="Filter departments by school ID"),
):
    """Returns a list of all unique department names, optionally scoped to a school."""
    with db_cursor() as cur:
        if school_id:
            cur.execute(
                "SELECT DISTINCT department FROM professors "
                "WHERE school_id = %s AND department IS NOT NULL ORDER BY department",
                (school_id,),
            )
        else:
            cur.execute(
                "SELECT DISTINCT department FROM professors "
                "WHERE department IS NOT NULL ORDER BY department"
            )
        return [r["department"] for r in cur.fetchall()]


@app.get("/professors", tags=["Professors"], summary="List and filter professors")
@limiter.limit("60/minute")
def get_professors(
    request: Request,
    school_id: str = Query(None, description="Filter by school ID"),
    department: str = Query(None, description="Filter by department name (partial match)"),
    min_ratings: int = Query(0, description="Minimum number of ratings"),
    min_rating: float = Query(None, description="Minimum average rating (1-5)"),
    max_difficulty: float = Query(None, description="Maximum average difficulty (1-5)"),
    sort_by: str = Query("avg_rating", description="Sort field: avg_rating, avg_difficulty, num_ratings"),
    sort_order: str = Query("desc", description="Sort direction: asc or desc"),
    limit: int = Query(50, description="Number of results (max 200)"),
    offset: int = Query(0, description="Pagination offset"),
):
    """
    Returns a paginated list of professors with optional filtering and sorting.

    Professors with no ratings have `avg_rating` and `avg_difficulty` set to `-1`.
    """
    if sort_by not in VALID_SORT_COLS:
        raise HTTPException(status_code=400, detail=f"sort_by must be one of {VALID_SORT_COLS}")
    if sort_order not in VALID_SORT_ORDERS:
        raise HTTPException(status_code=400, detail="sort_order must be 'asc' or 'desc'")
    limit = min(limit, 200)

    filters = ["num_ratings >= %s"]
    params = [min_ratings]
    if school_id:
        filters.append("school_id = %s")
        params.append(school_id)
    if department:
        filters.append("department ILIKE %s")
        params.append(f"%{department}%")
    if min_rating is not None:
        filters.append("avg_rating >= %s")
        params.append(min_rating)
    if max_difficulty is not None:
        filters.append("avg_difficulty <= %s")
        params.append(max_difficulty)

    where = " AND ".join(filters)
    nulls = "LAST" if sort_order == "desc" else "FIRST"
    order = f"{sort_by} {sort_order.upper()} NULLS {nulls}"

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM professors WHERE {where}", params)
        total = cur.fetchone()["count"]
        cur.execute(
            f"SELECT * FROM professors WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        professors = cur.fetchall()

    return paginate(professors, total, limit, offset)


@app.get("/professors/search", tags=["Professors"], summary="Search professors by name")
@limiter.limit("30/minute")
def search_professors(
    request: Request,
    name: str = Query(..., min_length=2, description="Professor name (first last, last first, or with middle name)"),
    school_id: str = Query(None, description="Scope search to a specific school"),
    limit: int = Query(10, description="Number of results (max 50)"),
):
    """
    Searches professors by name. Handles:
    - Case-insensitive matching
    - Middle names and initials (e.g. \"Jason F. Lambacher\" → matches \"Jason Lambacher\")
    - Reversed name order (e.g. \"Lambacher Jason\" → matches \"Jason Lambacher\")

    Results are ordered by number of ratings descending.
    """
    limit = min(limit, 50)

    parts = name.strip().split()
    normalized = f"{parts[0]} {parts[-1]}" if len(parts) >= 2 else parts[0] if parts else name
    normalized = normalized.lower()
    pattern = f"%{normalized}%"

    filters = [
        "(LOWER(first_name || ' ' || last_name) LIKE %s"
        " OR LOWER(last_name || ' ' || first_name) LIKE %s)"
    ]
    params = [pattern, pattern]
    if school_id:
        filters.append("school_id = %s")
        params.append(school_id)

    where = " AND ".join(filters)
    with db_cursor() as cur:
        cur.execute(
            f"SELECT * FROM professors WHERE {where} ORDER BY num_ratings DESC LIMIT %s",
            params + [limit],
        )
        return cur.fetchall()


@app.get("/professors/{professor_id}", tags=["Professors"], summary="Get a professor by ID")
@limiter.limit("60/minute")
def get_professor(request: Request, professor_id: str):
    """Returns a single professor record by their RMP ID."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM professors WHERE id = %s", (professor_id,))
        professor = cur.fetchone()
    if not professor:
        raise HTTPException(status_code=404, detail="Professor not found")
    return professor


@app.get("/professors/{professor_id}/ratings", tags=["Professors"], summary="Get ratings for a professor")
@limiter.limit("30/minute")
def get_ratings(
    request: Request,
    professor_id: str,
    limit: int = Query(50, ge=1, le=200, description="Number of ratings (max 200)"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """Returns paginated student ratings for a professor, ordered by date descending."""
    with db_cursor() as cur:
        cur.execute("SELECT id FROM professors WHERE id = %s", (professor_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Professor not found")
        cur.execute("SELECT COUNT(*) FROM ratings WHERE professor_id = %s", (professor_id,))
        total = cur.fetchone()["count"]
        cur.execute(
            "SELECT * FROM ratings WHERE professor_id = %s ORDER BY date DESC LIMIT %s OFFSET %s",
            (professor_id, limit, offset),
        )
        ratings = cur.fetchall()
    return paginate(ratings, total, limit, offset)
