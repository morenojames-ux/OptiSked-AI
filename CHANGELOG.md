
## Added — Calendar Editor (drag-and-drop rescheduling)

- New **Calendar Editor** page in the Program Head sidebar with a
  Google-Calendar-style weekly grid per section (Mon–Sat, 07:30–21:00, 30-minute
  snap).
- Draft **and** published sessions can be dragged to a new day/time; duration is
  preserved and each meeting moves independently. Moving a published session
  asks for confirmation.
- Moves are validated server-side by the same hard rules as the AI generator
  (clash, room type, time window, non-preferred windows). Rejected moves snap
  back and show the reason.
- `conflicts_for()` gained an `include_availability` flag; `generated_slot_errors()`
  now skips teacher availability hours/preferred days so manual edits match how
  the AI placed sessions (fixes classes scheduled after 17:00 being wrongly
  rejected on edit).
- New JSON endpoint `POST /schedule/<id>/move`; new `/calendar` route;
  `static/calendar.js` (vanilla drag/drop); calendar styles in `static/style.css`.
- Access codes are unaffected by day/time changes, so no re-sync is needed.


## Changed — Instructor selection uses registered teacher accounts

- The Program Head's **Instructors** page now lists a dropdown of registered
  Teacher accounts instead of a free-text name field.
- Teachers already assigned in the program are hidden from the dropdown to
  prevent duplicates.
- Selecting a teacher auto-fills the "courses they can teach" from that
  account's saved profile (`teachers.subject_qualification`) plus their
  previously assigned subject codes; the merged result is saved back to the
  profile on submit.
- Added `instructors.user_id` linking each instructor to the teacher account,
  with a best-effort backfill for existing rows (migration
  `004_instructor_teacher_link.sql`).
- AI-generated schedules now link `teacher_id`/`username` via the instructor's
  account, falling back to the previous name match for legacy rows.


## Added — AI Schedule Generator (fully automated scheduling)

- Replaced manual single-schedule entry with an automated generator driven by three inputs:
  - **Sections** per year level.
  - **Courses** (code, name, year level, sessions per week, duration, optional laboratory pattern).
  - **Instructors** (name, qualified course codes, and non-preferred day/time windows).
- Added `ai_generator.py`: expands courses into weekly lecture/lab blocks, enumerates hard-constraint-valid placements, scores them with the ML historical-pattern ranker, places them with limited backtracking, and reports any sessions it could not place.
- Laboratory patterns are mutually exclusive radio choices: whole session lab, half lecture/half lab, meets twice (both lab), meets twice (lab and lecture).
- Hard constraints enforced: no room/instructor/section clashes, labs only in Computer Labs, lectures only in Lecture Rooms, instructor non-preferred day/time windows, and course qualification.
- Generated schedules are saved as **Draft** for review, with inline editing and validation, then published individually or all at once (publishing generates access codes).
- `ml_scheduler.py`: added `build_scorer()` (train once, score many) and new features (`session_type`, room-type agreement, instructor load, non-preferred overlap).
- Added migration `003_ai_schedule_generator.sql` and new tables: `program_sections`, `courses`, `instructors`, `instructor_unavailability`, `instructor_qualifications`; new `schedules` columns `session_type`, `course_id`, `is_generated`.
- Program Head navigation now includes AI Schedule Generator, Sections, Courses, and Instructors.


## Added — Machine Learning Scheduling Recommendations

- Added `ml_scheduler.py` with a Scikit-learn Random Forest Classifier.
- Published schedules are used as positive historical examples and nearby unused alternatives as negative examples.
- Conflict-free schedule candidates are ML-ranked before being shown in the AI Assistant.
- The AI panel now reports ML suitability scores and model-training information.
- Added `scikit-learn>=1.8,<2.0` to deployment requirements.

