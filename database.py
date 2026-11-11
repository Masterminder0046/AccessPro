import os
import sys
import types
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, date, timezone
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------
# ENVIRONMENT CONFIGURATION
# ---------------------------------------------------------
load_dotenv()

TURSO_DATABASE_URL = os.environ.get(
    'TURSO_DATABASE_URL',
    'libsql://assessment-approtech-tech.aws-us-west-2.turso.io'
).strip()
TURSO_AUTH_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '').strip()


# ---------------------------------------------------------
# TURSO DB-API 2.0 PROTOCOL DRIVER
# Implements PEP 249 DB-API interface for Turso Hrana v2 pipeline
# Enables SQLAlchemy 'sqlite+libsql' dialect without native C/Rust compilers
# ---------------------------------------------------------

try:
    from libsql_client.hrana.convert import _value_to_proto, _value_from_proto
except Exception:
    def _value_to_proto(val):
        if val is None:
            return {"type": "null"}
        if isinstance(val, bool):
            return {"type": "integer", "value": "1" if val else "0"}
        if isinstance(val, int):
            return {"type": "integer", "value": str(val)}
        if isinstance(val, float):
            return {"type": "float", "value": val}
        if isinstance(val, (bytes, bytearray)):
            import base64
            return {"type": "blob", "base64": base64.b64encode(val).decode('ascii')}
        return {"type": "text", "value": str(val)}

    def _value_from_proto(proto_val):
        t = proto_val.get("type")
        if t == "null":
            return None
        if t == "integer":
            return int(proto_val.get("value", 0))
        if t == "float":
            return float(proto_val.get("value", 0.0))
        if t == "blob":
            import base64
            return base64.b64decode(proto_val.get("base64", ""))
        return proto_val.get("value")


class TursoCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self._rows = []
        self._idx = 0

    def _convert_params(self, parameters):
        if not parameters:
            return [], []
        if isinstance(parameters, dict):
            named = []
            for k, v in parameters.items():
                if isinstance(v, (datetime, date)):
                    v = v.isoformat(sep=' ')
                named.append({"name": k, "value": _value_to_proto(v)})
            return [], named
        args = []
        for v in parameters:
            if isinstance(v, (datetime, date)):
                v = v.isoformat(sep=' ')
            args.append(_value_to_proto(v))
        return args, []

    def execute(self, sql, parameters=()):
        sql = sql.strip()
        if not sql:
            return self

        args, named_args = self._convert_params(parameters)
        req = {
            "type": "execute",
            "stmt": {
                "sql": sql,
                "args": args,
                "named_args": named_args,
                "want_rows": True
            }
        }
        res = self.connection._send_requests([req])
        if not res:
            return self

        result_item = res[0]
        if result_item.get("type") == "error":
            err_msg = result_item.get("error", {}).get("message", "Unknown database error")
            raise sqlite3.OperationalError(err_msg)

        stmt_res = result_item.get("response", {}).get("result", {})
        cols = [c.get("name") or "" for c in stmt_res.get("cols", [])]
        if cols:
            self.description = tuple((c, None, None, None, None, None, None) for c in cols)
        else:
            self.description = None

        rows = []
        for r in stmt_res.get("rows", []):
            vals = tuple(_value_from_proto(v) for v in r)
            rows.append(vals)
        self._rows = rows
        self._idx = 0
        self.rowcount = stmt_res.get("affected_row_count", 0)
        rowid = stmt_res.get("last_insert_rowid")
        self.lastrowid = int(rowid) if rowid is not None else None
        return self

    def executemany(self, sql, seq_of_parameters):
        sql = sql.strip()
        if not sql or not seq_of_parameters:
            return self

        requests = []
        for params in seq_of_parameters:
            args, named_args = self._convert_params(params)
            requests.append({
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": args,
                    "named_args": named_args,
                    "want_rows": False
                }
            })

        res = self.connection._send_requests(requests)
        total_affected = 0
        for item in res:
            if item.get("type") == "error":
                err_msg = item.get("error", {}).get("message", "Unknown database error")
                raise sqlite3.OperationalError(err_msg)
            stmt_res = item.get("response", {}).get("result", {})
            total_affected += stmt_res.get("affected_row_count", 0)
            rowid = stmt_res.get("last_insert_rowid")
            if rowid is not None:
                self.lastrowid = int(rowid)

        self.rowcount = total_affected
        self._rows = []
        self._idx = 0
        self.description = None
        return self

    def fetchone(self):
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        if self._idx < len(self._rows):
            rows = self._rows[self._idx:]
            self._idx = len(self._rows)
            return rows
        return []

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        end = min(self._idx + size, len(self._rows))
        rows = self._rows[self._idx:end]
        self._idx = end
        return rows

    def close(self):
        self._rows = []
        self._idx = 0


