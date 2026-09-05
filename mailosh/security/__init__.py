"""Security primitives for the webmail app (design spec §9): Fernet
encryption for stored secrets (`crypto.py`), CSRF validation (`csrf.py`),
Postgres-backed login rate limiting (`ratelimit.py`), and DB-backed
sessions (`sessions.py`).

Pure library code — nothing here talks HTTP, holds a FastAPI dependency,
or calls Stalwart directly; Task 5 wires these into routes and adds the
password -> per-session API-key exchange (`exchange.py`).
"""
