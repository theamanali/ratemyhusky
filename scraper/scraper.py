import json
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
            id SERIAL PRIMARY KEY,
            rmp_id TEXT UNIQUE,
            name TEXT UNIQUE NOT NULL
        )
    """)
    db_cur.execute("""
        CREATE TABLE IF NOT EXISTS rmp_professors_raw (
            id TEXT PRIMARY KEY,
            school_id TEXT,
            first_name TEXT,
            last_name TEXT,
            department TEXT,
            avg_quality_rating REAL,
            avg_difficulty_rating REAL,
            rmp_rating_count INTEGER,
            would_take_again REAL,
            course_codes JSONB,
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
            quality_rating INTEGER,
            difficulty_rating INTEGER,
            grade TEXT,
            would_take_again BOOLEAN,
            is_online BOOLEAN,
            attendance_mandatory TEXT,
            textbook_used BOOLEAN,
            rating_tags TEXT
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
                        courseCodes {{ courseName courseCount }}
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
                wouldTakeAgainPercent
                teacherRatingTags {{ tagCount tagName }}
                ratings(first: 1000) {{
                    edges {{
                        node {{
                            id class date comment
                            qualityRating difficultyRating
                            grade wouldTakeAgain isForOnlineClass
                            attendanceMandatory textbookIsUsed ratingTags
                        }}
                    }}
                }}
            }}
        }}"""
        for idx, prof_id in enumerate(professor_ids)
    ])
    return post_with_retry(f"query {{ {aliases} }}")


def main():
    conn = psycopg2.connect(DB_URL, sslmode="require")
    init_db(conn)

    try:
        # Insert all schools upfront
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur,
            "INSERT INTO schools (rmp_id, name) VALUES %s ON CONFLICT (rmp_id) DO NOTHING",
            [(s["id"], s["name"]) for s in SCHOOLS]
        )
        conn.commit()
        cur.close()

        # Load all existing professors once
        cur = conn.cursor()
        cur.execute("SELECT id, rmp_rating_count FROM rmp_professors_raw")
        existing = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()

        all_professors = []
        all_ratings = []
        profs_to_fetch = []
        wta_updates = {}

        # Step 1: fetch professors for all schools
        print("Fetching professors...")
        for school in SCHOOLS:
            professors = []
            page_cursor = None
            page = 1
            while True:
                page_data = fetch_professors_page(school["id"], page_cursor)
                if not page_data:
                    print(f"  {school['name']}: failed to fetch page {page}, aborting")
                    break
                batch = [e["node"] for e in page_data["edges"]]
                professors.extend(batch)
                if not page_data["pageInfo"]["hasNextPage"]:
                    break
                page_cursor = page_data["pageInfo"]["endCursor"]
                page += 1
                time.sleep(0.5)

            new_prof_ids = {p["id"] for p in professors if p["id"] not in existing}
            rating_changed_ids = {
                p["id"] for p in professors
                if p["numRatings"] > 0 and (FORCE or p["numRatings"] != existing.get(p["id"]))
            }

            profs_to_upsert = new_prof_ids | rating_changed_ids
            all_professors.extend([(
                p["id"], school["id"], p["firstName"], p["lastName"],
                p["department"],
                p["avgRating"] if p["numRatings"] > 0 else None,
                p["avgDifficulty"] if p["numRatings"] > 0 else None,
                p["numRatings"], p["wouldTakeAgainPercent"] if p["wouldTakeAgainPercent"] != -1 else None,
                json.dumps(p.get("courseCodes")),
            ) for p in professors if p["id"] in profs_to_upsert])
            profs_to_fetch.extend(rating_changed_ids)

            print(f"  {school['name']}: {len(professors):,} professors ({page} pages) — {len(new_prof_ids)} new, {len(rating_changed_ids)} with new ratings")

        # Step 2: fetch ratings for all changed professors
        print("\nFetching ratings...")
        if not profs_to_fetch:
            print("  No new ratings")
        else:
            total_batches = (len(profs_to_fetch) + RATINGS_BATCH_SIZE - 1) // RATINGS_BATCH_SIZE
            for i in range(0, len(profs_to_fetch), RATINGS_BATCH_SIZE):
                batch_ids = profs_to_fetch[i:i + RATINGS_BATCH_SIZE]
                batch_num = (i // RATINGS_BATCH_SIZE) + 1
                batch_response = fetch_ratings_batch(batch_ids)

                if not batch_response:
                    print(f"  Batch {batch_num}/{total_batches}: SKIPPED after retries")
                    continue

                batch_ratings = 0
                for alias, teacher in batch_response.get("data", {}).items():
                    if not teacher or "ratings" not in teacher:
                        continue
                    alias_idx = int(alias[1:])
                    if alias_idx >= len(batch_ids):
                        continue
                    batch_prof_id = batch_ids[alias_idx]
                    wta = teacher.get("wouldTakeAgainPercent")
                    if wta is not None and wta != -1:
                        wta_updates[batch_prof_id] = wta
                    for edge in teacher["ratings"]["edges"]:
                        node = edge["node"]
                        wta = node["wouldTakeAgain"]
                        all_ratings.append((
                            node["id"], batch_prof_id, node["class"], node["date"], node["comment"],
                            node["qualityRating"], node["difficultyRating"],
                            node["grade"], None if wta is None else bool(wta), node["isForOnlineClass"],
                            node.get("attendanceMandatory"), node.get("textbookIsUsed"), node.get("ratingTags"),
                        ))
                        batch_ratings += 1

                print(f"  Batch {batch_num}/{total_batches}: {batch_ratings:,} ratings")
                time.sleep(0.5)

        # Step 3: write to DB
        print("\nSaving to DB...")
        if all_professors:
            cur = conn.cursor()
            psycopg2.extras.execute_values(cur, """
                INSERT INTO rmp_professors_raw (id, school_id, first_name, last_name, department, avg_quality_rating, avg_difficulty_rating, rmp_rating_count, would_take_again, course_codes)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    avg_quality_rating = EXCLUDED.avg_quality_rating,
                    avg_difficulty_rating = EXCLUDED.avg_difficulty_rating,
                    rmp_rating_count = EXCLUDED.rmp_rating_count,
                    would_take_again = EXCLUDED.would_take_again,
                    course_codes = EXCLUDED.course_codes,
                    updated_at = CURRENT_TIMESTAMP
            """, all_professors)
            conn.commit()
            cur.close()
        print(f"  {len(all_professors):,} new/updated professors")

        if all_ratings:
            cur = conn.cursor()
            psycopg2.extras.execute_values(cur, """
                INSERT INTO rmp_ratings_raw
                (id, professor_id, class, date, comment, quality_rating,
                 difficulty_rating, grade, would_take_again, is_online,
                 attendance_mandatory, textbook_used, rating_tags)
                VALUES %s
                ON CONFLICT (id) DO NOTHING
            """, all_ratings)
            conn.commit()
            cur.close()
        print(f"  {len(all_ratings):,} new ratings")

        if wta_updates:
            cur = conn.cursor()
            psycopg2.extras.execute_values(cur, """
                UPDATE rmp_professors_raw SET would_take_again = data.pct
                FROM (VALUES %s) AS data(id, pct)
                WHERE rmp_professors_raw.id = data.id
            """, [(pid, pct) for pid, pct in wta_updates.items()])
            conn.commit()
            cur.close()
        print(f"  {len(wta_updates):,} would_take_again values updated")


    finally:
        conn.close()

    print(f"\nAll schools complete!")


if __name__ == "__main__":
    main()
