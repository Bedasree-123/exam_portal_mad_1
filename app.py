"""A small examination portal built with Flask and SQLite."""

import os
import sqlite3
from datetime import date
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-for-a-real-deployment")

PROJECT_FOLDER = Path(__file__).parent
DATABASE_FOLDER = PROJECT_FOLDER / "instance"
DATABASE_FILE = DATABASE_FOLDER / "emp.db"

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]
EXAM_STATUSES = ["Draft", "Slot Creation", "Booking Open", "Closed", "Completed"]


def open_database():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_database():
    """One connection per request, stored on g."""
    if "database" not in g:
        g.database = open_database()
    return g.database


@app.teardown_appcontext
def close_database(error=None):
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()


def create_database():
    """Create the tables (if missing) and the default admin."""
    DATABASE_FOLDER.mkdir(exist_ok=True)
    connection = open_database()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'examiner', 'student')),
            department TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_code TEXT NOT NULL UNIQUE,
            course_name TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Active'
        );

        CREATE TABLE IF NOT EXISTS examinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            exam_type TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            max_marks INTEGER NOT NULL,
            slot_creation_start TEXT NOT NULL,
            slot_creation_end TEXT NOT NULL,
            booking_start TEXT NOT NULL,
            booking_end TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Draft',
            FOREIGN KEY (course_id) REFERENCES courses(id)
        );

        CREATE TABLE IF NOT EXISTS examiner_examinations (
            examiner_id INTEGER NOT NULL,
            examination_id INTEGER NOT NULL,
            PRIMARY KEY (examiner_id, examination_id),
            FOREIGN KEY (examiner_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (examination_id) REFERENCES examinations(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rubrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            examination_id INTEGER NOT NULL,
            criterion_name TEXT NOT NULL,
            max_marks INTEGER NOT NULL,
            weightage INTEGER NOT NULL DEFAULT 0,
            description TEXT DEFAULT '',
            FOREIGN KEY (examination_id) REFERENCES examinations(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            examination_id INTEGER NOT NULL,
            examiner_id INTEGER NOT NULL,
            exam_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Available',
            FOREIGN KEY (examination_id) REFERENCES examinations(id),
            FOREIGN KEY (examiner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            slot_id INTEGER NOT NULL,
            booked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'Booked',
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (slot_id) REFERENCES slots(id)
        );

        CREATE TABLE IF NOT EXISTS evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL UNIQUE,
            marks INTEGER NOT NULL,
            remarks TEXT DEFAULT '',
            published INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (booking_id) REFERENCES bookings(id)
        );

        CREATE TABLE IF NOT EXISTS rubric_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id INTEGER NOT NULL,
            rubric_id INTEGER NOT NULL,
            marks INTEGER NOT NULL,
            UNIQUE (evaluation_id, rubric_id),
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id) ON DELETE CASCADE,
            FOREIGN KEY (rubric_id) REFERENCES rubrics(id)
        );
        """
    )

    # create the default admin if there isn't one
    admin = connection.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()
    if admin is None:
        connection.execute(
            """INSERT INTO users (name, email, password_hash, role, status)
               VALUES (?, ?, ?, 'admin', 'active')""",
            ("Portal Admin", "admin@example.com", generate_password_hash("admin123", method="pbkdf2:sha256")),
        )

    connection.commit()
    connection.close()


def login_required(*allowed_roles):
    """Check the user is logged in, active, and has one of the given roles."""
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if "user_id" not in session:
                flash("Please sign in to continue.", "info")
                return redirect(url_for("login"))

            user = get_database().execute(
                "SELECT * FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
            if user is None or user["status"] != "active":
                session.clear()
                flash("This account is not active yet.", "warning")
                return redirect(url_for("login"))
            if allowed_roles and user["role"] not in allowed_roles:
                abort(403)
            g.user = user
            return view(*args, **kwargs)
        return wrapped_view
    return decorator


@app.context_processor
def add_user_to_templates():
    user = None
    if "user_id" in session:
        user = get_database().execute(
            "SELECT id, name, email, role FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
    return {"current_user": user}


def today_text():
    return date.today().isoformat()


def date_window_is_open(start, end):
    today = today_text()
    return bool(start and end and start <= today <= end)


def get_slot_seats(connection, slot_id):
    slot = connection.execute(
        "SELECT capacity FROM slots WHERE id = ?", (slot_id,)
    ).fetchone()
    if slot is None:
        return 0
    booked = connection.execute(
        "SELECT COUNT(*) AS total FROM bookings WHERE slot_id = ? AND status = 'Booked'",
        (slot_id,),
    ).fetchone()["total"]
    return slot["capacity"] - booked


def update_slot_status(connection, slot_id):
    seats = get_slot_seats(connection, slot_id)
    status = "Full" if seats <= 0 else "Available"
    connection.execute("UPDATE slots SET status = ? WHERE id = ?", (status, slot_id))


def exam_for_booking(connection, booking_id):
    return connection.execute(
        """SELECT b.*, s.examination_id, s.examiner_id, s.exam_date,
                  s.start_time, s.end_time, e.name AS exam_name,
                  e.booking_end, e.max_marks, u.name AS examiner_name,
                  student.name AS student_name
           FROM bookings b
           JOIN slots s ON s.id = b.slot_id
           JOIN examinations e ON e.id = s.examination_id
           JOIN users u ON u.id = s.examiner_id
           JOIN users student ON student.id = b.student_id
           WHERE b.id = ?""",
        (booking_id,),
    ).fetchone()


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_database().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            if user["status"] != "active":
                flash("Your account is waiting for admin approval.", "warning")
                return render_template("login.html")
            session.clear()
            session["user_id"] = user["id"]
            flash("Welcome back, " + user["name"].split()[0] + ".", "success")
            return redirect(url_for("dashboard"))
        flash("Email or password was not correct.", "danger")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")
        department = request.form.get("department", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name or not email or len(password) < 6:
            flash("Add your name and email, and use a password with at least 6 characters.", "danger")
        elif role not in ("student", "examiner"):
            flash("Choose student or examiner for your account.", "danger")
        else:
            status = "pending" if role == "examiner" else "active"
            try:
                get_database().execute(
                    """INSERT INTO users (name, email, password_hash, role, department, phone, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name, email, generate_password_hash(password, method="pbkdf2:sha256"), role, department, phone, status),
                )
                get_database().commit()
                if role == "examiner":
                    flash("Registered. You can log in once the admin approves your account.", "success")
                else:
                    flash("Registered successfully. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "danger")

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required("admin", "examiner", "student")
def dashboard():
    role = g.user["role"]
    connection = get_database()

    if role == "admin":
        totals = {
            "courses": connection.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
            "exams": connection.execute("SELECT COUNT(*) FROM examinations").fetchone()[0],
            "examiners": connection.execute("SELECT COUNT(*) FROM users WHERE role = 'examiner'").fetchone()[0],
            "students": connection.execute("SELECT COUNT(*) FROM users WHERE role = 'student'").fetchone()[0],
            "slots": connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0],
            "bookings": connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0],
        }
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'examiner' AND status = 'pending'"
        ).fetchone()[0]
        latest_bookings = connection.execute(
            """SELECT b.id, b.status, s.exam_date, s.start_time,
                      student.name AS student_name, e.name AS exam_name
               FROM bookings b JOIN slots s ON s.id = b.slot_id
               JOIN examinations e ON e.id = s.examination_id
               JOIN users student ON student.id = b.student_id
               ORDER BY b.id DESC LIMIT 6"""
        ).fetchall()
        return render_template("admin_dashboard.html", totals=totals,
                               pending_count=pending_count, bookings=latest_bookings)

    if role == "examiner":
        exams = connection.execute(
            """SELECT e.*, c.course_code FROM examinations e
               JOIN courses c ON c.id = e.course_id
               JOIN examiner_examinations ee ON ee.examination_id = e.id
               WHERE ee.examiner_id = ? ORDER BY e.slot_creation_start""",
            (g.user["id"],),
        ).fetchall()
        slots = connection.execute(
            """SELECT s.*, e.name AS exam_name,
                      (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'Booked') AS booked
               FROM slots s JOIN examinations e ON e.id = s.examination_id
               WHERE s.examiner_id = ? ORDER BY s.exam_date, s.start_time LIMIT 8""",
            (g.user["id"],),
        ).fetchall()
        pending_evaluations = connection.execute(
            """SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id = b.slot_id
               LEFT JOIN evaluations ev ON ev.booking_id = b.id
               WHERE s.examiner_id = ? AND b.status = 'Booked' AND ev.id IS NULL""",
            (g.user["id"],),
        ).fetchone()[0]
        booked_total = connection.execute(
            """SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id = b.slot_id
               WHERE s.examiner_id = ? AND b.status = 'Booked'""", (g.user["id"],)
        ).fetchone()[0]
        return render_template("examiner_dashboard.html", exams=exams, slots=slots,
                               pending_evaluations=pending_evaluations, booked_total=booked_total)

    available_exams = connection.execute(
        """SELECT e.*, c.course_code, c.course_name,
                  (SELECT COUNT(*) FROM slots s WHERE s.examination_id = e.id
                   AND s.exam_date >= ? AND s.status NOT IN ('Cancelled', 'Completed')
                   AND s.capacity > (SELECT COUNT(*) FROM bookings b
                                     WHERE b.slot_id = s.id AND b.status = 'Booked')) AS slot_count
           FROM examinations e JOIN courses c ON c.id = e.course_id
           WHERE e.status = 'Booking Open' ORDER BY e.booking_start""", (today_text(),)
    ).fetchall()
    bookings = connection.execute(
        """SELECT b.*, e.name AS exam_name, s.exam_date, s.start_time, s.end_time,
                  examiner.name AS examiner_name, ev.marks, ev.published
           FROM bookings b JOIN slots s ON s.id = b.slot_id
           JOIN examinations e ON e.id = s.examination_id
           JOIN users examiner ON examiner.id = s.examiner_id
           LEFT JOIN evaluations ev ON ev.booking_id = b.id
           WHERE b.student_id = ? AND b.status = 'Booked'
           ORDER BY s.exam_date, s.start_time LIMIT 8""", (g.user["id"],)
    ).fetchall()
    return render_template("student_dashboard.html", exams=available_exams, bookings=bookings)


