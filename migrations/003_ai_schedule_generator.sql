-- OptiSked AI migration 003
-- Adds AI schedule generator configuration and generated-session metadata.
-- app.py also performs these compatibility changes automatically at startup for SQLite.

ALTER TABLE schedules ADD COLUMN session_type TEXT DEFAULT 'Lecture';
ALTER TABLE schedules ADD COLUMN course_id INTEGER;
ALTER TABLE schedules ADD COLUMN is_generated INTEGER DEFAULT 0;

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
