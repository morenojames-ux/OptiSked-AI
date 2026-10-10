import os
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from ml_scheduler import train_and_rank
import ai_generator

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
DB_PATH = os.path.join(INSTANCE_DIR, 'optisked.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
try:
    APP_TZ = ZoneInfo(os.environ.get('OPTISKED_TIMEZONE', 'Asia/Manila'))
except Exception:
    try:
        APP_TZ = ZoneInfo('UTC+8')  # fallback for Asia/Manila offset if db missing
    except Exception:
        APP_TZ = ZoneInfo('UTC')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('OPTISKED_SECRET', 'change-this-in-production')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
DAY_ORDER = {d: i for i, d in enumerate(DAYS)}
DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
DAY_ORDER = {d: i for i, d in enumerate(DAYS)}
# Seed data for the editable, institution-wide room list (see TABLE rooms).
DEFAULT_ROOM_GROUPS = {
    'lab': [201, 202, 301, 103, 104, 105, 106],
    'lecture': list(range(203, 225)) + list(range(303, 311)),
}
ROOM_TYPE_LABELS = {'lab': 'Computer Lab', 'lecture': 'Lecture Room'}
PROGRAMS = [
    'BS Computer Science', 'BS Business Administration', 'BS Nursing',
    'BS Civil Engineering', 'BS Tourism Management', 'BS Hospitality Management',
    'BS Education', 'BS Criminology', 'AB Psychology'
]
YEAR_LEVELS = ['Year 1', 'Year 2', 'Year 3', 'Year 4']
DURATION_OPTIONS = ['1', '1.5', '2', '2.5', '3', '3.5', '4']
LAB_PATTERNS = [
    {'value': 'whole_lab', 'label': 'Whole session is Laboratory',
     'help': 'One lab meeting per week in a Computer Lab.'},
    {'value': 'half_half', 'label': 'Half lecture, half laboratory',
     'help': 'One day: a lecture block immediately followed by a lab block.'},
    {'value': 'twice_both_lab', 'label': 'Meets twice a week (both Laboratory)',
     'help': 'Two lab meetings per week in a Computer Lab.'},
    {'value': 'twice_lab_lecture', 'label': 'Meets twice a week (Lab and Lecture)',
     'help': 'One laboratory meeting and one lecture meeting per week.'},
]
LAB_PATTERN_VALUES = {p['value'] for p in LAB_PATTERNS}


def db():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def ensure_column(conn, table, column, definition):
    cols = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def room_label(name, room_type):
    """Human-readable room name; keeps the type keyword for room_type detection."""
    return f"Room {name} — {ROOM_TYPE_LABELS.get(room_type, 'Lecture Room')}"


def seed_default_rooms(conn):
    """Populate the global room list with the institution defaults once."""
    if conn.execute('SELECT COUNT(*) AS n FROM rooms').fetchone()['n']:
        return
    for room_type, numbers in DEFAULT_ROOM_GROUPS.items():
        for number in numbers:
            conn.execute('INSERT OR IGNORE INTO rooms(name,room_type) VALUES(?,?)',
                         (str(number), room_type))


def configured_rooms(conn):
    """All configured room labels, plus any rooms already stored on schedules."""
    rooms = []
    for row in conn.execute("SELECT name,room_type FROM rooms ORDER BY rowid").fetchall():
        label = room_label(row['name'], row['room_type'])
        if label not in rooms:
            rooms.append(label)
    for row in conn.execute("SELECT DISTINCT room FROM schedules WHERE room IS NOT NULL AND room<>''").fetchall():
        if row['room'] not in rooms:
            rooms.append(row['room'])
    return rooms


def room_type_options():
    return [{'value': k, 'label': v} for k, v in ROOM_TYPE_LABELS.items()]


def init_db():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    conn = db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('program_head','teacher','student')),
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        full_name TEXT NOT NULL,
        program TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        full_name TEXT NOT NULL,
        username TEXT NOT NULL,
        subject_qualification TEXT DEFAULT '',
        available_from TEXT DEFAULT '07:30',
        available_until TEXT DEFAULT '17:00',
        preferred_days TEXT DEFAULT 'Mon,Tue,Wed,Thu,Fri,Sat',
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        teacher_name TEXT NOT NULL,
        username TEXT DEFAULT '',
        subject_code TEXT NOT NULL,
        subject_name TEXT NOT NULL,
        program TEXT NOT NULL,
        year_level TEXT NOT NULL,
        section TEXT NOT NULL,
        day TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        room TEXT NOT NULL,
        status TEXT DEFAULT 'Published',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS leaves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_name TEXT NOT NULL,
        leave_date TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT DEFAULT 'Pending',
        schedule_id INTEGER,
        program TEXT DEFAULT '',
        year_level TEXT DEFAULT '',
        section TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_name TEXT NOT NULL,
        attendance_date TEXT NOT NULL,
        status TEXT NOT NULL,
        photo_name TEXT DEFAULT '',
        schedule_id INTEGER,
        program TEXT DEFAULT '',
        year_level TEXT DEFAULT '',
        section TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS class_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        code_type TEXT NOT NULL,
        schedule_id INTEGER,
        teacher_name TEXT DEFAULT '',
        subject_code TEXT DEFAULT '',
        subject_name TEXT DEFAULT '',
        program TEXT DEFAULT '',
        year_level TEXT DEFAULT '',
        section TEXT DEFAULT '',
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS student_classes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_user_id INTEGER NOT NULL,
        schedule_id INTEGER NOT NULL,
        FOREIGN KEY(student_user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE,
        UNIQUE(student_user_id, schedule_id)
    );
    CREATE TABLE IF NOT EXISTS student_attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER NOT NULL,
        student_user_id INTEGER NOT NULL,
        attendance_date TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('Present','Absent','Excused')),
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE,
        FOREIGN KEY(student_user_id) REFERENCES users(id) ON DELETE CASCADE,
        UNIQUE(schedule_id, student_user_id, attendance_date)
    );
    CREATE TABLE IF NOT EXISTS program_sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        program TEXT NOT NULL,
        year_level TEXT NOT NULL,
        section_name TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(program, year_level, section_name)
    );
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        program TEXT NOT NULL,
        course_code TEXT NOT NULL,
        course_name TEXT NOT NULL,
        year_level TEXT NOT NULL,
        sessions_per_week INTEGER DEFAULT 1,
        has_lab INTEGER DEFAULT 0,
        lab_pattern TEXT DEFAULT '',
        duration_hours REAL DEFAULT 1.5,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS instructors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        program TEXT NOT NULL,
        name TEXT NOT NULL,
        user_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS instructor_unavailability (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instructor_id INTEGER NOT NULL,
        day_from TEXT NOT NULL,
        day_to TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        FOREIGN KEY(instructor_id) REFERENCES instructors(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS instructor_qualifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instructor_id INTEGER NOT NULL,
        course_code TEXT NOT NULL,
        FOREIGN KEY(instructor_id) REFERENCES instructors(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        room_type TEXT NOT NULL CHECK(room_type IN ('lab','lecture'))
    );
    CREATE TABLE IF NOT EXISTS course_rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL,
        room_id INTEGER NOT NULL,
        UNIQUE(course_id, room_id),
        FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE,
        FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
    );
    ''')
    seed_default_rooms(conn)
    # AI schedule generator compatibility columns.
    ensure_column(conn, 'schedules', 'session_type', "TEXT DEFAULT 'Lecture'")
    ensure_column(conn, 'schedules', 'course_id', 'INTEGER')
    ensure_column(conn, 'schedules', 'is_generated', 'INTEGER DEFAULT 0')
    # Link instructors to their registered teacher account.
    ensure_column(conn, 'instructors', 'user_id', 'INTEGER')
    conn.execute('''UPDATE instructors SET user_id=(
                        SELECT t.user_id FROM teachers t
                        WHERE lower(t.full_name)=lower(instructors.name) AND t.user_id IS NOT NULL
                        LIMIT 1)
                    WHERE user_id IS NULL''')
    # Compatibility with databases created by earlier OptiSked builds.
    ensure_column(conn, 'users', 'program', "TEXT DEFAULT ''")
    ensure_column(conn, 'schedules', 'status', "TEXT DEFAULT 'Published'")
    ensure_column(conn, 'leaves', 'schedule_id', 'INTEGER')
    ensure_column(conn, 'leaves', 'program', "TEXT DEFAULT ''")
    ensure_column(conn, 'leaves', 'year_level', "TEXT DEFAULT ''")
    ensure_column(conn, 'leaves', 'section', "TEXT DEFAULT ''")
    ensure_column(conn, 'attendance', 'schedule_id', 'INTEGER')
    ensure_column(conn, 'attendance', 'program', "TEXT DEFAULT ''")
    ensure_column(conn, 'attendance', 'year_level', "TEXT DEFAULT ''")
    ensure_column(conn, 'attendance', 'section', "TEXT DEFAULT ''")
    # Backfill metadata when legacy records already contain a schedule_id.
    conn.execute('''UPDATE leaves SET program=COALESCE((SELECT program FROM schedules s WHERE s.id=leaves.schedule_id), program),
                                  year_level=COALESCE((SELECT year_level FROM schedules s WHERE s.id=leaves.schedule_id), year_level),
                                  section=COALESCE((SELECT section FROM schedules s WHERE s.id=leaves.schedule_id), section)
                    WHERE schedule_id IS NOT NULL''')
    conn.execute('''UPDATE attendance SET program=COALESCE((SELECT program FROM schedules s WHERE s.id=attendance.schedule_id), program),
                                      year_level=COALESCE((SELECT year_level FROM schedules s WHERE s.id=attendance.schedule_id), year_level),
                                      section=COALESCE((SELECT section FROM schedules s WHERE s.id=attendance.schedule_id), section)
                    WHERE schedule_id IS NOT NULL''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_schedule_day_time ON schedules(day,start_time,end_time)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_code_schedule ON class_codes(schedule_id,code_type)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_leave_program_date ON leaves(program,leave_date,status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_attendance_schedule_date ON attendance(schedule_id,attendance_date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_student_attendance_schedule_date ON student_attendance(schedule_id,attendance_date)')
    conn.commit()
    conn.close()


def parse_minutes(value):
    try:
        h, m = map(int, str(value).split(':'))
        return h * 60 + m
    except Exception:
        return -1


def format_12hr(value):
    try:
        return datetime.strptime(value, '%H:%M').strftime('%I:%M %p').lstrip('0')
    except Exception:
        return value or ''




def now_local():
    """Return the application-local current datetime (Philippines by default)."""
    return datetime.now(APP_TZ)


def today_string():
    return now_local().strftime('%Y-%m-%d')


def schedule_day_matches_date(schedule_day, date_value=None):
    """Return True only when the selected date falls on the schedule's weekday."""
    date_value = date_value or today_string()
    try:
        target = datetime.strptime(date_value, '%Y-%m-%d').date()
    except Exception:
        return False
    return target.strftime('%a').lower() == (schedule_day or '').strip()[:3].lower()


def upcoming_class_dates(schedule_day, count=12):
    """Return the next scheduled class dates for leave selection."""
    today = now_local().date()
    wanted = (schedule_day or '').strip()[:3].lower()
    results = []
    for offset in range(0, 8 * 7 + 1):
        candidate = today + timedelta(days=offset)
        if candidate.strftime('%a').lower() == wanted:
            results.append(candidate.strftime('%Y-%m-%d'))
            if len(results) >= count:
                break
    return results


def attendance_record(conn, schedule_id, date_value=None):
    date_value = date_value or today_string()
    return conn.execute('''SELECT * FROM attendance
                           WHERE schedule_id=? AND attendance_date=?
                           ORDER BY id DESC LIMIT 1''', (schedule_id, date_value)).fetchone()


def leave_record(conn, schedule_id, date_value=None, statuses=('Approved','Pending')):
    date_value = date_value or today_string()
    placeholders = ','.join('?' for _ in statuses)
    return conn.execute(f'''SELECT * FROM leaves
                            WHERE schedule_id=? AND leave_date=? AND status IN ({placeholders})
                            ORDER BY CASE status WHEN 'Approved' THEN 0 ELSE 1 END, id DESC
                            LIMIT 1''', (schedule_id, date_value, *statuses)).fetchone()


def class_status(conn, schedule_id, date_value=None, schedule_day=None):
    date_value = date_value or today_string()
    if schedule_day is None:
        schedule_row = conn.execute('SELECT day FROM schedules WHERE id=? LIMIT 1', (schedule_id,)).fetchone()
        schedule_day = schedule_row['day'] if schedule_row else ''

    # Attendance/leave status is an occurrence status: it applies only to an
    # actual class meeting date, so the next Monday starts fresh.
    if not schedule_day_matches_date(schedule_day, date_value):
        return 'Not Today'

    # A successfully submitted teacher attendance must be the source of truth
    # for the same schedule occurrence.  This intentionally takes priority over
    # a rejected/pending leave record so a teacher can attend after a leave is
    # rejected without enrolled students remaining stuck at "Pending".
    att = attendance_record(conn, schedule_id, date_value)
    if att:
        att_status = (att['status'] or '').strip().lower()
        if att_status in {'attended', 'attending', 'present', 'checked in', 'checked-in'} and (att['photo_name'] or '').strip():
            return 'Attending'

    # Only active leave requests affect today's class state.  Rejected/cancelled
    # requests are intentionally ignored.
    leave = leave_record(conn, schedule_id, date_value, statuses=('Approved','Pending'))
    if leave:
        if leave['status'] == 'Approved':
            return 'On Leave'
        return 'Leave Pending'

    return 'Pending'


def teacher_owns_schedule(conn, schedule_id, session_user_id, full_name='', username=''):
    row = conn.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
    if not row:
        return None
    teacher = conn.execute('SELECT id FROM teachers WHERE user_id=? LIMIT 1', (session_user_id,)).fetchone()
    if teacher and row['teacher_id'] == teacher['id']:
        return row
    if username and (row['username'] or '').lower() == username.lower():
        return row
    if full_name and (row['teacher_name'] or '').lower() == full_name.lower():
        return row
    return None


def enrich_teacher_classes(conn, schedules):
    today = today_string()
    class_data = []
    for s in schedules:
        item = dict(s)
        item['is_class_day'] = schedule_day_matches_date(s['day'], today)
        item['attendance_state'] = class_status(conn, s['id'], today, s['day'])
        att = attendance_record(conn, s['id'], today)
        item['attendance_photo'] = att['photo_name'] if att else ''
        leave = leave_record(conn, s['id'], today)
        item['leave_status'] = leave['status'] if leave else ''
        item['leave_options'] = [
            {'value': d, 'label': datetime.strptime(d, '%Y-%m-%d').strftime('%A, %B %d, %Y')}
            for d in upcoming_class_dates(s['day'])
        ]
        students = conn.execute('''SELECT u.id,u.full_name,
                                  COALESCE((SELECT sa.status FROM student_attendance sa
                                            WHERE sa.schedule_id=? AND sa.student_user_id=u.id AND sa.attendance_date=?
                                            ORDER BY sa.id DESC LIMIT 1),'') AS student_attendance
                                  FROM student_classes sc JOIN users u ON u.id=sc.student_user_id
                                  WHERE sc.schedule_id=? AND u.role='student'
                                  ORDER BY u.last_name,u.first_name,u.full_name''',
                                 (s['id'], today, s['id'])).fetchall()
        item['students'] = [dict(x) for x in students]
        class_data.append(item)
    return class_data

def normalize_subject(value):
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def teacher_initials(name):
    cleaned = re.sub(r'\b(Prof\.?|Professor|Dr\.?|Engr\.?)\b', '', name or '', flags=re.I).strip()
    parts = [p for p in cleaned.split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return (parts[0][:2] if parts else 'XX').upper()


def year_number(year_level):
    match = re.search(r'\d+', year_level or '')
    return match.group(0) if match else '1'


def clean_section(section):
    value = re.sub(r'\b(section|sec)\b', '', section or '', flags=re.I).strip()
    return re.sub(r'\s+', '', value).upper() or 'A'


def make_codes(teacher_name, subject_code, year_level, section, schedule_id, conn):
    base_teacher = f'TCH-{teacher_initials(teacher_name)}-{normalize_subject(subject_code)}'
    base_student = f'STU-{normalize_subject(subject_code)}-{year_number(year_level)}{clean_section(section)}'

    def unique(base, code_type):
        candidate = base
        n = 2
        while True:
            row = conn.execute('SELECT schedule_id FROM class_codes WHERE code=?', (candidate,)).fetchone()
            if not row or row['schedule_id'] == schedule_id:
                return candidate
            candidate = f'{base}-{n}'
            n += 1

    return unique(base_teacher, 'teacher'), unique(base_student, 'student')


def sync_codes(schedule_id, conn=None):
    owns = conn is None
    conn = conn or db()
    s = conn.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
    if not s:
        if owns: conn.close()
        return None, None
    teacher_code, student_code = make_codes(s['teacher_name'], s['subject_code'], s['year_level'], s['section'], schedule_id, conn)
    conn.execute('DELETE FROM class_codes WHERE schedule_id=?', (schedule_id,))
    common = (schedule_id, s['teacher_name'], s['subject_code'], s['subject_name'], s['program'], s['year_level'], s['section'])
    conn.execute('INSERT INTO class_codes(code,code_type,schedule_id,teacher_name,subject_code,subject_name,program,year_level,section) VALUES(?,?,?,?,?,?,?,?,?)', (teacher_code,'teacher',*common))
    conn.execute('INSERT INTO class_codes(code,code_type,schedule_id,teacher_name,subject_code,subject_name,program,year_level,section) VALUES(?,?,?,?,?,?,?,?,?)', (student_code,'student',*common))
    if owns:
        conn.commit(); conn.close()
    return teacher_code, student_code


def conflicts_for(candidate, ignore_id=None, conn=None, include_availability=True):
    owns = conn is None
    conn = conn or db()
    conflicts = []
    start = parse_minutes(candidate.get('start_time'))
    end = parse_minutes(candidate.get('end_time'))
    if start < 0 or end < 0 or start >= end:
        conflicts.append({'type':'Invalid time','message':'End time must be later than start time.'})
        if owns: conn.close()
        return conflicts
    if start < 450 or end > 1260:
        conflicts.append({'type':'Time range','message':'Schedules must be between 7:30 AM and 9:00 PM.'})

    rows = conn.execute('SELECT * FROM schedules').fetchall()
    for r in rows:
        if ignore_id and r['id'] == ignore_id:
            continue
        if r['day'] != candidate.get('day') or not (start < parse_minutes(r['end_time']) and parse_minutes(r['start_time']) < end):
            continue
        if (candidate.get('teacher_name','').strip().lower() == r['teacher_name'].strip().lower()):
            conflicts.append({'type':'Teacher conflict','message':f"{r['teacher_name']} is already teaching {r['subject_code']} from {format_12hr(r['start_time'])} to {format_12hr(r['end_time'])} on {r['day']}."})
        if (candidate.get('room','').strip().lower() == r['room'].strip().lower()):
            conflicts.append({'type':'Room conflict','message':f"{r['room']} is already assigned to {r['subject_code']} from {format_12hr(r['start_time'])} to {format_12hr(r['end_time'])}."})
        same_section = all(str(candidate.get(k,'')).strip().lower() == str(r[k]).strip().lower() for k in ('program','year_level','section'))
        if same_section:
            conflicts.append({'type':'Section conflict','message':f"{r['program']} {r['year_level']} - Section {r['section']} already has {r['subject_code']} at this time."})

    teacher = conn.execute('SELECT * FROM teachers WHERE lower(full_name)=lower(?) OR lower(username)=lower(?) LIMIT 1', (candidate.get('teacher_name',''), candidate.get('username',''))).fetchone()
    if teacher and include_availability:
        prefs = [x.strip() for x in (teacher['preferred_days'] or '').split(',') if x.strip()]
        if prefs and candidate.get('day') not in prefs:
            conflicts.append({'type':'Availability conflict','message':f"{teacher['full_name']} is not available on {candidate.get('day')} based on preferred teaching days."})
        af = parse_minutes(teacher['available_from'])
        au = parse_minutes(teacher['available_until'])
        if start < af or end > au:
            conflicts.append({'type':'Availability conflict','message':f"The selected time is outside {teacher['full_name']}'s availability ({format_12hr(teacher['available_from'])}–{format_12hr(teacher['available_until'])})."})

    approved = conn.execute("SELECT * FROM leaves WHERE status='Approved'").fetchall()
    for leave in approved:
        try:
            leave_day = datetime.strptime(leave['leave_date'], '%Y-%m-%d').strftime('%a')
        except Exception:
            leave_day = ''
        if leave_day == candidate.get('day') and leave['teacher_name'].strip().lower() == candidate.get('teacher_name','').strip().lower():
            program_note = f" for {leave['program']}" if leave['program'] else ''
            conflicts.append({'type':'Leave conflict','message':f"{candidate.get('teacher_name')} has approved leave{program_note} on {leave['leave_date']}."})
    if owns: conn.close()
    return conflicts


def ai_model_status():
    """Return lightweight ML readiness information for the dashboard."""
    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM schedules WHERE status='Published'"
    ).fetchone()['n']
    conn.close()

    try:
        import sklearn  # noqa: F401
    except Exception as exc:
        return {
            "active": False,
            "algorithm": "Random Forest Classifier",
            "training_schedules": count,
            "training_samples": 0,
            "message": f"Scikit-learn is unavailable: {exc}",
        }

    return {
        "active": count > 0,
        "algorithm": "Random Forest Classifier",
        "training_schedules": count,
        "training_samples": None,
        "message": (
            "ML ranking is ready and retrains from published schedules when "
            "recommendations are generated."
            if count else
            "No published schedules are available for ML training yet."
        ),
    }


def program_config(conn, program):
    """Return the Program Head's sections, courses and instructors."""
    sections = [dict(r) for r in conn.execute(
        'SELECT * FROM program_sections WHERE program=? ORDER BY year_level,section_name',
        (program,)).fetchall()]
    courses = [dict(r) for r in conn.execute(
        'SELECT * FROM courses WHERE program=? ORDER BY year_level,course_code',
        (program,)).fetchall()]
    cr = {}
    for r in conn.execute('''SELECT cr.course_id, rm.name, rm.room_type
                             FROM course_rooms cr
                             LEFT JOIN rooms rm ON rm.id=cr.room_id
                             ORDER BY cr.course_id''').fetchall():
        cr.setdefault(r['course_id'], []).append(room_label(r['name'], r['room_type']))
    for c in courses:
        c['allowed_rooms'] = sorted(set(cr.get(c['id'], [])))
    instructors = [dict(r) for r in conn.execute(
        'SELECT * FROM instructors WHERE program=? ORDER BY name', (program,)).fetchall()]
    return sections, courses, instructors


def teacher_accounts_for(conn, program):
    """Registered teacher accounts not yet assigned as instructors in this program."""
    rows = conn.execute('''SELECT t.user_id, t.id AS teacher_id, t.full_name, t.username,
                                  t.subject_qualification
                           FROM teachers t
                           WHERE t.user_id IS NOT NULL
                             AND NOT EXISTS (SELECT 1 FROM instructors i
                                             WHERE i.program=? AND i.user_id=t.user_id)
                           ORDER BY t.full_name''', (program,)).fetchall()
    accounts = []
    for r in rows:
        account = dict(r)
        codes = []
        stored = [c.strip().upper() for c in re.split(r'[,\n]', account.get('subject_qualification') or '') if c.strip()]
        codes.extend(stored)
        for code in teacher_taught_codes(conn, account):
            if code not in codes:
                codes.append(code)
        account['suggested_codes'] = ', '.join(codes)
        accounts.append(account)
    return accounts


def teacher_taught_codes(conn, account):
    """Distinct subject codes from schedules already tied to a teacher account."""
    rows = conn.execute('''SELECT DISTINCT subject_code FROM schedules
                           WHERE subject_code IS NOT NULL AND subject_code<>''
                             AND (teacher_id=? OR lower(username)=lower(?))''',
                        (account.get('teacher_id'), account.get('username') or '')).fetchall()
    return sorted({r['subject_code'].strip().upper() for r in rows if r['subject_code'].strip()})


def save_teacher_courses(conn, account, codes):
    """Merge newly assigned course codes into the teacher's profile qualification."""
    stored = [c.strip().upper() for c in re.split(r'[,\n]', account.get('subject_qualification') or '') if c.strip()]
    merged = list(stored)
    for code in codes:
        code = code.strip().upper()
        if code and code not in merged:
            merged.append(code)
    conn.execute('UPDATE teachers SET subject_qualification=? WHERE id=?',
                 (', '.join(merged), account.get('teacher_id')))


def load_generator_inputs(conn, program):
    """Collect every input the AI generator needs for one program."""
    sections, courses, instructors = program_config(conn, program)
    qualifications, unavailability = [], []
    instructor_ids = [i['id'] for i in instructors]
    if instructor_ids:
        placeholders = ','.join('?' for _ in instructor_ids)
        qualifications = [dict(r) for r in conn.execute(
            f'SELECT * FROM instructor_qualifications WHERE instructor_id IN ({placeholders})',
            instructor_ids).fetchall()]
        unavailability = [dict(r) for r in conn.execute(
            f'SELECT * FROM instructor_unavailability WHERE instructor_id IN ({placeholders})',
            instructor_ids).fetchall()]
    rooms = configured_rooms(conn)
    existing = [dict(r) for r in conn.execute('SELECT * FROM schedules').fetchall()]
    return sections, courses, instructors, qualifications, unavailability, rooms, existing


def build_year_groups(conn, program):
    """Group a program's schedules by year level for the dashboard table."""
    year_groups = []
    for year in YEAR_LEVELS:
        rows = conn.execute('''SELECT s.*,COUNT(sc.id) AS student_count
                              FROM schedules s
                              LEFT JOIN student_classes sc ON sc.schedule_id=s.id
                              WHERE s.program=? AND s.year_level=?
                              GROUP BY s.id
                              ORDER BY s.section,s.day,s.start_time,s.id''', (program, year)).fetchall()
        items = []
        for srow in rows:
            item = dict(srow)
            item['formatted_time'] = f"{format_12hr(srow['start_time'])} – {format_12hr(srow['end_time'])}"
            items.append(item)
        year_groups.append({'year_level': year, 'items': items})
    return year_groups


def generated_slot_errors(conn, candidate, ignore_id=None):
    """Validate a scheduled slot using the same hard rules as the AI generator.

    Teacher availability hours and preferred days are intentionally NOT enforced
    here so manual edits match how the AI placed sessions (clash, room type,
    time window and non-preferred windows only).
    """
    errors = [c['message'] for c in conflicts_for(candidate, ignore_id=ignore_id, conn=conn, include_availability=False)]
    session_type = candidate.get('session_type') or 'Lecture'
    if ai_generator.room_type(candidate.get('room', '')) != ai_generator.session_room_type(session_type):
        errors.append(f"A {session_type} session must use a matching room type.")

    # Instructor non-preferred windows are a hard rule, so edits must respect
    # them too -- not just the initial AI generation.
    program = candidate.get('program', '')
    name = (candidate.get('teacher_name') or '').strip()
    if name:
        inst = conn.execute('SELECT id FROM instructors WHERE program=? AND lower(name)=lower(?) LIMIT 1',
                            (program, name)).fetchone()
        if inst:
            start = parse_minutes(candidate.get('start_time'))
            end = parse_minutes(candidate.get('end_time'))
            for w in conn.execute('SELECT * FROM instructor_unavailability WHERE instructor_id=?', (inst['id'],)).fetchall():
                if not ai_generator.day_within_range(candidate.get('day', ''), w['day_from'], w['day_to']):
                    continue
                w_start = parse_minutes(w['start_time'])
                w_end = parse_minutes(w['end_time'])
                if w_start >= 0 and w_end >= 0 and start < w_end and w_start < end:
                    errors.append(
                        f"{name} is unavailable {w['day_from']}–{w['day_to']} "
                        f"{format_12hr(w['start_time'])}–{format_12hr(w['end_time'])} (non-preferred time)."
                    )
    return errors

def form_data_from_request():
    duration_hours = request.form.get('duration_hours', '1.5').strip()
    start_time = request.form.get('start_time', '07:30').strip()
    # End time is calculated server-side from the requested teaching duration.
    start_m = parse_minutes(start_time)
    try:
        duration_m = int(round(float(duration_hours) * 60))
    except Exception:
        duration_m = 90
    end_time = request.form.get('end_time', '').strip()
    if start_m >= 0:
        calculated_end = start_m + duration_m
        end_time = f'{calculated_end // 60:02d}:{calculated_end % 60:02d}'
    return {
        'teacher_name': request.form.get('teacher_name','').strip(),
        'teacher_id': request.form.get('teacher_id','').strip(),
        'subject_code': request.form.get('subject_code','').strip(),
        'subject_name': request.form.get('subject_name','').strip(),
        'program': request.form.get('program','').strip(),
        'year_level': request.form.get('year_level','Year 1').strip(),
        'section': request.form.get('section','').strip(),
        'preferred_days': ','.join(request.form.getlist('preferred_days')),
        'available_from': request.form.get('available_from','07:30'),
        'available_until': request.form.get('available_until','17:00'),
        'room': request.form.get('room','').strip(),
        'day': request.form.get('day','Mon'),
        'start_time': start_time,
        'end_time': end_time,
        'duration_hours': duration_hours,
    }


def validate_form(f):
    required = ['teacher_name','teacher_id','subject_code','subject_name','program','year_level','section','room','day','start_time']
    missing = [x for x in required if not f.get(x)]
    if missing:
        return [f"Please complete: {', '.join(missing).replace('_',' ')}."]
    if f['program'] not in PROGRAMS:
        return ['Please select a valid program.']
    if f['year_level'] not in YEAR_LEVELS:
        return ['Please select a valid year level.']
    if f['day'] not in DAYS:
        return ['Please select a valid day.']
    if f.get('duration_hours') not in DURATION_OPTIONS:
        return ['Please select a valid teaching duration.']
    if parse_minutes(f['available_from']) >= parse_minutes(f['available_until']):
        return ['Teacher availability must have a valid start and end time.']
    start_m = parse_minutes(f['start_time'])
    end_m = parse_minutes(f['end_time'])
    expected = int(round(float(f['duration_hours']) * 60))
    if start_m < 0 or end_m < 0 or end_m - start_m != expected:
        return [f"The class duration must be exactly {f['duration_hours']} hour(s)."]
    return []


@app.context_processor
def globals_for_templates():
    rooms = []
    if session.get('user_id'):
        conn = db()
        try:
            rooms = configured_rooms(conn)
        finally:
            conn.close()
    return {'app_name':'OptiSked AI','days':DAYS,'rooms':rooms,'programs':PROGRAMS,'year_levels':YEAR_LEVELS,'duration_options':DURATION_OPTIONS,'format_12hr':format_12hr}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session: return redirect(url_for('login'))
            if session.get('role') not in roles:
                flash('You do not have permission to access that page.', 'error')
                return redirect(url_for('dashboard'))
            return view(*args, **kwargs)
        return wrapped
    return deco


@app.route('/')
def index():
    if 'user_id' not in session: return redirect(url_for('login'))
    role=session.get('role')
    if role == 'program_head' and not session.get('program'):
        return redirect(url_for('program_head_setup'))
    return redirect(url_for('dashboard' if role=='program_head' else 'teacher_portal' if role=='teacher' else 'student_portal'))


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username=request.form.get('username','').strip()
        password=request.form.get('password','')
        conn=db(); user=conn.execute('SELECT * FROM users WHERE lower(username)=lower(?)',(username,)).fetchone(); conn.close()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session.update(user_id=user['id'],username=user['username'],role=user['role'],full_name=user['full_name'],program=user['program'] or '')
            if user['role'] == 'program_head' and not (user['program'] or '').strip():
                return redirect(url_for('program_head_setup'))
            return redirect(url_for('index'))
        flash('Invalid username or password.','error')
    return render_template('login.html', signup_mode=False)


@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        first=request.form.get('first_name','').strip(); last=request.form.get('last_name','').strip(); username=request.form.get('username','').strip(); password=request.form.get('password',''); role=request.form.get('role',''); program=request.form.get('program','').strip()
        if not all([first,last,username,password]) or role not in {'program_head','teacher','student'}:
            flash('Please complete all required fields.','error'); return render_template('login.html',signup_mode=True)
        if role == 'program_head' and program not in PROGRAMS:
            flash('Please select the academic program you manage as Program Head.','error'); return render_template('login.html',signup_mode=True)
        if len(password)<6:
            flash('Password must be at least 6 characters.','error'); return render_template('login.html',signup_mode=True)
        full=f'{first} {last}'
        conn=db()
        try:
            cur=conn.execute('INSERT INTO users(username,password_hash,role,first_name,last_name,full_name,program) VALUES(?,?,?,?,?,?,?)',(username,generate_password_hash(password),role,first,last,full,program if role=='program_head' else ''))
            uid=cur.lastrowid
            if role=='teacher': conn.execute('INSERT INTO teachers(user_id,full_name,username) VALUES(?,?,?)',(uid,full,username))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback(); conn.close(); flash('That username is already registered.','error'); return render_template('login.html',signup_mode=True)
        conn.close(); flash('Account created successfully. You can now log in.','success'); return redirect(url_for('login'))
    return render_template('login.html',signup_mode=True)


@app.route('/program-head/setup', methods=['GET','POST'])
@role_required('program_head')
def program_head_setup():
    if request.method == 'POST':
        program = request.form.get('program','').strip()
        if program not in PROGRAMS:
            flash('Please select a valid program.','error')
        else:
            conn=db(); conn.execute('UPDATE users SET program=? WHERE id=?',(program,session['user_id'])); conn.commit(); conn.close()
            session['program']=program
            flash(f'Program Head account assigned to {program}.','success')
            return redirect(url_for('dashboard'))
    return render_template('program_head_setup.html', programs=PROGRAMS, current_program=session.get('program',''))


@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))


@app.route('/dashboard')
@role_required('program_head')
def dashboard():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn = db()
    year_groups = build_year_groups(conn, program)
    sections, courses, instructors = program_config(conn, program)
    draft_count = conn.execute("SELECT COUNT(*) AS n FROM schedules WHERE program=? AND status='Draft'", (program,)).fetchone()['n']
    published_count = conn.execute("SELECT COUNT(*) AS n FROM schedules WHERE program=? AND status='Published'", (program,)).fetchone()['n']
    conn.close()
    report = session.pop('generation_report', None)
    return render_template('schedule_builder.html', active_page='dashboard',
                           year_groups=year_groups, sections=sections, courses=courses,
                           instructors=instructors, draft_count=draft_count,
                           published_count=published_count, ai_model_info=ai_model_status(),
                           report=report, lab_patterns=LAB_PATTERNS)


@app.route('/instructors', methods=['GET', 'POST'])
@role_required('program_head')
def instructors_page():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn = db()
    if request.method == 'POST':
        user_id = request.form.get('user_id','').strip()
        teacher = conn.execute('SELECT * FROM teachers WHERE user_id=?', (user_id,)).fetchone() if user_id.isdigit() else None
        codes = request.form.getlist('course_codes')
        day_from = request.form.get('day_from','').strip()
        day_to = request.form.get('day_to','').strip()
        np_start = request.form.get('np_start','').strip()
        np_end = request.form.get('np_end','').strip()
        if not teacher:
            flash('Please select a registered teacher account.', 'error')
        elif conn.execute('SELECT 1 FROM instructors WHERE program=? AND user_id=?',
                          (program, teacher['user_id'])).fetchone():
            flash('That teacher is already an instructor in your program.', 'error')
        else:
            name = teacher['full_name']
            cur = conn.execute('INSERT INTO instructors(program,name,user_id) VALUES(?,?,?)',
                               (program, name, teacher['user_id']))
            instructor_id = cur.lastrowid
            assigned = []
            for code in codes:
                code = str(code).strip().upper()
                if code:
                    assigned.append(code)
            # dedupe; only keep codes that exist in this program's courses
            valid = {c['course_code'].strip().upper() for c in conn.execute('SELECT course_code FROM courses WHERE program=?', (program,)).fetchall()}
            assigned = [c for c in assigned if c in valid]
            for code in assigned:
                conn.execute('INSERT INTO instructor_qualifications(instructor_id,course_code) VALUES(?,?)',
                             (instructor_id, code))
            account = {'teacher_id': teacher['id'], 'username': teacher['username'],
                       'subject_qualification': teacher['subject_qualification']}
            save_teacher_courses(conn, account, assigned)
            if day_from and day_to and np_start and np_end:
                if day_from not in DAYS or day_to not in DAYS:
                    flash('Non-preferred day range must use valid weekday names.', 'error')
                else:
                    conn.execute('''INSERT INTO instructor_unavailability(instructor_id,day_from,day_to,start_time,end_time)
                                    VALUES(?,?,?,?,?)''', (instructor_id, day_from, day_to, np_start, np_end))
            conn.commit()
            flash(f'Instructor {name} saved.', 'success')
        return redirect(url_for('instructors_page'))
    instructors = [dict(r) for r in conn.execute('SELECT * FROM instructors WHERE program=? ORDER BY name', (program,)).fetchall()]
    for inst in instructors:
        inst['qualifications'] = [dict(r) for r in conn.execute(
            'SELECT * FROM instructor_qualifications WHERE instructor_id=? ORDER BY course_code', (inst['id'],)).fetchall()]
        inst['windows'] = [dict(r) for r in conn.execute(
            'SELECT * FROM instructor_unavailability WHERE instructor_id=? ORDER BY day_from,start_time', (inst['id'],)).fetchall()]
    courses = [dict(r) for r in conn.execute('SELECT * FROM courses WHERE program=? ORDER BY course_code', (program,)).fetchall()]
    teacher_accounts = teacher_accounts_for(conn, program)
    conn.close()
    return render_template('instructors.html', active_page='instructors', instructors=instructors,
                           courses=courses, days=DAYS, teacher_accounts=teacher_accounts)


def _owns_instructor(conn, program, instructor_id):
    return conn.execute('SELECT id FROM instructors WHERE id=? AND program=?', (instructor_id, program)).fetchone()


@app.route('/instructors/<int:instructor_id>/subjects', methods=['POST'])
@role_required('program_head')
def edit_instructor_subjects(instructor_id):
    program = session.get('program','').strip()
    conn = db()
    if not _owns_instructor(conn, program, instructor_id):
        conn.close(); flash('Instructor not found in your program.', 'error'); return redirect(url_for('instructors_page'))
    codes = request.form.getlist('course_codes')
    assigned = []
    for code in codes:
        code = str(code).strip().upper()
        if code:
            assigned.append(code)
    valid = {c['course_code'].strip().upper() for c in conn.execute('SELECT course_code FROM courses WHERE program=?', (program,)).fetchall()}
    assigned = [c for c in assigned if c in valid]
    conn.execute('DELETE FROM instructor_qualifications WHERE instructor_id=?', (instructor_id,))
    for code in assigned:
        conn.execute('INSERT INTO instructor_qualifications(instructor_id,course_code) VALUES(?,?)',
                     (instructor_id, code))
    conn.commit()
    conn.close()
    flash('Instructor subjects updated.', 'success')
    return redirect(url_for('instructors_page'))


@app.route('/instructors/<int:instructor_id>/delete', methods=['POST'])
@role_required('program_head')
def delete_instructor(instructor_id):
    program = session.get('program','').strip()
    conn = db()
    if _owns_instructor(conn, program, instructor_id):
        conn.execute('DELETE FROM instructors WHERE id=?', (instructor_id,))
        conn.commit()
        flash('Instructor removed.', 'success')
    else:
        flash('Instructor not found in your program.', 'error')
    conn.close()
    return redirect(url_for('instructors_page'))


@app.route('/instructors/<int:instructor_id>/windows', methods=['POST'])
@role_required('program_head')
def add_instructor_window(instructor_id):
    program = session.get('program','').strip()
    day_from = request.form.get('day_from','').strip()
    day_to = request.form.get('day_to','').strip()
    np_start = request.form.get('np_start','').strip()
    np_end = request.form.get('np_end','').strip()
    conn = db()
    if not _owns_instructor(conn, program, instructor_id):
        conn.close(); flash('Instructor not found in your program.', 'error'); return redirect(url_for('instructors_page'))
    if day_from in DAYS and day_to in DAYS and np_start and np_end:
        conn.execute('''INSERT INTO instructor_unavailability(instructor_id,day_from,day_to,start_time,end_time)
                        VALUES(?,?,?,?,?)''', (instructor_id, day_from, day_to, np_start, np_end))
        conn.commit()
        flash('Non-preferred time window added.', 'success')
    else:
        flash('Please provide a valid day range and time range.', 'error')
    conn.close()
    return redirect(url_for('instructors_page'))


@app.route('/instructors/windows/<int:window_id>/delete', methods=['POST'])
@role_required('program_head')
def delete_instructor_window(window_id):
    program = session.get('program','').strip()
    conn = db()
    row = conn.execute('''SELECT w.id FROM instructor_unavailability w
                          JOIN instructors i ON i.id=w.instructor_id
                          WHERE w.id=? AND i.program=?''', (window_id, program)).fetchone()
    if row:
        conn.execute('DELETE FROM instructor_unavailability WHERE id=?', (window_id,))
        conn.commit()
        flash('Non-preferred window removed.', 'success')
    else:
        flash('Window not found.', 'error')
    conn.close()
    return redirect(url_for('instructors_page'))


@app.route('/courses', methods=['GET', 'POST'])
@role_required('program_head')
def courses_page():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn = db()
    if request.method == 'POST':
        course_code = request.form.get('course_code','').strip()
        course_name = request.form.get('course_name','').strip()
        year_level = request.form.get('year_level','Year 1').strip()
        sessions = request.form.get('sessions_per_week','1').strip()
        has_lab = 1 if request.form.get('has_lab') else 0
        lab_pattern = request.form.get('lab_pattern','').strip()
        duration = request.form.get('duration_hours','1.5').strip()
        try:
            sessions = max(1, int(sessions))
        except Exception:
            sessions = 1
        if not course_code or not course_name:
            flash('Course code and course name are required.', 'error')
        elif year_level not in YEAR_LEVELS:
            flash('Please select a valid year level.', 'error')
        elif duration not in DURATION_OPTIONS:
            flash('Please select a valid session duration.', 'error')
        elif has_lab and lab_pattern not in LAB_PATTERN_VALUES:
            flash('Please choose a valid laboratory meeting pattern.', 'error')
        else:
            if not has_lab:
                lab_pattern = ''
            cur = conn.execute('''INSERT INTO courses(program,course_code,course_name,year_level,sessions_per_week,has_lab,lab_pattern,duration_hours)
                                   VALUES(?,?,?,?,?,?,?,?)''',
                                (program, course_code, course_name, year_level, sessions, has_lab, lab_pattern, float(duration)))
            course_id = cur.lastrowid
            # attach selected rooms
            for rid in request.form.getlist('lab_rooms'):
                try:
                    conn.execute('INSERT OR IGNORE INTO course_rooms(course_id,room_id) VALUES(?,?)', (course_id,int(rid)))
                except Exception:
                    pass
            for rid in request.form.getlist('lecture_rooms'):
                try:
                    conn.execute('INSERT OR IGNORE INTO course_rooms(course_id,room_id) VALUES(?,?)', (course_id,int(rid)))
                except Exception:
                    pass
            conn.commit()
            flash(f'Course {course_code} saved.', 'success')
        return redirect(url_for('courses_page'))
    courses = [dict(r) for r in conn.execute('SELECT * FROM courses WHERE program=? ORDER BY year_level,course_code', (program,)).fetchall()]
    cr_map = {}
    for r in conn.execute('''SELECT cr.course_id, rm.name, rm.room_type
                             FROM course_rooms cr
                             LEFT JOIN rooms rm ON rm.id=cr.room_id
                             ORDER BY cr.course_id''').fetchall():
        cr_map.setdefault(r['course_id'], []).append(room_label(r['name'], r['room_type']))
    for c in courses:
        c['allowed_rooms'] = sorted(set(cr_map.get(c['id'], [])))
    lab_rooms = [dict(r, label=room_label(r['name'], r['room_type']))
                 for r in conn.execute("SELECT * FROM rooms WHERE room_type='lab' ORDER BY CAST(name as integer), name").fetchall()]
    lecture_rooms = [dict(r, label=room_label(r['name'], r['room_type']))
                     for r in conn.execute("SELECT * FROM rooms WHERE room_type='lecture' ORDER BY CAST(name as integer), name").fetchall()]
    conn.close()
    return render_template('courses.html', active_page='courses', courses=courses,
                           year_levels=YEAR_LEVELS, duration_options=DURATION_OPTIONS,
                           lab_patterns=LAB_PATTERNS, lab_rooms=lab_rooms,
                           lecture_rooms=lecture_rooms)


@app.route('/courses/<int:course_id>/delete', methods=['POST'])
@role_required('program_head')
def delete_course(course_id):
    program = session.get('program','').strip()
    conn = db()
    row = conn.execute('SELECT id FROM courses WHERE id=? AND program=?', (course_id, program)).fetchone()
    if row:
        conn.execute('DELETE FROM courses WHERE id=?', (course_id,))
        conn.commit()
        flash('Course removed.', 'success')
    else:
        flash('Course not found in your program.', 'error')
    conn.close()
    return redirect(url_for('courses_page'))


@app.route('/sections', methods=['GET', 'POST'])
@role_required('program_head')
def sections_page():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn = db()
    if request.method == 'POST':
        year_level = request.form.get('year_level','Year 1').strip()
        section_name = request.form.get('section_name','').strip().upper()
        if not section_name:
            flash('Section name is required.', 'error')
        elif year_level not in YEAR_LEVELS:
            flash('Please select a valid year level.', 'error')
        else:
            try:
                conn.execute('INSERT INTO program_sections(program,year_level,section_name) VALUES(?,?,?)',
                             (program, year_level, section_name))
                conn.commit()
                flash(f'Section {section_name} added to {year_level}.', 'success')
            except sqlite3.IntegrityError:
                conn.rollback()
                flash('That section already exists for this year level.', 'error')
        return redirect(url_for('sections_page'))
    year_groups = []
    for year in YEAR_LEVELS:
        items = [dict(r) for r in conn.execute(
            'SELECT * FROM program_sections WHERE program=? AND year_level=? ORDER BY section_name',
            (program, year)).fetchall()]
        year_groups.append({'year_level': year, 'items': items})
    conn.close()
    return render_template('sections.html', active_page='sections', year_groups=year_groups,
                           year_levels=YEAR_LEVELS)


@app.route('/sections/<int:section_id>/delete', methods=['POST'])
@role_required('program_head')
def delete_section(section_id):
    program = session.get('program','').strip()
    conn = db()
    row = conn.execute('SELECT id FROM program_sections WHERE id=? AND program=?', (section_id, program)).fetchone()
    if row:
        conn.execute('DELETE FROM program_sections WHERE id=?', (section_id,))
        conn.commit()
        flash('Section removed.', 'success')
    else:
        flash('Section not found in your program.', 'error')
    conn.close()
    return redirect(url_for('sections_page'))


@app.route('/rooms', methods=['GET', 'POST'])
@role_required('program_head')
def rooms_page():
    conn = db()
    if request.method == 'POST':
        room_type = request.form.get('room_type', 'lab').strip()
        range_from = request.form.get('range_from', '').strip()
        range_to = request.form.get('range_to', '').strip()
        name = request.form.get('name', '').strip()
        if room_type not in ROOM_TYPE_LABELS:
            flash('Please select a valid room type.', 'error')
        elif range_from or range_to:
            if not (range_from.isdigit() and range_to.isdigit()):
                flash('A range needs whole-number From and To values.', 'error')
            else:
                low, high = sorted((int(range_from), int(range_to)))
                if high - low > 200:
                    flash('Range is too large — add at most 200 rooms at once.', 'error')
                else:
                    added = 0
                    for number in range(low, high + 1):
                        added += conn.execute('INSERT OR IGNORE INTO rooms(name,room_type) VALUES(?,?)',
                                              (str(number), room_type)).rowcount
                    conn.commit()
                    label = ROOM_TYPE_LABELS[room_type]
                    if added:
                        flash(f'Added {added} {label} room(s).', 'success')
                    else:
                        flash('Those rooms already exist.', 'error')
        elif not name:
            flash('Room number is required.', 'error')
        else:
            added = conn.execute('INSERT OR IGNORE INTO rooms(name,room_type) VALUES(?,?)',
                                 (name, room_type)).rowcount
            conn.commit()
            if added:
                flash(f'Room {name} added as a {ROOM_TYPE_LABELS[room_type]}.', 'success')
            else:
                flash(f'Room {name} already exists.', 'error')
        conn.close()
        return redirect(url_for('rooms_page'))

    groups = []
    for key in ('lab', 'lecture'):
        rows = [dict(r, label=room_label(r['name'], r['room_type']))
                for r in conn.execute('SELECT * FROM rooms WHERE room_type=?', (key,)).fetchall()]
        rows.sort(key=lambda r: (0, int(r['name'])) if str(r['name']).isdigit() else (1, str(r['name'])))
        groups.append({'key': key, 'label': ROOM_TYPE_LABELS[key], 'items': rows})
    conn.close()
    return render_template('rooms.html', active_page='rooms', groups=groups,
                           room_types=room_type_options())


@app.route('/rooms/<int:room_id>/delete', methods=['POST'])
@role_required('program_head')
def delete_room(room_id):
    conn = db()
    row = conn.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
    if row:
        conn.execute('DELETE FROM rooms WHERE id=?', (room_id,))
        conn.commit()
        flash(f"Room {row['name']} removed.", 'success')
    else:
        flash('Room not found.', 'error')
    conn.close()
    return redirect(url_for('rooms_page'))


@app.route('/ai/generate', methods=['POST'])
@role_required('program_head')
def ai_generate():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn = db()
    sections, courses, instructors, qualifications, unavailability, rooms, existing = load_generator_inputs(conn, program)
    if not courses or not sections:
        conn.close()
        flash('Add at least one course and one section before generating a schedule.', 'error')
        return redirect(url_for('dashboard'))

    # Courses that already have published sessions must not be scheduled again.
    covered = {
        ((r.get('subject_code') or '').strip().upper(), r.get('year_level', ''), r.get('section', ''))
        for r in existing
        if (r.get('status') or '') == 'Published'
    }
    result = ai_generator.generate(courses, sections, instructors, qualifications,
                                   unavailability, rooms, existing, skip_keys=covered)

    # Replace any previous draft for this program with the fresh AI output.
    conn.execute("DELETE FROM schedules WHERE program=? AND status='Draft'", (program,))
    instructors_by_id = {i['id']: i for i in instructors}
    teachers_by_user = {r['user_id']: r for r in conn.execute('SELECT * FROM teachers').fetchall()}
    teacher_lookup = {(r['full_name'] or '').strip().lower(): r for r in conn.execute('SELECT * FROM teachers').fetchall()}
    for row in result['rows']:
        match = None
        inst = instructors_by_id.get(row.get('instructor_id'))
        if inst and inst.get('user_id') is not None:
            match = teachers_by_user.get(inst['user_id'])
        if not match:
            match = teacher_lookup.get((row['teacher_name'] or '').strip().lower())
        teacher_id = match['id'] if match else None
        username = match['username'] if match else ''
        conn.execute('''INSERT INTO schedules(teacher_id,teacher_name,username,subject_code,subject_name,program,year_level,section,day,start_time,end_time,room,status,session_type,course_id,is_generated)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (teacher_id, row['teacher_name'], username, row['subject_code'], row['subject_name'],
                      row['program'], row['year_level'], row['section'], row['day'], row['start_time'],
                      row['end_time'], row['room'], 'Draft', row['session_type'], row['course_id'], 1))
    conn.commit()
    conn.close()

    report = dict(result['report'])
    report['ml'] = result['ml']
    session['generation_report'] = report
    skipped = report.get('skipped') or []
    note = f" {len(skipped)} course offering(s) already published and left unchanged." if skipped else ""
    flash(f"AI generated {len(result['rows'])} draft session(s). {result['report']['message']}{note}", 'success')
    return redirect(url_for('dashboard'))


@app.route('/ai/clear-drafts', methods=['POST'])
@role_required('program_head')
def clear_drafts():
    program = session.get('program','').strip()
    conn = db()
    conn.execute("DELETE FROM schedules WHERE program=? AND status='Draft'", (program,))
    conn.commit()
    conn.close()
    flash('All draft sessions cleared.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/ai/publish-all', methods=['POST'])
@role_required('program_head')
def publish_all_drafts():
    program = session.get('program','').strip()
    conn = db()
    drafts = conn.execute("SELECT id FROM schedules WHERE program=? AND status='Draft'", (program,)).fetchall()
    for row in drafts:
        conn.execute("UPDATE schedules SET status='Published' WHERE id=?", (row['id'],))
        sync_codes(row['id'], conn)
    conn.commit()
    conn.close()
    flash(f'{len(drafts)} draft session(s) published with access codes.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/schedule/<int:schedule_id>/publish', methods=['POST'])
@role_required('program_head')
def publish_schedule(schedule_id):
    program = session.get('program','').strip()
    conn = db()
    row = conn.execute('SELECT * FROM schedules WHERE id=? AND program=?', (schedule_id, program)).fetchone()
    if not row:
        conn.close(); flash('Schedule not found in your program.', 'error'); return redirect(url_for('dashboard'))
    conn.execute("UPDATE schedules SET status='Published' WHERE id=?", (schedule_id,))
    sync_codes(schedule_id, conn)
    conn.commit()
    conn.close()
    flash('Schedule published.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/schedule/<int:schedule_id>/edit', methods=['POST'])
@role_required('program_head')
def edit_generated_schedule(schedule_id):
    program = session.get('program','').strip()
    conn = db()
    row = conn.execute('SELECT * FROM schedules WHERE id=? AND program=?', (schedule_id, program)).fetchone()
    if not row:
        conn.close(); flash('Schedule not found in your program.', 'error'); return redirect(url_for('dashboard'))

    day = request.form.get('day', row['day']).strip()
    start_time = request.form.get('start_time', row['start_time']).strip()
    end_time = request.form.get('end_time', row['end_time']).strip()
    room = request.form.get('room', row['room']).strip()
    teacher_name = request.form.get('teacher_name', row['teacher_name']).strip()

    errors = []
    if day not in DAYS:
        errors.append('Invalid day.')
    if parse_minutes(start_time) < 0 or parse_minutes(end_time) < 0 or parse_minutes(start_time) >= parse_minutes(end_time):
        errors.append('End time must be later than start time.')
    candidate = dict(row)
    candidate.update(day=day, start_time=start_time, end_time=end_time, room=room, teacher_name=teacher_name)
    if not errors:
        errors = generated_slot_errors(conn, candidate, ignore_id=schedule_id)

    if errors:
        conn.close()
        flash('Edit rejected: ' + ' '.join(errors), 'error')
        return redirect(url_for('dashboard'))

    conn.execute('UPDATE schedules SET day=?,start_time=?,end_time=?,room=?,teacher_name=? WHERE id=?',
                 (day, start_time, end_time, room, teacher_name, schedule_id))
    # Published sessions keep their access codes valid; refresh them so a changed
    # instructor is reflected (room/day/time edits leave the codes untouched).
    if row['status'] == 'Published':
        sync_codes(schedule_id, conn)
    conn.commit()
    conn.close()
    flash(f"{'Published' if row['status'] == 'Published' else 'Draft'} session updated.", 'success')
    return redirect(url_for('dashboard'))


@app.route('/schedule/<int:schedule_id>/move', methods=['POST'])
@role_required('program_head')
def move_schedule(schedule_id):
    """Drag-and-drop endpoint: move a session to a new day/time.

    Validation uses the AI hard rules (clash, room type, time window and
    instructor non-preferred windows). Access codes do not encode day/time
    (see make_codes), so no re-sync is required when only the slot changes.
    """
    program = session.get('program','').strip()
    payload = request.get_json(silent=True) or request.form
    day = (payload.get('day') or '').strip()
    start_time = (payload.get('start_time') or '').strip()
    end_time = (payload.get('end_time') or '').strip()

    conn = db()
    row = conn.execute('SELECT * FROM schedules WHERE id=? AND program=?', (schedule_id, program)).fetchone()
    if not row:
        conn.close()
        return jsonify(ok=False, errors=['Schedule not found in your program.']), 404

    errors = []
    if day not in DAYS:
        errors.append('Invalid day.')
    start = parse_minutes(start_time)
    end = parse_minutes(end_time)
    if start < 0 or end < 0 or start >= end:
        errors.append('End time must be later than start time.')

    candidate = dict(row)
    candidate.update(day=day, start_time=start_time, end_time=end_time)
    if not errors:
        errors = generated_slot_errors(conn, candidate, ignore_id=schedule_id)

    if errors:
        conn.close()
        return jsonify(ok=False, errors=errors), 409

    conn.execute('UPDATE schedules SET day=?,start_time=?,end_time=? WHERE id=?',
                 (day, start_time, end_time, schedule_id))
    conn.commit()
    conn.close()
    return jsonify(ok=True, id=schedule_id, day=day, start_time=start_time,
                   end_time=end_time, status=row['status'])


@app.route('/calendar')
@role_required('program_head')
def calendar_editor():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn = db()
    sections = [dict(r) for r in conn.execute(
        'SELECT * FROM program_sections WHERE program=? ORDER BY year_level,section_name',
        (program,)).fetchall()]
    rows = conn.execute('''SELECT id,day,start_time,end_time,room,teacher_name,subject_code,
                                  subject_name,session_type,status,year_level,section,course_id
                           FROM schedules WHERE program=?
                           ORDER BY year_level,section,day,start_time''', (program,)).fetchall()
    events = [dict(r) for r in rows]
    conn.close()
    grid = {
        'day_start': 450,
        'day_end': 1260,
        'step': 30,
        'px_per_hour': 56,
        'days': DAYS,
    }
    return render_template('calendar.html', active_page='calendar', sections=sections,
                           events=events, grid=grid)


@app.route('/schedule/<int:schedule_id>/delete',methods=['POST'])
@role_required('program_head')
def delete_schedule(schedule_id):
    conn=db(); row=conn.execute('SELECT program FROM schedules WHERE id=?',(schedule_id,)).fetchone()
    if not row:
        conn.close(); flash('Schedule not found.','error'); return redirect(url_for('dashboard'))
    if row['program'] != session.get('program',''):
        conn.close(); flash('You can only delete schedules in your assigned program.','error'); return redirect(url_for('dashboard'))
    conn.execute('DELETE FROM schedules WHERE id=?',(schedule_id,)); conn.commit(); conn.close(); flash('Schedule and linked access codes deleted.','success'); return redirect(url_for('dashboard'))


@app.route('/code-generation',methods=['GET','POST'])
@role_required('program_head')
def code_generation():
    program=session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn=db()
    schedules=conn.execute('SELECT * FROM schedules WHERE program=? ORDER BY program,year_level,section,subject_code,id',(program,)).fetchall()
    # Repair any missing/corrupted codes before rendering the page.
    for s in schedules:
        count=conn.execute('SELECT COUNT(*) AS n FROM class_codes WHERE schedule_id=?',(s['id'],)).fetchone()['n']
        if count < 2: sync_codes(s['id'],conn)
    conn.commit()
    teachers=conn.execute("SELECT * FROM class_codes WHERE code_type='teacher' AND program=? ORDER BY program,year_level,section,subject_code,id",(program,)).fetchall()
    students=conn.execute("SELECT * FROM class_codes WHERE code_type='student' AND program=? ORDER BY program,year_level,section,subject_code,id",(program,)).fetchall()
    conn.close()
    return render_template('code_generation.html',active_page='code_generation',teacher_codes=teachers,student_codes=students,schedules=schedules)


@app.route('/code-generation/generate',methods=['POST'])
@role_required('program_head')
def generate_all_codes():
    program=session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    conn=db(); schedules=conn.execute('SELECT id FROM schedules WHERE program=?',(program,)).fetchall()
    for s in schedules: sync_codes(s['id'],conn)
    conn.commit(); conn.close(); flash('All schedule access codes have been generated and synchronized.','success'); return redirect(url_for('code_generation'))


@app.route('/attendance')
@role_required('program_head')
def attendance_records():
    program = session.get('program','').strip()
    if not program:
        return redirect(url_for('program_head_setup'))
    today = today_string()
    conn = db()
    leaves = conn.execute('''SELECT l.*,s.subject_code,s.subject_name,s.day,s.start_time,s.end_time
                            FROM leaves l LEFT JOIN schedules s ON s.id=l.schedule_id
                            WHERE l.program=?
                            ORDER BY CASE l.status WHEN 'Pending' THEN 0 ELSE 1 END,l.leave_date,l.id DESC''', (program,)).fetchall()
    schedules = conn.execute('''SELECT * FROM schedules WHERE program=?
                                ORDER BY CASE year_level WHEN 'Year 1' THEN 1 WHEN 'Year 2' THEN 2 WHEN 'Year 3' THEN 3 WHEN 'Year 4' THEN 4 ELSE 9 END,
                                         section,day,start_time,id''', (program,)).fetchall()
    today_classes=[]
    for srow in schedules:
        if not schedule_day_matches_date(srow['day'], today):
            continue
        item=dict(srow)
        item['attendance_state']=class_status(conn,srow['id'],today,srow['day'])
        att=attendance_record(conn,srow['id'],today)
        item['photo_name']=att['photo_name'] if att else ''
        today_classes.append(item)
    history = conn.execute('''SELECT a.*,s.subject_code,s.subject_name,s.day,s.start_time,s.end_time,s.room,s.year_level,s.section
                             FROM attendance a JOIN schedules s ON s.id=a.schedule_id
                             WHERE s.program=?
                             ORDER BY a.attendance_date DESC,a.id DESC''', (program,)).fetchall()
    students = conn.execute('''SELECT u.full_name AS student_name,s.subject_code,s.subject_name,s.program,s.year_level,s.section,s.day
                               FROM student_classes sc JOIN users u ON u.id=sc.student_user_id
                               JOIN schedules s ON s.id=sc.schedule_id
                               WHERE s.program=? ORDER BY s.year_level,s.section,u.last_name,u.first_name,u.full_name''', (program,)).fetchall()
    year_student_groups=[]
    for year in YEAR_LEVELS:
        year_student_groups.append({'year_level':year,'items':[dict(r) for r in students if r['year_level']==year]})
    conn.close()
    return render_template('attendance.html',active_page='attendance',leaves=leaves,today_classes=today_classes,attendance_history=history,students=students,year_student_groups=year_student_groups,today=today)


@app.route('/leave/<int:leave_id>/<action>',methods=['POST'])
@role_required('program_head')
def decide_leave(leave_id,action):
    if action not in {'approve','reject'}:
        return redirect(url_for('attendance_records'))
    conn=db()
    leave=conn.execute('SELECT * FROM leaves WHERE id=?',(leave_id,)).fetchone()
    if not leave:
        conn.close(); flash('Leave request not found.','error'); return redirect(url_for('attendance_records'))
    if leave['program'] != session.get('program','').strip():
        conn.close(); flash('Only the Program Head of the teacher’s scheduled program can decide this leave request.','error'); return redirect(url_for('attendance_records'))
    status='Approved' if action=='approve' else 'Rejected'
    conn.execute('UPDATE leaves SET status=? WHERE id=?',(status,leave_id))
    conn.commit(); conn.close()
    flash(f'Leave request {status.lower()}.','success')
    return redirect(url_for('attendance_records'))


def _month_bounds(month_key, today_date):
    year, month = map(int, month_key.split('-'))
    start = datetime(year, month, 1).date()
    if month == 12:
        next_start = datetime(year + 1, 1, 1).date()
    else:
        next_start = datetime(year, month + 1, 1).date()
    end = next_start - timedelta(days=1)
    # Attendance history should never show future dates.
    if end > today_date:
        end = today_date
    return start, end


def _month_label(month_key):
    return datetime.strptime(month_key + '-01', '%Y-%m-%d').strftime('%B %Y')


def build_teacher_student_attendance_months(conn, schedules):
    """Build month -> year level -> class -> scheduled-date attendance history.

    Each occurrence is independent. Student attendance is editable only when the
    teacher has an Attending record with required photo proof for that exact class date.
    """
    today_date = now_local().date()
    month_keys = {today_date.strftime('%Y-%m')}

    schedule_ids = [int(s['id']) for s in schedules]
    if schedule_ids:
        placeholders = ','.join('?' for _ in schedule_ids)
        rows = conn.execute(
            f'''SELECT DISTINCT substr(attendance_date,1,7) AS month_key
                FROM student_attendance
                WHERE schedule_id IN ({placeholders})''', schedule_ids
        ).fetchall()
        month_keys.update(r['month_key'] for r in rows if r['month_key'])
        rows = conn.execute(
            f'''SELECT DISTINCT substr(attendance_date,1,7) AS month_key
                FROM attendance
                WHERE schedule_id IN ({placeholders})''', schedule_ids
        ).fetchall()
        month_keys.update(r['month_key'] for r in rows if r['month_key'])
        rows = conn.execute(
            f'''SELECT DISTINCT substr(leave_date,1,7) AS month_key
                FROM leaves
                WHERE schedule_id IN ({placeholders})''', schedule_ids
        ).fetchall()
        month_keys.update(r['month_key'] for r in rows if r['month_key'])

    months = []
    for month_key in sorted(month_keys, reverse=True):
        start_date, end_date = _month_bounds(month_key, today_date)
        if end_date < start_date:
            continue
        year_groups = []
        for year in YEAR_LEVELS:
            classes = []
            for s in schedules:
                if (s['year_level'] or '') != year:
                    continue
                students = conn.execute(
                    '''SELECT u.id,u.full_name
                       FROM student_classes sc
                       JOIN users u ON u.id=sc.student_user_id
                       WHERE sc.schedule_id=? AND u.role='student'
                       ORDER BY u.last_name,u.first_name,u.full_name''', (s['id'],)
                ).fetchall()
                if not students:
                    continue
                occurrence_dates = []
                cursor = start_date
                while cursor <= end_date:
                    date_str = cursor.strftime('%Y-%m-%d')
                    if schedule_day_matches_date(s['day'], date_str):
                        teacher_state = class_status(conn, s['id'], date_str, s['day'])
                        status_rows = conn.execute(
                            '''SELECT student_user_id,status
                               FROM student_attendance
                               WHERE schedule_id=? AND attendance_date=?''',
                            (s['id'], date_str)
                        ).fetchall()
                        status_map = {row['student_user_id']: row['status'] for row in status_rows}
                        occurrence_dates.append({
                            'date': date_str,
                            'label': cursor.strftime('%A, %B %d, %Y'),
                            'teacher_status': teacher_state,
                            'can_edit': teacher_state == 'Attending',
                            'students': [
                                {'id': st['id'], 'full_name': st['full_name'],
                                 'status': status_map.get(st['id'], '')}
                                for st in students
                            ]
                        })
                    cursor += timedelta(days=1)
                if occurrence_dates:
                    classes.append({
                        'id': s['id'], 'subject_code': s['subject_code'],
                        'subject_name': s['subject_name'], 'program': s['program'],
                        'year_level': s['year_level'], 'section': s['section'],
                        'day': s['day'], 'start_time': s['start_time'],
                        'end_time': s['end_time'], 'room': s['room'],
                        'occurrences': occurrence_dates
                    })
            if classes:
                year_groups.append({'year_level': year, 'classes': classes})
        if year_groups:
            months.append({'key': month_key, 'label': _month_label(month_key), 'year_groups': year_groups})
    return months


def teacher_page_context():
    conn = db()
    teacher = conn.execute('SELECT * FROM teachers WHERE user_id=? LIMIT 1',(session['user_id'],)).fetchone()
    if teacher:
        schedules = conn.execute("""SELECT * FROM schedules WHERE teacher_id=?
                                  OR (lower(teacher_name)=lower(?) AND lower(username)=lower(?))
                                  ORDER BY CASE year_level WHEN 'Year 1' THEN 1 WHEN 'Year 2' THEN 2 WHEN 'Year 3' THEN 3 WHEN 'Year 4' THEN 4 ELSE 9 END,day,start_time""",
                                 (teacher['id'],session['full_name'],session['username'])).fetchall()
    else:
        schedules = conn.execute("""SELECT * FROM schedules WHERE lower(teacher_name)=lower(?) OR lower(username)=lower(?)
                                  ORDER BY CASE year_level WHEN 'Year 1' THEN 1 WHEN 'Year 2' THEN 2 WHEN 'Year 3' THEN 3 WHEN 'Year 4' THEN 4 ELSE 9 END,day,start_time""",
                                 (session['full_name'],session['username'])).fetchall()
    class_data = enrich_teacher_classes(conn,schedules)
    student_attendance_months = build_teacher_student_attendance_months(conn,schedules)
    leaves = conn.execute("""SELECT l.*,s.subject_code,s.subject_name,s.program,s.year_level,s.section,s.day,s.start_time,s.end_time
                           FROM leaves l LEFT JOIN schedules s ON s.id=l.schedule_id
                           WHERE lower(l.teacher_name)=lower(?) ORDER BY l.leave_date DESC,l.id DESC""",
                          (session['full_name'],)).fetchall()
    conn.close()
    return {'class_data':class_data,'student_attendance_months':student_attendance_months,'leaves':leaves,'today':today_string()}

@app.route('/teacher')
@role_required('teacher')
def teacher_portal():
    return render_template('teacher.html',active_page='teacher_classes',**teacher_page_context())

@app.route('/teacher/leave-requests')
@role_required('teacher')
def teacher_leave_requests():
    return render_template('teacher_leave.html',active_page='teacher_leave',**teacher_page_context())

@app.route('/teacher/student-attendance')
@role_required('teacher')
def teacher_student_attendance_page():
    return render_template('teacher_student_attendance.html',active_page='teacher_student_attendance',**teacher_page_context())


@app.route('/teacher/claim-subject',methods=['POST'])
@role_required('teacher')
def teacher_claim_subject():
    code=request.form.get('code','').strip().upper()
    conn=db()
    row=conn.execute("SELECT s.id FROM class_codes c JOIN schedules s ON s.id=c.schedule_id WHERE upper(c.code)=? AND c.code_type='teacher'",(code,)).fetchone()
    if not row:
        flash('Invalid Teacher Code.','error')
        conn.close(); return redirect(url_for('teacher_portal'))
    teacher=conn.execute('SELECT * FROM teachers WHERE user_id=? LIMIT 1',(session['user_id'],)).fetchone()
    if not teacher:
        flash('Teacher profile not found.','error')
        conn.close(); return redirect(url_for('teacher_portal'))
    schedule=conn.execute('SELECT * FROM schedules WHERE id=?',(row['id'],)).fetchone()
    conn.execute('UPDATE schedules SET teacher_id=?,teacher_name=?,username=? WHERE id=?',(teacher['id'],teacher['full_name'],teacher['username'],row['id']))
    conn.commit(); sync_codes(row['id'],conn); conn.commit(); conn.close()
    flash(f"Class load added to your portal: {schedule['subject_code']} — {schedule['subject_name']}.",'success')
    return redirect(url_for('teacher_portal'))


@app.route('/teacher/attendance',methods=['POST'])
@role_required('teacher')
def teacher_attendance():
    schedule_id=request.form.get('schedule_id','').strip()
    photo=request.files.get('photo')
    conn=db()
    schedule=teacher_owns_schedule(conn,schedule_id,session['user_id'],session['full_name'],session['username']) if schedule_id.isdigit() else None
    if not schedule:
        conn.close(); flash('That schedule is not assigned to your teacher account.','error'); return redirect(url_for('teacher_portal'))
    if not photo or not photo.filename:
        conn.close(); flash('Attendance cannot be submitted until you attach a photo proof file.','error'); return redirect(url_for('teacher_portal'))
    if not (photo.mimetype or '').startswith('image/'):
        conn.close(); flash('Attendance proof must be an image file.','error'); return redirect(url_for('teacher_portal'))
    today=today_string()
    if not schedule_day_matches_date(schedule['day'], today):
        conn.close(); flash(f'Attendance can only be submitted on the scheduled class day ({schedule["day"]}). Today is not a class day for this subject.','error'); return redirect(url_for('teacher_portal'))
    approved_leave=conn.execute("SELECT 1 FROM leaves WHERE schedule_id=? AND leave_date=? AND status='Approved' LIMIT 1",(schedule_id,today)).fetchone()
    if approved_leave:
        conn.close(); flash('You cannot mark attendance for a class with an approved leave today.','error'); return redirect(url_for('teacher_portal'))
    filename=secure_filename(f"{session['username']}_{schedule_id}_{datetime.now():%Y%m%d_%H%M%S}_{photo.filename}")
    photo.save(os.path.join(UPLOAD_FOLDER,filename))
    existing=attendance_record(conn,schedule_id,today)
    if existing:
        conn.execute('UPDATE attendance SET teacher_name=?,status=?,photo_name=?,program=?,year_level=?,section=? WHERE id=?',(session['full_name'],'Attended',filename,schedule['program'],schedule['year_level'],schedule['section'],existing['id']))
    else:
        conn.execute('''INSERT INTO attendance(teacher_name,attendance_date,status,photo_name,schedule_id,program,year_level,section)
                        VALUES(?,?,?,?,?,?,?,?)''',(session['full_name'],today,'Attended',filename,schedule_id,schedule['program'],schedule['year_level'],schedule['section']))
    conn.commit(); conn.close()
    flash(f"Attendance marked Attending for {schedule['subject_code']} with photo proof.",'success')
    return redirect(url_for('teacher_portal'))


@app.route('/teacher/leave',methods=['POST'])
@role_required('teacher')
def teacher_leave():
    schedule_id=request.form.get('schedule_id','').strip()
    date=request.form.get('leave_date','').strip()
    reason=request.form.get('reason','').strip()
    conn=db()
    schedule=teacher_owns_schedule(conn,schedule_id,session['user_id'],session['full_name'],session['username']) if schedule_id.isdigit() else None
    if not schedule:
        conn.close(); flash('That schedule is not assigned to your teacher account.','error'); return redirect(url_for('teacher_leave_requests'))
    if not date or not reason:
        conn.close(); flash('Leave date and reason are required.','error'); return redirect(url_for('teacher_leave_requests'))
    if date < today_string():
        conn.close(); flash('Leave date cannot be in the past.','error'); return redirect(url_for('teacher_leave_requests'))
    if not schedule_day_matches_date(schedule['day'], date):
        conn.close(); flash(f'Leave can only be filed for this class on its scheduled day ({schedule["day"]}).','error'); return redirect(url_for('teacher_leave_requests'))
    duplicate=conn.execute("SELECT 1 FROM leaves WHERE schedule_id=? AND leave_date=? AND status IN ('Pending','Approved') LIMIT 1",(schedule_id,date)).fetchone()
    if duplicate:
        conn.close(); flash('A pending or approved leave already exists for this class and date.','error'); return redirect(url_for('teacher_leave_requests'))
    conn.execute('''INSERT INTO leaves(teacher_name,leave_date,reason,status,schedule_id,program,year_level,section)
                    VALUES(?,?,?,?,?,?,?,?)''',(session['full_name'],date,reason,'Pending',schedule_id,schedule['program'],schedule['year_level'],schedule['section']))
    conn.commit(); conn.close()
    flash(f"Leave request submitted to the {schedule['program']} Program Head for approval.",'success')
    return redirect(url_for('teacher_leave_requests'))


@app.route('/teacher/student-attendance',methods=['POST'])
@role_required('teacher')
def teacher_student_attendance():
    schedule_id=request.form.get('schedule_id','').strip()
    student_id=request.form.get('student_user_id','').strip()
    attendance_date=request.form.get('attendance_date','').strip()
    status=request.form.get('status','').strip()
    if status not in {'Present','Absent','Excused'}:
        flash('Invalid student attendance status.','error'); return redirect(url_for('teacher_student_attendance_page'))
    conn=db()
    schedule=teacher_owns_schedule(conn,schedule_id,session['user_id'],session['full_name'],session['username']) if schedule_id.isdigit() else None
    if not schedule or not student_id.isdigit():
        conn.close(); flash('Invalid class or student attendance request.','error'); return redirect(url_for('teacher_student_attendance_page'))
    try:
        datetime.strptime(attendance_date, '%Y-%m-%d')
    except ValueError:
        conn.close(); flash('Please select a valid class date.','error'); return redirect(url_for('teacher_student_attendance_page'))
    if attendance_date > today_string():
        conn.close(); flash('Student attendance cannot be recorded for a future date.','error'); return redirect(url_for('teacher_student_attendance_page'))
    enrolled=conn.execute('SELECT 1 FROM student_classes WHERE schedule_id=? AND student_user_id=?',(schedule_id,student_id)).fetchone()
    if not enrolled:
        conn.close(); flash('That student is not enrolled in this class.','error'); return redirect(url_for('teacher_student_attendance_page'))
    if not schedule_day_matches_date(schedule['day'], attendance_date):
        conn.close(); flash(f'Student attendance can only be recorded on the scheduled class day ({schedule["day"]}).','error'); return redirect(url_for('teacher_student_attendance_page'))
    # Student attendance is unlocked only after the teacher has successfully
    # marked Attending for this exact class occurrence with photo proof.
    teacher_state = class_status(conn, schedule_id, attendance_date, schedule['day'])
    if teacher_state != 'Attending':
        conn.close(); flash('Student attendance is locked. The teacher must be marked Attending for this class date with photo proof first. If the teacher is on leave or did not attend, students do not need to be checked.','error'); return redirect(url_for('teacher_student_attendance_page'))
    existing=conn.execute('SELECT id FROM student_attendance WHERE schedule_id=? AND student_user_id=? AND attendance_date=?',(schedule_id,student_id,attendance_date)).fetchone()
    if existing:
        conn.execute('UPDATE student_attendance SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,existing['id']))
    else:
        conn.execute('INSERT INTO student_attendance(schedule_id,student_user_id,attendance_date,status) VALUES(?,?,?,?)',(schedule_id,student_id,attendance_date,status))
    conn.commit(); conn.close()
    flash(f'Student attendance updated to {status} for {attendance_date}.','success')
    return redirect(url_for('teacher_student_attendance_page'))


@app.route('/student')
@role_required('student')
def student_portal():
    today=today_string()
    conn=db()
    rows=conn.execute('''SELECT sc.id,s.* FROM student_classes sc JOIN schedules s ON s.id=sc.schedule_id
                         WHERE sc.student_user_id=?
                         ORDER BY CASE s.year_level WHEN 'Year 1' THEN 1 WHEN 'Year 2' THEN 2 WHEN 'Year 3' THEN 3 WHEN 'Year 4' THEN 4 ELSE 9 END,s.day,s.start_time''',(session['user_id'],)).fetchall()
    classes=[]
    for srow in rows:
        item=dict(srow)
        item['attendance_status']=class_status(conn,srow['id'],today,srow['day'])
        item['is_class_day']=schedule_day_matches_date(srow['day'], today)
        classes.append(item)
    conn.close()
    return render_template('student.html',classes=classes,today=today)


@app.route('/student/add-subject',methods=['POST'])
@role_required('student')
def student_add_subject():
    code=request.form.get('code','').strip().upper(); conn=db(); row=conn.execute("SELECT schedule_id FROM class_codes WHERE upper(code)=? AND code_type='student'",(code,)).fetchone()
    if not row: flash('Class code not found.','error')
    else:
        try: conn.execute('INSERT INTO student_classes(student_user_id,schedule_id) VALUES(?,?)',(session['user_id'],row['schedule_id'])); conn.commit(); flash('Subject added to your schedule.','success')
        except sqlite3.IntegrityError: flash('Subject is already in your schedule.','error')
    conn.close(); return redirect(url_for('student_portal'))


@app.route('/health')
def health(): return jsonify({'status':'ok','service':'OptiSked AI','database':'sqlite'})

init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=os.environ.get('FLASK_DEBUG','0')=='1')