class TursoConnection:
    def __init__(self, endpoint_url, auth_token, host=""):
        self.endpoint_url = endpoint_url
        self.auth_token = auth_token
        self.host = host
        self.timeout = 30
        self._baton = None
        self.isolation_level = None

    def _send_requests(self, requests):
        if not self.auth_token:
            raise sqlite3.OperationalError(
                "TURSO_AUTH_TOKEN is missing. Please set TURSO_AUTH_TOKEN in your environment or .env file."
            )
        body = {
            "baton": self._baton,
            "requests": requests
        }
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}"
        }
        req = urllib.request.Request(self.endpoint_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                self._baton = resp_data.get("baton", self._baton)
                return resp_data.get("results", [])
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            if e.code in (401, 403):
                raise sqlite3.OperationalError(f"Turso authentication failed ({e.code}): Invalid or expired TURSO_AUTH_TOKEN: {err_body}")
            raise sqlite3.OperationalError(f"Turso HTTP {e.code} error: {err_body}")
        except Exception as e:
            raise sqlite3.OperationalError(f"Turso connection error: {str(e)}")

    def cursor(self):
        return TursoCursor(self)

    def commit(self):
        if self._baton:
            try:
                self._send_requests([{"type": "execute", "stmt": {"sql": "COMMIT;", "want_rows": False}}])
            except sqlite3.OperationalError:
                pass

    def rollback(self):
        if self._baton:
            try:
                self._send_requests([{"type": "execute", "stmt": {"sql": "ROLLBACK;", "want_rows": False}}])
            except sqlite3.OperationalError:
                pass

    def close(self):
        if self._baton:
            try:
                self._send_requests([{"type": "close"}])
            except Exception:
                pass
            self._baton = None

    @property
    def total_changes(self):
        return 0


# Register Turso DB-API 2.0 module as 'libsql_experimental'
_turso_dbapi = types.ModuleType("libsql_experimental")
_turso_dbapi.Connection = TursoConnection

def turso_connect(database, *args, **kwargs):
    # Support local database paths if explicitly passed
    if database == ":memory:" or (not database.startswith("http://") and not database.startswith("https://") and "turso.io" not in database):
        valid_kwargs = {k: v for k, v in kwargs.items() if k in ("timeout", "detect_types", "isolation_level", "check_same_thread", "uri")}
        return sqlite3.connect(database, **valid_kwargs)

    parsed = urllib.parse.urlparse(database)
    host = parsed.netloc or parsed.path
    qs = urllib.parse.parse_qs(parsed.query)

    token = qs.get("authToken", [None])[0] or qs.get("auth_token", [None])[0] or os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    scheme = "http" if parsed.scheme in ("http", "ws") else "https"
    endpoint = f"{scheme}://{host}/v2/pipeline"
    return TursoConnection(endpoint, token, host=host)

_turso_dbapi.connect = turso_connect
_turso_dbapi.paramstyle = "qmark"
_turso_dbapi.threadsafety = 1
_turso_dbapi.apilevel = "2.0"
_turso_dbapi.sqlite_version = sqlite3.sqlite_version
_turso_dbapi.sqlite_version_info = sqlite3.sqlite_version_info
_turso_dbapi.Error = sqlite3.Error
_turso_dbapi.DatabaseError = sqlite3.DatabaseError
_turso_dbapi.OperationalError = sqlite3.OperationalError
_turso_dbapi.IntegrityError = sqlite3.IntegrityError
_turso_dbapi.ProgrammingError = sqlite3.ProgrammingError
_turso_dbapi.DataError = sqlite3.DataError
_turso_dbapi.NotSupportedError = sqlite3.NotSupportedError
_turso_dbapi.InternalError = sqlite3.InternalError

sys.modules["libsql_experimental"] = _turso_dbapi

# Now import sqlalchemy_libsql to guarantee dialect registration
try:
    import sqlalchemy_libsql
except ImportError:
    pass


def get_database_uri():
    """Generates the SQLAlchemy URI using the sqlalchemy-libsql dialect."""
    raw_url = os.environ.get(
        'TURSO_DATABASE_URL',
        'libsql://assessment-approtech-tech.aws-us-west-2.turso.io'
    ).strip()
    token = os.environ.get('TURSO_AUTH_TOKEN', '').strip()

    if "://" in raw_url:
        host = raw_url.split("://", 1)[1].rstrip("/")
    else:
        host = raw_url.rstrip("/")

    if "/" in host:
        host = host.split("/", 1)[0]

    # Provide URI with sqlalchemy-libsql dialect
    if token:
        return f"sqlite+libsql://{host}?authToken={token}&secure=true"
    elif os.environ.get("USE_LOCAL_FALLBACK") == "1":
        return "sqlite:///assesspro.db"
    else:
        return f"sqlite+libsql://{host}?secure=true"


db = SQLAlchemy()

def init_app_database(app):
    """Configures the Flask app to use Turso via sqlalchemy-libsql dialect."""
    app.config['SQLALCHEMY_DATABASE_URI'] = get_database_uri()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)


