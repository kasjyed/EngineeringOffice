from flask import (
    Flask, render_template, request,
    redirect, session, flash, send_from_directory, abort, jsonify, url_for, make_response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from functools import wraps
import os, secrets, smtplib, threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── App Setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sesf_prod_key_change_this")

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
TRASH_FOLDER  = os.path.join(BASE_DIR, "trash")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRASH_FOLDER,  exist_ok=True)

ALLOWED_EXTENSIONS  = {
    "pdf","doc","docx","xls","xlsx","ppt","pptx",
    "txt","csv","png","jpg","jpeg","dwg","dxf","zip","rar"
}
STORAGE_LIMIT_BYTES = 50 * 1024 * 1024 * 1024
MAX_FILE_BYTES      = 500 * 1024 * 1024
RECYCLE_DAYS        = 15

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
TRASH_FOLDER  = os.path.join(BASE_DIR, "trash")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRASH_FOLDER,  exist_ok=True)

# ── SMTP Config — read live from os.environ so set/run order doesn't matter ───
def SMTP_HOST():      return os.environ.get("SMTP_HOST",      "smtp.gmail.com")
def SMTP_PORT():      return int(os.environ.get("SMTP_PORT",  "587"))
def SMTP_USER():      return os.environ.get("SMTP_USER",      "")
def SMTP_PASS():      return os.environ.get("SMTP_PASS",      "")
def SMTP_FROM():      return os.environ.get("SMTP_FROM",      "") or os.environ.get("SMTP_USER", "")
def SMTP_FROM_NAME(): return os.environ.get("SMTP_FROM_NAME", "Engineering Office Storage")
def APP_BASE_URL():   return os.environ.get("APP_BASE_URL",   "http://localhost:5000").rstrip("/")

app.config["SQLALCHEMY_DATABASE_URI"]        = f"sqlite:///{os.path.join(BASE_DIR,'database.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"]                  = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"]             = MAX_FILE_BYTES

db = SQLAlchemy(app)

CATEGORIES = [
    "General","Design Drawing","Specification","Report",
    "Contract","Permit","As-Built","Inspection",
    "Survey","Correspondence","Other"
]

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id         = db.Column(db.Integer,     primary_key=True)
    fullname   = db.Column(db.String(100), nullable=False)
    email      = db.Column(db.String(100), unique=True, nullable=False)
    password   = db.Column(db.String(255), nullable=False)
    role       = db.Column(db.String(20),  default="Staff")
    department = db.Column(db.String(100), default="Engineering Office")
    created_at = db.Column(db.DateTime,   default=datetime.utcnow)
    is_active  = db.Column(db.Boolean,    default=True)

    documents = db.relationship("Document",    backref="uploader", lazy=True)
    logs      = db.relationship("ActivityLog", backref="actor",    lazy=True)


class Document(db.Model):
    id            = db.Column(db.Integer,     primary_key=True)
    filename      = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    category      = db.Column(db.String(100), default="General")
    description   = db.Column(db.String(500), default="")
    tags          = db.Column(db.String(300), default="")
    file_size     = db.Column(db.Integer,     default=0)
    file_ext      = db.Column(db.String(20),  default="")
    upload_date   = db.Column(db.DateTime,    default=datetime.utcnow)
    uploaded_by   = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False)
    is_pinned     = db.Column(db.Boolean,     default=False)

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()] if self.tags else []


class TrashedDocument(db.Model):
    id             = db.Column(db.Integer,     primary_key=True)
    filename       = db.Column(db.String(255), nullable=False)
    original_name  = db.Column(db.String(255), nullable=False)
    category       = db.Column(db.String(100), default="General")
    description    = db.Column(db.String(500), default="")
    tags           = db.Column(db.String(300), default="")
    file_size      = db.Column(db.Integer,     default=0)
    file_ext       = db.Column(db.String(20),  default="")
    upload_date    = db.Column(db.DateTime,    nullable=False)
    deleted_at     = db.Column(db.DateTime,    default=datetime.utcnow)
    deleted_by     = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False)
    original_owner = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False)

    deleter = db.relationship("User", foreign_keys=[deleted_by])
    owner   = db.relationship("User", foreign_keys=[original_owner])

    @property
    def expires_at(self):
        return self.deleted_at + timedelta(days=RECYCLE_DAYS)

    @property
    def days_remaining(self):
        return max(0, (self.expires_at - datetime.utcnow()).days)

    @property
    def is_expired(self):
        return datetime.utcnow() >= self.expires_at

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()] if self.tags else []


class ActivityLog(db.Model):
    id        = db.Column(db.Integer,     primary_key=True)
    action    = db.Column(db.String(255))
    detail    = db.Column(db.String(500), default="")
    timestamp = db.Column(db.DateTime,   default=datetime.utcnow)
    user_id   = db.Column(db.Integer,    db.ForeignKey("user.id"), nullable=False)


class SharedLink(db.Model):
    """
    visibility:
      'public'   — anyone with the link
      'private'  — password protected
      'email'    — specific email addresses only
    """
    id               = db.Column(db.Integer,     primary_key=True)
    token            = db.Column(db.String(64),  unique=True, nullable=False)
    document_id      = db.Column(db.Integer,     db.ForeignKey("document.id"), nullable=True)
    created_by       = db.Column(db.Integer,     db.ForeignKey("user.id"),     nullable=False)
    visibility       = db.Column(db.String(10),  default="public")
    password         = db.Column(db.String(255), nullable=True)
    allowed_emails   = db.Column(db.Text,        nullable=True)
    created_at       = db.Column(db.DateTime,    default=datetime.utcnow)
    expires_at       = db.Column(db.DateTime,    nullable=True)
    view_count       = db.Column(db.Integer,     default=0)
    download_count   = db.Column(db.Integer,     default=0)
    download_limit   = db.Column(db.Integer,     nullable=True)   # None = unlimited
    is_active        = db.Column(db.Boolean,     default=True)
    note             = db.Column(db.String(500), nullable=True)   # optional message to recipient

    document     = db.relationship("Document", backref="shared_links", foreign_keys=[document_id])
    sharer       = db.relationship("User",     foreign_keys=[created_by])
    transactions = db.relationship("ShareTransaction", backref="link", lazy=True,
                                   cascade="all, delete-orphan")

    @property
    def is_expired(self):
        return self.expires_at is not None and datetime.utcnow() >= self.expires_at

    @property
    def download_limit_reached(self):
        return self.download_limit is not None and self.download_count >= self.download_limit

    @property
    def is_usable(self):
        return self.is_active and not self.is_expired and not self.download_limit_reached

    @property
    def needs_password(self):
        return self.visibility == "private" and bool(self.password)

    @property
    def needs_email(self):
        return self.visibility == "email" and bool(self.allowed_emails)

    @property
    def email_list(self):
        if not self.allowed_emails:
            return []
        return [e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()]

    def email_allowed(self, email):
        return email.strip().lower() in self.email_list


