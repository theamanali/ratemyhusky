import json
import re
import asyncio
import os
import time
import psycopg2
import psycopg2.extras
from playwright.async_api import async_playwright

BASE_URL = "https://www.washington.edu/cec"
TOC_URL = f"{BASE_URL}/toc.html"
LETTERS = list("abcdefghijklmnopqrstuvwxyz")
CONCURRENT_PAGES = 50
DB_URL = os.environ["DATABASE_URL"]


def get_existing_urls():
    conn = psycopg2.connect(DB_URL, sslmode="require")
    try:
        cur = conn.cursor()
        cur.execute("SELECT url FROM cec_evaluations_raw")
        urls = {row[0] for row in cur.fetchall()}
        cur.close()
    finally:
        conn.close()
    return urls


def save_to_db(results):
    if not results:
        return
    conn = psycopg2.connect(DB_URL, sslmode="require")
    try:
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO cec_evaluations_raw
            (url, course_name, course_code, section, instructor_name, title, quarter, form_type, surveyed, enrolled, questions)
            VALUES %s
            ON CONFLICT (url) DO UPDATE SET
                instructor_name = EXCLUDED.instructor_name,
                title = EXCLUDED.title,
                quarter = EXCLUDED.quarter,
                form_type = EXCLUDED.form_type,
                surveyed = EXCLUDED.surveyed,
                enrolled = EXCLUDED.enrolled,
                questions = EXCLUDED.questions
        """, [(
            r["url"], r["course_name"], r["course_code"], r["section"],
            r["instructor_name"], r["title"], r["quarter"], r["form_type"],
            r["surveyed"], r["enrolled"], json.dumps(r["questions"]),
        ) for r in results])
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _pct(s):
    s = s.strip().replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_url(url):
    filename = url.split("/")[-1].replace(".html", "")
    match = re.match(r"([A-Z]+\d+)([A-Z]+)(\d+)", filename)
    if not match:
        print(f"  Warning: could not parse URL: {url}")
        return None, None
    return match.group(1), match.group(2)


async def get_letter_links(page, letter):
    url = f"{BASE_URL}/{letter}-toc.html"
    await page.goto(url)
    await page.wait_for_load_state("networkidle")
    links = await page.eval_on_selector_all("a[href]", """
        els => els
            .map(el => ({href: el.href, text: el.innerText.trim()}))
            .filter(a => a.href.includes('/cec/') && !a.href.includes('toc')
                      && !a.href.includes('index') && !a.href.includes('search'))
    """)
    return links


async def parse_evaluation_page(page, url):
    await page.goto(url)
    await page.wait_for_load_state("networkidle")

    course_code, section = parse_url(url)

    h1 = (await page.inner_text("h1")).strip()
    h2 = (await page.inner_text("h2")).strip()

    h2_parts = [p.strip() for p in re.split(r'\s{2,}|\xa0{2,}', h2) if p.strip()]
    instructor_name = h2_parts[0] if len(h2_parts) > 0 else None
    title           = h2_parts[1] if len(h2_parts) > 1 else None
    quarter         = h2_parts[2] if len(h2_parts) > 2 else None

    caption = (await page.inner_text("caption")).strip()
    form_match     = re.match(r'(Form \w+:[^"]+)', caption)
    surveyed_match = re.search(r'"(\d+)"\s+surveyed', caption)
    enrolled_match = re.search(r'"(\d+)"\s+enrolled', caption)

    form_type = form_match.group(1).strip() if form_match else None
    surveyed  = int(surveyed_match.group(1)) if surveyed_match else None
    enrolled  = int(enrolled_match.group(1)) if enrolled_match else None

    questions = {}
    rows = await page.query_selector_all("table tr")
    for row in rows[1:]:
        cells = await row.query_selector_all("td")
        if len(cells) < 7:
            continue
        question = (await cells[0].inner_text()).strip().rstrip(":")
        try:
            questions[question] = {
                "excellent": _pct(await cells[1].inner_text()),
                "very_good": _pct(await cells[2].inner_text()),
                "good":      _pct(await cells[3].inner_text()),
                "fair":      _pct(await cells[4].inner_text()),
                "poor":      _pct(await cells[5].inner_text()),
                "very_poor": _pct(await cells[6].inner_text()),
                "median":    float((await cells[7].inner_text()).strip()) if len(cells) > 7 else None,
            }
        except (ValueError, IndexError):
            pass

    return {
        "url": url,
        "course_name": h1,
        "course_code": course_code,
        "section": section,
        "instructor_name": instructor_name,
        "title": title,
        "quarter": quarter,
        "form_type": form_type,
        "surveyed": surveyed,
        "enrolled": enrolled,
        "questions": questions,
    }


def fmt_time(seconds):
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m{secs:02d}s"


def fmt_eta(seconds):
    mins = seconds / 60
    if mins < 1:
        return "<1 min"
    return f"~{round(mins)} min"


def render_bar(completed, total, elapsed, width=40):
    pct = completed / total if total > 0 else 0
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    if 0 < completed < total:
        eta = (elapsed / completed) * (total - completed)
        eta_str = f" ETA {fmt_eta(eta)}"
    else:
        eta_str = ""
    return f"[{bar}] {completed:,}/{total:,} ({pct:.0%}) {fmt_time(elapsed)}{eta_str}"


async def scrape_all(context, links):
    total = len(links)
    completed = 0
    sem = asyncio.Semaphore(CONCURRENT_PAGES)
    start = time.monotonic()
    scraped = []
    failed = []

    print(render_bar(0, total, 0), end="", flush=True)

    async def scrape_one(href):
        nonlocal completed
        async with sem:
            eval_page = await context.new_page()
            try:
                result = await parse_evaluation_page(eval_page, href)
                scraped.append(result)
                completed += 1
            except Exception as e:
                failed.append((href, str(e)))
            finally:
                await eval_page.close()
            print(f"\r{render_bar(completed, total, time.monotonic() - start)}", end="", flush=True)

    await asyncio.gather(*[scrape_one(link["href"]) for link in links])
    elapsed = time.monotonic() - start
    avg = elapsed / total if total > 0 else 0
    print(f"\nDone in {fmt_time(elapsed)} — {avg:.2f}s avg per page")

    if failed:
        print(f"\n  {len(failed)} failed, retrying...")
        retry_urls = [url for url, _ in failed]
        failed.clear()
        await asyncio.gather(*[scrape_one(url) for url in retry_urls])
        retried = len(retry_urls)
        still_failed = len(failed)
        print(f"  Retried {retried} pages — {retried - still_failed} succeeded, {still_failed} still failed")

    if failed:
        print(f"\n{len(failed)} still failed:")
        for url, err in failed:
            print(f"  {url.split('/')[-1]}: {err}")

    print("Saving to DB...", end="", flush=True)
    save_to_db(scraped)
    print(f"\r{len(scraped):,} evaluations saved to DB")


async def main():
    print("Loading existing evaluations from DB...", end="", flush=True)
    existing_urls = get_existing_urls()
    print(f"\r{len(existing_urls):,} previously scraped evaluations loaded")

    async with async_playwright() as p:
        # Visible browser for manual UW NetID login
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(TOC_URL)
        print("\nBrowser opened. Please log in with your UW NetID...")

        # Wait for letter TOC links to appear — only visible when authenticated
        try:
            await page.wait_for_selector("a[href*='-toc.html']", timeout=300_000)
        except Exception:
            print("\nLogin timed out or failed. Closing browser.")
            await browser.close()
            return
        print("Authenticated! Switching to headless browser...")

        # Transfer session cookies to a headless browser
        cookies = await context.cookies()
        await browser.close()
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)

        # Scan phase
        print("\nScanning...", end="", flush=True)
        scan_pages = [await context.new_page() for _ in LETTERS]
        results_scan = await asyncio.gather(*[
            get_letter_links(scan_pages[i], letter)
            for i, letter in enumerate(LETTERS)
        ])
        await asyncio.gather(*[pg.close() for pg in scan_pages])

        letter_links = {letter: links for letter, links in zip(LETTERS, results_scan)}
        total_all = sum(len(v) for v in letter_links.values())
        total_new = sum(
            sum(1 for l in links if l["href"] not in existing_urls)
            for links in letter_links.values()
        )
        print(f"\r{total_all:,} evaluations found, {total_new:,} new to scrape\n")

        if total_new == 0:
            print("Nothing new to scrape.")
            await browser.close()
            return

        all_new_links = [
            link for letter in LETTERS
            for link in letter_links[letter]
            if link["href"] not in existing_urls
        ]

        await scrape_all(context, all_new_links)
        await browser.close()

    print(f"\nDone! {len(existing_urls) + total_new:,} total evaluations in DB")


if __name__ == "__main__":
    asyncio.run(main())