def get_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='admin', nullable=False)  # 'admin', 'evaluator'
    full_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    admin_logs = db.relationship('AdminLog', backref='user', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'role': self.role,
            'full_name': self.full_name,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Candidate(db.Model):
    __tablename__ = 'candidates'

    id = db.Column(db.Integer, primary_key=True)
    candidate_id = db.Column(db.String(50), unique=True, nullable=False, index=True)  # Roll / ID
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=True)
    department = db.Column(db.String(100), nullable=True)
    college = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    assignments = db.relationship('Assignment', backref='candidate', cascade='all, delete-orphan', lazy='dynamic')
    attempts = db.relationship('Attempt', backref='candidate', cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'candidate_id': self.candidate_id,
            'name': self.name,
            'email': self.email,
            'phone': self.phone,
            'department': self.department,
            'college': self.college,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Section(db.Model):
    __tablename__ = 'sections'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    order_index = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    questions = db.relationship('Question', backref='section', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'order_index': self.order_index
        }


class Question(db.Model):
    __tablename__ = 'questions'

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=True)
    question_text = db.Column(db.Text, nullable=False)
    question_type = db.Column(db.String(20), default='mcq')  # mcq
    marks = db.Column(db.Float, default=1.0)
    negative_marks = db.Column(db.Float, default=0.0)
    time_limit_seconds = db.Column(db.Integer, default=60)
    explanation = db.Column(db.Text, nullable=True)
    difficulty = db.Column(db.String(20), default='medium')  # easy, medium, hard
    category = db.Column(db.String(50), default='General')
    created_at = db.Column(db.DateTime, default=get_utc_now)

    options = db.relationship('Option', backref='question', cascade='all, delete-orphan', lazy='joined')
    assessment_links = db.relationship('AssessmentQuestion', backref='question', cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self, include_correct=True):
        return {
            'id': self.id,
            'section_id': self.section_id,
            'section_name': self.section.name if self.section else 'General',
            'question_text': self.question_text,
            'question_type': self.question_type,
            'marks': self.marks,
            'negative_marks': self.negative_marks,
            'time_limit_seconds': self.time_limit_seconds,
            'explanation': self.explanation if include_correct else None,
            'difficulty': self.difficulty,
            'category': self.category,
            'options': [opt.to_dict(include_correct=include_correct) for opt in self.options]
        }


