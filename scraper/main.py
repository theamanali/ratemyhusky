import argparse
import asyncio
import os
import sys
import requests
from dotenv import load_dotenv
import rmp_scraper
import cleaner

load_dotenv()


def notify(message, title=None, priority="default", tags=None):
    url = os.environ.get("NTFY_URL")
    topic = os.environ.get("NTFY_TOPIC")
    token = os.environ.get("NTFY_TOKEN")
    if not url or not topic or not token:
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Title": title or "RateMyHusky",
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        requests.post(f"{url}/{topic}", data=message, headers=headers, timeout=5)
    except Exception:
        pass


def run_step(name, fn, *args, **kwargs):
    print("=" * 60)
    print(f"{name}")
    print("=" * 60)
    notify(f"{name} started", tags=["hourglass_flowing_sand"])
    try:
        fn(*args, **kwargs)
        notify(f"{name} completed successfully", tags=["white_check_mark"])
    except Exception as e:
        notify(f"{name} failed: {e}", priority="high", tags=["x"])
        raise


def parse_args():
    parser = argparse.ArgumentParser(description="RateMyHusky scraper pipeline")
    parser.add_argument("--rmp",   action="store_true", help="Run RMP scraper only")
    parser.add_argument("--cec",   action="store_true", help="Run CEC scraper only (requires interactive login)")
    parser.add_argument("--clean", action="store_true", help="Run cleaner only")
    parser.add_argument("--force", action="store_true", help="Pass --force to RMP scraper")
    return parser.parse_args()


def main():
    args = parse_args()
    run_all = not args.rmp and not args.cec and not args.clean

    if args.force:
        sys.argv.append("--force")

    if args.rmp or run_all:
        run_step("RMP scraper", rmp_scraper.main)

    if args.cec or run_all:
        import cec_scraper
        run_step("CEC scraper", lambda: asyncio.run(cec_scraper.main()))

    if args.clean or run_all:
        run_step("Cleaner", cleaner.main)


if __name__ == "__main__":
    main()
