from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import time

app = FastAPI()

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


@app.get("/health")
def health():
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


@app.get("/schools")
def get_schools():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM schools ORDER BY name")
        return cur.fetchall()


@app.get("/departments")
def get_departments(school_id: str = Query(None)):
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


@app.get("/professors")
def get_professors(
    school_id: str = Query(None),
    department: str = Query(None),
    min_ratings: int = Query(0),
    min_rating: float = Query(None),
    max_difficulty: float = Query(None),
    sort_by: str = Query("avg_rating"),
    sort_order: str = Query("desc"),
    limit: int = Query(50),
    offset: int = Query(0),
):
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


@app.get("/professors/search")
def search_professors(
    name: str = Query(..., min_length=2),
    school_id: str = Query(None),
    limit: int = Query(10),
):
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


@app.get("/professors/{professor_id}")
def get_professor(professor_id: str):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM professors WHERE id = %s", (professor_id,))
        professor = cur.fetchone()
    if not professor:
        raise HTTPException(status_code=404, detail="Professor not found")
    return professor


@app.get("/professors/{professor_id}/ratings")
def get_ratings(
    professor_id: str,
    limit: int = Query(50),
    offset: int = Query(0),
):
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