# ---------- admin ----------

@app.route("/admin/courses", methods=["GET", "POST"])
@login_required("admin")
def admin_courses():
    connection = get_database()
    if request.method == "POST":
        course_id = request.form.get("course_id", "").strip()
        code = request.form.get("course_code", "").strip().upper()
        name = request.form.get("course_name", "").strip()
        description = request.form.get("description", "").strip()
        if not code or not name:
            flash("Course code and course name are required.", "danger")
        else:
            try:
                if course_id:
                    connection.execute(
                        "UPDATE courses SET course_code = ?, course_name = ?, description = ? WHERE id = ?",
                        (code, name, description, course_id),
                    )
                    flash("Course updated.", "success")
                else:
                    connection.execute(
                        "INSERT INTO courses (course_code, course_name, description) VALUES (?, ?, ?)",
                        (code, name, description),
                    )
                    flash("Course added.", "success")
                connection.commit()
                return redirect(url_for("admin_courses"))
            except sqlite3.IntegrityError:
                flash("That course code is already in use.", "danger")

    courses = connection.execute("SELECT * FROM courses ORDER BY course_code").fetchall()
    edit_course = None
    if request.args.get("edit", "").isdigit():
        edit_course = connection.execute(
            "SELECT * FROM courses WHERE id = ?", (request.args["edit"],)
        ).fetchone()
    return render_template("admin_courses.html", courses=courses, edit_course=edit_course)


