"""Password hashing for the user table."""

import hashlib
import hmac

MIN_LENGTH = 10


def hash_password(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def verify_password(password: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), stored_hash)


def password_is_strong(password: str) -> bool:
    has_digit = any(c.isdigit() for c in password)
    has_letter = any(c.isalpha() for c in password)
    return len(password) >= MIN_LENGTH and has_digit and has_letter
