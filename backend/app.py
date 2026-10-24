import os

from datetime import datetime, date, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    get_jwt_identity,
    jwt_required,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, func
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

app = Flask(__name__)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

database_url = os.getenv("DATABASE_URL")

if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=12)


frontend_url = os.getenv(
    "FRONTEND_URL",
    "https://studypilottt.netlify.app"
).rstrip("/")

allowed_origins = [
    frontend_url,
    "https://studypilottt.netlify.app",
]

CORS(
    app,
    resources={r"/api/*": {"origins": allowed_origins}},
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
)


db = SQLAlchemy()
bcrypt = Bcrypt(app)
jwt = JWTManager(app)

if database_url:
    db.init_app(app)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def current_utc():
    """Return current UTC time as a timezone-naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_current_user_id():
    return int(get_jwt_identity())


def parse_datetime(value):
    """Parse an ISO datetime string and normalize it to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Enter a valid ISO datetime, such as 2026-10-22T14:30:00Z."
        )


def error_response(message, status=400):
    return jsonify({"error": message}), status


def get_json_body():
    return request.get_json(silent=True) or {}


def serialize_subject(subject):
    progress = (
        round(subject.syllabus_completed / subject.syllabus_total * 100)
        if subject.syllabus_total > 0 else 0
    )
    return {
        "id": subject.id,
        "name": subject.name,
        "color": subject.color,
        "syllabus_total": subject.syllabus_total,
        "syllabus_completed": subject.syllabus_completed,
        "progress": progress,
    }


def serialize_session(session, subject_name=None):
    return {
        "id": session.id,
        "title": session.title,
        "subject_id": session.subject_id,
        "subject": subject_name,
        "starts_at": session.starts_at.isoformat() + "Z",
        "duration_minutes": session.duration_minutes,
        "status": session.status,
    }


def serialize_assignment(assignment, subject_name=None):
    return {
        "id": assignment.id,
        "title": assignment.title,
        "subject_id": assignment.subject_id,
        "subject": subject_name,
        "due_at": assignment.due_at.isoformat() + "Z" if assignment.due_at else None,
        "status": assignment.status,
        "created_at": assignment.created_at.isoformat() + "Z",
        "completed_at": assignment.completed_at.isoformat() + "Z"
        if assignment.completed_at else None,
    }


def validate_subject_for_user(subject_id, user_id):
    if subject_id is None:
        return None
    subject = Subject.query.filter_by(id=subject_id, user_id=user_id).first()
    if not subject:
        raise ValueError("The selected subject does not exist.")
    return subject


def get_owned_chapter(chapter_id, user_id):
    """Return a chapter only when its parent subject belongs to this user."""
    return (
        SyllabusChapter.query
        .join(Subject, SyllabusChapter.subject_id == Subject.id)
        .filter(
            SyllabusChapter.id == chapter_id,
            Subject.user_id == user_id
        )
        .first()
    )


def get_owned_topic(topic_id, user_id):
    """Return a topic only when its chapter's subject belongs to this user."""
    return (
        SyllabusTopic.query
        .join(SyllabusChapter, SyllabusTopic.chapter_id == SyllabusChapter.id)
        .join(Subject, SyllabusChapter.subject_id == Subject.id)
        .filter(
            SyllabusTopic.id == topic_id,
            Subject.user_id == user_id
        )
        .first()
    )


def refresh_subject_progress(subject_id):
    """Keep the dashboard's topic totals synchronized with saved topics."""
    subject = db.session.get(Subject, subject_id)
    if not subject:
        return
    total = (
        db.session.query(func.count(SyllabusTopic.id))
        .join(SyllabusChapter, SyllabusTopic.chapter_id == SyllabusChapter.id)
        .filter(SyllabusChapter.subject_id == subject_id)
        .scalar()
    ) or 0
    completed = (
        db.session.query(func.count(SyllabusTopic.id))
        .join(SyllabusChapter, SyllabusTopic.chapter_id == SyllabusChapter.id)
        .filter(
            SyllabusChapter.subject_id == subject_id,
            SyllabusTopic.completed.is_(True)
        )
        .scalar()
    ) or 0
    subject.syllabus_total = total
    subject.syllabus_completed = completed


def serialize_topic(topic):
    return {
        "id": topic.id,
        "chapter_id": topic.chapter_id,
        "title": topic.title,
        "completed": topic.completed,
        "position": topic.position,
    }


def serialize_chapter(chapter, include_topics=True):
    result = {
        "id": chapter.id,
        "subject_id": chapter.subject_id,
        "title": chapter.title,
        "position": chapter.position,
    }
    if include_topics:
        topics = (
            SyllabusTopic.query
            .filter_by(chapter_id=chapter.id)
            .order_by(SyllabusTopic.position.asc(), SyllabusTopic.id.asc())
            .all()
        )
        result["topics"] = [serialize_topic(topic) for topic in topics]
    return result


# --------------------------------------------------
# DATABASE MODELS
# --------------------------------------------------

class User(db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)


class AIStudyPlan(db.Model):
    """A study plan generated for and owned by one authenticated user."""
    __tablename__ = "ai_study_plans"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    subjects = db.Column(db.Text, nullable=False)
    plan = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), nullable=False)
    daily_hours = db.Column(db.Float, nullable=False)
    target_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=current_utc, index=True)


