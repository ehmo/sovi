#!/usr/bin/env python3
"""Check persona and email account status in DB."""
import sys
sys.path.insert(0, "src")
from sovi.db import sync_execute

# First check what columns exist
pcols = sync_execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position", ("personas",))
print("personas columns:", [list(c.values())[0] for c in pcols])

ecols = sync_execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position", ("email_accounts",))
print("email_accounts columns:", [list(c.values())[0] for c in ecols])

personas = sync_execute("SELECT COUNT(*) as cnt FROM personas")
emails = sync_execute("SELECT COUNT(*) as cnt FROM email_accounts")

# Find the niche-like column
niche_col = None
for c in pcols:
    name = list(c.values())[0]
    if name in ("niche", "category", "vertical", "topic"):
        niche_col = name
        break

if niche_col:
    niches = sync_execute(f"SELECT {niche_col}, COUNT(*) as total FROM personas GROUP BY {niche_col} ORDER BY {niche_col}")
else:
    niches = []

p_count = list(personas[0].values())[0] if personas else 0
e_count = list(emails[0].values())[0] if emails else 0
print(f"Personas: {p_count}")
print(f"Email accounts: {e_count}")

print("\nBy niche:")
for n in niches:
    vals = list(n.values())
    print(f"  {vals[0]}: {vals[1]}")

# Check email_accounts schema
cols = sync_execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'email_accounts' ORDER BY ordinal_position")
print("\nemail_accounts columns:")
for c in cols:
    vals = list(c.values())
    print(f"  {vals[0]}: {vals[1]}")

# Check persona_id type in email_accounts
pid_type = sync_execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'email_accounts' AND column_name = 'persona_id'")
if pid_type:
    print(f"\npersona_id type: {list(pid_type[0].values())[0]}")

# Check persona id type
pid_type2 = sync_execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'personas' AND column_name = 'id'")
if pid_type2:
    print(f"personas.id type: {list(pid_type2[0].values())[0]}")

# Count personas without email (using correct cast)
try:
    no_email = sync_execute("SELECT COUNT(*) as cnt FROM personas p LEFT JOIN email_accounts ea ON ea.persona_id = p.id::text WHERE ea.id IS NULL")
    print(f"\nPersonas without email: {list(no_email[0].values())[0]}")
except Exception:
    try:
        no_email = sync_execute("SELECT COUNT(*) as cnt FROM personas p LEFT JOIN email_accounts ea ON ea.persona_id::uuid = p.id WHERE ea.id IS NULL")
        print(f"\nPersonas without email: {list(no_email[0].values())[0]}")
    except Exception as e:
        print(f"\nJoin failed: {e}")
