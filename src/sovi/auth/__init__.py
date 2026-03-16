"""Authentication utilities: TOTP, email verification, SMS, CAPTCHA."""

import random
import string


def generate_password() -> str:
    """Generate a strong random password (16 chars).

    Ensures at least one uppercase, one lowercase, one digit, one symbol.
    """
    chars = string.ascii_letters + string.digits + "!@#$%"
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%"),
    ]
    pw.extend(random.choices(chars, k=12))
    random.shuffle(pw)
    return "".join(pw)
