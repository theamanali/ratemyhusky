import requests
import psycopg2
import psycopg2.extras
import time
import os
import json
import sys

FORCE = "--force" in sys.argv

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
                            grade wouldTakeAgain isForOnlineClass
                        }}
                    }}
                }}
            }}
        }}"""
        for i, pid in enumerate(professor_ids)
    ])
    return post_with_retry(f"query {{ {aliases} }}")

GRADE_KEYS = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F", "Rather not say", "Not sure yet"]

def compute_professor_derived(ratings):
    if not ratings:
        return (
            json.dumps({g: 0 for g in GRADE_KEYS}),
            json.dumps({"1":0,"2":0,"3":0,"4":0,"5":0}),
            json.dumps({"1":0,"2":0,"3":0,"4":0,"5":0}),
            json.dumps([]),
        )

    grade_counts = {g: 0 for g in GRADE_KEYS}
    rating_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    diff_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    course_counts = {}

    for r in ratings:
        g = r["grade"] if r["grade"] in grade_counts else None
        if g:
            grade_counts[g] += 1

        hr = r["helpful_rating"]
        if hr and 1 <= hr <= 5:
            rating_counts[str(hr)] += 1

        dr = r["difficulty_rating"]
        if dr and 1 <= dr <= 5:
            diff_counts[str(dr)] += 1

        c = (r["class"] or "").strip()
        if c:
            course_counts[c] = course_counts.get(c, 0) + 1

    courses_list = [{"code": k, "count": v} for k, v in course_counts.items()]

    return (
        json.dumps(grade_counts),
        json.dumps(rating_counts),
        json.dumps(diff_counts),
        json.dumps(courses_list),
    )

# --- Main ---
conn = psycopg2.connect(DB_URL, sslmode="require")
init_db(conn)

try:
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
            start = time.time()
            data = fetch_professors_page(school["id"], cursor)
            elapsed = time.time() - start
            if not data:
                print("  Failed to fetch page, aborting")
                break
            batch = [e["node"] for e in data["edges"]]
            professors.extend(batch)
            print(f"  Page {page}: {len(batch)} professors (total: {len(professors)}) in {elapsed:.2f}s")
            if not data["pageInfo"]["hasNextPage"]:
                break
            cursor = data["pageInfo"]["endCursor"]
            page += 1
            time.sleep(0.5)

        # Compare fetched professors against DB to find new/changed ones
        cur = conn.cursor()
        cur.execute(
            "SELECT id, num_ratings FROM professors WHERE id = ANY(%s)",
            ([p["id"] for p in professors],)
        )
        existing = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()

        new_prof_ids = {p["id"] for p in professors if p["id"] not in existing}
        if FORCE:
            changed_prof_ids = {p["id"] for p in professors if p["numRatings"] > 0}
        else:
            changed_prof_ids = {
                p["id"] for p in professors
                if p["numRatings"] > 0 and existing.get(p["id"]) != p["numRatings"]
            }
        any_changes = new_prof_ids or changed_prof_ids

        if any_changes:
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
                p["department"],
                p["avgRating"] if p["numRatings"] > 0 else -1,
                p["avgDifficulty"] if p["numRatings"] > 0 else -1,
                p["numRatings"], p["wouldTakeAgainPercent"],
            ) for p in professors])
            conn.commit()
            cur.close()
            print(f"  Done: Saved {len(new_prof_ids)} new professors, Found {len(changed_prof_ids)} professors with new ratings")
        else:
            print("  No new professors or ratings — skipping steps 2 and 3.")

        if any_changes:
            # Step 2: fetch ratings for professors with new ratings
            print("Step 2: Fetching ratings...")
            ids = [p["id"] for p in professors if p["id"] in changed_prof_ids]
            total_ratings = 0
            total_batches = (len(ids) + RATINGS_BATCH_SIZE - 1) // RATINGS_BATCH_SIZE

            for i in range(0, len(ids), RATINGS_BATCH_SIZE):
                batch_ids = ids[i:i + RATINGS_BATCH_SIZE]
                batch_num = (i // RATINGS_BATCH_SIZE) + 1

                start = time.time()
                data = fetch_ratings_batch(batch_ids)

                if not data:
                    print(f"  Batch {batch_num}/{total_batches}: SKIPPED after retries")
                    changed_prof_ids -= set(batch_ids)
                    continue

                ratings = []
                for key, val in data.get("data", {}).items():
                    if not val or "ratings" not in val:
                        continue
                    idx = int(key[1:])
                    if idx >= len(batch_ids):
                        continue
                    prof_id = batch_ids[idx]
                    for e in val["ratings"]["edges"]:
                        n = e["node"]
                        ratings.append((
                            n["id"], prof_id, n["class"], n["date"], n["comment"],
                            n["clarityRating"], n["helpfulRating"], n["difficultyRating"],
                            n["grade"], n["wouldTakeAgain"], n["isForOnlineClass"],
                        ))

                cur = conn.cursor()
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO ratings
                    (id, professor_id, class, date, comment, clarity_rating, helpful_rating,
                     difficulty_rating, grade, would_take_again, is_online)
                    VALUES %s
                    ON CONFLICT (id) DO NOTHING
                """, ratings)
                inserted = cur.rowcount
                conn.commit()
                cur.close()
                elapsed = time.time() - start
                total_ratings += inserted
                print(f"  Batch {batch_num}/{total_batches}: {inserted} new ratings in {elapsed:.2f}s (total: {total_ratings})")
                time.sleep(0.5)

            print(f"  Done: Inserted {total_ratings} new rating(s) across {len(changed_prof_ids)} professor(s)")

            if total_ratings == 0 and not FORCE:
                print("  No new ratings inserted — skipping step 3.")
            else:
                # Step 3: compute derived columns for changed professors
                print("Step 3: Computing derived professor columns...")
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT professor_id, helpful_rating, difficulty_rating, grade, class
                    FROM ratings WHERE professor_id = ANY(%s)
                """, (list(changed_prof_ids),))
                all_ratings = cur.fetchall()
                cur.close()

                ratings_by_prof = {}
                for r in all_ratings:
                    pid = r["professor_id"]
                    if pid not in ratings_by_prof:
                        ratings_by_prof[pid] = []
                    ratings_by_prof[pid].append(r)

                updates = []
                for prof_id in changed_prof_ids:
                    derived = compute_professor_derived(ratings_by_prof.get(prof_id, []))
                    updates.append((*derived, prof_id))

                cur = conn.cursor()
                psycopg2.extras.execute_values(cur, """
                    UPDATE professors SET
                        grade_distribution      = data.grade_distribution,
                        rating_distribution     = data.rating_distribution,
                        difficulty_distribution = data.difficulty_distribution,
                        courses                 = data.courses
                    FROM (VALUES %s) AS data(
                        grade_distribution, rating_distribution, difficulty_distribution,
                        courses, id
                    )
                    WHERE professors.id = data.id
                """, updates)
                conn.commit()
                cur.close()

                print(f"  Done: Recomputed derived stats for {len(changed_prof_ids)} professor(s)")

finally:
    conn.close()

print(f"\nAll schools complete!")