## Schedule Builder Teacher Profile Auto-Fill
- Selecting a Teacher Account now automatically loads that teacher's saved preferred teaching days.
- Saved available-from and available-until times are loaded at the same time.
- The schedule Day selector switches to a saved preferred day when the current day is not allowed.
- This keeps the form and AI conflict suggestions aligned with the selected teacher profile.

# Changelog

## 2026-10-06 — Program Isolation & Global Conflict Detection

### Added
- Program Head signup now requires selecting the academic program managed by the account.
- Program Head Setup page for older accounts without an assigned program.
- Session-level Program Head program assignment.
- Program-specific schedule visibility.
- Program-specific Code Generation visibility and code repair.
- Authorization checks that prevent a Program Head from deleting or applying fixes to schedules outside their program.

### Changed
- Schedule creation is locked to the Program Head's assigned program.
- Student class records shown to a Program Head are limited to that Program Head's program.

### Preserved
- Conflict detection continues to query **all programs**, allowing OptiSked AI to detect teacher and room conflicts across different academic programs.
- Teacher Portal and Student Portal class access remain usable across programs where the correct class code is provided.


## Readable text update
- Increased interface font sizes across Program Head, Teacher, Student, scheduling, code generation, attendance, and authentication screens.
- Preserved the correct `static/optisked-logo.jpg` branding asset.
- Updated CSS cache-busting version.

## Attendance and Program Management Update

### Added
- Program-specific leave approval: only the Program Head responsible for the class program can approve or reject the request.
- Year-level organization on Program Head, Teacher, and Student views.
- Teacher class status controls: Pending, Attending, Leave Pending, and On Leave.
- Mandatory photo proof before a teacher can submit attendance.
- Class-specific teacher attendance tracking.
- Student roster grouped by the year level of each class.
- Present, Absent, and Excused student attendance buttons with editable status.
- Program-scoped attendance and leave records for Program Heads.
- Global conflict checking remains active across all academic programs.

## 2026-10-06 — Teacher Attendance Workflow Update

### Added
- Teacher-side attendance navigation sidebar with year-level links.
- Date-specific teacher attendance states: Attending, Pending, Leave Pending, On Leave, and Not Today.
- Server-side validation so teachers can mark attendance only on the scheduled weekday and only with an image proof file.
- Leave-date selector showing future occurrences of the class's scheduled day.
- Leave records remain tied to the exact class date, so a leave on one Monday does not carry over to the next Monday.
- Student attendance organized by year level with editable Present, Absent, and Excused actions.
- Student attendance actions are limited to the scheduled class day and blocked during approved teacher leave.
- Philippines/Asia-Manila application timezone for correct online daily attendance resets.
- Program Head attendance view now focuses on classes actually scheduled for the current day.

## Teacher student attendance monthly organization
- Organized student attendance by month, then year level, class, and scheduled class date.
- Added date-specific student attendance editing for historical scheduled occurrences.
- Locked student attendance unless the teacher has successfully marked Attending for that exact class date with photo proof.
- Teachers on approved leave, leave pending, or without confirmed attendance do not need to check students; the student controls are disabled.
- Added the selected attendance date to student-attendance submissions and validated it server-side.

## Teacher navigation fix
- Removed the duplicate inner Teacher Menu so the portal has one sidebar only.
- Teacher sidebar is now exactly: My Classes, Leave Requests, Student Attendance.
- Section navigation now smoothly scrolls to the selected section and updates the active sidebar item.
- Hash navigation is preserved for direct links and browser back/forward behavior.

## Teacher portal navigation update
- Split Teacher Portal into separate pages: My Classes, Leave Requests, and Student Attendance.
- Removed scroll/hash navigation so each area has its own route and page.
- Kept monthly/year-level student attendance organization and date-based attendance rules.

## Attendance status fix
- Teacher attendance records are now recognized using canonical and legacy attendance labels when photo proof exists.
- A rejected leave request no longer overrides a later successful teacher attendance submission for the same scheduled class date.
- Student faculty status is derived from the same date-specific class occurrence, so enrolled students immediately see **Attending** after the teacher successfully submits proof.