@app.route("/admin/courses/<int:course_id>/delete", methods=["POST"])
@login_required("admin")
def delete_course(course_id):
    try:
        get_database().execute("DELETE FROM courses WHERE id = ?", (course_id,))
        get_database().commit()
        flash("Course removed.", "success")
    except sqlite3.IntegrityError:
        flash("Remove this course's examinations before deleting the course.", "warning")
    return redirect(url_for("admin_courses"))


@app.route("/admin/examiners", methods=["GET", "POST"])
@login_required("admin")
def admin_examiners():
    if request.method == "POST":
        examiner_id = request.form.get("examiner_id", "")
        status = request.form.get("status", "")
        if status not in ("active", "inactive", "pending"):
            abort(400)
        get_database().execute(
            "UPDATE users SET status = ? WHERE id = ? AND role = 'examiner'",
            (status, examiner_id),
        )
        get_database().commit()
        flash("Examiner account updated.", "success")
        return redirect(url_for("admin_examiners"))

    examiners = get_database().execute(
        "SELECT * FROM users WHERE role = 'examiner' ORDER BY status, name"
    ).fetchall()
    return render_template("admin_examiners.html", examiners=examiners)


@app.route("/admin/exams", methods=["GET", "POST"])
@login_required("admin")
def admin_exams():
    connection = get_database()
    if request.method == "POST":
        exam_id = request.form.get("exam_id", "").strip()
        course_id = request.form.get("course_id", "")
        name = request.form.get("name", "").strip()
        exam_type = request.form.get("exam_type", "")
        duration = request.form.get("duration_minutes", "")
        max_marks = request.form.get("max_marks", "")
        slot_start = request.form.get("slot_creation_start", "")
        slot_end = request.form.get("slot_creation_end", "")
        booking_start = request.form.get("booking_start", "")
        booking_end = request.form.get("booking_end", "")
        status = request.form.get("status", "Draft")
        assigned = request.form.getlist("examiner_ids")

        error = None
        try:
            duration_number = int(duration)
            marks_number = int(max_marks)
        except ValueError:
            duration_number = marks_number = 0

        if not name or not course_id:
            error = "Name and course are required."
        elif exam_type not in EXAM_TYPES or status not in EXAM_STATUSES:
            error = "Invalid exam type or status."
        elif duration_number < 1 or marks_number < 1:
            error = "Duration and max marks must be at least 1."
        elif not (slot_start and slot_end and booking_start and booking_end):
            error = "All four dates are required."
        elif slot_start > slot_end or booking_start > booking_end:
            error = "A start date cannot be after its end date."

        if error:
            flash(error, "danger")
            return redirect(url_for("admin_exams", edit=exam_id) if exam_id else url_for("admin_exams"))

        if exam_id:
            connection.execute(
                """UPDATE examinations SET course_id = ?, name = ?, exam_type = ?,
                   duration_minutes = ?, max_marks = ?, slot_creation_start = ?,
                   slot_creation_end = ?, booking_start = ?, booking_end = ?, status = ?
                   WHERE id = ?""",
                (course_id, name, exam_type, duration_number, marks_number, slot_start,
                 slot_end, booking_start, booking_end, status, exam_id),
            )
            saved_id = int(exam_id)
        else:
            cursor = connection.execute(
                """INSERT INTO examinations
                   (course_id, name, exam_type, duration_minutes, max_marks,
                    slot_creation_start, slot_creation_end, booking_start, booking_end, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (course_id, name, exam_type, duration_number, marks_number, slot_start,
                 slot_end, booking_start, booking_end, status),
            )
            saved_id = cursor.lastrowid

        connection.execute("DELETE FROM examiner_examinations WHERE examination_id = ?", (saved_id,))
        for examiner_id in assigned:
            connection.execute(
                """INSERT OR IGNORE INTO examiner_examinations (examiner_id, examination_id)
                   SELECT id, ? FROM users WHERE id = ? AND role = 'examiner' AND status = 'active'""",
                (saved_id, examiner_id),
            )
        connection.commit()
        flash("Examination saved.", "success")
        return redirect(url_for("admin_exams"))

    exams = connection.execute(
        """SELECT e.*, c.course_code, c.course_name,
                  (SELECT GROUP_CONCAT(u.name, ', ') FROM examiner_examinations ee
                   JOIN users u ON u.id = ee.examiner_id WHERE ee.examination_id = e.id) AS examiner_names
           FROM examinations e JOIN courses c ON c.id = e.course_id
           ORDER BY e.id DESC"""
    ).fetchall()
    courses = connection.execute("SELECT * FROM courses ORDER BY course_code").fetchall()
    examiners = connection.execute(
        "SELECT id, name FROM users WHERE role = 'examiner' AND status = 'active' ORDER BY name"
    ).fetchall()
    edit_exam = None
    assigned_ids = []
    if request.args.get("edit", "").isdigit():
        edit_exam = connection.execute(
            "SELECT * FROM examinations WHERE id = ?", (request.args["edit"],)
        ).fetchone()
        if edit_exam:
            assigned_ids = [row[0] for row in connection.execute(
                "SELECT examiner_id FROM examiner_examinations WHERE examination_id = ?",
                (edit_exam["id"],),
            ).fetchall()]
    return render_template("admin_exams.html", exams=exams, courses=courses,
                           examiners=examiners, edit_exam=edit_exam, assigned_ids=assigned_ids,
                           exam_types=EXAM_TYPES, exam_statuses=EXAM_STATUSES)


@app.route("/admin/exams/<int:exam_id>/delete", methods=["POST"])
@login_required("admin")
def delete_exam(exam_id):
    connection = get_database()
    used = connection.execute(
        "SELECT COUNT(*) FROM slots WHERE examination_id = ?", (exam_id,)
    ).fetchone()[0]
    if used:
        flash("This exam has slots or history, so it cannot be deleted.", "warning")
    else:
        connection.execute("DELETE FROM examinations WHERE id = ?", (exam_id,))
        connection.commit()
        flash("Examination removed.", "success")
    return redirect(url_for("admin_exams"))


@app.route("/admin/exams/<int:exam_id>/rubrics", methods=["GET", "POST"])
@login_required("admin")
def admin_rubrics(exam_id):
    connection = get_database()
    exam = connection.execute("SELECT * FROM examinations WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        abort(404)

    if request.method == "POST":
        rubric_id = request.form.get("rubric_id", "").strip()
        criterion = request.form.get("criterion_name", "").strip()
        description = request.form.get("description", "").strip()
        try:
            max_marks = int(request.form.get("max_marks", ""))
            weightage = int(request.form.get("weightage", "0"))
            if not criterion or max_marks < 1 or weightage < 0:
                raise ValueError
        except ValueError:
            flash("Add a criterion and enter valid marks and weightage.", "danger")
            return redirect(url_for("admin_rubrics", exam_id=exam_id))

        if rubric_id:
            connection.execute(
                """UPDATE rubrics SET criterion_name = ?, max_marks = ?, weightage = ?, description = ?
                   WHERE id = ? AND examination_id = ?""",
                (criterion, max_marks, weightage, description, rubric_id, exam_id),
            )
            flash("Rubric updated.", "success")
        else:
            connection.execute(
                """INSERT INTO rubrics (examination_id, criterion_name, max_marks, weightage, description)
                   VALUES (?, ?, ?, ?, ?)""",
                (exam_id, criterion, max_marks, weightage, description),
            )
            flash("Rubric criterion added.", "success")
        connection.commit()
        return redirect(url_for("admin_rubrics", exam_id=exam_id))

    rubrics = connection.execute(
        "SELECT * FROM rubrics WHERE examination_id = ? ORDER BY id", (exam_id,)
    ).fetchall()
    edit_rubric = None
    if request.args.get("edit", "").isdigit():
        edit_rubric = connection.execute(
            "SELECT * FROM rubrics WHERE id = ? AND examination_id = ?",
            (request.args["edit"], exam_id),
        ).fetchone()
    return render_template("admin_rubrics.html", exam=exam, rubrics=rubrics,
                           edit_rubric=edit_rubric)


@app.route("/admin/rubrics/<int:rubric_id>/delete", methods=["POST"])
@login_required("admin")
def delete_rubric(rubric_id):
    connection = get_database()
    rubric = connection.execute(
        "SELECT examination_id FROM rubrics WHERE id = ?", (rubric_id,)
    ).fetchone()
    if rubric:
        try:
            connection.execute("DELETE FROM rubrics WHERE id = ?", (rubric_id,))
            connection.commit()
            flash("Rubric criterion removed.", "success")
        except sqlite3.IntegrityError:
            flash("This criterion has already been used in an evaluation.", "warning")
        return redirect(url_for("admin_rubrics", exam_id=rubric["examination_id"]))
    abort(404)


@app.route("/admin/slots", methods=["GET", "POST"])
@login_required("admin")
def admin_slots():
    connection = get_database()
    if request.method == "POST":
        slot_id = request.form.get("slot_id", "")
        examiner_id = request.form.get("examiner_id", "")
        examiner = connection.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'examiner' AND status = 'active'",
            (examiner_id,),
        ).fetchone()
        if examiner:
            connection.execute("UPDATE slots SET examiner_id = ? WHERE id = ?", (examiner_id, slot_id))
            connection.commit()
            flash("Examiner assignment changed.", "success")
        else:
            flash("Choose an active examiner.", "danger")
        return redirect(url_for("admin_slots"))

    slots = connection.execute(
        """SELECT s.*, e.name AS exam_name, c.course_code, u.name AS examiner_name,
                  (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'Booked') AS booked
           FROM slots s JOIN examinations e ON e.id = s.examination_id
           JOIN courses c ON c.id = e.course_id JOIN users u ON u.id = s.examiner_id
           ORDER BY s.exam_date DESC, s.start_time"""
    ).fetchall()
    examiners = connection.execute(
        "SELECT id, name FROM users WHERE role = 'examiner' AND status = 'active' ORDER BY name"
    ).fetchall()
    return render_template("admin_slots.html", slots=slots, examiners=examiners)


@app.route("/admin/bookings", methods=["GET", "POST"])
@login_required("admin")
def admin_bookings():
    connection = get_database()
    if request.method == "POST":
        booking_id = request.form.get("booking_id", "")
        new_slot_id = request.form.get("slot_id", "")
        booking = exam_for_booking(connection, booking_id)
        new_slot = connection.execute(
            "SELECT * FROM slots WHERE id = ? AND examination_id = ?",
            (new_slot_id, booking["examination_id"] if booking else -1),
        ).fetchone()
        if not booking or not new_slot or new_slot["status"] in ("Cancelled", "Completed"):
            flash("Choose a valid slot for the same examination.", "danger")
        elif new_slot["id"] != booking["slot_id"] and get_slot_seats(connection, new_slot["id"]) < 1:
            flash("That slot has no seats left.", "warning")
        else:
            duplicate = connection.execute(
                """SELECT b.id FROM bookings b JOIN slots s ON s.id = b.slot_id
                   WHERE b.student_id = ? AND s.examination_id = ?
                   AND b.status = 'Booked' AND b.id != ?""",
                (booking["student_id"], booking["examination_id"], booking_id),
            ).fetchone()
            if duplicate:
                flash("This student already has another active booking for the same examination.", "warning")
            else:
                old_slot_id = booking["slot_id"]
                connection.execute("UPDATE bookings SET slot_id = ? WHERE id = ?", (new_slot["id"], booking_id))
                update_slot_status(connection, old_slot_id)
                update_slot_status(connection, new_slot["id"])
                connection.commit()
                flash("Booking rescheduled.", "success")
        return redirect(url_for("admin_bookings"))

    bookings = connection.execute(
        """SELECT b.*, student.name AS student_name, student.email AS student_email,
                  e.id AS examination_id, e.name AS exam_name,
                  s.exam_date, s.start_time, s.end_time, examiner.name AS examiner_name
           FROM bookings b JOIN users student ON student.id = b.student_id
           JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = s.examination_id
           JOIN users examiner ON examiner.id = s.examiner_id
           ORDER BY b.id DESC"""
    ).fetchall()
    slots_by_exam = {}
    for booking in bookings:
        if booking["examination_id"] not in slots_by_exam:
            slots_by_exam[booking["examination_id"]] = connection.execute(
                """SELECT s.id, s.exam_date, s.start_time, s.end_time, s.status,
                          u.name AS examiner_name,
                          (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'Booked') AS booked,
                          s.capacity
                   FROM slots s JOIN users u ON u.id = s.examiner_id
                   WHERE s.examination_id = ? ORDER BY s.exam_date, s.start_time""",
                (booking["examination_id"],),
            ).fetchall()
    return render_template("admin_bookings.html", bookings=bookings, slots_by_exam=slots_by_exam)


@app.route("/admin/search")
@login_required("admin")
def admin_search():
    term = request.args.get("q", "").strip()
    users = []
    exams = []
    bookings = []
    if term:
        search = "%" + term + "%"
        connection = get_database()
        users = connection.execute(
            """SELECT id, name, email, role, status FROM users
               WHERE role != 'admin' AND (name LIKE ? OR email LIKE ? OR CAST(id AS TEXT) LIKE ?)
               ORDER BY role, name LIMIT 30""", (search, search, search)
        ).fetchall()
        exams = connection.execute(
            """SELECT e.id, e.name, e.status, c.course_code FROM examinations e
               JOIN courses c ON c.id = e.course_id
               WHERE e.name LIKE ? OR c.course_code LIKE ? OR CAST(e.id AS TEXT) LIKE ?
               ORDER BY e.id DESC LIMIT 30""", (search, search, search)
        ).fetchall()
        bookings = connection.execute(
            """SELECT b.id, b.status, student.name AS student_name, e.name AS exam_name,
                      s.exam_date, s.start_time
               FROM bookings b JOIN users student ON student.id = b.student_id
               JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = s.examination_id
               WHERE student.name LIKE ? OR e.name LIKE ? OR CAST(b.id AS TEXT) LIKE ?
               ORDER BY b.id DESC LIMIT 30""", (search, search, search)
        ).fetchall()
    return render_template("admin_search.html", term=term, users=users, exams=exams, bookings=bookings)


@app.route("/admin/students")
@login_required("admin")
def admin_students():
    term = request.args.get("q", "").strip()
    connection = get_database()
    if term:
        pattern = "%" + term + "%"
        students = connection.execute(
            """SELECT * FROM users WHERE role = 'student'
               AND (name LIKE ? OR email LIKE ? OR CAST(id AS TEXT) LIKE ?)
               ORDER BY name LIMIT 100""", (pattern, pattern, pattern)
        ).fetchall()
    else:
        students = connection.execute(
            "SELECT * FROM users WHERE role = 'student' ORDER BY name LIMIT 100"
        ).fetchall()
    return render_template("admin_students.html", students=students, term=term)


# ---------- examiner ----------

@app.route("/examiner/slots", methods=["GET", "POST"])
@login_required("examiner")
def examiner_slots():
    connection = get_database()
    exams = connection.execute(
        """SELECT e.*, c.course_code FROM examinations e
           JOIN courses c ON c.id = e.course_id
           JOIN examiner_examinations ee ON ee.examination_id = e.id
           WHERE ee.examiner_id = ? ORDER BY e.name""", (g.user["id"],)
    ).fetchall()

    if request.method == "POST":
        slot_id = request.form.get("slot_id", "").strip()
        exam_id = request.form.get("examination_id", "")
        exam = connection.execute(
            """SELECT e.* FROM examinations e JOIN examiner_examinations ee ON ee.examination_id = e.id
               WHERE e.id = ? AND ee.examiner_id = ?""", (exam_id, g.user["id"])
        ).fetchone()
        exam_date = request.form.get("exam_date", "")
        start_time = request.form.get("start_time", "")
        end_time = request.form.get("end_time", "")
        existing_slot = None
        if slot_id.isdigit():
            existing_slot = connection.execute(
                "SELECT * FROM slots WHERE id = ? AND examiner_id = ?",
                (slot_id, g.user["id"]),
            ).fetchone()
        try:
            capacity = int(request.form.get("capacity", ""))
        except ValueError:
            capacity = 0

        error = None
        if not exam:
            error = "Choose one of your assigned examinations."
        elif slot_id and not existing_slot:
            error = "Slot not found."
        elif existing_slot:
            booked_count = connection.execute(
                "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'Booked'",
                (existing_slot["id"],),
            ).fetchone()[0]
            if existing_slot["examination_id"] != exam["id"]:
                error = "The examination of a slot cannot be changed."
            elif (booked_count or exam["status"] in ("Booking Open", "Completed")
                    or today_text() >= exam["booking_start"]):
                error = "This slot can no longer be edited because booking has started."
        elif exam["status"] != "Slot Creation":
            error = "Slots can only be created while the exam is in the Slot Creation stage."
        elif not date_window_is_open(exam["slot_creation_start"], exam["slot_creation_end"]):
            error = "The slot creation period for this exam is not open today."

        if not error:
            if not exam_date or exam_date < today_text():
                error = "The slot date cannot be in the past."
            elif not start_time or not end_time or start_time >= end_time:
                error = "End time must be after start time."
            elif capacity < 1:
                error = "Capacity must be at least 1."

        if error:
            flash(error, "danger")
        else:
            overlap_sql = """SELECT id FROM slots WHERE examiner_id = ? AND exam_date = ?
                             AND start_time < ? AND end_time > ?"""
            overlap_args = [g.user["id"], exam_date, end_time, start_time]
            if existing_slot:
                overlap_sql += " AND id != ?"
                overlap_args.append(existing_slot["id"])
            overlap = connection.execute(overlap_sql, overlap_args).fetchone()
            if overlap:
                flash("You already have a slot during that time.", "warning")
                return redirect(url_for("examiner_slots"))

            if existing_slot:
                connection.execute(
                    """UPDATE slots SET exam_date = ?, start_time = ?, end_time = ?, capacity = ?
                       WHERE id = ?""",
                    (exam_date, start_time, end_time, capacity, existing_slot["id"]),
                )
                update_slot_status(connection, existing_slot["id"])
                flash("Slot updated.", "success")
            else:
                connection.execute(
                    """INSERT INTO slots (examination_id, examiner_id, exam_date, start_time, end_time, capacity)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (exam_id, g.user["id"], exam_date, start_time, end_time, capacity),
                )
                flash("Slot created.", "success")
            connection.commit()
            return redirect(url_for("examiner_slots"))

    slots = connection.execute(
        """SELECT s.*, e.name AS exam_name, e.status AS exam_status,
                  (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'Booked') AS booked
           FROM slots s JOIN examinations e ON e.id = s.examination_id
           WHERE s.examiner_id = ? ORDER BY s.exam_date, s.start_time""", (g.user["id"],)
    ).fetchall()
    edit_slot = None
    if request.args.get("edit", "").isdigit():
        edit_slot = connection.execute(
            "SELECT * FROM slots WHERE id = ? AND examiner_id = ?",
            (request.args["edit"], g.user["id"]),
        ).fetchone()
    return render_template("examiner_slots.html", exams=exams, slots=slots,
                           edit_slot=edit_slot, today=today_text())


