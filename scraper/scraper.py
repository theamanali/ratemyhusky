import requests
import psycopg2
import psycopg2.extras
import time
import os
import json
import re

GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.ratemyprofessors.com/",
    "Origin": "https://www.ratemyprofessors.com",
}
SCHOOLS = [
    {"id": "U2Nob29sLTE1MzA=",  "name": "UW Seattle"},
    {"id": "U2Nob29sLTQ0NjY=",  "name": "UW Bothell"},
    {"id": "U2Nob29sLTQ3NDQ=",  "name": "UW Tacoma"},
]
PROF_BATCH_SIZE = 1000
RATINGS_BATCH_SIZE = 1000
MAX_RETRIES = 3
RETRY_DELAY = 2
DB_URL = os.environ["DATABASE_URL"]

STOP_WORDS = {
    "the","a","an","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","shall","should",
    "may","might","must","can","could","to","of","in","for","on","with",
    "at","by","from","up","about","into","through","and","but","or","nor",
    "so","yet","both","either","neither","not","just","very","also","too",
    "he","she","they","we","i","you","it","this","that","his","her","their",
    "my","your","its","our","class","professor","prof","teacher","course",
    "really","very","good","great","bad","ok","okay","time","like","get",
    "take","make","know","think","even","much","some","only","than","then",
}