class Option(db.Model):
    __tablename__ = 'options'

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('questions.id', ondelete='CASCADE'), nullable=False)
    option_key = db.Column(db.String(10), nullable=False)  # A, B, C, D
    option_text = db.Column(db.Text, nullable=False)
    is_correct = db.Column(db.Boolean, default=False)

    def to_dict(self, include_correct=True):
        data = {
            'id': self.id,
            'option_key': self.option_key,
            'option_text': self.option_text
        }
        if include_correct:
            data['is_correct'] = self.is_correct
        return data


class Assessment(db.Model):
    __tablename__ = 'assessments'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    duration_minutes = db.Column(db.Integer, default=60)
    enable_question_timer = db.Column(db.Boolean, default=False)
    default_question_time = db.Column(db.Integer, default=60)  # seconds
    enable_negative_marking = db.Column(db.Boolean, default=False)
    negative_marking_rate = db.Column(db.Float, default=0.25)
    passing_percentage = db.Column(db.Float, default=50.0)
    randomize_questions = db.Column(db.Boolean, default=True)
    randomize_options = db.Column(db.Boolean, default=False)
    allow_backtracking = db.Column(db.Boolean, default=True)
    max_violations = db.Column(db.Integer, default=3)
    strict_fullscreen = db.Column(db.Boolean, default=True)
    block_copy_paste = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default='draft')  # draft, published, archived
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    # Proctored & Shared Common Link Configuration
    shared_slug = db.Column(db.String(80), unique=True, nullable=True, index=True)
    enable_shared_link = db.Column(db.Boolean, default=True)
    access_code = db.Column(db.String(50), nullable=True)  # Optional passkey for common link
    shared_link_active_from = db.Column(db.DateTime, nullable=True)
    shared_link_expires_at = db.Column(db.DateTime, nullable=True)
    max_attempts_per_candidate = db.Column(db.Integer, default=1)
    allow_self_registration = db.Column(db.Boolean, default=True)
    allow_duplicate_attempts = db.Column(db.Boolean, default=False)
    questions_to_deliver = db.Column(db.Integer, nullable=True)
    randomize_sections = db.Column(db.Boolean, default=True)
    link_visits_count = db.Column(db.Integer, default=0)

    # Relationships
    questions = db.relationship('AssessmentQuestion', backref='assessment', cascade='all, delete-orphan', lazy='joined', order_by='AssessmentQuestion.order_index')
    assignments = db.relationship('Assignment', backref='assessment', cascade='all, delete-orphan', lazy='dynamic')
    attempts = db.relationship('Attempt', backref='assessment', cascade='all, delete-orphan', lazy='dynamic')

    @property
    def total_questions(self):
        return len(self.questions)

    @property
    def total_marks(self):
        return sum(aq.effective_marks for aq in self.questions)

    @property
    def started_attempts_count(self):
        return self.attempts.count()

    @property
    def completed_attempts_count(self):
        return self.attempts.filter(Attempt.status.in_(['submitted', 'auto_submitted', 'terminated'])).count()

    @property
    def registered_candidates_count(self):
        return self.assignments.count()

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'code': self.code,
            'shared_slug': self.shared_slug,
            'enable_shared_link': self.enable_shared_link,
            'access_code': self.access_code,
            'description': self.description,
            'instructions': self.instructions,
            'duration_minutes': self.duration_minutes,
            'enable_question_timer': self.enable_question_timer,
            'default_question_time': self.default_question_time,
            'enable_negative_marking': self.enable_negative_marking,
            'negative_marking_rate': self.negative_marking_rate,
            'passing_percentage': self.passing_percentage,
            'randomize_questions': self.randomize_questions,
            'randomize_options': self.randomize_options,
            'randomize_sections': self.randomize_sections,
            'questions_to_deliver': self.questions_to_deliver,
            'allow_backtracking': self.allow_backtracking,
            'max_violations': self.max_violations,
            'strict_fullscreen': self.strict_fullscreen,
            'block_copy_paste': self.block_copy_paste,
            'status': self.status,
            'total_questions': self.total_questions,
            'total_marks': self.total_marks,
            'link_visits_count': self.link_visits_count,
            'registered_candidates': self.registered_candidates_count,
            'started_attempts': self.started_attempts_count,
            'completed_attempts': self.completed_attempts_count,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AssessmentQuestion(db.Model):
    __tablename__ = 'assessment_questions'

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey('assessments.id', ondelete='CASCADE'), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey('questions.id', ondelete='CASCADE'), nullable=False)
    order_index = db.Column(db.Integer, default=0)
    custom_marks = db.Column(db.Float, nullable=True)
    custom_negative_marks = db.Column(db.Float, nullable=True)
    custom_time_limit = db.Column(db.Integer, nullable=True)

    @property
    def effective_marks(self):
        if self.custom_marks is not None:
            return self.custom_marks
        return self.question.marks if self.question else 1.0

    @property
    def effective_negative_marks(self):
        if self.custom_negative_marks is not None:
            return self.custom_negative_marks
        return self.question.negative_marks if self.question else 0.0

    @property
    def effective_time_limit(self):
        if self.custom_time_limit is not None:
            return self.custom_time_limit
        return self.question.time_limit_seconds if self.question else 60


