#!/usr/bin/env python
"""CLI entry point for the automated/scheduled run.

Runs analysis over the 'scheduled' portfolio and prints the new run id.
Wire into cron, e.g. weekdays at 16:00 IST (after market close):

    0 16 * * 1-5  cd /path/to/stock-agent && .venv/bin/python run_scheduled.py >> data/cron.log 2>&1
"""
from app import db
from app.analysis import run_analysis


def main() -> None:
    db.init_db()
    run_id = run_analysis("scheduled")
    run = db.get_run(run_id)
    status = run["status"] if run else "unknown"
    print(f"scheduled run #{run_id} -> {status}")


if __name__ == "__main__":
    main()