class Subject(db.Model):
    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    color = db.Column(db.String(30), nullable=False, default="#6366f1")
    syllabus_total = db.Column(db.Integer, nullable=False, default=0)
    syllabus_completed = db.Column(db.Integer, nullable=False, default=0)


class StudySession(db.Model):
    __tablename__ = "study_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)
    title = db.Column(db.String(160), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False, default=25)
    status = db.Column(db.String(20), nullable=False, default="planned")


class Assignment(db.Model):
    __tablename__ = "assignments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    due_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, nullable=False, default=current_utc)
    completed_at = db.Column(db.DateTime, nullable=True)

class CalendarEvent(db.Model):
    __tablename__ = "calendar_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    event_type = db.Column(db.String(40), nullable=False, default="Other")
    subject = db.Column(db.String(120), nullable=False, default="")
    event_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.String(5), nullable=False, default="")
    end_time = db.Column(db.String(5), nullable=False, default="")
    location = db.Column(db.String(250), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=current_utc)


def serialize_calendar_event(event):
    return {
        "id": event.id,
        "title": event.title,
        "type": event.event_type,
        "subject": event.subject,
        "date": event.event_date.isoformat(),
        "start_time": event.start_time,
        "end_time": event.end_time,
        "location": event.location,
    }


# Add these routes in app.py after the model definitions and before the RUN SERVER section.
@app.route("/api/events", methods=["GET"])
@jwt_required()
def get_calendar_events():
    user_id = get_current_user_id()
    items = (CalendarEvent.query
             .filter_by(user_id=user_id)
             .order_by(CalendarEvent.event_date.asc(), CalendarEvent.start_time.asc())
             .all())
    return jsonify({"events": [serialize_calendar_event(item) for item in items]})


@app.route("/api/events", methods=["POST"])
@jwt_required()
def create_calendar_event():
    body = get_json_body()
    title = str(body.get("title", "")).strip()
    date_value = str(body.get("date", "")).strip()
    if not title:
        return error_response("Event title is required.")
    try:
        event_date = date.fromisoformat(date_value)
    except ValueError:
        return error_response("Enter a valid event date in YYYY-MM-DD format.")

    event_type = str(body.get("type", "Other")).strip() or "Other"
    if event_type not in {"Exam", "Assignment Deadline", "Other"}:
        return error_response("Choose a valid event type.")
    start_time = str(body.get("start_time", "") or "").strip()
    end_time = str(body.get("end_time", "") or "").strip()
    for value in (start_time, end_time):
        if value:
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError:
                return error_response("Times must use HH:MM 24-hour format.")

    event = CalendarEvent(
        user_id=get_current_user_id(), title=title[:200], event_type=event_type,
        subject=str(body.get("subject", "") or "").strip()[:120],
        event_date=event_date, start_time=start_time, end_time=end_time,
        location=str(body.get("location", "") or "").strip()[:250],
    )
    try:
        db.session.add(event)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return error_response("Could not save the calendar event.", 500)
    return jsonify({"event": serialize_calendar_event(event)}), 201


@app.route("/api/events/<int:event_id>", methods=["PATCH"])
@jwt_required()
def update_calendar_event(event_id):
    event = CalendarEvent.query.filter_by(
        id=event_id, user_id=get_current_user_id()
    ).first()
    if event is None:
        return error_response("Calendar event not found.", 404)
    body = get_json_body()
    if "title" in body:
        title = str(body.get("title", "")).strip()
        if not title:
            return error_response("Event title is required.")
        event.title = title[:200]
    if "type" in body:
        event_type = str(body.get("type", "Other")).strip()
        if event_type not in {"Exam", "Assignment Deadline", "Other"}:
            return error_response("Choose a valid event type.")
        event.event_type = event_type
    if "subject" in body:
        event.subject = str(body.get("subject", "") or "").strip()[:120]
    if "date" in body:
        try:
            event.event_date = date.fromisoformat(str(body.get("date", "")))
        except ValueError:
            return error_response("Enter a valid event date in YYYY-MM-DD format.")
    for field in ("start_time", "end_time"):
        if field in body:
            value = str(body.get(field, "") or "").strip()
            if value:
                try:
                    datetime.strptime(value, "%H:%M")
                except ValueError:
                    return error_response("Times must use HH:MM 24-hour format.")
            setattr(event, field, value)
    if "location" in body:
        event.location = str(body.get("location", "") or "").strip()[:250]
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return error_response("Could not update the calendar event.", 500)
    return jsonify({"event": serialize_calendar_event(event)})


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@jwt_required()
def delete_calendar_event(event_id):
    event = CalendarEvent.query.filter_by(
        id=event_id, user_id=get_current_user_id()
    ).first()
    if event is None:
        return error_response("Calendar event not found.", 404)
    try:
        db.session.delete(event)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return error_response("Could not delete the calendar event.", 500)
    return jsonify({"message": "Calendar event deleted."})
class StudyLog(db.Model):
    __tablename__ = "study_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    studied_on = db.Column(db.Date, nullable=False, index=True)
    minutes = db.Column(db.Integer, nullable=False, default=0)


class SyllabusChapter(db.Model):
    __tablename__ = "syllabus_chapters"

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(
        db.Integer,
        db.ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)


class SyllabusTopic(db.Model):
    __tablename__ = "syllabus_topics"

    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(
        db.Integer,
        db.ForeignKey("syllabus_chapters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(250), nullable=False)
    completed = db.Column(db.Boolean, nullable=False, default=False)
    position = db.Column(db.Integer, nullable=False, default=0)


# --------------------------------------------------
# BASIC ROUTES
# --------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "app": "StudyPilot API",
        "message": "Welcome to StudyPilot!",
        "status": "running"
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "StudyPilot backend"})


