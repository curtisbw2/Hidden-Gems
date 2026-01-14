"""Hashing utilities for privacy."""
import hashlib
import secrets
from typing import Tuple


def normalize_email(email: str) -> str:
    """Normalize email: strip whitespace, lowercase."""
    return email.strip().lower()


def hash_email(email: str) -> str:
    """Hash email using SHA-256."""
    normalized = normalize_email(email)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_otp_code(code: str) -> str:
    """Hash OTP code using SHA-256."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_otp_code() -> Tuple[str, str]:
    """Generate a 6-digit OTP code and return (code, hash)."""
    code = f"{secrets.randbelow(1000000):06d}"
    code_hash = hash_otp_code(code)
    return code, code_hash
