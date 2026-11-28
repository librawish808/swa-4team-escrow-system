"""Read-only check: run `python -m app.check_db` from backend/."""

from app.database import connect_db


def main():
    try:
        with connect_db() as db:
            db.execute("SELECT 1").fetchone()
            rows = db.execute(
                "SELECT id, status, version FROM public.transactions ORDER BY id LIMIT 5"
            ).fetchall()
    except Exception as exc:
        # Driver exception messages can contain connection details. Do not print them.
        print(f"Database check failed ({type(exc).__name__}). Check configuration/network/schema.")
        raise SystemExit(1) from None
    print("Database connection OK")
    for row in rows:
        print(f"id={row['id']} status={row['status']} version={row['version']}")


if __name__ == "__main__":
    main()