@app.route("/examiner/slots/<int:slot_id>/delete", methods=["POST"])
@login_required("examiner")
def delete_examiner_slot(slot_id):
    connection = get_database()
    slot = connection.execute(
        "SELECT * FROM slots WHERE id = ? AND examiner_id = ?", (slot_id, g.user["id"])
    ).fetchone()
    if not slot:
        abort(404)
    count = connection.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'Booked'", (slot_id,)
    ).fetchone()[0]
    exam = connection.execute(
        "SELECT status FROM examinations WHERE id = ?", (slot["examination_id"],)
    ).fetchone()
    if count or exam["status"] in ("Booking Open", "Completed"):
        flash("A slot cannot be removed after students have started booking it.", "warning")
    else:
        connection.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        connection.commit()
        flash("Slot removed.", "success")
    return redirect(url_for("examiner_slots"))


@app.route("/examiner/slots/<int:slot_id>")
@login_required("examiner")
def examiner_slot_detail(slot_id):
    connection = get_database()
    slot = connection.execute(
        """SELECT s.*, e.name AS exam_name FROM slots s JOIN examinations e ON e.id = s.examination_id
           WHERE s.id = ? AND s.examiner_id = ?""", (slot_id, g.user["id"])
    ).fetchone()
    if slot is None:
        abort(404)
    students = connection.execute(
        """SELECT b.id AS booking_id, b.status AS booking_status, u.name, u.email,
                  ev.marks, ev.published
           FROM bookings b JOIN users u ON u.id = b.student_id
           LEFT JOIN evaluations ev ON ev.booking_id = b.id
           WHERE b.slot_id = ? ORDER BY u.name""", (slot_id,)
    ).fetchall()
    return render_template("examiner_slot_detail.html", slot=slot, students=students)