class Assignment(db.Model):
    __tablename__ = 'assignments'

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey('assessments.id', ondelete='CASCADE'), nullable=False)
    candidate_id = db.Column(db.Integer, db.ForeignKey('candidates.id', ondelete='CASCADE'), nullable=False)
    access_token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), default='assigned')  # assigned, in_progress, completed, terminated, expired
    valid_from = db.Column(db.DateTime, nullable=True)
    valid_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    attempt = db.relationship('Attempt', backref='assignment', uselist=False, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'assessment_id': self.assessment_id,
            'assessment_title': self.assessment.title if self.assessment else '',
            'candidate_id': self.candidate_id,
            'candidate_name': self.candidate.name if self.candidate else '',
            'candidate_email': self.candidate.email if self.candidate else '',
            'candidate_roll': self.candidate.candidate_id if self.candidate else '',
            'access_token': self.access_token,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Attempt(db.Model):
    __tablename__ = 'attempts'

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('assignments.id', ondelete='CASCADE'), nullable=False)
    candidate_id = db.Column(db.Integer, db.ForeignKey('candidates.id', ondelete='CASCADE'), nullable=False)
    assessment_id = db.Column(db.Integer, db.ForeignKey('assessments.id', ondelete='CASCADE'), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False, default=get_utc_now)
    submitted_at = db.Column(db.DateTime, nullable=True)
    due_at = db.Column(db.DateTime, nullable=False)  # Server authoritative expiration
    status = db.Column(db.String(20), default='in_progress')  # in_progress, submitted, auto_submitted, terminated
    total_marks = db.Column(db.Float, default=0.0)
    obtained_marks = db.Column(db.Float, default=0.0)
    correct_count = db.Column(db.Integer, default=0)
    incorrect_count = db.Column(db.Integer, default=0)
    unanswered_count = db.Column(db.Integer, default=0)
    percentage = db.Column(db.Float, default=0.0)
    passed = db.Column(db.Boolean, default=False)
    time_taken_seconds = db.Column(db.Integer, default=0)
    violation_count = db.Column(db.Integer, default=0)
    attempt_number = db.Column(db.Integer, default=1)
    termination_reason = db.Column(db.String(255), nullable=True)
    client_ip = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    question_order_json = db.Column(db.Text, nullable=True)  # JSON array of question IDs
    option_order_json = db.Column(db.Text, nullable=True)    # JSON dict {str(qid): [opt_ids]}

    # Relationships
    answers = db.relationship('Answer', backref='attempt', cascade='all, delete-orphan', lazy='joined')
    violations = db.relationship('Violation', backref='attempt', cascade='all, delete-orphan', lazy='dynamic')
    activity_logs = db.relationship('ActivityLog', backref='attempt', cascade='all, delete-orphan', lazy='dynamic')
    exam_sessions = db.relationship('ExamSession', backref='attempt', cascade='all, delete-orphan', lazy='dynamic')

    def get_question_ids(self):
        if self.question_order_json:
            try:
                return json.loads(self.question_order_json)
            except Exception:
                pass
        return [aq.question_id for aq in self.assessment.questions]

    def get_option_order_for_question(self, question_id):
        if self.option_order_json:
            try:
                order_map = json.loads(self.option_order_json)
                return order_map.get(str(question_id))
            except Exception:
                pass
        return None

    def to_dict(self, include_details=False):
        data = {
            'id': self.id,
            'assignment_id': self.assignment_id,
            'candidate_id': self.candidate_id,
            'candidate_name': self.candidate.name if self.candidate else '',
            'candidate_email': self.candidate.email if self.candidate else '',
            'candidate_roll': self.candidate.candidate_id if self.candidate else '',
            'assessment_id': self.assessment_id,
            'assessment_title': self.assessment.title if self.assessment else '',
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
            'due_at': self.due_at.isoformat() if self.due_at else None,
            'status': self.status,
            'total_marks': round(self.total_marks, 2),
            'obtained_marks': round(self.obtained_marks, 2),
            'correct_count': self.correct_count,
            'incorrect_count': self.incorrect_count,
            'unanswered_count': self.unanswered_count,
            'percentage': round(self.percentage, 2),
            'passed': self.passed,
            'time_taken_seconds': self.time_taken_seconds,
            'violation_count': self.violation_count,
            'attempt_number': self.attempt_number,
            'termination_reason': self.termination_reason
        }
        if include_details:
            data['answers'] = [ans.to_dict() for ans in self.answers]
            data['violations'] = [v.to_dict() for v in self.violations.order_by(Violation.recorded_at.desc()).all()]
        return data


