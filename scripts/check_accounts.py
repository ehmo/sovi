#!/usr/bin/env python3
"""List all email accounts in DB."""
import sys
sys.path.insert(0, "src")
from sovi.db import sync_execute
from sovi.crypto import decrypt

rows = sync_execute(
    "SELECT provider, email, password, domain, status, created_at FROM email_accounts ORDER BY created_at DESC"
)
print(f"Total accounts: {len(rows)}")
for r in rows:
    try:
        em = decrypt(r["email"])
    except Exception:
        em = "(decrypt fail)"
    try:
        pw = decrypt(r["password"])
        pw_display = pw[:3] + "***" if len(pw) > 3 else "***"
    except Exception:
        pw_display = "(decrypt fail)"
    print(f"{r['status']:10s} {r['provider']:8s} {em:45s} {pw_display:20s} {r['domain']}")
