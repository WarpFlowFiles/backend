import re
import secrets
import sqlite3

CODE_PATTERN = re.compile(r"[0-9]{6}")
MAX_ATTEMPTS = 20


class NoFreeCodeError(Exception):
    pass


def is_valid_code(code: str) -> bool:
    return CODE_PATTERN.fullmatch(code) is not None


def generate_unique_code(conn: sqlite3.Connection) -> str:
    """Picks a random 6-digit code not used by any stored upload (expired ones included)."""
    for _ in range(MAX_ATTEMPTS):
        code = f"{secrets.randbelow(10**6):06d}"
        if conn.execute("SELECT 1 FROM uploads WHERE code = ?", (code,)).fetchone() is None:
            return code
    raise NoFreeCodeError