@app.route("/api/db-check", methods=["GET"])
def db_check():
    if not database_url:
        return error_response("Database URL is missing.", 500)
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify({"status": "database connected"})
    except SQLAlchemyError:
        app.logger.exception("Database connection check failed")
        return error_response("Database connection failed.", 500)


# --------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------

@app.route("/api/register", methods=["POST"])
def register():
    if not database_url:
        return error_response("Database is not configured.", 500)

    data = get_json_body()
    full_name = data.get("full_name", "")
    email = data.get("email", "")
    password = data.get("password", "")

    if not all(isinstance(value, str) for value in (full_name, email, password)):
        return error_response("Name, email, and password must be text.")

    full_name = full_name.strip()
    email = email.strip().lower()

    if not full_name or not email or not password:
        return error_response("Name, email, and password are required.")
    if len(full_name) > 120 or len(email) > 255:
        return error_response("Name or email is too long.")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return error_response("Enter a valid email address.")
    if len(password) < 8:
        return error_response("Password must be at least 8 characters.")
    if User.query.filter_by(email=email).first():
        return error_response("An account with this email already exists.", 409)

    try:
        user = User(
            full_name=full_name,
            email=email,
            password_hash=bcrypt.generate_password_hash(password).decode("utf-8")
        )
        db.session.add(user)
        db.session.commit()
        token = create_access_token(identity=str(user.id))
        return jsonify({
            "message": "Account created successfully.",
            "access_token": token,
            "user": {"id": user.id, "full_name": user.full_name, "email": user.email}
        }), 201
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Registration database error")
        return error_response("Could not create account.", 500)


@app.route("/api/login", methods=["POST"])
def login():
    if not database_url:
        return error_response("Database is not configured.", 500)

    data = get_json_body()
    email = data.get("email", "")
    password = data.get("password", "")

    if not isinstance(email, str) or not isinstance(password, str):
        return error_response("Email and password must be text.")
    email = email.strip().lower()
    if not email or not password:
        return error_response("Email and password are required.")

    user = User.query.filter_by(email=email).first()
    if not user or not bcrypt.check_password_hash(user.password_hash, password):
        return error_response("Invalid email or password.", 401)

    token = create_access_token(identity=str(user.id))
    return jsonify({
        "message": "Login successful.",
        "access_token": token,
        "user": {"id": user.id, "full_name": user.full_name, "email": user.email}
    })


@app.route("/api/me", methods=["GET"])
@jwt_required()
def me():
    user = db.session.get(User, get_current_user_id())
    if not user:
        return error_response("User not found.", 404)
    return jsonify({
        "user": {"id": user.id, "full_name": user.full_name, "email": user.email}
    })


# --------------------------------------------------
# DASHBOARD API
# --------------------------------------------------

@app.route("/api/dashboard", methods=["GET"])
@jwt_required()
def dashboard():
    user_id = get_current_user_id()
    today = current_utc().date()
    tomorrow = today + timedelta(days=1)
    day_start = datetime.combine(today, datetime.min.time())
    day_end = datetime.combine(tomorrow, datetime.min.time())

    subjects = Subject.query.filter_by(user_id=user_id).order_by(Subject.name.asc()).all()
    subject_by_id = {subject.id: subject for subject in subjects}

    sessions = (
        StudySession.query.filter_by(user_id=user_id)
        .filter(StudySession.starts_at >= day_start, StudySession.starts_at < day_end)
        .order_by(StudySession.starts_at.asc()).all()
    )
    assignments = (
        Assignment.query.filter_by(user_id=user_id)
        .filter(Assignment.status != "completed")
        .filter((Assignment.due_at.is_(None)) | (Assignment.due_at >= day_start))
        .order_by(Assignment.due_at.asc().nullslast()).limit(10).all()
    )
    logs = (
        StudyLog.query.filter_by(user_id=user_id)
        .filter(StudyLog.studied_on >= today - timedelta(days=6))
        .filter(StudyLog.studied_on <= today).all()
    )

    minutes_by_day = {}
    for log in logs:
        minutes_by_day[log.studied_on] = minutes_by_day.get(log.studied_on, 0) + log.minutes

    activity = []
    for offset in range(7):
        day = today - timedelta(days=6 - offset)
        activity.append({
            "date": day.isoformat(),
            "day": day.strftime("%a"),
            "minutes": minutes_by_day.get(day, 0)
        })

    total_topics = sum(subject.syllabus_total for subject in subjects)
    completed_topics = sum(subject.syllabus_completed for subject in subjects)
    syllabus_progress = round(completed_topics / total_topics * 100) if total_topics else 0

    completed_tasks_today = Assignment.query.filter_by(
        user_id=user_id, status="completed"
    ).filter(Assignment.completed_at >= day_start, Assignment.completed_at < day_end).count()

    pending_tasks_today = Assignment.query.filter_by(
        user_id=user_id
    ).filter(
        Assignment.status != "completed",
        Assignment.due_at >= day_start,
        Assignment.due_at < day_end
    ).count()

    streak = 0
    streak_day = today
    while minutes_by_day.get(streak_day, 0) > 0:
        streak += 1
        streak_day -= timedelta(days=1)
    if streak == 0:
        yesterday = today - timedelta(days=1)
        while minutes_by_day.get(yesterday, 0) > 0:
            streak += 1
            yesterday -= timedelta(days=1)

    return jsonify({
        "stats": {
            "study_minutes_today": minutes_by_day.get(today, 0),
            "study_hours_today": round(minutes_by_day.get(today, 0) / 60, 2),
            "completed_tasks_today": completed_tasks_today,
            "pending_tasks_today": pending_tasks_today,
            "syllabus_progress": syllabus_progress,
            "streak_days": streak,
            "study_minutes_last_7_days": sum(minutes_by_day.values()),
        },
        "subjects": [serialize_subject(subject) for subject in subjects],
        "sessions": [
            serialize_session(
                session,
                subject_by_id[session.subject_id].name
                if session.subject_id in subject_by_id else None
            ) for session in sessions
        ],
        "assignments": [
            serialize_assignment(
                assignment,
                subject_by_id[assignment.subject_id].name
                if assignment.subject_id in subject_by_id else None
            ) for assignment in assignments
        ],
        "activity": activity
    })


