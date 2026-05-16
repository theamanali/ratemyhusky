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
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_professors_name_trgm
        ON professors
        USING GIST (
            (REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\\s+', ' ', 'g'))
            gist_trgm_ops
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
        SELECT REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\\s+', ' ', 'g') as full_name,
               array_agg(id ORDER BY num_ratings DESC) as ids,
               array_agg(school_id ORDER BY num_ratings DESC) as school_ids,
               array_agg(department ORDER BY num_ratings DESC) as departments,
               array_agg(avg_rating ORDER BY num_ratings DESC) as avg_ratings,
               array_agg(avg_difficulty ORDER BY num_ratings DESC) as avg_diffs,
               array_agg(would_take_again ORDER BY num_ratings DESC) as wtas,
               array_agg(num_ratings ORDER BY num_ratings DESC) as num_ratings_list
        FROM rmp_professors_raw
        GROUP BY REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\\s+', ' ', 'g')
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

    same_school_groups = [g for g in dup_groups if len(set(g["school_ids"])) == 1]
    cross_campus_groups = [g for g in dup_groups if len(set(g["school_ids"])) > 1]
    same_school_total = sum(len(g["ids"]) for g in same_school_groups)
    cross_campus_total = sum(len(g["ids"]) for g in cross_campus_groups)
    same_school = len(same_school_groups)
    cross_campus = len(cross_campus_groups)
    print(f"  Same-school:  {same_school_total} profiles combined into {same_school} professors")
    print(f"  Cross-campus: {cross_campus_total} profiles combined into {cross_campus} professors")
    print(f"  Total: {len(loser_ids_set)} duplicates removed, {same_school + cross_campus} professors merged")
    print(f"  RMP professors after dedup: {rmp_prof_count - len(loser_ids_set):,}")

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
    late_merges = 0
    new_profs = 0
    cec_name_to_prof_id = {}

    plain_cur = conn.cursor()

    # Phase 1: exact matches via Python dict lookup — batch UPDATE
    unmatched_names = []
    exact_update_rows = []
    for name in cec_names:
        normalized = ' '.join(name.strip().lower().split())
        matches = rmp_lookup.get(normalized, [])
        if len(matches) == 1:
            cec_name_to_prof_id[name] = matches[0]
            exact_update_rows.append((name, matches[0]))
            exact_matches += 1
        else:
            unmatched_names.append(name)

    if exact_update_rows:
        psycopg2.extras.execute_values(plain_cur, """
            UPDATE professors SET cec_id = data.cec_name, source = 'both'
            FROM (VALUES %s) AS data(cec_name, prof_id)
            WHERE professors.id = data.prof_id::integer
        """, exact_update_rows)

    # Phase 2: single LATERAL fuzzy query for all unmatched names
    fuzzy_by_name = {}
    if unmatched_names:
        plain_cur.execute("""
            WITH cec AS (SELECT unnest(%s::text[]) AS name)
            SELECT c.name AS cec_name, p.id AS prof_id,
                   similarity(
                       REGEXP_REPLACE(LOWER(TRIM(p.first_name || ' ' || p.last_name)), '\\s+', ' ', 'g'),
                       LOWER(c.name)
                   ) AS sim
            FROM cec c
            CROSS JOIN LATERAL (
                SELECT id, first_name, last_name
                FROM professors
                WHERE rmp_id IS NOT NULL
                  AND similarity(
                      REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\\s+', ' ', 'g'),
                      LOWER(c.name)
                  ) > 0.7
                ORDER BY similarity(
                    REGEXP_REPLACE(LOWER(TRIM(first_name || ' ' || last_name)), '\\s+', ' ', 'g'),
                    LOWER(c.name)
                ) DESC
                LIMIT 5
            ) p
            ORDER BY cec_name, sim DESC
        """, (unmatched_names,))
        for row in plain_cur.fetchall():
            fuzzy_by_name.setdefault(row[0], []).append((row[1], row[2]))

    # Phase 3: process fuzzy results — collect batches for writes
    fuzzy_update_rows = []
    new_prof_rows = []

    for name in unmatched_names:
        candidates = fuzzy_by_name.get(name, [])

        if len(candidates) == 1:
            prof_id = candidates[0][0]
            cec_name_to_prof_id[name] = prof_id
            fuzzy_update_rows.append((name, prof_id))
            fuzzy_matches += 1

        elif len(candidates) > 1:
            winner_id = candidates[0][0]
            loser_ids = [c[0] for c in candidates[1:]]

            # Check inter-match similarity — similar losers are the same person
            similar_loser_ids = []
            for loser_id in loser_ids:
                plain_cur.execute("""
                    SELECT similarity(
                        REGEXP_REPLACE(LOWER(TRIM(p1.first_name || ' ' || p1.last_name)), '\\s+', ' ', 'g'),
                        REGEXP_REPLACE(LOWER(TRIM(p2.first_name || ' ' || p2.last_name)), '\\s+', ' ', 'g')
                    )
                    FROM professors p1, professors p2
                    WHERE p1.id = %s AND p2.id = %s
                """, (winner_id, loser_id))
                if plain_cur.fetchone()[0] > 0.7:
                    similar_loser_ids.append(loser_id)

            if similar_loser_ids:
                all_ids = [winner_id] + similar_loser_ids
                plain_cur.execute(
                    "SELECT rmp_id, avg_rating, avg_difficulty, would_take_again, num_ratings FROM professors WHERE id = ANY(%s)",
                    (all_ids,)
                )
                prof_rows = plain_cur.fetchall()
                combined_ratings = [
                    r for row in prof_rows if row[0]
                    for r in ratings_by_prof.get(row[0], [])
                ]
                grade_dist, rating_dist, diff_dist, courses_json = compute_derived(combined_ratings)
                num_ratings_list = [row[4] for row in prof_rows]
                total_n = sum(n for n in num_ratings_list if n)
                plain_cur.execute("""
                    UPDATE professors SET
                        avg_rating = %s, avg_difficulty = %s, num_ratings = %s,
                        would_take_again = %s, grade_distribution = %s,
                        rating_distribution = %s, difficulty_distribution = %s,
                        courses = %s, cec_id = %s, source = 'both'
                    WHERE id = %s
                """, (
                    weighted([row[1] for row in prof_rows], num_ratings_list),
                    weighted([row[2] for row in prof_rows], num_ratings_list),
                    total_n,
                    weighted([row[3] for row in prof_rows], num_ratings_list),
                    grade_dist, rating_dist, diff_dist, courses_json,
                    name, winner_id,
                ))
                plain_cur.execute("DELETE FROM professors WHERE id = ANY(%s)", (similar_loser_ids,))
                loser_set = set(similar_loser_ids)
                cec_name_to_prof_id = {
                    k: winner_id if v in loser_set else v
                    for k, v in cec_name_to_prof_id.items()
                }
                cec_name_to_prof_id[name] = winner_id
                late_merges += 1
                continue

            # Ambiguous — no confident match, fall through to new professor
            parts = name.strip().split()
            new_prof_rows.append((name, parts[0] if parts else name, parts[-1] if len(parts) > 1 else None))

        else:
            # No match at all
            parts = name.strip().split()
            new_prof_rows.append((name, parts[0] if parts else name, parts[-1] if len(parts) > 1 else None))

    # Phase 4: batched writes
    if fuzzy_update_rows:
        psycopg2.extras.execute_values(plain_cur, """
            UPDATE professors SET cec_id = data.cec_name, source = 'both'
            FROM (VALUES %s) AS data(cec_name, prof_id)
            WHERE professors.id = data.prof_id::integer
        """, fuzzy_update_rows)

    if new_prof_rows:
        returned = psycopg2.extras.execute_values(plain_cur, """
            INSERT INTO professors (rmp_id, cec_id, first_name, last_name, school,
                avg_rating, avg_difficulty, num_ratings, source)
            VALUES %s
            RETURNING id, cec_id
        """, new_prof_rows, template="(NULL, %s, %s, %s, NULL, NULL, NULL, 0, 'cec')", fetch=True)
        for row in returned:
            cec_name_to_prof_id[row[1]] = row[0]
            new_profs += 1

    plain_cur.close()
    print(f"  Exact matches:  {exact_matches:,}")
    print(f"  Fuzzy matches:  {fuzzy_matches:,}")
    print(f"  Late merges:    {late_merges:,}")
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
