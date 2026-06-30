from flask import (
    Flask, render_template, request,
    redirect, session, flash, send_from_directory, abort, jsonify, url_for, make_response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from functools import wraps
import os
import secrets

# ── App Setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "engoffice_maroon_2025_change_me")

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
TRASH_FOLDER  = os.path.join(BASE_DIR, "trash")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRASH_FOLDER,  exist_ok=True)

ALLOWED_EXTENSIONS = {
    "pdf","doc","docx","xls","xlsx","ppt","pptx",
    "txt","csv","png","jpg","jpeg","dwg","dxf","zip","rar"
}
STORAGE_LIMIT_BYTES = 3 * 1024 * 1024 * 1024 * 1024
MAX_FILE_BYTES      = 500 * 1024 * 1024
RECYCLE_DAYS        = 15

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
    visibility options:
      'public'   — anyone with the link
      'private'  — password protected
      'email'    — specific email addresses only (stored comma-separated in allowed_emails)
    """
    id             = db.Column(db.Integer,     primary_key=True)
    token          = db.Column(db.String(64),  unique=True, nullable=False)
    document_id    = db.Column(db.Integer,     db.ForeignKey("document.id"), nullable=False)
    created_by     = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False)
    visibility     = db.Column(db.String(10),  default="public")  # public | private | email
    password       = db.Column(db.String(255), nullable=True)     # hashed, for 'private'
    allowed_emails = db.Column(db.Text,        nullable=True)     # comma-separated, for 'email'
    created_at     = db.Column(db.DateTime,    default=datetime.utcnow)
    expires_at     = db.Column(db.DateTime,    nullable=True)
    view_count     = db.Column(db.Integer,     default=0)
    is_active      = db.Column(db.Boolean,     default=True)

    document = db.relationship("Document", backref="shared_links")
    sharer   = db.relationship("User", foreign_keys=[created_by])

    @property
    def is_expired(self):
        return self.expires_at is not None and datetime.utcnow() >= self.expires_at

    @property
    def is_usable(self):
        return self.is_active and not self.is_expired

    @property
    def needs_password(self):
        return self.visibility == "private" and bool(self.password)

    @property
    def needs_email(self):
        return self.visibility == "email" and bool(self.allowed_emails)

    @property
    def email_list(self):
        """Return list of allowed emails (lowercase, stripped)."""
        if not self.allowed_emails:
            return []
        return [e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()]

    def email_allowed(self, email):
        """Check if a given email is on the allowed list."""
        return email.strip().lower() in self.email_list


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
    elif b < 1024**4:  return f"{b/1024**3:.2f} GB"
    else:              return f"{b/1024**4:.2f} TB"

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

        # Security: the role is never trusted from the client.
        # The very first account created becomes Admin automatically
        # (so there's always someone who can manage the system).
        # Every signup after that is forced to Staff. Admins can
        # promote users later from the Users page.
        is_first_user = User.query.count() == 0
        role = "Admin" if is_first_user else "Staff"

        if len(password) < 6:
            flash("Password must be at least 6 characters.","danger"); return redirect("/signup")
        if password != confirm:
            flash("Passwords do not match.","danger"); return redirect("/signup")
        if User.query.filter_by(email=email).first():
            flash("That email is already registered.","danger"); return redirect("/signup")
        db.session.add(User(fullname=fullname, email=email,
                            password=generate_password_hash(password), role=role))
        db.session.commit()
        if is_first_user:
            flash("Account created as Admin (first account) — please sign in.","success")
        else:
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

    visibility = request.form.get("visibility", "public")
    password   = request.form.get("password", "").strip()
    emails_raw = request.form.get("allowed_emails", "").strip()
    expiry_opt = request.form.get("expiry", "never")

    expires_at = None
    if expiry_opt == "1d":    expires_at = datetime.utcnow() + timedelta(days=1)
    elif expiry_opt == "7d":  expires_at = datetime.utcnow() + timedelta(days=7)
    elif expiry_opt == "30d": expires_at = datetime.utcnow() + timedelta(days=30)

    hashed_pw     = None
    allowed_emails = None

    if visibility == "private":
        if not password:
            flash("A password is required for password-protected links.", "danger")
            return redirect(request.referrer or "/dashboard")
        hashed_pw = generate_password_hash(password)

    elif visibility == "email":
        # Parse, validate and normalise email list
        raw_list = [e.strip().lower() for e in emails_raw.replace(";",",").split(",") if e.strip()]
        if not raw_list:
            flash("Please enter at least one email address.", "danger")
            return redirect(request.referrer or "/dashboard")
        # Basic format check
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
    )
    db.session.add(link)
    db.session.commit()
    log_action("Created share link", f"{doc.original_name} [{visibility}]")
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

    # ── Email-restricted gate ────────────────────────────────────────────────
    if link.needs_email:
        if not session.get(email_key):
            error = None
            if request.method == "POST":
                submitted = request.form.get("email", "").strip().lower()
                if link.email_allowed(submitted):
                    session[email_key] = submitted   # store which email was used
                    return redirect(url_for("view_shared", token=token))
                else:
                    error = "This email address is not authorised to view this file."
            return render_template("share_email_gate.html", link=link, doc=doc, error=error)

    # ── Password gate ────────────────────────────────────────────────────────
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

    # ── Increment view count once per session ────────────────────────────────
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

# ── Recycle Bin ───────────────────────────────────────────────────────────────

@app.route("/delete/<int:doc_id>", methods=["POST"])
@login_required
def delete(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.uploaded_by != session["user_id"] and session.get("role") != "Admin":
        flash("You can only delete your own files.","danger")
        return redirect(request.referrer or "/dashboard")

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