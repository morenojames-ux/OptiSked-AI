-- OptiSked AI migration 006
-- Adds per-course allowed rooms and ensures the rooms table schema exists.
-- app.py also creates these tables automatically at startup.

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

INSERT OR IGNORE INTO rooms(name,room_type) VALUES
    ('201','lab'), ('202','lab'), ('301','lab'),
    ('103','lab'), ('104','lab'), ('105','lab'), ('106','lab');

INSERT OR IGNORE INTO rooms(name,room_type) VALUES
    ('203','lecture'), ('204','lecture'), ('205','lecture'), ('206','lecture'),
    ('207','lecture'), ('208','lecture'), ('209','lecture'), ('210','lecture'),
    ('211','lecture'), ('212','lecture'), ('213','lecture'), ('214','lecture'),
    ('215','lecture'), ('216','lecture'), ('217','lecture'), ('218','lecture'),
    ('219','lecture'), ('220','lecture'), ('221','lecture'), ('222','lecture'),
    ('223','lecture'), ('224','lecture'),
    ('303','lecture'), ('304','lecture'), ('305','lecture'), ('306','lecture'),
    ('307','lecture'), ('308','lecture'), ('309','lecture'), ('310','lecture');
