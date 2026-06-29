import smtplib, os

print("=" * 50)
print("  SMTP EMAIL TEST")
print("=" * 50)

# ── Step 1: Read config ───────────────────────────────
print("\nSTEP 1 — Reading config...")

host     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
port     = int(os.environ.get("SMTP_PORT", "587"))
user     = os.environ.get("SMTP_USER", "")
password = os.environ.get("SMTP_PASS", "")

print(f"  SMTP_HOST : {host}")
print(f"  SMTP_PORT : {port}")
print(f"  SMTP_USER : {user if user else '(NOT SET)'}")
print(f"  SMTP_PASS : {'*' * len(password) if password else '(NOT SET)'} ({len(password)} chars)")

if not user:
    print("\n  ERROR: SMTP_USER is not set.")
    print("  Run:  set SMTP_USER=your@gmail.com")
    print("  Then run this script again in the SAME window.")
    input("\nPress Enter to exit...")
    exit()

if not password:
    print("\n  ERROR: SMTP_PASS is not set.")
    print("  Run:  set SMTP_PASS=xxxx xxxx xxxx xxxx")
    print("  Then run this script again in the SAME window.")
    input("\nPress Enter to exit...")
    exit()

if len(password) < 16:
    print(f"\n  WARNING: SMTP_PASS is only {len(password)} chars.")
    print("  A Gmail App Password should be 16 characters.")
    print("  Make sure you copied it correctly (spaces are OK).")

# ── Step 2: Get recipient ─────────────────────────────
print("\nSTEP 2 — Who to send the test email to?")
to_email = input("  Enter recipient email: ").strip()

if not to_email or "@" not in to_email:
    print("  ERROR: Invalid email address.")
    input("\nPress Enter to exit...")
    exit()

# ── Step 3: Connect ───────────────────────────────────
print(f"\nSTEP 3 — Connecting to {host}:{port}...")
try:
    server = smtplib.SMTP(host, port, timeout=10)
    server.ehlo()
    print("  OK — Connected.")
except Exception as e:
    print(f"  FAILED: {e}")
    print("\n  Possible causes:")
    print("  - No internet connection")
    print("  - Firewall blocking port 587")
    input("\nPress Enter to exit...")
    exit()

# ── Step 4: Start TLS ─────────────────────────────────
print("\nSTEP 4 — Starting TLS encryption...")
try:
    server.starttls()
    print("  OK — TLS started.")
except Exception as e:
    print(f"  FAILED: {e}")
    input("\nPress Enter to exit...")
    exit()

# ── Step 5: Login ─────────────────────────────────────
print(f"\nSTEP 5 — Logging in as {user}...")
try:
    server.login(user, password)
    print("  OK — Login successful.")
except smtplib.SMTPAuthenticationError:
    print("  FAILED: Wrong email or password.")
    print("\n  If you are using Gmail:")
    print("  - You need an APP PASSWORD, not your Gmail password.")
    print("  - Go to: myaccount.google.com")
    print("  - Security > 2-Step Verification > App passwords")
    print("  - Create one for 'Mail' and use that 16-char code.")
    input("\nPress Enter to exit...")
    exit()
except Exception as e:
    print(f"  FAILED: {e}")
    input("\nPress Enter to exit...")
    exit()

# ── Step 6: Send ──────────────────────────────────────
print(f"\nSTEP 6 — Sending test email to {to_email}...")
try:
    message = f"Subject: Test Email from Engineering Office Storage\n\nThis is a test email.\nIf you received this, SMTP is working correctly."
    server.sendmail(user, to_email, message)
    server.quit()
    print("  OK — Email sent!")
except Exception as e:
    print(f"  FAILED: {e}")
    input("\nPress Enter to exit...")
    exit()

# ── Done ──────────────────────────────────────────────
print("\n" + "=" * 50)
print("  SUCCESS! Check the inbox of:")
print(f"  {to_email}")
print("  Also check the Spam / Junk folder.")
print("=" * 50)

input("\nPress Enter to exit...")