# --------------------------------------------------
# SUBJECTS API
# --------------------------------------------------

@app.route("/api/subjects", methods=["GET"])
@jwt_required()
def get_subjects():
    subjects = (
        Subject.query.filter_by(user_id=get_current_user_id())
        .order_by(Subject.name.asc()).all()
    )
    return jsonify({"subjects": [serialize_subject(subject) for subject in subjects]})


@app.route("/api/subjects", methods=["POST"])
@jwt_required()
def create_subject():
    user_id = get_current_user_id()
    data = get_json_body()
    name = data.get("name", "")
    color = data.get("color", "#6366f1")
    total = data.get("syllabus_total", 0)
    completed = data.get("syllabus_completed", 0)

    if not isinstance(name, str) or not name.strip():
        return error_response("Subject name is required.")
    if not isinstance(color, str) or len(color) > 30:
        return error_response("Invalid subject color.")
    if (type(total) is not int or type(completed) is not int or
            total < 0 or completed < 0 or completed > total):
        return error_response(
            "Syllabus counts must be valid non-negative integers, and completed topics cannot exceed total topics."
        )

    subject = Subject(
        user_id=user_id, name=name.strip(), color=color,
        syllabus_total=total, syllabus_completed=completed
    )
    try:
        db.session.add(subject)
        db.session.commit()
        return jsonify({
            "message": "Subject created successfully.",
            "subject": serialize_subject(subject)
        }), 201
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Subject creation failed")
        return error_response("Could not create subject.", 500)


@app.route("/api/subjects/<int:subject_id>", methods=["PATCH"])
@jwt_required()
def update_subject(subject_id):
    subject = Subject.query.filter_by(
        id=subject_id, user_id=get_current_user_id()
    ).first()
    if not subject:
        return error_response("Subject not found.", 404)

    data = get_json_body()
    if "name" in data:
        if not isinstance(data["name"], str) or not data["name"].strip():
            return error_response("Subject name cannot be empty.")
        subject.name = data["name"].strip()
    if "color" in data:
        if not isinstance(data["color"], str) or len(data["color"]) > 30:
            return error_response("Invalid subject color.")
        subject.color = data["color"]

    total = data.get("syllabus_total", subject.syllabus_total)
    completed = data.get("syllabus_completed", subject.syllabus_completed)
    if (type(total) is not int or type(completed) is not int or
            total < 0 or completed < 0 or completed > total):
        return error_response("Invalid syllabus counts.")

    subject.syllabus_total = total
    subject.syllabus_completed = completed
    try:
        db.session.commit()
        return jsonify({
            "message": "Subject updated successfully.",
            "subject": serialize_subject(subject)
        })
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Subject update failed")
        return error_response("Could not update subject.", 500)

@app.route("/api/subjects/<int:subject_id>", methods=["DELETE"])
@jwt_required()
def delete_subject(subject_id):
    user_id = get_current_user_id()

    # Only allow users to delete their own subjects.
    subject = Subject.query.filter_by(
        id=subject_id,
        user_id=user_id
    ).first()

    if not subject:
        return error_response("Subject not found.", 404)

    try:
        # Keep assignments, but disconnect them from this subject.
        Assignment.query.filter_by(
            subject_id=subject.id,
            user_id=user_id
        ).update(
            {"subject_id": None},
            synchronize_session=False
        )

        # Keep study sessions, but disconnect them from this subject.
        StudySession.query.filter_by(
            subject_id=subject.id,
            user_id=user_id
        ).update(
            {"subject_id": None},
            synchronize_session=False
        )

        # Find the subject's chapters.
        chapter_ids = [
            chapter_id
            for (chapter_id,) in db.session.query(
                SyllabusChapter.id
            ).filter_by(subject_id=subject.id).all()
        ]

        # Delete the topics belonging to those chapters first.
        if chapter_ids:
            SyllabusTopic.query.filter(
                SyllabusTopic.chapter_id.in_(chapter_ids)
            ).delete(synchronize_session=False)

        # Then delete the chapters.
        SyllabusChapter.query.filter_by(
            subject_id=subject.id
        ).delete(synchronize_session=False)

        # Finally, delete the subject itself.
        db.session.delete(subject)
        db.session.commit()

        return jsonify({
            "message": "Subject deleted successfully."
        }), 200

    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Subject deletion failed")
        return error_response("Could not delete subject.", 500)


