"""Optional passwords on QR uploads: then the code alone does not open them."""

import hashlib
import hmac
import secrets
import unicodedata

MIN_LENGTH = 8
MAX_LENGTH = 128
# scrypt with 16 MB of memory per guess: slow to brute-force even if the database leaks.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def normalize(password: str) -> str:
    # The same letters typed on different devices may come in different Unicode forms.
    return unicodedata.normalize("NFC", password)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(normalize(password).encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    actual = hashlib.scrypt(normalize(password).encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(actual.hex(), digest)


def file_key(stored: str, upload_id: str) -> str:
    """Opens the files once the password was given: download links cannot carry headers.

    Derived from the secret hash, so only someone who passed the password check learns it.
    """
    return hmac.new(stored.encode(), upload_id.encode(), hashlib.sha256).hexdigest()