@app.route("/examiner/bookings/<int:booking_id>/evaluate", methods=["GET", "POST"])
@login_required("examiner")
def evaluate_student(booking_id):
    connection = get_database()
    booking = exam_for_booking(connection, booking_id)
    if booking is None or booking["examiner_id"] != g.user["id"]:
        abort(404)
    if booking["status"] != "Booked":
        flash("Only an active booking can be evaluated.", "warning")
        return redirect(url_for("examiner_slot_detail", slot_id=booking["slot_id"]))

    rubrics = connection.execute(
        "SELECT * FROM rubrics WHERE examination_id = ? ORDER BY id",
        (booking["examination_id"],),
    ).fetchall()
    evaluation = connection.execute(
        "SELECT * FROM evaluations WHERE booking_id = ?", (booking_id,)
    ).fetchone()
    saved_scores = {}
    if evaluation:
        saved_scores = {
            row["rubric_id"]: row["marks"]
            for row in connection.execute(
                "SELECT rubric_id, marks FROM rubric_scores WHERE evaluation_id = ?",
                (evaluation["id"],),
            ).fetchall()
        }

    if request.method == "POST":
        marks_by_rubric = {}
        try:
            for rubric in rubrics:
                mark = int(request.form.get("rubric_" + str(rubric["id"]), "0"))
                if mark < 0 or mark > rubric["max_marks"]:
                    raise ValueError
                marks_by_rubric[rubric["id"]] = mark
            total_marks = sum(marks_by_rubric.values()) if rubrics else int(request.form.get("marks", ""))
            if total_marks < 0 or total_marks > booking["max_marks"]:
                raise ValueError
        except ValueError:
            flash("Enter marks within the limits shown on the rubric.", "danger")
            return redirect(url_for("evaluate_student", booking_id=booking_id))

        remarks = request.form.get("remarks", "").strip()
        published = 1 if request.form.get("action") == "publish" else 0
        if evaluation:
            connection.execute(
                """UPDATE evaluations SET marks = ?, remarks = ?, published = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (total_marks, remarks, published, evaluation["id"]),
            )
            evaluation_id = evaluation["id"]
            connection.execute("DELETE FROM rubric_scores WHERE evaluation_id = ?", (evaluation_id,))
        else:
            cursor = connection.execute(
                """INSERT INTO evaluations (booking_id, marks, remarks, published)
                   VALUES (?, ?, ?, ?)""", (booking_id, total_marks, remarks, published)
            )
            evaluation_id = cursor.lastrowid
        for rubric_id, mark in marks_by_rubric.items():
            connection.execute(
                "INSERT INTO rubric_scores (evaluation_id, rubric_id, marks) VALUES (?, ?, ?)",
                (evaluation_id, rubric_id, mark),
            )
        connection.commit()
        flash("Result published." if published else "Evaluation saved as a draft.", "success")
        return redirect(url_for("examiner_slot_detail", slot_id=booking["slot_id"]))

    return render_template("evaluate_student.html", booking=booking, rubrics=rubrics,
                           evaluation=evaluation, saved_scores=saved_scores)


# ---------- student ----------

@app.route("/student/exams")
@login_required("student")
def student_exams():
    term = request.args.get("q", "").strip()
    course_id = request.args.get("course_id", "")
    exam_type = request.args.get("exam_type", "")
    connection = get_database()
    if not course_id.isdigit():
        course_id = ""
    if exam_type not in EXAM_TYPES:
        exam_type = ""
    pattern = "%" + term + "%"
    exams = connection.execute(
        """SELECT e.*, c.course_code, c.course_name FROM examinations e
           JOIN courses c ON c.id = e.course_id WHERE e.status = 'Booking Open'
           AND (? = '' OR e.name LIKE ? OR c.course_code LIKE ?
                OR c.course_name LIKE ? OR e.exam_type LIKE ?)
           AND (? = '' OR c.id = ?)
           AND (? = '' OR e.exam_type = ?)
           ORDER BY e.booking_start""",
        (term, pattern, pattern, pattern, pattern,
         course_id, course_id, exam_type, exam_type),
    ).fetchall()
    courses = connection.execute("SELECT * FROM courses WHERE status = 'Active' ORDER BY course_code").fetchall()
    slots_by_exam = {}
    for exam in exams:
        slots_by_exam[exam["id"]] = connection.execute(
            """SELECT s.*, u.name AS examiner_name,
                      (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'Booked') AS booked
               FROM slots s JOIN users u ON u.id = s.examiner_id
               WHERE s.examination_id = ? AND s.status NOT IN ('Cancelled', 'Completed')
               ORDER BY s.exam_date, s.start_time""", (exam["id"],)
        ).fetchall()
    return render_template("student_exams.html", exams=exams, slots_by_exam=slots_by_exam,
                           courses=courses, term=term, course_id=course_id,
                           exam_type=exam_type, exam_types=EXAM_TYPES, today=today_text())


@app.route("/student/book/<int:slot_id>", methods=["POST"])
@login_required("student")
def book_slot(slot_id):
    connection = get_database()
    slot = connection.execute(
        """SELECT s.*, e.status AS exam_status, e.name AS exam_name,
                  e.booking_start, e.booking_end
           FROM slots s JOIN examinations e ON e.id = s.examination_id WHERE s.id = ?""",
        (slot_id,),
    ).fetchone()
    if slot is None:
        abort(404)
    if slot["exam_status"] != "Booking Open" or not date_window_is_open(
        slot["booking_start"], slot["booking_end"]
    ):
        flash("Booking is not open for this examination today.", "warning")
    elif slot["exam_date"] < today_text() or slot["status"] in ("Cancelled", "Completed"):
        flash("This examination slot is no longer available.", "warning")
    else:
        # BEGIN IMMEDIATE locks the db so two students can't grab the last seat together
        connection.execute("BEGIN IMMEDIATE")
        if get_slot_seats(connection, slot_id) < 1:
            update_slot_status(connection, slot_id)
            connection.commit()
            flash("This slot is already full.", "warning")
        else:
            existing = connection.execute(
                """SELECT b.id FROM bookings b JOIN slots old_slot ON old_slot.id = b.slot_id
                   WHERE b.student_id = ? AND old_slot.examination_id = ? AND b.status = 'Booked'""",
                (g.user["id"], slot["examination_id"]),
            ).fetchone()
            if existing:
                connection.rollback()
                flash("You already have a booking for this examination.", "warning")
            else:
                connection.execute(
                    "INSERT INTO bookings (student_id, slot_id) VALUES (?, ?)",
                    (g.user["id"], slot_id),
                )
                update_slot_status(connection, slot_id)
                connection.commit()
                flash("Your slot is booked.", "success")
    return redirect(url_for("student_exams"))


@app.route("/student/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required("student")
def cancel_booking(booking_id):
    connection = get_database()
    booking = connection.execute(
        """SELECT b.*, s.id AS actual_slot_id, s.exam_date, e.booking_end
           FROM bookings b JOIN slots s ON s.id = b.slot_id
           JOIN examinations e ON e.id = s.examination_id
           WHERE b.id = ? AND b.student_id = ?""", (booking_id, g.user["id"])
    ).fetchone()
    if not booking or booking["status"] != "Booked":
        flash("That booking is no longer active.", "warning")
    elif today_text() > booking["booking_end"] or today_text() > booking["exam_date"]:
        flash("The cancellation deadline has passed.", "warning")
    else:
        connection.execute("UPDATE bookings SET status = 'Cancelled' WHERE id = ?", (booking_id,))
        connection.execute("DELETE FROM evaluations WHERE booking_id = ?", (booking_id,))
        update_slot_status(connection, booking["actual_slot_id"])
        connection.commit()
        flash("Booking cancelled.", "success")
    return redirect(url_for("dashboard"))


@app.route("/student/history")
@login_required("student")
def student_history():
    bookings = get_database().execute(
        """SELECT b.*, e.name AS exam_name, c.course_code, s.exam_date, s.start_time,
                  s.end_time, examiner.name AS examiner_name,
                  ev.marks, ev.remarks, ev.published, e.max_marks
           FROM bookings b JOIN slots s ON s.id = b.slot_id
           JOIN examinations e ON e.id = s.examination_id
           JOIN courses c ON c.id = e.course_id JOIN users examiner ON examiner.id = s.examiner_id
           LEFT JOIN evaluations ev ON ev.booking_id = b.id
           WHERE b.student_id = ? ORDER BY s.exam_date DESC, s.start_time DESC""",
        (g.user["id"],),
    ).fetchall()
    return render_template("student_history.html", bookings=bookings)


@app.route("/student/profile", methods=["GET", "POST"])
@login_required("student")
def student_profile():
    connection = get_database()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        department = request.form.get("department", "").strip()
        if not name:
            flash("Your name cannot be empty.", "danger")
        else:
            connection.execute(
                "UPDATE users SET name = ?, phone = ?, department = ? WHERE id = ?",
                (name, phone, department, g.user["id"]),
            )
            connection.commit()
            flash("Profile saved.", "success")
            return redirect(url_for("student_profile"))
    user = connection.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    return render_template("student_profile.html", user=user)


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You do not have access to this page."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="We could not find that page."), 404


create_database()


if __name__ == "__main__":
    app.run(debug=True)