# --------------------------------------------------
# SYLLABUS API
# --------------------------------------------------

@app.route("/api/subjects/<int:subject_id>/syllabus", methods=["GET"])
@jwt_required()
def get_subject_syllabus(subject_id):
    user_id = get_current_user_id()
    subject = Subject.query.filter_by(id=subject_id, user_id=user_id).first()
    if not subject:
        return error_response("Subject not found.", 404)

    chapters = (
        SyllabusChapter.query.filter_by(subject_id=subject.id)
        .order_by(SyllabusChapter.position.asc(), SyllabusChapter.id.asc()).all()
    )
    return jsonify({
        "subject": serialize_subject(subject),
        "chapters": [serialize_chapter(chapter) for chapter in chapters]
    })


@app.route("/api/subjects/<int:subject_id>/chapters", methods=["POST"])
@jwt_required()
def create_syllabus_chapter(subject_id):
    user_id = get_current_user_id()
    subject = Subject.query.filter_by(id=subject_id, user_id=user_id).first()
    if not subject:
        return error_response("Subject not found.", 404)

    data = get_json_body()
    title = data.get("title", "")
    position = data.get("position", 0)
    if not isinstance(title, str) or not title.strip():
        return error_response("Chapter title is required.")
    if type(position) is not int or position < 0:
        return error_response("Chapter position must be a non-negative integer.")

    chapter = SyllabusChapter(
        subject_id=subject.id, title=title.strip(), position=position
    )
    try:
        db.session.add(chapter)
        db.session.commit()
        return jsonify({
            "message": "Chapter created successfully.",
            "chapter": serialize_chapter(chapter)
        }), 201
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Chapter creation failed")
        return error_response("Could not create chapter.", 500)


@app.route("/api/chapters/<int:chapter_id>", methods=["PATCH"])
@jwt_required()
def update_syllabus_chapter(chapter_id):
    chapter = get_owned_chapter(chapter_id, get_current_user_id())
    if not chapter:
        return error_response("Chapter not found.", 404)

    data = get_json_body()
    if "title" in data:
        if not isinstance(data["title"], str) or not data["title"].strip():
            return error_response("Chapter title cannot be empty.")
        chapter.title = data["title"].strip()
    if "position" in data:
        if type(data["position"]) is not int or data["position"] < 0:
            return error_response("Chapter position must be a non-negative integer.")
        chapter.position = data["position"]

    try:
        db.session.commit()
        return jsonify({
            "message": "Chapter updated successfully.",
            "chapter": serialize_chapter(chapter)
        })
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Chapter update failed")
        return error_response("Could not update chapter.", 500)


@app.route("/api/chapters/<int:chapter_id>", methods=["DELETE"])
@jwt_required()
def delete_syllabus_chapter(chapter_id):
    chapter = get_owned_chapter(chapter_id, get_current_user_id())
    if not chapter:
        return error_response("Chapter not found.", 404)

    subject_id = chapter.subject_id
    try:
        # Delete child topics explicitly so deletion works consistently across DB setups.
        SyllabusTopic.query.filter_by(chapter_id=chapter.id).delete(
            synchronize_session=False
        )
        db.session.delete(chapter)
        refresh_subject_progress(subject_id)
        db.session.commit()
        return jsonify({"message": "Chapter deleted successfully."})
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Chapter deletion failed")
        return error_response("Could not delete chapter.", 500)


@app.route("/api/chapters/<int:chapter_id>/topics", methods=["POST"])
@jwt_required()
def create_syllabus_topic(chapter_id):
    chapter = get_owned_chapter(chapter_id, get_current_user_id())
    if not chapter:
        return error_response("Chapter not found.", 404)

    data = get_json_body()
    title = data.get("title", "")
    position = data.get("position", 0)
    if not isinstance(title, str) or not title.strip():
        return error_response("Topic title is required.")
    if type(position) is not int or position < 0:
        return error_response("Topic position must be a non-negative integer.")

    topic = SyllabusTopic(
        chapter_id=chapter.id, title=title.strip(),
        completed=False, position=position
    )
    try:
        db.session.add(topic)
        db.session.flush()
        refresh_subject_progress(chapter.subject_id)
        db.session.commit()
        return jsonify({
            "message": "Topic created successfully.",
            "topic": serialize_topic(topic)
        }), 201
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Topic creation failed")
        return error_response("Could not create topic.", 500)


@app.route("/api/topics/<int:topic_id>", methods=["PATCH"])
@jwt_required()
def update_syllabus_topic(topic_id):
    topic = get_owned_topic(topic_id, get_current_user_id())
    if not topic:
        return error_response("Topic not found.", 404)

    data = get_json_body()
    if "title" in data:
        if not isinstance(data["title"], str) or not data["title"].strip():
            return error_response("Topic title cannot be empty.")
        topic.title = data["title"].strip()
    if "position" in data:
        if type(data["position"]) is not int or data["position"] < 0:
            return error_response("Topic position must be a non-negative integer.")
        topic.position = data["position"]
    if "completed" in data:
        if type(data["completed"]) is not bool:
            return error_response("'completed' must be true or false.")
        topic.completed = data["completed"]

    chapter = db.session.get(SyllabusChapter, topic.chapter_id)
    try:
        refresh_subject_progress(chapter.subject_id)
        db.session.commit()
        return jsonify({
            "message": "Topic updated successfully.",
            "topic": serialize_topic(topic),
            "subject": serialize_subject(db.session.get(Subject, chapter.subject_id))
        })
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Topic update failed")
        return error_response("Could not update topic.", 500)


