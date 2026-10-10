# OptiSked AI — AI Auto-Schedule Generator

OptiSked AI now builds the timetable for you. Instead of entering each class
manually, a Program Head configures three inputs and lets the AI place every
session:

- **Sections** — the class sections per year level (for example Year 1: A, B).
- **Courses** — course code, name, year level, sessions per week, session
  duration, and an optional **laboratory** with a meeting pattern.
- **Instructors** — pick from the registered **Teacher accounts** (a dropdown;
  free typing is no longer used), confirm the course codes they can teach, and
  set any **non-preferred day/time windows** the AI must avoid. The course codes
  auto-fill from the teacher's saved profile and previously assigned classes,
  and are saved back to the profile for next time. Teachers already added here
  are hidden from the dropdown.

## Laboratory meeting patterns

When a course has a laboratory, exactly one pattern is chosen:

| Pattern | Weekly meetings | Room types |
|---|---|---|
| Whole session is Laboratory | 1 lab | Computer Lab |
| Half lecture, half laboratory | 1 day: lecture + lab back-to-back | Lecture Room, then Computer Lab |
| Meets twice a week (both Laboratory) | 2 labs | Computer Lab |
| Meets twice a week (Lab and Lecture) | 1 lab + 1 lecture | Computer Lab, Lecture Room |

## How the AI builds the schedule

1. Each course is expanded into weekly lecture/lab **blocks** for every section.
2. Hard constraints are applied: no room, instructor or section clashes; labs
   only in Computer Labs; lectures only in Lecture Rooms; instructors never
   placed inside a non-preferred window; and instructors only teaching courses
   they are qualified for.
3. Feasible placements are scored by the machine-learning historical-pattern
   ranker (Scikit-learn Random Forest). With little history, a deterministic
   fallback is used so a schedule is always produced.
4. Blocks are placed with limited backtracking. Sessions that cannot be placed
   are reported instead of being forced into an invalid schedule.
5. Results are saved as **Draft** sessions for review, with inline editing.
   Publish individually or all at once; publishing generates access codes.

The existing Program Head, Teacher, and Student portals, attendance, leave, and
code-generation workflows continue to work on published schedules.

Generation only schedules **new** course offerings: any course that already has
published sessions for a section is skipped, so re-running the generator never
duplicates a course's weekly meetings. Drafts are still replaced on every run;
delete a published session to include that course in a future generation.

## Calendar Editor (drag-and-drop)

The **Calendar Editor** page (program-head sidebar) shows a Google-Calendar-style
weekly grid per section. Pick a Year + Section, then **drag** any draft or
published session to a new day and time. Dragging changes only the day and time
(the duration and everything else stay the same).

- Validation matches the AI's hard rules: instructor, room and section clashes,
  laboratory-vs-lecture room type, the 07:30–21:00 window, and instructor
  non-preferred windows. A rejected move snaps back and explains why.
- Access codes do not encode the day/time, so moving a session does not
  invalidate them.
- Moving a **published** session asks for confirmation first.
- The table view on the AI Schedule Generator page is still available for
  editing the instructor or room. **Published sessions can be edited too** — a
  confirmation is shown, and access codes refresh automatically if the
  instructor changes (room/day/time edits leave codes unchanged).

## Room configuration

The **Rooms** page (program-head sidebar) is where the list of available rooms
is managed. Rooms are added one at a time (number + type) or as a numeric range,
and are grouped into **Computer Labs** and **Lecture Rooms**.

- The room list is **shared across all programs** so double-bookings are detected
  everywhere.
- Lab sessions can only be placed in Computer Labs; every other session uses
  Lecture Rooms.
- Defaults seeded on first run: labs `201, 202, 301, 103–106`; lecture rooms
  `203–224, 303–310`.

## Run locally

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

There are no demo accounts or demo schedules in this build.


UI update: larger, more readable text throughout the web interface. The logo remains the real static/optisked-logo.jpg asset.


## Program Head data separation

When a user signs up as **Program Head**, the system requires them to select the academic program they manage. The Program Head Schedule Builder and Code Generation pages only display records for that assigned program. Program Heads cannot create, delete, or modify schedules belonging to another program.

Conflict detection remains global: when a Program Head creates a schedule, OptiSked checks the complete schedule database for cross-program conflicts such as a shared teacher or double-booked room, while section conflicts remain scoped to the same program/year/section.

Existing Program Head accounts created before this update are sent to a one-time **Program Head Setup** page after login so their managed program can be assigned.


### Readability Update
The current UI uses larger typography and spacing for easier classroom and panel demonstration. The official uploaded logo is located at `static/optisked-logo.jpg`.

## Attendance and year-level workflow

- Program Head schedules, code generation, attendance records, and student class records are filtered to the Program Head's assigned program.
- Teacher leave requests are tied to the specific class/program. Only the Program Head responsible for that class program can approve or reject the request.
- Teacher class status shows **Pending**, **Attending**, **Leave Pending**, or **On Leave** for the current day.
- Teacher attendance requires an attached image proof. The server rejects attendance submissions without an image file.
- Teacher class cards show enrolled students under the class's year level, with editable **Present**, **Absent**, and **Excused** attendance controls.
- Student schedules show their faculty status as **Attending**, **On Leave**, **Leave Pending**, or **Pending**.
- Program Head Attendance Records show today's class-by-class teacher status organized by year level, plus photo proof and program-scoped leave requests.
- Global conflict checking remains active across all programs, including cross-program teacher and room conflicts.

## Machine Learning Recommendation Model

OptiSked AI uses a hybrid recommendation approach:

1. The rule-based constraint engine removes candidates with teacher, room, section, preferred-day, availability, or approved-leave conflicts.
2. The ML layer uses a **Scikit-learn Random Forest Classifier** to rank the remaining candidates based on historical scheduling patterns.
3. Published schedules are treated as positive historical examples; nearby alternative slots that were not historically used are generated as negative examples.
4. New recommendations are ranked by ML suitability and can be applied through **Apply Fix**.
5. The model is retrained from the current published-schedule dataset whenever recommendations are generated, so newly verified schedules become part of future training data.

If there is not enough history or Scikit-learn is temporarily unavailable, the deterministic constraint engine still protects schedule validity and the UI reports that ML ranking is inactive.
