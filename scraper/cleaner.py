import json
import os
import re
import psycopg2
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]

GRADE_KEYS = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F", "Rather not say", "Not sure yet"]
QUARTER_MAP = {"WI": ("Winter", 1), "SP": ("Spring", 3), "SU": ("Summer", 7), "AU": ("Autumn", 9)}


def parse_quarter(raw):
    if not raw or len(raw) < 3:
        return None, None
    prefix = raw[:2].upper()
    try:
        year = 2000 + int(raw[2:])
    except ValueError:
        return None, None
    name, _ = QUARTER_MAP.get(prefix, (None, None))
    return name, year


def quarter_sort_key(quarter, year):
    if not quarter or not year:
        return 0
    order = {"Winter": 1, "Spring": 3, "Summer": 7, "Autumn": 9}
    return year * 100 + order.get(quarter, 0)


def compute_derived(ratings):
    grade_counts = {g: 0 for g in GRADE_KEYS}
    rating_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    diff_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    course_counts = {}

    for r in ratings:
        grade = r["grade"] if r["grade"] in grade_counts else None
        if grade:
            grade_counts[grade] += 1
        hr = r["helpful_rating"]
        if hr and 1 <= hr <= 5:
            rating_counts[str(hr)] += 1
        dr = r["difficulty_rating"]
        if dr and 1 <= dr <= 5:
            diff_counts[str(dr)] += 1
        course = (r["class"] or "").strip()
        if course:
            course_counts[course] = course_counts.get(course, 0) + 1

    return (
        json.dumps(grade_counts),
        json.dumps(rating_counts),
        json.dumps(diff_counts),
        json.dumps([{"code": k, "count": v} for k, v in course_counts.items()]),
    )