@app.route("/api/topics/<int:topic_id>", methods=["DELETE"])
@jwt_required()
def delete_syllabus_topic(topic_id):
    topic = get_owned_topic(topic_id, get_current_user_id())
    if not topic:
        return error_response("Topic not found.", 404)

    chapter = db.session.get(SyllabusChapter, topic.chapter_id)
    subject_id = chapter.subject_id
    try:
        db.session.delete(topic)
        db.session.flush()
        refresh_subject_progress(subject_id)
        db.session.commit()
        return jsonify({"message": "Topic deleted successfully."})
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Topic deletion failed")
        return error_response("Could not delete topic.", 500)


# --------------------------------------------------
# STUDY SESSIONS API
# --------------------------------------------------

@app.route("/api/sessions", methods=["GET"])
@jwt_required()
def get_sessions():
    user_id = get_current_user_id()
    sessions = StudySession.query.filter_by(user_id=user_id).order_by(
        StudySession.starts_at.asc()
    ).all()
    subject_ids = {s.subject_id for s in sessions if s.subject_id is not None}
    subjects = Subject.query.filter(
        Subject.user_id == user_id, Subject.id.in_(subject_ids)
    ).all() if subject_ids else []
    subject_by_id = {subject.id: subject.name for subject in subjects}
    return jsonify({
        "sessions": [
            serialize_session(session, subject_by_id.get(session.subject_id))
            for session in sessions
        ]
    })


@app.route("/api/sessions", methods=["POST"])
@jwt_required()
def create_session():
    user_id = get_current_user_id()
    data = get_json_body()
    title = data.get("title", "")
    duration = data.get("duration_minutes", 25)
    subject_id = data.get("subject_id")

    if not isinstance(title, str) or not title.strip():
        return error_response("Session title is required.")
    if type(duration) is not int or not 1 <= duration <= 1440:
        return error_response("Duration must be between 1 and 1440 minutes.")
    if subject_id is not None and type(subject_id) is not int:
        return error_response("Invalid subject ID.")

    try:
        starts_at = parse_datetime(data.get("starts_at"))
        if starts_at is None:
            raise ValueError("Session start time is required.")
        subject = validate_subject_for_user(subject_id, user_id)
        session = StudySession(
            user_id=user_id, subject_id=subject_id, title=title.strip(),
            starts_at=starts_at, duration_minutes=duration, status="planned"
        )
        db.session.add(session)
        db.session.commit()
        return jsonify({
            "message": "Study session created successfully.",
            "session": serialize_session(session, subject.name if subject else None)
        }), 201
    except ValueError as exc:
        db.session.rollback()
        return error_response(str(exc))
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Session creation failed")
        return error_response("Could not create study session.", 500)


@app.route("/api/sessions/<int:session_id>", methods=["PATCH"])
@jwt_required()
def update_session(session_id):
    session = StudySession.query.filter_by(
        id=session_id, user_id=get_current_user_id()
    ).first()
    if not session:
        return error_response("Study session not found.", 404)

    data = get_json_body()
    if "title" in data:
        if not isinstance(data["title"], str) or not data["title"].strip():
            return error_response("Session title cannot be empty.")
        session.title = data["title"].strip()
    if "starts_at" in data:
        try:
            starts_at = parse_datetime(data["starts_at"])
            if starts_at is None:
                raise ValueError("Start time is required.")
            session.starts_at = starts_at
        except ValueError as exc:
            return error_response(str(exc))
    if "duration_minutes" in data:
        duration = data["duration_minutes"]
        if type(duration) is not int or not 1 <= duration <= 1440:
            return error_response("Invalid session duration.")
        session.duration_minutes = duration
    if "subject_id" in data:
        subject_id = data["subject_id"]
        if subject_id is not None and type(subject_id) is not int:
            return error_response("Invalid subject ID.")
        try:
            validate_subject_for_user(subject_id, get_current_user_id())
        except ValueError as exc:
            return error_response(str(exc))
        session.subject_id = subject_id
    if "status" in data:
        if data["status"] not in ("planned", "in_progress", "completed", "cancelled"):
            return error_response("Invalid session status.")
        session.status = data["status"]

    try:
        db.session.commit()
        subject = validate_subject_for_user(
            session.subject_id, get_current_user_id()
        ) if session.subject_id is not None else None
        return jsonify({
            "message": "Study session updated successfully.",
            "session": serialize_session(session, subject.name if subject else None)
        })
    except (SQLAlchemyError, ValueError):
        db.session.rollback()
        app.logger.exception("Session update failed")
        return error_response("Could not update study session.", 500)


# --------------------------------------------------
# ASSIGNMENTS API
# --------------------------------------------------

@app.route("/api/assignments", methods=["GET"])
@jwt_required()
def get_assignments():
    user_id = get_current_user_id()
    assignments = Assignment.query.filter_by(user_id=user_id).order_by(
        Assignment.due_at.asc().nullslast()
    ).all()
    subject_ids = {a.subject_id for a in assignments if a.subject_id is not None}
    subjects = Subject.query.filter(
        Subject.user_id == user_id, Subject.id.in_(subject_ids)
    ).all() if subject_ids else []
    subject_by_id = {subject.id: subject.name for subject in subjects}
    return jsonify({
        "assignments": [
            serialize_assignment(a, subject_by_id.get(a.subject_id))
            for a in assignments
        ]
    })


