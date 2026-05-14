from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
import psycopg2.extras
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_conn():
    url = os.environ["DATABASE_URL"]
    return psycopg2.connect(url, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)

@app.get("/schools")
def get_schools():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM schools")
    schools = cur.fetchall()
    conn.close()
    return schools

@app.get("/professors")
def get_professors(
    school_id: str = Query(None),
    department: str = Query(None),
    min_ratings: int = Query(0),
    limit: int = Query(50),
    offset: int = Query(0),
):
    conn = get_conn()
    cur = conn.cursor()
    filters = ["num_ratings >= %s"]
    params = [min_ratings]
    if school_id:
        filters.append("school_id = %s")
        params.append(school_id)
    if department:
        filters.append("department ILIKE %s")
        params.append(f"%{department}%")
    where = " AND ".join(filters)
    params.extend([limit, offset])
    cur.execute(f"""
        SELECT * FROM professors
        WHERE {where}
        ORDER BY avg_rating DESC NULLS LAST
        LIMIT %s OFFSET %s
    """, params)
    professors = cur.fetchall()
    conn.close()
    return professors

@app.get("/professors/search")
def search_professors(name: str = Query(...)):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM professors
        WHERE LOWER(first_name || ' ' || last_name) LIKE LOWER(%s)
           OR LOWER(last_name || ' ' || first_name) LIKE LOWER(%s)
        ORDER BY num_ratings DESC
        LIMIT 10
    """, (f"%{name}%", f"%{name}%"))
    professors = cur.fetchall()
    conn.close()
    return professors

@app.get("/professors/{professor_id}")
def get_professor(professor_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM professors WHERE id = %s", (professor_id,))
    professor = cur.fetchone()
    if not professor:
        raise HTTPException(status_code=404, detail="Professor not found")
    conn.close()
    return professor

@app.get("/professors/{professor_id}/ratings")
def get_ratings(professor_id: str, limit: int = Query(50), offset: int = Query(0)):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM professors WHERE id = %s", (professor_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Professor not found")
    cur.execute("""
        SELECT * FROM ratings
        WHERE professor_id = %s
        ORDER BY date DESC
        LIMIT %s OFFSET %s
    """, (professor_id, limit, offset))
    ratings = cur.fetchall()
    conn.close()
    return ratings