def init_db(conn):
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS professors (
            id SERIAL PRIMARY KEY,
            rmp_id TEXT UNIQUE,
            cec_id TEXT,
            school TEXT,
            first_name TEXT,
            last_name TEXT,
            departments JSONB,
            avg_rating REAL,
            avg_difficulty REAL,
            num_ratings INTEGER,
            would_take_again REAL,
            updated_at TIMESTAMP,
            grade_distribution JSONB,
            rating_distribution JSONB,
            difficulty_distribution JSONB,
            courses JSONB,
            title TEXT,
            source TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cec_evaluations (
            professor_id INTEGER,
            url TEXT PRIMARY KEY,
            course_name TEXT,
            course_code TEXT,
            section TEXT,
            instructor_name TEXT,
            title TEXT,
            quarter TEXT,
            year INTEGER,
            form_type TEXT,
            surveyed INTEGER,
            enrolled INTEGER,
            questions JSONB
        )
    """)
    conn.commit()
    cur.close()


def main():
    conn = psycopg2.connect(DB_URL, sslmode="require")
    conn.autocommit = False

    init_db(conn)

    # Full rebuild: truncate processed tables before repopulating from raw data
    plain_cur = conn.cursor()
    plain_cur.execute("TRUNCATE professors, cec_evaluations RESTART IDENTITY CASCADE")
    plain_cur.close()

    print("Reading raw data...")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) FROM rmp_professors_raw")
    rmp_prof_count = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM rmp_ratings_raw")
    rmp_rating_count = cur.fetchone()["count"]
    cur.execute("SELECT COUNT(*) FROM cec_evaluations_raw")
    cec_count = cur.fetchone()["count"]
    print(f"  RMP: {rmp_prof_count:,} professors, {rmp_rating_count:,} ratings")
    print(f"  CEC: {cec_count:,} evaluations")

    # ── Step 1: Find duplicate groups in memory (raw tables untouched) ──
    print("\nDeduplicating RMP professors...")

    # Load school id → name mapping
    cur.execute("SELECT rmp_id, name FROM schools WHERE rmp_id IS NOT NULL")
    school_names = {r["rmp_id"]: r["name"] for r in cur.fetchall()}

    # Group by name only (across all schools) to catch cross-campus duplicates
    cur.execute("""
        SELECT REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\s+', ' ', 'g') as full_name,
               array_agg(id ORDER BY num_ratings DESC) as ids,
               array_agg(school_id ORDER BY num_ratings DESC) as school_ids,
               array_agg(department ORDER BY num_ratings DESC) as departments,
               array_agg(avg_rating ORDER BY num_ratings DESC) as avg_ratings,
               array_agg(avg_difficulty ORDER BY num_ratings DESC) as avg_diffs,
               array_agg(would_take_again ORDER BY num_ratings DESC) as wtas,
               array_agg(num_ratings ORDER BY num_ratings DESC) as num_ratings_list
        FROM rmp_professors_raw
        GROUP BY REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\s+', ' ', 'g')
        HAVING COUNT(*) > 1
    """)
    dup_groups = cur.fetchall()

    def weighted(values, weights):
        pairs = [(v, w) for v, w in zip(values, weights) if v is not None and w]
        if not pairs:
            return None
        return round(sum(v * w for v, w in pairs) / sum(w for _, w in pairs), 2)

    def combined_school_name(school_ids):
        names = sorted(set(school_names.get(sid, sid) for sid in school_ids if sid))
        if len(names) == 3:
            return "All campuses"
        return " and ".join(names)

    winner_to_all_ids = {}
    winner_overrides = {}
    loser_ids_set = set()
    plain_cur = conn.cursor()

    for group in dup_groups:
        ids = group["ids"]
        winner_id = ids[0]
        winner_to_all_ids[winner_id] = ids
        loser_ids_set.update(ids[1:])

        total_n = sum(n for n in group["num_ratings_list"] if n)
        school = combined_school_name(group["school_ids"])

        # Ensure combined school exists in schools_new
        plain_cur.execute(
            "INSERT INTO schools (rmp_id, name) VALUES (NULL, %s) ON CONFLICT (name) DO NOTHING",
            (school,)
        )

        unique_depts = list(dict.fromkeys(d for d in group["departments"] if d))
        winner_overrides[winner_id] = {
            "avg_rating":     weighted(group["avg_ratings"], group["num_ratings_list"]),
            "avg_difficulty":  weighted(group["avg_diffs"], group["num_ratings_list"]),
            "would_take_again": weighted(group["wtas"], group["num_ratings_list"]),
            "num_ratings":    total_n,
            "school":         school,
            "departments":    json.dumps(unique_depts),
        }

    plain_cur.close()

    same_school = sum(1 for g in dup_groups if len(set(g["school_ids"])) == 1)
    cross_campus = len(dup_groups) - same_school
    print(f"  Same-school duplicates: {same_school} groups")
    print(f"  Cross-campus duplicates: {cross_campus} groups")
    print(f"  Total extra profiles removed: {len(loser_ids_set)}")

    # ── Step 2: Build professors from RMP ──
    print("\nBuilding master professors table from RMP...")
    cur.execute("SELECT * FROM rmp_professors_raw")
    rmp_profs = [p for p in cur.fetchall() if p["id"] not in loser_ids_set]

    # Build ratings lookup — include all IDs in each group
    cur.execute("SELECT professor_id, helpful_rating, difficulty_rating, grade, class FROM rmp_ratings_raw")
    all_ratings = cur.fetchall()
    ratings_by_prof = {}
    for r in all_ratings:
        pid = r["professor_id"]
        if pid not in ratings_by_prof:
            ratings_by_prof[pid] = []
        ratings_by_prof[pid].append(r)

    def get_all_ratings(prof_id):
        all_ids = winner_to_all_ids.get(prof_id, [prof_id])
        return [r for pid in all_ids for r in ratings_by_prof.get(pid, [])]

    def get_school(prof):
        if prof["id"] in winner_overrides:
            return winner_overrides[prof["id"]]["school"]
        return school_names.get(prof["school_id"])

    plain_cur = conn.cursor()
    psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO professors
        (rmp_id, school, first_name, last_name, departments, avg_rating, avg_difficulty,
         num_ratings, would_take_again, updated_at, grade_distribution, rating_distribution,
         difficulty_distribution, courses, source)
        VALUES %s
        ON CONFLICT (rmp_id) DO UPDATE SET
            school = EXCLUDED.school,
            departments = EXCLUDED.departments,
            avg_rating = EXCLUDED.avg_rating,
            avg_difficulty = EXCLUDED.avg_difficulty,
            num_ratings = EXCLUDED.num_ratings,
            would_take_again = EXCLUDED.would_take_again,
            updated_at = EXCLUDED.updated_at,
            grade_distribution = EXCLUDED.grade_distribution,
            rating_distribution = EXCLUDED.rating_distribution,
            difficulty_distribution = EXCLUDED.difficulty_distribution,
            courses = EXCLUDED.courses
    """, [(
        p["id"], get_school(p), p["first_name"], p["last_name"],
        winner_overrides.get(p["id"], {}).get("departments", json.dumps([p["department"]] if p["department"] else [])),
        winner_overrides.get(p["id"], {}).get("avg_rating", p["avg_rating"]),
        winner_overrides.get(p["id"], {}).get("avg_difficulty", p["avg_difficulty"]),
        winner_overrides.get(p["id"], {}).get("num_ratings", p["num_ratings"]),
        winner_overrides.get(p["id"], {}).get("would_take_again", p["would_take_again"]),
        p["updated_at"], *compute_derived(get_all_ratings(p["id"])), "rmp"
    ) for p in rmp_profs])
    plain_cur.close()
    print(f"  Inserted {len(rmp_profs):,} professors from RMP")

    # ── Step 3: Match CEC instructors to RMP professors ──
    print("\nMatching CEC instructors to RMP professors...")
    cur.execute("SELECT DISTINCT instructor_name FROM cec_evaluations_raw WHERE instructor_name IS NOT NULL")
    cec_names = [r["instructor_name"] for r in cur.fetchall()]

    # Build lookup: normalized RMP name → professor serial id
    cur.execute("SELECT id, rmp_id, REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\\s+', ' ', 'g') as full_name FROM professors WHERE rmp_id IS NOT NULL")
    rmp_lookup = {}
    for r in cur.fetchall():
        name = r["full_name"]
        if name not in rmp_lookup:
            rmp_lookup[name] = []
        rmp_lookup[name].append(r["id"])

    exact_matches = 0
    fuzzy_matches = 0
    new_profs = 0
    cec_name_to_prof_id = {}

    plain_cur = conn.cursor()
    for name in cec_names:
        normalized = ' '.join(name.strip().lower().split())

        # Exact match
        matches = rmp_lookup.get(normalized, [])
        if len(matches) == 1:
            cec_name_to_prof_id[name] = matches[0]
            # Set cec_id on the matched professor
            plain_cur.execute(
                "UPDATE professors SET cec_id = %s, source = 'both' WHERE id = %s",
                (name, matches[0])
            )
            exact_matches += 1
            continue

        # Fuzzy match
        plain_cur.execute("""
            SELECT id, similarity(LOWER(first_name || ' ' || last_name), LOWER(%s)) AS sim
            FROM professors
            WHERE rmp_id IS NOT NULL
              AND similarity(LOWER(first_name || ' ' || last_name), LOWER(%s)) > 0.7
            ORDER BY sim DESC
            LIMIT 2
        """, (name, name))
        fuzzy = plain_cur.fetchall()
        if len(fuzzy) == 1:
            prof_id = fuzzy[0][0]
            cec_name_to_prof_id[name] = prof_id
            plain_cur.execute(
                "UPDATE professors SET cec_id = %s, source = 'both' WHERE id = %s",
                (name, prof_id)
            )
            fuzzy_matches += 1
            continue

        # No match — create new CEC-only professor
        parts = name.strip().split()
        first = parts[0] if parts else name
        last = parts[-1] if len(parts) > 1 else None
        plain_cur.execute("""
            INSERT INTO professors (rmp_id, cec_id, first_name, last_name, school,
                avg_rating, avg_difficulty, num_ratings, source)
            VALUES (NULL, %s, %s, %s, NULL, NULL, NULL, 0, 'cec')
            RETURNING id
        """, (name, first, last))
        row = plain_cur.fetchone()
        if row:
            cec_name_to_prof_id[name] = row[0]
            new_profs += 1

    plain_cur.close()
    print(f"  Exact matches:  {exact_matches:,}")
    print(f"  Fuzzy matches:  {fuzzy_matches:,}")
    print(f"  New professors: {new_profs:,}")

    # ── Step 4: Populate cec_evaluations ──
    print("\nPopulating cec_evaluations...")
    cur.execute("SELECT * FROM cec_evaluations_raw")
    raw_evals = cur.fetchall()

    plain_cur = conn.cursor()
    rows = []
    for e in raw_evals:
        instructor = e["instructor_name"]
        prof_id = cec_name_to_prof_id.get(instructor) if instructor else None
        quarter, year = parse_quarter(e["quarter"])
        rows.append((
            prof_id, e["url"], e["course_name"], e["course_code"], e["section"],
            instructor, e["title"], quarter, year, e["form_type"],
            e["surveyed"], e["enrolled"], json.dumps(e["questions"]) if isinstance(e["questions"], dict) else e["questions"],
        ))

    psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO cec_evaluations
        (professor_id, url, course_name, course_code, section, instructor_name, title,
         quarter, year, form_type, surveyed, enrolled, questions)
        VALUES %s
        ON CONFLICT (url) DO UPDATE SET
            professor_id = EXCLUDED.professor_id,
            quarter = EXCLUDED.quarter,
            year = EXCLUDED.year
    """, rows)
    plain_cur.close()
    print(f"  Linked {len(rows):,} evaluations")

    # ── Step 5: Update professor titles from most recent CEC entry ──
    print("\nUpdating professor titles...")
    cur.execute("""
        SELECT professor_id, instructor_name, title, quarter, year
        FROM cec_evaluations
        WHERE professor_id IS NOT NULL AND title IS NOT NULL
    """)
    title_rows = cur.fetchall()

    # Find most recent title per professor
    best = {}
    for r in title_rows:
        pid = r["professor_id"]
        key = quarter_sort_key(r["quarter"], r["year"])
        if pid not in best or key > best[pid][0]:
            best[pid] = (key, r["title"])

    plain_cur = conn.cursor()
    for prof_id, (_, title) in best.items():
        plain_cur.execute(
            "UPDATE professors SET title = %s WHERE id = %s",
            (title, prof_id)
        )
    plain_cur.close()
    print(f"  Updated titles for {len(best):,} professors")

    conn.commit()

    # ── Summary ──
    cur.execute("SELECT COUNT(*) FROM professors")
    total = cur.fetchone()["count"]
    cur.execute("SELECT source, COUNT(*) FROM professors GROUP BY source ORDER BY source")
    by_source = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM cec_evaluations WHERE professor_id IS NOT NULL")
    linked = cur.fetchone()["count"]

    print(f"\nDone.")
    print(f"  Total professors: {total:,}")
    for row in by_source:
        print(f"    {row['source']}: {row['count']:,}")
    print(f"  CEC evaluations linked: {linked:,}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