@app.route("/api/assignments", methods=["POST"])
@jwt_required()
def create_assignment():
    user_id = get_current_user_id()
    data = get_json_body()
    title = data.get("title", "")
    subject_id = data.get("subject_id")
    if not isinstance(title, str) or not title.strip():
        return error_response("Assignment title is required.")
    if subject_id is not None and type(subject_id) is not int:
        return error_response("Invalid subject ID.")

    try:
        subject = validate_subject_for_user(subject_id, user_id)
        due_at = parse_datetime(data.get("due_at"))
        assignment = Assignment(
            user_id=user_id, subject_id=subject_id, title=title.strip(),
            due_at=due_at, status="pending"
        )
        db.session.add(assignment)
        db.session.commit()
        return jsonify({
            "message": "Assignment created successfully.",
            "assignment": serialize_assignment(assignment, subject.name if subject else None)
        }), 201
    except ValueError as exc:
        db.session.rollback()
        return error_response(str(exc))
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Assignment creation failed")
        return error_response("Could not create assignment.", 500)


@app.route("/api/assignments/<int:assignment_id>", methods=["PATCH"])
@jwt_required()
def update_assignment(assignment_id):
    assignment = Assignment.query.filter_by(
        id=assignment_id, user_id=get_current_user_id()
    ).first()
    if not assignment:
        return error_response("Assignment not found.", 404)

    data = get_json_body()
    if "title" in data:
        if not isinstance(data["title"], str) or not data["title"].strip():
            return error_response("Assignment title cannot be empty.")
        assignment.title = data["title"].strip()
    if "due_at" in data:
        try:
            assignment.due_at = parse_datetime(data["due_at"])
        except ValueError as exc:
            return error_response(str(exc))
    if "subject_id" in data:
        subject_id = data["subject_id"]
        if subject_id is not None and type(subject_id) is not int:
            return error_response("Invalid subject ID.")
        try:
            validate_subject_for_user(subject_id, get_current_user_id())
        except ValueError as exc:
            return error_response(str(exc))
        assignment.subject_id = subject_id
    if "status" in data:
        status = data["status"]
        if status not in ("pending", "in_progress", "completed"):
            return error_response("Invalid assignment status.")
        assignment.status = status
        assignment.completed_at = current_utc() if status == "completed" else None

    try:
        db.session.commit()
        subject = validate_subject_for_user(
            assignment.subject_id, get_current_user_id()
        ) if assignment.subject_id is not None else None
        return jsonify({
            "message": "Assignment updated successfully.",
            "assignment": serialize_assignment(assignment, subject.name if subject else None)
        })
    except (SQLAlchemyError, ValueError):
        db.session.rollback()
        app.logger.exception("Assignment update failed")
        return error_response("Could not update assignment.", 500)


@app.route("/api/assignments/<int:assignment_id>", methods=["DELETE"])
@jwt_required()
def delete_assignment(assignment_id):
    assignment = Assignment.query.filter_by(
        id=assignment_id, user_id=get_current_user_id()
    ).first()
    if not assignment:
        return error_response("Assignment not found.", 404)
    try:
        db.session.delete(assignment)
        db.session.commit()
        return jsonify({"message": "Assignment deleted successfully."})
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Assignment deletion failed")
        return error_response("Could not delete assignment.", 500)


# --------------------------------------------------
# STUDY LOGS API
# --------------------------------------------------

@app.route("/api/study-logs", methods=["GET"])
@jwt_required()
def get_study_logs():
    logs = (
        StudyLog.query.filter_by(user_id=get_current_user_id())
        .order_by(StudyLog.studied_on.desc()).limit(90).all()
    )
    return jsonify({
        "study_logs": [
            {"id": log.id, "studied_on": log.studied_on.isoformat(), "minutes": log.minutes}
            for log in logs
        ]
    })


@app.route("/api/study-logs", methods=["POST"])
@jwt_required()
def create_study_log():
    user_id = get_current_user_id()
    data = get_json_body()
    minutes = data.get("minutes")
    studied_on_value = data.get("studied_on")

    if type(minutes) is not int or not 1 <= minutes <= 1440:
        return error_response("Study minutes must be between 1 and 1440.")

    if studied_on_value:
        try:
            studied_on = date.fromisoformat(studied_on_value)
        except (ValueError, TypeError):
            return error_response("Enter the study date in YYYY-MM-DD format.")
    else:
        studied_on = current_utc().date()

    if studied_on > current_utc().date():
        return error_response("Study logs cannot be recorded for future dates.")

    log = StudyLog(user_id=user_id, studied_on=studied_on, minutes=minutes)
    try:
        db.session.add(log)
        db.session.commit()
        return jsonify({
            "message": "Study time recorded successfully.",
            "study_log": {
                "id": log.id, "studied_on": log.studied_on.isoformat(),
                "minutes": log.minutes
            }
        }), 201
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Study log creation failed")
        return error_response("Could not record study time.", 500)


# --------------------------------------------------
# AI STUDY PLANNER API
# --------------------------------------------------