class ShareTransaction(db.Model):
    """Tracks every email-based share send + recipient open/download events."""
    __tablename__ = "share_transaction"

    id              = db.Column(db.Integer,     primary_key=True)
    link_id         = db.Column(db.Integer,     db.ForeignKey("shared_link.id"), nullable=False)
    recipient_email = db.Column(db.String(200), nullable=False)
    sent_at         = db.Column(db.DateTime,    default=datetime.utcnow)
    delivered       = db.Column(db.Boolean,     default=False)   # SMTP accepted without error
    opened_at       = db.Column(db.DateTime,    nullable=True)   # first view after email
    downloaded_at   = db.Column(db.DateTime,    nullable=True)   # first download after email
    open_token      = db.Column(db.String(64),  unique=True, nullable=False)  # pixel / redirect token
    error_msg       = db.Column(db.String(500), nullable=True)

    @property
    def status(self):
        if self.downloaded_at: return "downloaded"
        if self.opened_at:     return "opened"
        if self.delivered:     return "delivered"
        return "failed" if self.error_msg else "pending"

    @property
    def status_icon(self):
        s = self.status
        return {
            "downloaded": ("bi-download-fill",    "text-success"),
            "opened":     ("bi-eye-fill",          "text-info"),
            "delivered":  ("bi-check2-all",        "text-primary"),
            "failed":     ("bi-x-circle-fill",     "text-danger"),
            "pending":    ("bi-hourglass-split",   "text-warning"),
        }.get(s, ("bi-question-circle", "text-muted"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(fn):
    return "." in fn and fn.rsplit(".",1)[1].lower() in ALLOWED_EXTENSIONS


def log_action(action, detail=""):
    if "user_id" in session:
        db.session.add(ActivityLog(action=action, detail=detail, user_id=session["user_id"]))
        db.session.commit()


def fmt_size(b):
    b = b or 0
    if b < 1024:       return f"{b} B"
    elif b < 1024**2:  return f"{b/1024:.1f} KB"
    elif b < 1024**3:  return f"{b/1024**2:.1f} MB"
    else:              return f"{b/1024**3:.2f} GB"

app.jinja_env.filters["fmt_size"] = fmt_size


def get_used_bytes():
    return db.session.query(db.func.sum(Document.file_size)).scalar() or 0


def storage_info():
    used  = get_used_bytes()
    limit = STORAGE_LIMIT_BYTES
    pct   = round((used / limit) * 100, 1)
    free  = limit - used
    user_usage = (
        db.session.query(User.id, User.fullname,
                         db.func.sum(Document.file_size).label("total"))
        .join(Document, Document.uploaded_by == User.id)
        .group_by(User.id)
        .order_by(db.text("total DESC"))
        .limit(8).all()
    )
    segments = []
    for row in user_usage:
        segments.append({
            "name": row.fullname,
            "bytes": row.total or 0,
            "pct": round(((row.total or 0) / limit) * 100, 2),
            "size_str": fmt_size(row.total or 0),
        })
    trash_count = TrashedDocument.query.count()
    trash_size  = db.session.query(db.func.sum(TrashedDocument.file_size)).scalar() or 0
    return {
        "used": used, "limit": limit, "free": free, "pct": pct,
        "used_str":  fmt_size(used),
        "free_str":  fmt_size(free),
        "limit_str": fmt_size(limit),
        "critical":  pct >= 90, "warning": pct >= 75,
        "segments":  segments,
        "trash_count": trash_count,
        "trash_size":  fmt_size(trash_size),
    }


def purge_expired_trash():
    expired = TrashedDocument.query.filter(
        TrashedDocument.deleted_at < datetime.utcnow() - timedelta(days=RECYCLE_DAYS)
    ).all()
    for item in expired:
        fp = os.path.join(TRASH_FOLDER, item.filename)
        if os.path.exists(fp):
            os.remove(fp)
        db.session.delete(item)
    if expired:
        db.session.commit()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to continue.", "warning")
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "Admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated


def ext_icon(ext):
    ext = (ext or "").lower().lstrip(".")
    m = {
        "pdf":"bi-file-earmark-pdf-fill","doc":"bi-file-earmark-word-fill",
        "docx":"bi-file-earmark-word-fill","xls":"bi-file-earmark-excel-fill",
        "xlsx":"bi-file-earmark-excel-fill","ppt":"bi-file-earmark-ppt-fill",
        "pptx":"bi-file-earmark-ppt-fill","png":"bi-file-earmark-image-fill",
        "jpg":"bi-file-earmark-image-fill","jpeg":"bi-file-earmark-image-fill",
        "txt":"bi-file-earmark-text-fill","csv":"bi-file-earmark-spreadsheet-fill",
        "zip":"bi-file-earmark-zip-fill","rar":"bi-file-earmark-zip-fill",
        "dwg":"bi-file-earmark-ruled-fill","dxf":"bi-file-earmark-ruled-fill",
    }
    return m.get(ext, "bi-file-earmark-fill")

app.jinja_env.globals["ext_icon"] = ext_icon

FILE_TYPE_GROUPS = {
    "documents":     {"label":"Documents",      "icon":"bi-file-earmark-text-fill",        "exts":{"pdf","doc","docx","txt"}},
    "spreadsheets":  {"label":"Spreadsheets",   "icon":"bi-file-earmark-spreadsheet-fill", "exts":{"xls","xlsx","csv"}},
    "presentations": {"label":"Presentations",  "icon":"bi-file-earmark-ppt-fill",         "exts":{"ppt","pptx"}},
    "media":         {"label":"Images & Media", "icon":"bi-file-earmark-image-fill",       "exts":{"png","jpg","jpeg","gif","mp4","mov","avi"}},
    "drawings":      {"label":"Drawings & CAD", "icon":"bi-file-earmark-ruled-fill",       "exts":{"dwg","dxf"}},
    "archives":      {"label":"Archives",       "icon":"bi-file-earmark-zip-fill",         "exts":{"zip","rar"}},
}

def file_type_group(ext):
    ext = (ext or "").lower().lstrip(".")
    for key, g in FILE_TYPE_GROUPS.items():
        if ext in g["exts"]: return key
    return "other"

app.jinja_env.globals["file_type_group"]  = file_type_group
app.jinja_env.globals["FILE_TYPE_GROUPS"] = FILE_TYPE_GROUPS

# ── SMTP Email ────────────────────────────────────────────────────────────────

def _send_email_worker(to_email, subject, html_body, txn_id):
    """Send email in background thread; update transaction status."""
    with app.app_context():
        txn = ShareTransaction.query.get(txn_id)
        if not txn:
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{SMTP_FROM_NAME()} <{SMTP_FROM()}>"
            msg["To"]      = to_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(SMTP_HOST(), SMTP_PORT()) as srv:
                srv.ehlo()
                srv.starttls()
                srv.login(SMTP_USER(), SMTP_PASS())
                srv.sendmail(SMTP_FROM(), [to_email], msg.as_string())

            txn.delivered  = True
            txn.error_msg  = None
        except Exception as exc:
            txn.delivered  = False
            txn.error_msg  = str(exc)[:490]
        db.session.commit()


def send_share_email_async(to_email, txn_id, link, doc, sender_name, note):
    """Build the share email and fire it in a background thread."""
    open_url     = f"{APP_BASE_URL()}/st/{txn_id}/open"
    download_url = f"{APP_BASE_URL()}/st/{txn_id}/download"
    share_url    = f"{APP_BASE_URL()}/s/{link.token}"

    expiry_line = ""
    if link.expires_at:
        expiry_line = f"<p style='color:#888;font-size:13px;'>⏱ Link expires on {link.expires_at.strftime('%B %d, %Y')}.</p>"

    limit_line = ""
    if link.download_limit:
        limit_line = f"<p style='color:#888;font-size:13px;'>⬇ Download limit: {link.download_limit} time(s).</p>"

    note_block = ""
    if note:
        note_block = f"""
        <div style="background:#f0f4ff;border-left:4px solid #4361ee;padding:12px 16px;margin:16px 0;border-radius:0 6px 6px 0;">
          <p style="margin:0;color:#2c3e50;font-size:14px;">{note}</p>
        </div>"""

    html = f"""
<!DOCTYPE html><html><body style="font-family:Inter,Arial,sans-serif;background:#f5f6fa;margin:0;padding:0;">
<div style="max-width:520px;margin:40px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">
  <div style="background:linear-gradient(135deg,#1a1f36 0%,#4361ee 100%);padding:32px 36px;">
    <p style="margin:0;color:#a0aec0;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Municipal Engineering Office</p>
    <h1 style="margin:8px 0 0;color:#fff;font-size:22px;font-weight:700;">File Shared With You</h1>
  </div>
  <div style="padding:32px 36px;">
    <p style="color:#2d3748;font-size:15px;margin-top:0;">
      <strong>{sender_name}</strong> shared a file with you from the Engineering Office Storage system.
    </p>
    {note_block}
    <div style="background:#f8f9ff;border:1px solid #e2e8f0;border-radius:8px;padding:16px 20px;margin:20px 0;">
      <p style="margin:0 0 4px;font-size:12px;color:#718096;text-transform:uppercase;letter-spacing:.5px;">File</p>
      <p style="margin:0;font-size:16px;font-weight:600;color:#1a1f36;">{doc.original_name}</p>
      <p style="margin:4px 0 0;font-size:13px;color:#718096;">{fmt_size(doc.file_size)} &nbsp;·&nbsp; {doc.category}</p>
    </div>
    {expiry_line}{limit_line}
    <a href="{open_url}" style="display:block;background:#4361ee;color:#fff;text-align:center;padding:14px 24px;border-radius:8px;text-decoration:none;font-size:15px;font-weight:600;margin:24px 0 12px;">View & Download File →</a>
    <p style="color:#a0aec0;font-size:12px;text-align:center;margin:0;">
      Or copy this link: <a href="{share_url}" style="color:#4361ee;">{share_url}</a>
    </p>
    <!-- tracking pixel -->
    <img src="{APP_BASE_URL()}/st/{txn_id}/pixel.gif" width="1" height="1" style="display:none;" alt="">
  </div>
  <div style="background:#f5f6fa;padding:16px 36px;border-top:1px solid #e2e8f0;">
    <p style="margin:0;color:#a0aec0;font-size:12px;">
      Sent via Engineering Office Storage &nbsp;·&nbsp; Sta. Lucia, Ilocos Sur<br>
      If you did not expect this file, you may ignore this email.
    </p>
  </div>
</div>
</body></html>"""

    subject = f"{sender_name} shared \"{doc.original_name}\" with you"
    t = threading.Thread(target=_send_email_worker, args=(to_email, subject, html, txn_id), daemon=True)
    t.start()


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def home(): return redirect("/login")


@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method == "POST":
        fullname = request.form["fullname"].strip()
        email    = request.form["email"].strip().lower()
        password = request.form["password"]
        confirm  = request.form.get("confirm_password","")
        if len(password) < 6:
            flash("Password must be at least 6 characters.","danger"); return redirect("/signup")
        if password != confirm:
            flash("Passwords do not match.","danger"); return redirect("/signup")
        if User.query.filter_by(email=email).first():
            flash("That email is already registered.","danger"); return redirect("/signup")
        db.session.add(User(fullname=fullname, email=email,
                            password=generate_password_hash(password)))
        db.session.commit()
        flash("Account created — please sign in.","success")
        return redirect("/login")
    return render_template("signup.html")


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email    = request.form["email"].strip().lower()
        password = request.form["password"]
        user     = User.query.filter_by(email=email).first()
        if user and user.is_active and check_password_hash(user.password, password):
            session["user_id"]  = user.id
            session["fullname"] = user.fullname
            session["role"]     = user.role
            purge_expired_trash()
            log_action("Signed in")
            return redirect("/dashboard")
        flash("Incorrect email or password.","danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    log_action("Signed out"); session.clear(); return redirect("/login")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    purge_expired_trash()
    doc_count  = Document.query.count()
    user_count = User.query.count()
    stor       = storage_info()
    recent     = Document.query.order_by(Document.upload_date.desc()).limit(10).all()
    pinned     = Document.query.filter_by(is_pinned=True).order_by(Document.upload_date.desc()).all()
    logs       = (ActivityLog.query.filter_by(user_id=session["user_id"])
                  .order_by(ActivityLog.timestamp.desc()).limit(8).all())

    cat_counts = {}
    for doc in Document.query.all():
        cat_counts[doc.category] = cat_counts.get(doc.category, 0) + 1

    type_counts = {k: 0 for k in FILE_TYPE_GROUPS}
    type_counts["other"] = 0
    type_latest = {}
    for doc in Document.query.order_by(Document.upload_date.desc()).all():
        grp = file_type_group(doc.file_ext)
        type_counts[grp] = type_counts.get(grp, 0) + 1
        if grp not in type_latest:
            type_latest[grp] = doc

    top_uploaders = (
        db.session.query(User.id, User.fullname,
                         db.func.count(Document.id).label("cnt"),
                         db.func.sum(Document.file_size).label("total"))
        .join(Document, Document.uploaded_by == User.id)
        .group_by(User.id)
        .order_by(db.text("cnt DESC"))
        .limit(5).all()
    )

    return render_template(
        "dashboard.html",
        doc_count=doc_count, user_count=user_count, stor=stor,
        recent=recent, pinned=pinned, logs=logs,
        categories=CATEGORIES, cat_counts=cat_counts,
        type_counts=type_counts, type_latest=type_latest,
        top_uploaders=top_uploaders,
    )


# ── Documents ─────────────────────────────────────────────────────────────────

@app.route("/documents")
@login_required
def documents():
    q        = request.args.get("q","").strip()
    cat      = request.args.get("category","")
    ext      = request.args.get("ext","")
    ftype    = request.args.get("type","")
    uploader = request.args.get("uploader","")
    query    = Document.query

    if q:
        query = query.filter(
            Document.original_name.ilike(f"%{q}%") |
            Document.description.ilike(f"%{q}%")   |
            Document.tags.ilike(f"%{q}%")
        )
    if cat:      query = query.filter_by(category=cat)
    if ext:      query = query.filter_by(file_ext=ext.lower())
    if uploader:
        try:     query = query.filter_by(uploaded_by=int(uploader))
        except:  pass

    docs = query.order_by(Document.is_pinned.desc(), Document.upload_date.desc()).all()
    if ftype:
        docs = [d for d in docs if file_type_group(d.file_ext) == ftype]

    if session.get("role") == "Admin":
        all_uploaders = User.query.order_by(User.fullname).all()
    else:
        all_uploaders = [User.query.get(session["user_id"])]

    uploader_obj = User.query.get(int(uploader)) if uploader else None

    return render_template(
        "documents.html",
        docs=docs, categories=CATEGORIES,
        selected_cat=cat, q=q, ext_filter=ext,
        type_filter=ftype, uploader_filter=uploader,
        uploader_obj=uploader_obj,
        all_uploaders=all_uploaders,
        stor=storage_info(), doc_count=Document.query.count(),
    )


# ── Users ─────────────────────────────────────────────────────────────────────

@app.route("/users")
@login_required
def users():
    q      = request.args.get("q","").strip()
    role_f = request.args.get("role","")
    status = request.args.get("status","")
    query  = User.query

    if q:
        query = query.filter(
            User.fullname.ilike(f"%{q}%") |
            User.email.ilike(f"%{q}%")    |
            User.department.ilike(f"%{q}%")
        )
    if role_f:                query = query.filter_by(role=role_f)
    if status == "active":    query = query.filter_by(is_active=True)
    if status == "inactive":  query = query.filter_by(is_active=False)

    all_users = query.order_by(User.created_at.desc()).all()
    upload_counts = dict(
        db.session.query(Document.uploaded_by, db.func.count(Document.id))
        .group_by(Document.uploaded_by).all()
    )
    return render_template(
        "users.html",
        all_users=all_users, upload_counts=upload_counts,
        q=q, role_filter=role_f, status_filter=status,
        stor=storage_info(),
    )


@app.route("/users/<int:uid>/toggle-status", methods=["POST"])
@login_required
@admin_required
def toggle_user_status(uid):
    if uid == session["user_id"]:
        flash("You cannot deactivate your own account.","danger")
        return redirect("/users")
    user = User.query.get_or_404(uid)
    user.is_active = not user.is_active
    db.session.commit()
    action = "Activated" if user.is_active else "Deactivated"
    log_action(f"{action} user account", user.email)
    flash(f"'{user.fullname}' has been {action.lower()}.", "info")
    return redirect("/users")


@app.route("/users/<int:uid>/set-role", methods=["POST"])
@login_required
@admin_required
def set_user_role(uid):
    if uid == session["user_id"]:
        flash("You cannot change your own role.","danger")
        return redirect("/users")
    user = User.query.get_or_404(uid)
    new_role = request.form.get("role","Staff")
    if new_role not in ("Admin","Staff"):
        flash("Invalid role.","danger"); return redirect("/users")
    user.role = new_role
    db.session.commit()
    log_action("Changed user role", f"{user.email} → {new_role}")
    flash(f"'{user.fullname}' role set to {new_role}.", "info")
    return redirect("/users")


@app.route("/users/<int:uid>/history")
@login_required
def user_history(uid):
    if uid != session["user_id"] and session.get("role") != "Admin":
        abort(403)
    user  = User.query.get_or_404(uid)
    docs  = (Document.query.filter_by(uploaded_by=uid)
             .order_by(Document.upload_date.desc()).all())
    logs  = (ActivityLog.query.filter_by(user_id=uid)
             .order_by(ActivityLog.timestamp.desc()).limit(50).all())
    total = sum(d.file_size for d in docs)
    return render_template(
        "user_history.html",
        subject=user, docs=docs, logs=logs,
        total_size=fmt_size(total), stor=storage_info(),
    )


# ── Profile ───────────────────────────────────────────────────────────────────

@app.route("/profile", methods=["GET","POST"])
@login_required
def profile():
    user = User.query.get_or_404(session["user_id"])
    if request.method == "POST":
        fullname   = request.form.get("fullname","").strip()
        department = request.form.get("department","").strip()
        new_pw     = request.form.get("new_password","").strip()
        if not fullname:
            flash("Full name cannot be empty.","danger"); return redirect("/profile")
        user.fullname   = fullname
        user.department = department
        if new_pw:
            if len(new_pw) < 6:
                flash("New password must be at least 6 characters.","danger")
                return redirect("/profile")
            user.password = generate_password_hash(new_pw)
        db.session.commit()
        session["fullname"] = user.fullname
        log_action("Updated profile")
        flash("Profile updated successfully.","success")
        return redirect("/profile")
    my_docs = (Document.query.filter_by(uploaded_by=user.id)
               .order_by(Document.upload_date.desc()).all())
    return render_template("profile.html", user=user, my_docs=my_docs, stor=storage_info())


# ── Upload ────────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file        = request.files.get("file")
    category    = request.form.get("category","General")
    description = request.form.get("description","").strip()
    tags        = request.form.get("tags","").strip()

    if not file or file.filename == "":
        flash("No file was selected.","danger"); return redirect(request.referrer or "/dashboard")
    if not allowed_file(file.filename):
        flash("File type not allowed.","danger"); return redirect(request.referrer or "/dashboard")

    file_bytes = file.read()
    incoming   = len(file_bytes)
    if incoming > MAX_FILE_BYTES:
        flash(f"File too large. Max {fmt_size(MAX_FILE_BYTES)}.","danger")
        return redirect(request.referrer or "/dashboard")

    used = get_used_bytes()
    if used + incoming > STORAGE_LIMIT_BYTES:
        flash(f"Storage limit reached. Only {fmt_size(STORAGE_LIMIT_BYTES-used)} remaining.","danger")
        return redirect(request.referrer or "/dashboard")

    original  = file.filename
    ext       = original.rsplit(".",1)[1].lower() if "." in original else ""
    safe_name = secure_filename(original)
    ts        = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    unique    = f"{ts}_{safe_name}"

    with open(os.path.join(UPLOAD_FOLDER, unique), "wb") as f_out:
        f_out.write(file_bytes)

    db.session.add(Document(
        filename=unique, original_name=original,
        category=category, description=description, tags=tags,
        file_size=incoming, file_ext=ext, uploaded_by=session["user_id"],
    ))
    db.session.commit()
    log_action("Uploaded file", f"{original} [{category}] {fmt_size(incoming)}")
    flash(f"'{original}' uploaded successfully.","success")
    return redirect(request.referrer or "/dashboard")


# ── Pin ───────────────────────────────────────────────────────────────────────

@app.route("/pin/<int:doc_id>", methods=["POST"])
@login_required
def pin(doc_id):
    doc = Document.query.get_or_404(doc_id)
    doc.is_pinned = not doc.is_pinned
    db.session.commit()
    log_action(f"{'Pinned' if doc.is_pinned else 'Unpinned'} file", doc.original_name)
    flash(f"'{doc.original_name}' {'pinned' if doc.is_pinned else 'unpinned'}.","info")
    return redirect(request.referrer or "/dashboard")


# ── Download ──────────────────────────────────────────────────────────────────

@app.route("/download/<int:doc_id>")
@login_required
def download(doc_id):
    doc = Document.query.get_or_404(doc_id)
    log_action("Downloaded file", doc.original_name)
    return send_from_directory(UPLOAD_FOLDER, doc.filename,
                               as_attachment=True, download_name=doc.original_name)


# ── Share Links ───────────────────────────────────────────────────────────────

@app.route("/share/<int:doc_id>", methods=["POST"])
@login_required
def create_share(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.uploaded_by != session["user_id"] and session.get("role") != "Admin":
        flash("You can only share your own files.", "danger")
        return redirect(request.referrer or "/dashboard")

    visibility     = request.form.get("visibility", "public")
    password       = request.form.get("password", "").strip()
    emails_raw     = request.form.get("allowed_emails", "").strip()
    expiry_opt     = request.form.get("expiry", "never")
    note           = request.form.get("note", "").strip()
    dl_limit_raw   = request.form.get("download_limit", "").strip()
    send_email_now = request.form.get("send_email_now") == "1"

    expires_at = None
    if expiry_opt == "1d":    expires_at = datetime.utcnow() + timedelta(days=1)
    elif expiry_opt == "7d":  expires_at = datetime.utcnow() + timedelta(days=7)
    elif expiry_opt == "30d": expires_at = datetime.utcnow() + timedelta(days=30)
    elif expiry_opt == "custom":
        custom_date = request.form.get("expiry_date", "").strip()
        try:
            expires_at = datetime.strptime(custom_date, "%Y-%m-%d")
        except ValueError:
            pass

    download_limit = None
    if dl_limit_raw:
        try:
            dl = int(dl_limit_raw)
            if dl > 0:
                download_limit = dl
        except ValueError:
            pass

    hashed_pw      = None
    allowed_emails = None

    if visibility == "private":
        if not password:
            flash("A password is required for password-protected links.", "danger")
            return redirect(request.referrer or "/dashboard")
        hashed_pw = generate_password_hash(password)

    elif visibility == "email":
        raw_list = [e.strip().lower() for e in emails_raw.replace(";",",").split(",") if e.strip()]
        if not raw_list:
            flash("Please enter at least one email address.", "danger")
            return redirect(request.referrer or "/dashboard")
        invalid = [e for e in raw_list if "@" not in e or "." not in e.split("@")[-1]]
        if invalid:
            flash(f"Invalid email address(es): {', '.join(invalid)}", "danger")
            return redirect(request.referrer or "/dashboard")
        allowed_emails = ",".join(raw_list)

    link = SharedLink(
        token=secrets.token_urlsafe(24),
        document_id=doc.id,
        created_by=session["user_id"],
        visibility=visibility,
        password=hashed_pw,
        allowed_emails=allowed_emails,
        expires_at=expires_at,
        download_limit=download_limit,
        note=note or None,
    )
    db.session.add(link)
    db.session.commit()
    log_action("Created share link", f"{doc.original_name} [{visibility}]")

    # ── Send email(s) if requested ────────────────────────────────────────────
    emails_to_notify = []
    if send_email_now and SMTP_USER():
        if visibility == "email" and allowed_emails:
            emails_to_notify = link.email_list
        else:
            extra = request.form.get("notify_emails", "").strip()
            if extra:
                emails_to_notify = [e.strip().lower() for e in extra.replace(";",",").split(",") if e.strip()]

    sender_name = session.get("fullname", "Engineering Office")
    for recip in emails_to_notify:
        txn = ShareTransaction(
            link_id=link.id,
            recipient_email=recip,
            open_token=secrets.token_urlsafe(20),
        )
        db.session.add(txn)
        db.session.commit()
        send_share_email_async(recip, txn.id, link, doc, sender_name, note)

    if emails_to_notify:
        flash(f"Share link created and email sent to {len(emails_to_notify)} recipient(s).", "success")
    else:
        flash(f"Share link created for '{doc.original_name}'. Copy it from My Share Links.", "success")

    return redirect("/shared-with-me")


@app.route("/shared-with-me")
@login_required
def shared_with_me():
    purge_expired_trash()
    links = (SharedLink.query
             .filter_by(created_by=session["user_id"])
             .order_by(SharedLink.created_at.desc()).all())
    return render_template("shared_with_me.html", links=links, stor=storage_info())


# ── Email Share — also send to additional recipients on existing link ─────────

@app.route("/share/<int:link_id>/send-email", methods=["POST"])
@login_required
def send_link_email(link_id):
    link = SharedLink.query.get_or_404(link_id)
    if link.created_by != session["user_id"] and session.get("role") != "Admin":
        flash("Not authorised.", "danger")
        return redirect("/shared-with-me")
    if not link.is_usable:
        flash("This link is no longer active.", "danger")
        return redirect("/shared-with-me")
    if not SMTP_USER():
        flash("SMTP is not configured. Set SMTP_USER and SMTP_PASS environment variables.", "warning")
        return redirect("/shared-with-me")

    emails_raw = request.form.get("emails","").strip()
    raw_list   = [e.strip().lower() for e in emails_raw.replace(";",",").split(",") if e.strip()]
    if not raw_list:
        flash("No email address provided.", "danger")
        return redirect("/shared-with-me")

    doc         = link.document
    sender_name = session.get("fullname","Engineering Office")
    note        = request.form.get("note","").strip()

    sent = 0
    for recip in raw_list:
        txn = ShareTransaction(
            link_id=link.id,
            recipient_email=recip,
            open_token=secrets.token_urlsafe(20),
        )
        db.session.add(txn)
        db.session.commit()
        send_share_email_async(recip, txn.id, link, doc, sender_name, note)
        sent += 1

    log_action("Sent share email", f"{doc.original_name if doc else ''} → {emails_raw}")
    flash(f"Email sent to {sent} recipient(s).", "success")
    return redirect("/shared-with-me")


# ── Share Transaction Tracking Endpoints ──────────────────────────────────────

@app.route("/st/<int:txn_id>/pixel.gif")
def share_pixel(txn_id):
    """1×1 transparent GIF tracking pixel — marks email as opened."""
    txn = ShareTransaction.query.get(txn_id)
    if txn and not txn.opened_at:
        txn.opened_at = datetime.utcnow()
        db.session.commit()
    gif = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    resp = make_response(gif)
    resp.headers["Content-Type"]  = "image/gif"
    resp.headers["Cache-Control"] = "no-store, no-cache"
    return resp


@app.route("/st/<int:txn_id>/open")
def share_txn_open(txn_id):
    """Redirect from email CTA; marks as opened and goes to share view."""
    txn = ShareTransaction.query.get(txn_id)
    if txn:
        if not txn.opened_at:
            txn.opened_at = datetime.utcnow()
            db.session.commit()
        link = txn.link
        if link and link.is_usable:
            # For email-restricted links, pre-authorise the recipient via session
            if link.needs_email:
                session[f"share_email_{link.id}"] = txn.recipient_email
            return redirect(url_for("view_shared", token=link.token))
    return render_template("share_invalid.html"), 404


@app.route("/st/<int:txn_id>/download")
def share_txn_download(txn_id):
    """Direct download from email CTA; marks downloaded + pre-authorises."""
    txn = ShareTransaction.query.get(txn_id)
    if not txn:
        return render_template("share_invalid.html"), 404
    link = txn.link
    if not link or not link.is_usable:
        return render_template("share_invalid.html"), 404
    if not txn.opened_at:
        txn.opened_at = datetime.utcnow()
    if not txn.downloaded_at:
        txn.downloaded_at = datetime.utcnow()
    db.session.commit()
    if link.needs_email:
        session[f"share_email_{link.id}"] = txn.recipient_email
    doc = link.document
    if not doc:
        return render_template("share_invalid.html"), 404
    link.download_count = (link.download_count or 0) + 1
    db.session.commit()
    return send_from_directory(UPLOAD_FOLDER, doc.filename,
                               as_attachment=True, download_name=doc.original_name)


# ── Share Transaction Dashboard ───────────────────────────────────────────────

@app.route("/share/<int:link_id>/activity")
@login_required
def share_activity(link_id):
    link = SharedLink.query.get_or_404(link_id)
    if link.created_by != session["user_id"] and session.get("role") != "Admin":
        abort(403)
    txns = (ShareTransaction.query.filter_by(link_id=link_id)
            .order_by(ShareTransaction.sent_at.desc()).all())
    return render_template(
        "share_activity.html",
        link=link, txns=txns,
        stor=storage_info(),
        smtp_configured=bool(SMTP_USER()),
    )


# ── Email Shares Dashboard (all my outgoing share emails) ─────────────────────

@app.route("/share-emails")
@login_required
def share_emails():
    my_links = SharedLink.query.filter_by(created_by=session["user_id"]).all()
    link_ids = [l.id for l in my_links]
    txns = (ShareTransaction.query.filter(ShareTransaction.link_id.in_(link_ids))
            .order_by(ShareTransaction.sent_at.desc()).all()) if link_ids else []
    stats = {
        "total":      len(txns),
        "delivered":  sum(1 for t in txns if t.delivered),
        "opened":     sum(1 for t in txns if t.opened_at),
        "downloaded": sum(1 for t in txns if t.downloaded_at),
        "failed":     sum(1 for t in txns if t.error_msg),
    }
    return render_template(
        "share_emails.html",
        txns=txns, stats=stats,
        stor=storage_info(),
        smtp_configured=bool(SMTP_USER()),
    )


@app.route("/share/<int:link_id>/revoke", methods=["POST"])
@login_required
def revoke_share(link_id):
    link = SharedLink.query.get_or_404(link_id)
    if link.created_by != session["user_id"] and session.get("role") != "Admin":
        flash("You can only revoke your own share links.", "danger")
        return redirect("/shared-with-me")
    link.is_active = False
    db.session.commit()
    log_action("Revoked share link", link.document.original_name if link.document else "")
    flash("Share link revoked.", "info")
    return redirect(request.referrer or "/shared-with-me")


# ── Public share viewer ───────────────────────────────────────────────────────

@app.route("/s/<token>", methods=["GET", "POST"])
def view_shared(token):
    link = SharedLink.query.filter_by(token=token).first()
    if not link or not link.is_usable:
        return render_template("share_invalid.html"), 404

    doc = link.document
    if not doc:
        return render_template("share_invalid.html"), 404

    pw_key    = f"share_unlocked_{link.id}"
    email_key = f"share_email_{link.id}"

    if link.needs_email:
        if not session.get(email_key):
            error = None
            if request.method == "POST":
                submitted = request.form.get("email", "").strip().lower()
                if link.email_allowed(submitted):
                    session[email_key] = submitted
                    return redirect(url_for("view_shared", token=token))
                else:
                    error = "This email address is not authorised to view this file."
            return render_template("share_email_gate.html", link=link, doc=doc, error=error)

    if link.needs_password:
        if not session.get(pw_key):
            error = None
            if request.method == "POST":
                pw = request.form.get("password", "")
                if check_password_hash(link.password, pw):
                    session[pw_key] = True
                    return redirect(url_for("view_shared", token=token))
                else:
                    error = "Incorrect password. Please try again."
            return render_template("share_password.html", link=link, doc=doc, error=error)

    view_key = f"share_viewed_{link.id}"
    if not session.get(view_key):
        link.view_count += 1
        db.session.commit()
        session[view_key] = True

    ext            = (doc.file_ext or "").lower().lstrip(".")
    is_image       = ext in {"png", "jpg", "jpeg", "gif"}
    is_pdf         = ext == "pdf"
    is_previewable = is_image or is_pdf

    return render_template(
        "share_view.html",
        link=link, doc=doc,
        is_previewable=is_previewable,
        is_image=is_image,
        is_pdf=is_pdf,
    )


@app.route("/s/<token>/download")
def download_shared(token):
    link = SharedLink.query.filter_by(token=token).first()
    if not link or not link.is_usable:
        return render_template("share_invalid.html"), 404

    if link.needs_email and not session.get(f"share_email_{link.id}"):
        return redirect(url_for("view_shared", token=token))
    if link.needs_password and not session.get(f"share_unlocked_{link.id}"):
        return redirect(url_for("view_shared", token=token))

    doc = link.document
    if not doc:
        return render_template("share_invalid.html"), 404

    link.download_count = (link.download_count or 0) + 1
    db.session.commit()

    return send_from_directory(UPLOAD_FOLDER, doc.filename,
                               as_attachment=True, download_name=doc.original_name)


@app.route("/s/<token>/preview")
def preview_shared(token):
    link = SharedLink.query.filter_by(token=token).first()
    if not link or not link.is_usable:
        return render_template("share_invalid.html"), 404

    if link.needs_email and not session.get(f"share_email_{link.id}"):
        abort(403)
    if link.needs_password and not session.get(f"share_unlocked_{link.id}"):
        abort(403)

    doc = link.document
    if not doc:
        return render_template("share_invalid.html"), 404

    file_path = os.path.join(UPLOAD_FOLDER, doc.filename)
    if not os.path.exists(file_path):
        abort(404)

    ext = (doc.file_ext or "").lower().lstrip(".")
    mime_map = {
        "pdf":  "application/pdf",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "gif":  "image/gif",
        "webp": "image/webp",
    }
    mimetype = mime_map.get(ext, "application/octet-stream")
    response = make_response(send_from_directory(UPLOAD_FOLDER, doc.filename, as_attachment=False))
    response.headers["Content-Type"]        = mimetype
    response.headers["Content-Disposition"] = f'inline; filename="{doc.original_name}"'
    response.headers["Cache-Control"]       = "no-store"
    return response


# ── Recycle Bin ───────────────────────────────────────────────────────────────

@app.route("/delete/<int:doc_id>", methods=["POST"])
@login_required
def delete(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.uploaded_by != session["user_id"] and session.get("role") != "Admin":
        flash("You can only delete your own files.","danger")
        return redirect(request.referrer or "/dashboard")

    # Nullify share links so they don't break (shows "file deleted" in UI)
    for sl in SharedLink.query.filter_by(document_id=doc.id).all():
        sl.document_id = None
    db.session.flush()

    src  = os.path.join(UPLOAD_FOLDER, doc.filename)
    dest = os.path.join(TRASH_FOLDER,  doc.filename)
    if os.path.exists(src): os.rename(src, dest)

    db.session.add(TrashedDocument(
        filename=doc.filename, original_name=doc.original_name,
        category=doc.category, description=doc.description, tags=doc.tags,
        file_size=doc.file_size, file_ext=doc.file_ext,
        upload_date=doc.upload_date,
        deleted_by=session["user_id"], original_owner=doc.uploaded_by,
    ))
    db.session.delete(doc)
    db.session.commit()
    log_action("Moved to Recycle Bin", doc.original_name)
    flash(f"'{doc.original_name}' moved to Recycle Bin ({RECYCLE_DAYS}-day expiry).","info")
    return redirect(request.referrer or "/dashboard")


@app.route("/recycle-bin")
@login_required
def recycle_bin():
    purge_expired_trash()
    items      = TrashedDocument.query.order_by(TrashedDocument.deleted_at.desc()).all()
    trash_size = db.session.query(db.func.sum(TrashedDocument.file_size)).scalar() or 0
    return render_template("recycle_bin.html",
        items=items, stor=storage_info(),
        trash_size=fmt_size(trash_size), recycle_days=RECYCLE_DAYS)


@app.route("/recycle-bin/restore/<int:item_id>", methods=["POST"])
@login_required
def restore(item_id):
    item = TrashedDocument.query.get_or_404(item_id)
    if item.original_owner != session["user_id"] and session.get("role") != "Admin":
        flash("You can only restore your own files.","danger"); return redirect("/recycle-bin")
    src  = os.path.join(TRASH_FOLDER,  item.filename)
    dest = os.path.join(UPLOAD_FOLDER, item.filename)
    if os.path.exists(src): os.rename(src, dest)
    db.session.add(Document(
        filename=item.filename, original_name=item.original_name,
        category=item.category, description=item.description, tags=item.tags,
        file_size=item.file_size, file_ext=item.file_ext,
        upload_date=item.upload_date, uploaded_by=item.original_owner,
    ))
    db.session.delete(item)
    db.session.commit()
    log_action("Restored from Recycle Bin", item.original_name)
    flash(f"'{item.original_name}' restored successfully.","success")
    return redirect("/recycle-bin")


@app.route("/recycle-bin/delete/<int:item_id>", methods=["POST"])
@login_required
def permanent_delete(item_id):
    item = TrashedDocument.query.get_or_404(item_id)
    if item.original_owner != session["user_id"] and session.get("role") != "Admin":
        flash("You can only permanently delete your own files.","danger")
        return redirect("/recycle-bin")
    fp = os.path.join(TRASH_FOLDER, item.filename)
    if os.path.exists(fp): os.remove(fp)
    name = item.original_name
    db.session.delete(item); db.session.commit()
    log_action("Permanently deleted file", name)
    flash(f"'{name}' permanently deleted.","danger")
    return redirect("/recycle-bin")


@app.route("/recycle-bin/empty", methods=["POST"])
@login_required
def empty_recycle_bin():
    items = (TrashedDocument.query.all() if session.get("role") == "Admin"
             else TrashedDocument.query.filter_by(original_owner=session["user_id"]).all())
    count = 0
    for item in items:
        fp = os.path.join(TRASH_FOLDER, item.filename)
        if os.path.exists(fp): os.remove(fp)
        db.session.delete(item); count += 1
    db.session.commit()
    log_action("Emptied Recycle Bin", f"{count} file(s) deleted")
    flash(f"Recycle Bin emptied — {count} file(s) permanently deleted.","danger")
    return redirect("/recycle-bin")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/storage")
@login_required
def api_storage():
    return jsonify(storage_info())


@app.route("/api/share/<int:link_id>/transactions")
@login_required
def api_share_transactions(link_id):
    link = SharedLink.query.get_or_404(link_id)
    if link.created_by != session["user_id"] and session.get("role") != "Admin":
        abort(403)
    txns = ShareTransaction.query.filter_by(link_id=link_id).order_by(ShareTransaction.sent_at.desc()).all()
    return jsonify([{
        "id":             t.id,
        "recipient":      t.recipient_email,
        "sent_at":        t.sent_at.isoformat(),
        "delivered":      t.delivered,
        "opened_at":      t.opened_at.isoformat() if t.opened_at else None,
        "downloaded_at":  t.downloaded_at.isoformat() if t.downloaded_at else None,
        "status":         t.status,
        "error":          t.error_msg,
    } for t in txns])


# ── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
                           message="You don't have permission to do that.",
                           stor=storage_info() if "user_id" in session else None), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404,
                           message="The page or file you're looking for doesn't exist.",
                           stor=storage_info() if "user_id" in session else None), 404

@app.errorhandler(413)
def too_large(e):
    flash(f"File too large. Maximum allowed is {fmt_size(MAX_FILE_BYTES)}.","danger")
    return redirect(request.referrer or "/dashboard")


# ── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)