class Answer(db.Model):
    __tablename__ = 'answers'

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey('attempts.id', ondelete='CASCADE'), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey('questions.id', ondelete='CASCADE'), nullable=False)
    selected_option_id = db.Column(db.Integer, db.ForeignKey('options.id'), nullable=True)
    is_marked_for_review = db.Column(db.Boolean, default=False)
    is_answered = db.Column(db.Boolean, default=False)
    is_correct = db.Column(db.Boolean, nullable=True)
    marks_awarded = db.Column(db.Float, default=0.0)
    time_spent_seconds = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=get_utc_now, onupdate=get_utc_now)

    question = db.relationship('Question')
    selected_option = db.relationship('Option')

    def to_dict(self):
        return {
            'id': self.id,
            'question_id': self.question_id,
            'selected_option_id': self.selected_option_id,
            'selected_option_key': self.selected_option.option_key if self.selected_option else None,
            'selected_option_text': self.selected_option.option_text if self.selected_option else None,
            'is_marked_for_review': self.is_marked_for_review,
            'is_answered': self.is_answered,
            'is_correct': self.is_correct,
            'marks_awarded': round(self.marks_awarded, 2),
            'time_spent_seconds': self.time_spent_seconds
        }


class Violation(db.Model):
    __tablename__ = 'violations'

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey('attempts.id', ondelete='CASCADE'), nullable=False)
    violation_type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=True)
    recorded_at = db.Column(db.DateTime, default=get_utc_now)
    question_index = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'violation_type': self.violation_type,
            'description': self.description,
            'recorded_at': self.recorded_at.isoformat() if self.recorded_at else None,
            'question_index': self.question_index
        }


class ExamSession(db.Model):
    __tablename__ = 'exam_sessions'

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey('attempts.id', ondelete='CASCADE'), nullable=False)
    session_token = db.Column(db.String(64), nullable=False)
    last_heartbeat = db.Column(db.DateTime, default=get_utc_now)
    is_active = db.Column(db.Boolean, default=True)
    current_question_index = db.Column(db.Integer, default=0)
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'attempt_id': self.attempt_id,
            'last_heartbeat': self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            'is_active': self.is_active,
            'current_question_index': self.current_question_index,
            'ip_address': self.ip_address
        }


class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey('attempts.id', ondelete='CASCADE'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'action': self.action,
            'details': self.details,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AdminLog(db.Model):
    __tablename__ = 'admin_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    target_type = db.Column(db.String(50), nullable=True)
    target_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'user_name': self.user.username if self.user else 'System',
            'action': self.action,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'details': self.details,
            'ip_address': self.ip_address,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
