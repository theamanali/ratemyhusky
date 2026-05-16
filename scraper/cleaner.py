import json
import os
import re
import psycopg2
import psycopg2.extras
from collections import defaultdict

DB_URL = os.environ["DATABASE_URL"]

GRADE_KEYS = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F", "Rather not say", "Not sure yet"]
QUARTER_MAP = {"WI": ("Winter", 1), "SP": ("Spring", 3), "SU": ("Summer", 7), "AU": ("Autumn", 9)}
FUZZY_THRESHOLD = 0.7


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


def weighted(values, weights):
    pairs = [(v, w) for v, w in zip(values, weights) if v is not None and w]
    if not pairs:
        return None
    return round(sum(v * w for v, w in pairs) / sum(w for _, w in pairs), 2)


def norm_name(first, last):
    return ' '.join(((first or '') + ' ' + (last or '')).strip().lower().split())


def pg_trigrams(s):
    trgms = set()
    for word in re.split(r'[^a-z0-9]+', s):
        if not word:
            continue
        padded = '  ' + word + ' '
        for i in range(len(padded) - 2):
            trgms.add(padded[i:i+3])
    return frozenset(trgms)


def pg_similarity(ta, tb):
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union


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

    # ── READ PHASE ──
    print("Reading raw data...")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM rmp_professors_raw")
    rmp_profs_raw = cur.fetchall()
    cur.execute("SELECT professor_id, helpful_rating, difficulty_rating, grade, class FROM rmp_ratings_raw")
    all_ratings_raw = cur.fetchall()
    cur.execute("SELECT * FROM cec_evaluations_raw")
    cec_evals_raw = cur.fetchall()
    cur.execute("SELECT rmp_id, name FROM schools WHERE rmp_id IS NOT NULL")
    school_names = {r["rmp_id"]: r["name"] for r in cur.fetchall()}
    cur.close()
    print(f"  RMP: {len(rmp_profs_raw):,} professors, {len(all_ratings_raw):,} ratings")
    print(f"  CEC: {len(cec_evals_raw):,} evaluations")

    ratings_by_prof = defaultdict(list)
    for r in all_ratings_raw:
        ratings_by_prof[r["professor_id"]].append(r)

    # ── STEP 1: DEDUPLICATION ──
    print("\nDeduplicating RMP professors...")

    def combined_school_name(school_ids):
        names = sorted(set(school_names.get(sid, sid) for sid in school_ids if sid))
        if len(names) == 3:
            return "All campuses"
        return " and ".join(names)

    name_to_profs = defaultdict(list)
    for p in rmp_profs_raw:
        name_to_profs[norm_name(p["first_name"], p["last_name"])].append(p)

    loser_ids = set()
    winner_overrides = {}
    winner_to_all_rmp_ids = {}
    new_school_names = set()
    same_school_groups = []
    cross_campus_groups = []

    for name, profs in name_to_profs.items():
        if len(profs) <= 1:
            continue
        profs = sorted(profs, key=lambda p: p["num_ratings"] or 0, reverse=True)
        winner = profs[0]

        school_ids = [p["school_id"] for p in profs]
        if len(set(school_ids)) == 1:
            same_school_groups.append(profs)
        else:
            cross_campus_groups.append(profs)

        for loser in profs[1:]:
            loser_ids.add(loser["id"])

        winner_to_all_rmp_ids[winner["id"]] = [p["id"] for p in profs]

        num_ratings_list = [p["num_ratings"] for p in profs]
        school = combined_school_name(school_ids)
        new_school_names.add(school)
        unique_depts = list(dict.fromkeys(p["department"] for p in profs if p["department"]))

        winner_overrides[winner["id"]] = {
            "avg_rating":      weighted([p["avg_rating"] for p in profs], num_ratings_list),
            "avg_difficulty":  weighted([p["avg_difficulty"] for p in profs], num_ratings_list),
            "would_take_again": weighted([p["would_take_again"] for p in profs], num_ratings_list),
            "num_ratings":     sum(n for n in num_ratings_list if n),
            "school":          school,
            "departments":     json.dumps(unique_depts),
        }

    same_school_total = sum(len(g) for g in same_school_groups)
    cross_campus_total = sum(len(g) for g in cross_campus_groups)
    print(f"  Same-school:  {same_school_total} profiles combined into {len(same_school_groups)} professors")
    print(f"  Cross-campus: {cross_campus_total} profiles combined into {len(cross_campus_groups)} professors")
    print(f"  Total: {len(loser_ids)} duplicates removed, {len(same_school_groups) + len(cross_campus_groups)} professors merged")
    print(f"  RMP professors after dedup: {len(rmp_profs_raw) - len(loser_ids):,}")

    # ── STEP 2: BUILD RMP PROFESSORS ──
    print("\nBuilding professor data...")

    def get_all_ratings(rmp_id):
        all_ids = winner_to_all_rmp_ids.get(rmp_id, [rmp_id])
        return [r for pid in all_ids for r in ratings_by_prof.get(pid, [])]

    professors = []
    rmp_norm_to_prof = {}

    for p in rmp_profs_raw:
        if p["id"] in loser_ids:
            continue
        ov = winner_overrides.get(p["id"], {})
        ratings = get_all_ratings(p["id"])
        grade_dist, rating_dist, diff_dist, courses = compute_derived(ratings)
        prof = {
            "rmp_id":               p["id"],
            "cec_id":               None,
            "school":               ov.get("school", school_names.get(p["school_id"])),
            "first_name":           p["first_name"],
            "last_name":            p["last_name"],
            "departments":          ov.get("departments", json.dumps([p["department"]] if p["department"] else [])),
            "avg_rating":           ov.get("avg_rating", p["avg_rating"]),
            "avg_difficulty":       ov.get("avg_difficulty", p["avg_difficulty"]),
            "num_ratings":          ov.get("num_ratings", p["num_ratings"]),
            "would_take_again":     ov.get("would_take_again", p["would_take_again"]),
            "updated_at":           p["updated_at"],
            "grade_distribution":   grade_dist,
            "rating_distribution":  rating_dist,
            "difficulty_distribution": diff_dist,
            "courses":              courses,
            "title":                None,
            "source":               "rmp",
        }
        name = norm_name(p["first_name"], p["last_name"])
        rmp_norm_to_prof.setdefault(name, []).append(prof)
        professors.append(prof)

    print(f"  Built {len(professors):,} professors from RMP")

    # ── STEP 3: CEC MATCHING ──
    print("\nMatching CEC instructors to RMP professors...")

    # Precompute trigrams for all RMP professor names
    rmp_norm_trgms = {name: pg_trigrams(name) for name in rmp_norm_to_prof}

    cec_names = list({e["instructor_name"] for e in cec_evals_raw if e["instructor_name"]})
    cec_name_to_prof = {}
    exact_matches = fuzzy_matches = late_merges = new_profs = 0

    for cec_name in cec_names:
        normalized = ' '.join(cec_name.strip().lower().split())

        # Exact match
        profs = rmp_norm_to_prof.get(normalized, [])
        if len(profs) == 1:
            prof = profs[0]
            prof["cec_id"] = cec_name
            prof["source"] = "both"
            cec_name_to_prof[cec_name] = prof
            exact_matches += 1
            continue

        # Fuzzy match using precomputed trigrams
        cec_trgms = pg_trigrams(normalized)
        scored = sorted(
            [(name, pg_similarity(cec_trgms, trgms))
             for name, trgms in rmp_norm_trgms.items()
             if pg_similarity(cec_trgms, trgms) > FUZZY_THRESHOLD],
            key=lambda x: -x[1]
        )[:5]

        if len(scored) == 1:
            prof = rmp_norm_to_prof[scored[0][0]][0]
            prof["cec_id"] = cec_name
            prof["source"] = "both"
            cec_name_to_prof[cec_name] = prof
            fuzzy_matches += 1
            continue

        if len(scored) > 1:
            winner_name, _ = scored[0]
            winner_prof = rmp_norm_to_prof[winner_name][0]

            # Check inter-match similarity — similar candidates are the same person
            similar_losers = [
                rmp_norm_to_prof[name][0]
                for name, _ in scored[1:]
                if pg_similarity(rmp_norm_trgms[winner_name], rmp_norm_trgms[name]) > FUZZY_THRESHOLD
            ]

            if similar_losers:
                all_profs = [winner_prof] + similar_losers
                loser_rmp_ids = [p["rmp_id"] for p in similar_losers if p["rmp_id"]]
                combined_ratings = get_all_ratings(winner_prof["rmp_id"])
                for rmp_id in loser_rmp_ids:
                    combined_ratings.extend(ratings_by_prof.get(rmp_id, []))

                grade_dist, rating_dist, diff_dist, courses = compute_derived(combined_ratings)
                num_ratings_list = [p["num_ratings"] for p in all_profs]
                winner_prof.update({
                    "avg_rating":            weighted([p["avg_rating"] for p in all_profs], num_ratings_list),
                    "avg_difficulty":         weighted([p["avg_difficulty"] for p in all_profs], num_ratings_list),
                    "would_take_again":       weighted([p["would_take_again"] for p in all_profs], num_ratings_list),
                    "num_ratings":            sum(n for n in num_ratings_list if n),
                    "grade_distribution":     grade_dist,
                    "rating_distribution":    rating_dist,
                    "difficulty_distribution": diff_dist,
                    "courses":                courses,
                    "cec_id":                 cec_name,
                    "source":                 "both",
                })

                # Redirect stale mappings and remove losers
                for loser in similar_losers:
                    for k, v in list(cec_name_to_prof.items()):
                        if v is loser:
                            cec_name_to_prof[k] = winner_prof
                    loser["_merged"] = True
                    loser_name = norm_name(loser["first_name"], loser["last_name"])
                    rmp_norm_to_prof.pop(loser_name, None)
                    rmp_norm_trgms.pop(loser_name, None)

                cec_name_to_prof[cec_name] = winner_prof
                late_merges += 1
                continue

        # No match — new CEC-only professor
        parts = cec_name.strip().split()
        prof = {
            "rmp_id": None, "cec_id": cec_name,
            "school": None,
            "first_name": parts[0] if parts else cec_name,
            "last_name": parts[-1] if len(parts) > 1 else None,
            "departments": None,
            "avg_rating": None, "avg_difficulty": None,
            "num_ratings": 0, "would_take_again": None,
            "updated_at": None,
            "grade_distribution": None, "rating_distribution": None,
            "difficulty_distribution": None, "courses": None,
            "title": None, "source": "cec",
        }
        professors.append(prof)
        cec_name_to_prof[cec_name] = prof
        new_profs += 1

    # Remove merged losers
    professors = [p for p in professors if not p.get("_merged")]

    print(f"  Exact matches:  {exact_matches:,}")
    print(f"  Fuzzy matches:  {fuzzy_matches:,}")
    print(f"  Late merges:    {late_merges:,}")
    print(f"  New professors: {new_profs:,}")

    # ── STEP 4 & 5: CEC EVALUATIONS + TITLES ──
    print("\nBuilding CEC evaluations and computing titles...")

    cec_eval_rows = []
    for e in cec_evals_raw:
        instructor = e["instructor_name"]
        quarter, year = parse_quarter(e["quarter"])

        # Update professor title if this is their most recent entry
        if instructor and e["title"]:
            prof = cec_name_to_prof.get(instructor)
            if prof is not None:
                key = quarter_sort_key(quarter, year)
                if key > prof.get("_title_key", 0):
                    prof["_title_key"] = key
                    prof["title"] = e["title"]

        cec_eval_rows.append((
            instructor,
            e["url"], e["course_name"], e["course_code"], e["section"],
            instructor, e["title"], quarter, year, e["form_type"],
            e["surveyed"], e["enrolled"],
            json.dumps(e["questions"]) if isinstance(e["questions"], dict) else e["questions"],
        ))

    print(f"  Built {len(cec_eval_rows):,} evaluations")

    # ── WRITE PHASE ──
    print("\nWriting to database...")
    plain_cur = conn.cursor()

    plain_cur.execute("TRUNCATE professors, cec_evaluations RESTART IDENTITY CASCADE")

    # Insert combined school names
    combined_names = new_school_names - set(school_names.values())
    if combined_names:
        psycopg2.extras.execute_values(plain_cur,
            "INSERT INTO schools (rmp_id, name) VALUES %s ON CONFLICT (name) DO NOTHING",
            [(None, name) for name in combined_names]
        )

    # Insert professors
    returned = psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO professors
        (rmp_id, cec_id, school, first_name, last_name, departments, avg_rating, avg_difficulty,
         num_ratings, would_take_again, updated_at, grade_distribution, rating_distribution,
         difficulty_distribution, courses, title, source)
        VALUES %s
        RETURNING id, rmp_id, cec_id
    """, [(
        p["rmp_id"], p["cec_id"], p["school"], p["first_name"], p["last_name"],
        p["departments"], p["avg_rating"], p["avg_difficulty"],
        p["num_ratings"], p["would_take_again"], p["updated_at"],
        p["grade_distribution"], p["rating_distribution"],
        p["difficulty_distribution"], p["courses"], p["title"], p["source"],
    ) for p in professors], fetch=True)

    # Build lookup from returned serial ids
    rmp_id_to_serial = {rmp_id: sid for sid, rmp_id, cec_id in returned if rmp_id}
    cec_only_to_serial = {cec_id: sid for sid, rmp_id, cec_id in returned if not rmp_id and cec_id}

    # Map each CEC instructor name to its professor's serial id via cec_name_to_prof
    cec_name_to_serial = {}
    for cec_name, prof in cec_name_to_prof.items():
        if prof["rmp_id"]:
            sid = rmp_id_to_serial.get(prof["rmp_id"])
        else:
            sid = cec_only_to_serial.get(prof["cec_id"])
        if sid:
            cec_name_to_serial[cec_name] = sid

    print(f"  Inserted {len(professors):,} professors")

    # Insert CEC evaluations with resolved professor_ids
    resolved_evals = []
    for row in cec_eval_rows:
        instructor = row[0]
        prof_id = cec_name_to_serial.get(instructor) if instructor else None
        resolved_evals.append((prof_id,) + row[1:])

    psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO cec_evaluations
        (professor_id, url, course_name, course_code, section, instructor_name, title,
         quarter, year, form_type, surveyed, enrolled, questions)
        VALUES %s
        ON CONFLICT (url) DO UPDATE SET
            professor_id = EXCLUDED.professor_id,
            quarter = EXCLUDED.quarter,
            year = EXCLUDED.year
    """, resolved_evals)

    conn.commit()
    plain_cur.close()

    # ── SUMMARY ──
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) FROM professors")
    total = cur.fetchone()["count"]
    cur.execute("SELECT source, COUNT(*) FROM professors GROUP BY source ORDER BY source")
    by_source = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM cec_evaluations WHERE professor_id IS NOT NULL")
    linked = cur.fetchone()["count"]
    cur.close()

    titles_updated = sum(1 for p in professors if p["title"])

    print(f"\nDone.")
    print(f"  Total professors: {total:,}")
    for row in by_source:
        print(f"    {row['source']}: {row['count']:,}")
    print(f"  CEC evaluations linked: {linked:,}")
    print(f"  Titles set: {titles_updated:,}")

    conn.close()


if __name__ == "__main__":
    main()
