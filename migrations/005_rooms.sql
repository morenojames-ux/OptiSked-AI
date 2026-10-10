-- OptiSked AI migration 005
-- Adds the shared, editable room list used by the AI schedule generator.
-- app.py also creates and seeds this table automatically at startup.

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    room_type TEXT NOT NULL CHECK(room_type IN ('lab','lecture'))
);

-- Default Computer Labs (201, 202, 301, 103-106).
INSERT OR IGNORE INTO rooms(name,room_type) VALUES
    ('201','lab'), ('202','lab'), ('301','lab'),
    ('103','lab'), ('104','lab'), ('105','lab'), ('106','lab');

-- Default Lecture Rooms (203-224, 303-310).
INSERT OR IGNORE INTO rooms(name,room_type) VALUES
    ('203','lecture'), ('204','lecture'), ('205','lecture'), ('206','lecture'),
    ('207','lecture'), ('208','lecture'), ('209','lecture'), ('210','lecture'),
    ('211','lecture'), ('212','lecture'), ('213','lecture'), ('214','lecture'),
    ('215','lecture'), ('216','lecture'), ('217','lecture'), ('218','lecture'),
    ('219','lecture'), ('220','lecture'), ('221','lecture'), ('222','lecture'),
    ('223','lecture'), ('224','lecture'),
    ('303','lecture'), ('304','lecture'), ('305','lecture'), ('306','lecture'),
    ('307','lecture'), ('308','lecture'), ('309','lecture'), ('310','lecture');