@app.route("/api/ai/study-plan", methods=["POST"])
@jwt_required()
def generate_ai_study_plan():
    """Generate a plan with Gemini and persist it for the current user."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return error_response(
            "Gemini is not configured on the server. Check GEMINI_API_KEY in .env.",
            503,
        )

    data = get_json_body()
    requested_subjects = data.get("subjects", [])
    if not isinstance(requested_subjects, list):
        return error_response("Subjects must be sent as a list.")

    requested_subjects = [
        item.strip() for item in requested_subjects
        if isinstance(item, str) and item.strip()
    ]
    if not requested_subjects:
        return error_response("Select at least one subject.")

    try:
        daily_hours = float(data.get("daily_hours", 2))
    except (TypeError, ValueError):
        return error_response("Daily study hours must be a number.")
    if not 0.5 <= daily_hours <= 12:
        return error_response("Daily study hours must be between 0.5 and 12.")

    target_date_value = data.get("target_date", "")
    if not isinstance(target_date_value, str):
        return error_response("Enter a valid target date in YYYY-MM-DD format.")
    try:
        target = date.fromisoformat(target_date_value.strip())
    except (ValueError, TypeError):
        return error_response("Enter a valid target date in YYYY-MM-DD format.")
    if target < current_utc().date():
        return error_response("Target date cannot be in the past.")

    priority = str(data.get("priority", "Medium")).strip()
    if priority not in {"Low", "Medium", "High"}:
        return error_response("Priority must be Low, Medium, or High.")
    topics_value = data.get("topics", "")
    topics = str(topics_value).strip()[:4000] if topics_value is not None else ""
    user_id = get_current_user_id()

    saved_subjects = (
        Subject.query.filter_by(user_id=user_id)
        .order_by(Subject.name.asc()).all()
    )
    saved_by_lower_name = {
        subject.name.casefold(): subject.name for subject in saved_subjects
    }
    selected_subjects = []
    for requested in requested_subjects:
        saved_name = saved_by_lower_name.get(requested.casefold())
        if saved_name and saved_name not in selected_subjects:
            selected_subjects.append(saved_name)
    if not selected_subjects:
        return error_response(
            "None of the selected subjects matched your saved subjects. "
            "Add subjects in My Subjects & Syllabus and try again."
        )

    days_remaining = max(1, (target - current_utc().date()).days)
    context = {
        "subjects": selected_subjects,
        "daily_available_hours": daily_hours,
        "target_date": target.isoformat(),
        "days_until_target": days_remaining,
        "priority": priority,
        "topics_to_focus_on": topics or "No specific topics supplied; suggest a sensible progression.",
    }
    prompt = (
        "You are StudyPilot's AI Study Planner. Create a realistic, actionable study plan "
        "for the authenticated student's saved subjects and preferences below. "
        "Use clear headings and a day-by-day schedule with subject, topic/task, duration, "
        "short breaks where appropriate, review/practice, and milestone checkpoints. "
        "Keep the total planned study time per day within the stated available hours. "
        "Respect the target date and priority. If the target is far away, provide a useful "
        "weekly structure and the first 14 days in detail. Do not claim that sessions were "
        "saved to a calendar. Do not include subjects that are not in the supplied list.\n\n"
        f"Student planning preferences (JSON): {context}"
    )

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
            contents=prompt,
        )
        plan_text = (response.text or "").strip()
        if not plan_text:
            return error_response(
                "Gemini returned an empty study plan. Please try again.", 502
            )

        saved_plan = AIStudyPlan(
            user_id=user_id,
            title=f"Study Plan - {target.strftime('%d %b %Y')}",
            subjects=", ".join(selected_subjects),
            plan=plan_text,
            priority=priority,
            daily_hours=daily_hours,
            target_date=target,
        )
        db.session.add(saved_plan)
        db.session.commit()

        return jsonify({
            "message": "Study plan generated and saved successfully.",
            "plan_id": saved_plan.id,
            "plan": plan_text,
            "subjects": selected_subjects,
            "target_date": target.isoformat(),
            "priority": priority,
            "daily_hours": daily_hours,
            "created_at": saved_plan.created_at.isoformat() + "Z",
        }), 201
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Saving AI study plan failed")
        return error_response(
            "The plan was generated but could not be saved to the database.", 500
        )
    except Exception:
        db.session.rollback()
        app.logger.exception("Gemini study plan generation failed")
        return error_response(
            "Gemini could not generate a study plan right now. Please try again shortly.",
            502,
        )


@app.route("/api/ai/study-plans", methods=["GET"])
@jwt_required()
def get_saved_ai_study_plans():
    """List saved plans belonging only to the authenticated user."""
    plans = (
        AIStudyPlan.query.filter_by(user_id=get_current_user_id())
        .order_by(AIStudyPlan.created_at.desc(), AIStudyPlan.id.desc())
        .all()
    )
    return jsonify({
        "study_plans": [
            {
                "id": item.id,
                "title": item.title,
                "subjects": item.subjects.split(", ") if item.subjects else [],
                "plan": item.plan,
                "priority": item.priority,
                "daily_hours": item.daily_hours,
                "target_date": item.target_date.isoformat(),
                "created_at": item.created_at.isoformat() + "Z",
            }
            for item in plans
        ]
    })


# --------------------------------------------------
# CREATE MISSING DATABASE TABLES
# --------------------------------------------------

with app.app_context():
    if database_url:
        db.create_all()


# --------------------------------------------------
# RUN SERVER
# --------------------------------------------------

if __name__ == "__main__":
    if not app.config["JWT_SECRET_KEY"]:
        raise RuntimeError("Set JWT_SECRET_KEY in your .env file.")
    app.run(host="0.0.0.0", port=5000, debug=True)