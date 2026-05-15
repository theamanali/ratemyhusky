import os
import sys
import time
import psycopg2.extras
import requests

FORCE = "--force" in sys.argv

RMP_BASE_URL = "https://www.ratemyprofessors.com"
GRAPHQL_URL = f"{RMP_BASE_URL}/graphql"
HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{RMP_BASE_URL}/",
    "Origin": RMP_BASE_URL,
}
SCHOOLS = [
    {"id": "U2Nob29sLTE1MzA=", "name": "UW Seattle"},
    {"id": "U2Nob29sLTQ0NjY=", "name": "UW Bothell"},
    {"id": "U2Nob29sLTQ3NDQ=", "name": "UW Tacoma"},
]
PROF_BATCH_SIZE = 1000
RATINGS_BATCH_SIZE = 1000
MAX_RETRIES = 3
RETRY_DELAY = 2
DB_URL = os.environ["DATABASE_URL"]


def init_db(db_conn):
    db_cur = db_conn.cursor()
    db_cur.execute("""
        CREATE TABLE IF NOT EXISTS schools (
            id TEXT PRIMARY KEY,
            name TEXT
        )
    """)
    db_cur.execute("""
        CREATE TABLE IF NOT EXISTS rmp_professors_raw (
            id TEXT PRIMARY KEY,
            school_id TEXT,
            first_name TEXT,
            last_name TEXT,
            department TEXT,
            avg_rating REAL,
            avg_difficulty REAL,
            num_ratings INTEGER,
            would_take_again REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db_cur.execute("""
        CREATE TABLE IF NOT EXISTS rmp_ratings_raw (
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
            is_online BOOLEAN
        )
    """)
    db_conn.commit()
    db_cur.close()


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


def fetch_professors_page(school_id, page_cursor=None):
    after = f', after: "{page_cursor}"' if page_cursor else ""
    response = post_with_retry(f"""
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
    return response["data"]["newSearch"]["teachers"] if response else None


def fetch_ratings_batch(professor_ids):
    aliases = "\n".join([
        f"""p{idx}: node(id: "{prof_id}") {{
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
        for idx, prof_id in enumerate(professor_ids)
    ])
    return post_with_retry(f"query {{ {aliases} }}")


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
        page_cursor = None
        page = 1
        while True:
            start = time.time()
            page_data = fetch_professors_page(school["id"], page_cursor)
            elapsed = time.time() - start
            if not page_data:
                print("  Failed to fetch page, aborting")
                break
            batch = [e["node"] for e in page_data["edges"]]
            professors.extend(batch)
            print(f"  Page {page}: {len(batch)} professors (total: {len(professors)}) in {elapsed:.2f}s")
            if not page_data["pageInfo"]["hasNextPage"]:
                break
            page_cursor = page_data["pageInfo"]["endCursor"]
            page += 1
            time.sleep(0.5)

        # Compare fetched professors against DB to find new/changed ones
        cur = conn.cursor()
        cur.execute(
            "SELECT id, num_ratings FROM rmp_professors_raw WHERE id = ANY(%s)",
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
                if 0 < p["numRatings"] != existing.get(p["id"])
            }
        any_changes = new_prof_ids or changed_prof_ids

        if any_changes:
            cur = conn.cursor()
            psycopg2.extras.execute_values(cur, """
                INSERT INTO rmp_professors_raw (id, school_id, first_name, last_name, department, avg_rating, avg_difficulty, num_ratings, would_take_again)
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
                p["avgRating"] if p["numRatings"] > 0 else None,
                p["avgDifficulty"] if p["numRatings"] > 0 else None,
                p["numRatings"], p["wouldTakeAgainPercent"] if p["wouldTakeAgainPercent"] != -1 else None,
            ) for p in professors])
            conn.commit()
            cur.close()
            print(f"  Done: Saved {len(new_prof_ids)} new professors, Found {len(changed_prof_ids)} professors with new ratings")
        else:
            print("  No new professors or ratings — skipping step 2.")

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
                batch_response = fetch_ratings_batch(batch_ids)

                if not batch_response:
                    print(f"  Batch {batch_num}/{total_batches}: SKIPPED after retries")
                    changed_prof_ids -= set(batch_ids)
                    continue

                new_ratings = []
                for alias, teacher in batch_response.get("data", {}).items():
                    if not teacher or "ratings" not in teacher:
                        continue
                    alias_idx = int(alias[1:])
                    if alias_idx >= len(batch_ids):
                        continue
                    batch_prof_id = batch_ids[alias_idx]
                    for edge in teacher["ratings"]["edges"]:
                        node = edge["node"]
                        new_ratings.append((
                            node["id"], batch_prof_id, node["class"], node["date"], node["comment"],
                            node["clarityRating"], node["helpfulRating"], node["difficultyRating"],
                            node["grade"], node["wouldTakeAgain"], node["isForOnlineClass"],
                        ))

                cur = conn.cursor()
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO rmp_ratings_raw
                    (id, professor_id, class, date, comment, clarity_rating, helpful_rating,
                     difficulty_rating, grade, would_take_again, is_online)
                    VALUES %s
                    ON CONFLICT (id) DO NOTHING
                """, new_ratings)
                inserted = cur.rowcount
                conn.commit()
                cur.close()
                elapsed = time.time() - start
                total_ratings += inserted
                print(f"  Batch {batch_num}/{total_batches}: {inserted} new ratings in {elapsed:.2f}s (total: {total_ratings})")
                time.sleep(0.5)

            print(f"  Done: Inserted {total_ratings} new rating(s) across {len(changed_prof_ids)} professor(s)")

finally:
    conn.close()

print(f"\nAll schools complete!")