def init_db(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schools (
            id TEXT PRIMARY KEY,
            name TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS professors (
            id TEXT PRIMARY KEY,
            school_id TEXT,
            first_name TEXT,
            last_name TEXT,
            department TEXT,
            avg_rating REAL,
            avg_difficulty REAL,
            num_ratings INTEGER,
            would_take_again REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            grade_distribution TEXT DEFAULT NULL,
            rating_distribution TEXT DEFAULT NULL,
            difficulty_distribution TEXT DEFAULT NULL,
            courses TEXT DEFAULT NULL,
            common_tags TEXT DEFAULT NULL,
            attendance_mandatory_pct REAL DEFAULT NULL,
            online_pct REAL DEFAULT NULL,
            FOREIGN KEY (school_id) REFERENCES schools(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id TEXT PRIMARY KEY,
            professor_id TEXT,
            class TEXT,
            date TEXT,
            comment TEXT,
            clarity_rating INTEGER,
            helpful_rating INTEGER,
            difficulty_rating INTEGER,
            grade TEXT,
            would_take_again INTEGER,
            is_online BOOLEAN,
            attendance_mandatory BOOLEAN,
            FOREIGN KEY (professor_id) REFERENCES professors(id)
        )
    """)
    conn.commit()
    cur.close()

def post_with_retry(query):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.post(GRAPHQL_URL, headers=HEADERS, json={"query": query})
            if res.status_code == 200 and res.text:
                return res.json()
            print(f"    Attempt {attempt} failed (status {res.status_code}), retrying in {RETRY_DELAY}s...")
        except Exception as e:
            print(f"    Attempt {attempt} error: {e}, retrying in {RETRY_DELAY}s...")
        time.sleep(RETRY_DELAY)
    print(f"    All {MAX_RETRIES} attempts failed, skipping batch")
    return None

def fetch_professors_page(school_id, cursor=None):
    after = f', after: "{cursor}"' if cursor else ""
    data = post_with_retry(f"""
    query {{
        newSearch {{
            teachers(query: {{ schoolID: "{school_id}" }}, first: {PROF_BATCH_SIZE}{after}) {{
                edges {{
                    node {{
                        id firstName lastName department
                        avgRating avgDifficulty numRatings wouldTakeAgainPercent
                    }}
                }}
                pageInfo {{ hasNextPage endCursor }}
            }}
        }}
    }}
    """)
    return data["data"]["newSearch"]["teachers"] if data else None

def fetch_ratings_batch(professor_ids):
    aliases = "\n".join([
        f"""p{i}: node(id: "{pid}") {{
            ... on Teacher {{
                ratings(first: 1000) {{
                    edges {{
                        node {{
                            id class date comment
                            clarityRating helpfulRating difficultyRating
                            grade wouldTakeAgain isForOnlineClass attendanceMandatory
                        }}
                    }}
                }}
            }}
        }}"""
        for i, pid in enumerate(professor_ids)
    ])
    return post_with_retry(f"query {{ {aliases} }}")

def compute_professor_derived(ratings):
    if not ratings:
        return (
            json.dumps({}), json.dumps({"1":0,"2":0,"3":0,"4":0,"5":0}),
            json.dumps({"1":0,"2":0,"3":0,"4":0,"5":0}),
            json.dumps([]), json.dumps([]), None, None,
        )

    # grade_distribution
    grade_counts = {}
    for r in ratings:
        g = r["grade"] if r["grade"] else "N/A"
        grade_counts[g] = grade_counts.get(g, 0) + 1

    # rating_distribution (helpful_rating 1-5)
    rating_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for r in ratings:
        hr = r["helpful_rating"]
        if hr and 1 <= hr <= 5:
            rating_counts[str(hr)] += 1

    # difficulty_distribution
    diff_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for r in ratings:
        dr = r["difficulty_rating"]
        if dr and 1 <= dr <= 5:
            diff_counts[str(dr)] += 1

    # courses
    course_counts = {}
    for r in ratings:
        c = (r["class"] or "").strip()
        if c:
            course_counts[c] = course_counts.get(c, 0) + 1
    courses_list = [{"code": k, "count": v} for k, v in course_counts.items()]

    # common_tags
    word_counts = {}
    for r in ratings:
        comment = r["comment"] or ""
        words = re.findall(r"[a-z]+(?:'[a-z]+)?", comment.lower())
        for w in words:
            if w not in STOP_WORDS and len(w) >= 4:
                word_counts[w] = word_counts.get(w, 0) + 1
    top_tags = sorted(word_counts, key=lambda w: word_counts[w], reverse=True)[:20]

    # attendance_mandatory_pct
    mandatory = sum(1 for r in ratings if r["attendance_mandatory"])
    attendance_pct = round(mandatory / len(ratings) * 100, 2)

    # online_pct
    online = sum(1 for r in ratings if r["is_online"])
    online_pct = round(online / len(ratings) * 100, 2)

    return (
        json.dumps(grade_counts),
        json.dumps(rating_counts),
        json.dumps(diff_counts),
        json.dumps(courses_list),
        json.dumps(top_tags),
        attendance_pct,
        online_pct,
    )

# --- Main ---
conn = psycopg2.connect(DB_URL, sslmode="require")
init_db(conn)

for school in SCHOOLS:
    print(f"\n{'='*50}")
    print(f"School: {school['name']}")
    print(f"{'='*50}")

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO schools (id, name) VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
    """, (school["id"], school["name"]))
    conn.commit()
    cur.close()

    # Step 1: fetch all professors
    print("Step 1: Fetching professor info...")
    professors = []
    cursor = None
    page = 1
    while True:
        data = fetch_professors_page(school["id"], cursor)
        if not data:
            print("  Failed to fetch page, aborting")
            break
        batch = [e["node"] for e in data["edges"]]
        professors.extend(batch)
        print(f"  Page {page}: {len(batch)} professors (total: {len(professors)})")
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
        page += 1
        time.sleep(0.5)

    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        INSERT INTO professors (id, school_id, first_name, last_name, department, avg_rating, avg_difficulty, num_ratings, would_take_again)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            avg_rating = EXCLUDED.avg_rating,
            avg_difficulty = EXCLUDED.avg_difficulty,
            num_ratings = EXCLUDED.num_ratings,
            would_take_again = EXCLUDED.would_take_again,
            updated_at = CURRENT_TIMESTAMP
    """, [(
        p["id"], school["id"], p["firstName"], p["lastName"],
        p["department"], p["avgRating"], p["avgDifficulty"],
        p["numRatings"], p["wouldTakeAgainPercent"],
    ) for p in professors])
    conn.commit()
    cur.close()
    print(f"  Saved {len(professors)} professors")

    # Step 2: fetch all ratings
    print("Step 2: Fetching ratings...")
    ids = [p["id"] for p in professors if p["numRatings"] > 0]
    total_ratings = 0
    total_batches = (len(ids) + RATINGS_BATCH_SIZE - 1) // RATINGS_BATCH_SIZE

    for i in range(0, len(ids), RATINGS_BATCH_SIZE):
        batch_ids = ids[i:i + RATINGS_BATCH_SIZE]
        batch_num = (i // RATINGS_BATCH_SIZE) + 1

        start = time.time()
        data = fetch_ratings_batch(batch_ids)
        elapsed = time.time() - start

        if not data:
            print(f"  Batch {batch_num}/{total_batches}: SKIPPED after retries")
            continue

        ratings = []
        for key, val in data.get("data", {}).items():
            if not val or "ratings" not in val:
                continue
            prof_id = batch_ids[int(key[1:])]
            for e in val["ratings"]["edges"]:
                n = e["node"]
                attendance = n["attendanceMandatory"]
                ratings.append((
                    n["id"], prof_id, n["class"], n["date"], n["comment"],
                    n["clarityRating"], n["helpfulRating"], n["difficultyRating"],
                    n["grade"], n["wouldTakeAgain"], n["isForOnlineClass"],
                    attendance == "mandatory" if attendance else None,
                ))

        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ratings
            (id, professor_id, class, date, comment, clarity_rating, helpful_rating,
             difficulty_rating, grade, would_take_again, is_online, attendance_mandatory)
            VALUES %s
            ON CONFLICT (id) DO NOTHING
        """, ratings)
        conn.commit()
        cur.close()
        total_ratings += len(ratings)
        print(f"  Batch {batch_num}/{total_batches}: {len(ratings)} ratings in {elapsed:.2f}s (total: {total_ratings})")
        time.sleep(0.5)

    print(f"  Done: {len(professors)} professors, {total_ratings} ratings")

    # Step 3: compute derived columns per professor
    print("Step 3: Computing derived professor columns...")
    cur = conn.cursor()
    cur.execute("SELECT id FROM professors WHERE school_id = %s", (school["id"],))
    prof_ids = [row[0] for row in cur.fetchall()]
    cur.close()

    for prof_id in prof_ids:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT helpful_rating, difficulty_rating, grade, comment,
                   class, is_online, attendance_mandatory
            FROM ratings WHERE professor_id = %s
        """, (prof_id,))
        prof_ratings = cur.fetchall()
        cur.close()

        derived = compute_professor_derived(prof_ratings)

        cur = conn.cursor()
        cur.execute("""
            UPDATE professors SET
                grade_distribution        = %s,
                rating_distribution       = %s,
                difficulty_distribution   = %s,
                courses                   = %s,
                common_tags               = %s,
                attendance_mandatory_pct  = %s,
                online_pct                = %s
            WHERE id = %s
        """, (*derived, prof_id))
        conn.commit()
        cur.close()

    print(f"  Derived columns updated for {len(prof_ids)} professors")

conn.close()
print(f"\nAll schools complete!")
