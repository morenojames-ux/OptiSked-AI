"""AI schedule generation engine for OptiSked AI.

This module turns the Program Head's three inputs -- instructors, courses and
sections -- into a complete weekly timetable.

Pipeline
--------
1. Expand every course into one or more *blocks*.  A block is a group of one
   or more consecutive class segments (for example a "half lecture / half lab"
   course is a single block with a Lecture segment immediately followed by a
   Lab segment).  The laboratory pattern chosen on the course controls this.
2. Order the blocks by scarcity so the hardest placements are attempted first
   (lab sessions compete for the fewest rooms).
3. For each block, enumerate every candidate placement (instructor + day +
   start time + room assignment) that satisfies the hard constraints.
4. Score the candidates with the machine-learning historical-pattern ranker
   from :mod:`ml_scheduler`.  When there is not enough history to train the
   model, a deterministic fallback heuristic is used instead.
5. Place the best scoring candidate and backtrack when a later block has no
   feasible placement.  Blocks that still cannot be placed are reported back
   to the Program Head rather than forced into an invalid schedule.

The engine never returns an arrangement that violates a hard constraint such
as a room double-booking, instructor double-booking, section clash, an
instructor's non-preferred day/time window, room-type mismatch, or an
instructor teaching a course they are not qualified for.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from ml_scheduler import build_scorer

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}

DAY_START = 450      # 07:30
DAY_END = 1260       # 21:00
STEP = 30            # scheduling grid granularity in minutes

MAX_CANDIDATES_PER_BLOCK = 30
MAX_STEPS = 60000

LAB_SESSION = "Lab"
LECTURE_SESSION = "Lecture"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def parse_minutes(value) -> int:
    try:
        h, m = map(int, str(value).split(":"))
        return h * 60 + m
    except Exception:
        return -1


def minutes_to_hhmm(value: int) -> str:
    value = max(0, min(1439, int(value)))
    return f"{value // 60:02d}:{value % 60:02d}"


def room_type(room: str) -> str:
    room = (room or "").lower()
    if "computer lab" in room:
        return "computer_lab"
    if "lecture room" in room:
        return "lecture_room"
    return "other"


def session_room_type(session_type: str) -> str:
    return "computer_lab" if session_type == LAB_SESSION else "lecture_room"


def ranges_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def day_within_range(day: str, day_from: str, day_to: str) -> bool:
    """Return True when ``day`` falls inside an inclusive day range.

    Ranges are expressed as weekday names and support wrapping, so a range of
    ``Fri`` to ``Mon`` covers Friday, Saturday, Sunday and Monday.
    """
    if not day_from or not day_to:
        return False
    if day not in DAY_INDEX or day_from not in DAY_INDEX or day_to not in DAY_INDEX:
        return False
    start = DAY_INDEX[day_from]
    end = DAY_INDEX[day_to]
    current = DAY_INDEX[day]
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def normalize_code(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


# ---------------------------------------------------------------------------
# block expansion
# ---------------------------------------------------------------------------
def _segments_for_course(course: dict) -> List[List[dict]]:
    """Return the list of blocks (each a list of segments) for a course.

    Each segment is ``{"session_type": "Lecture"|"Lab", "minutes": int}``.
    """
    base = int(round(float(course.get("duration_hours") or 1.5) * 60))
    base = max(30, base)
    has_lab = bool(course.get("has_lab"))
    pattern = (course.get("lab_pattern") or "").strip()
    sessions = int(course.get("sessions_per_week") or 1)
    sessions = max(1, sessions)

    if not has_lab:
        return [[{"session_type": LECTURE_SESSION, "minutes": base}] for _ in range(sessions)]

    if pattern == "whole_lab":
        return [[{"session_type": LAB_SESSION, "minutes": base}]]

    if pattern == "half_half":
        half = max(15, base // 2)
        remainder = max(15, base - half)
        return [[
            {"session_type": LECTURE_SESSION, "minutes": half},
            {"session_type": LAB_SESSION, "minutes": remainder},
        ]]

    if pattern == "twice_both_lab":
        return [[{"session_type": LAB_SESSION, "minutes": base}] for _ in range(2)]

    if pattern == "twice_lab_lecture":
        return [
            [{"session_type": LAB_SESSION, "minutes": base}],
            [{"session_type": LECTURE_SESSION, "minutes": base}],
        ]

    # Unknown lab pattern: fall back to a single lab meeting.
    return [[{"session_type": LAB_SESSION, "minutes": base}]]


def expand_blocks(courses: Iterable[dict], sections: Iterable[dict]) -> List[dict]:
    courses = [dict(c) for c in courses]
    sections = [dict(s) for s in sections]
    blocks: List[dict] = []

    for course in courses:
        applicable = [s for s in sections if s.get("year_level") == course.get("year_level")]
        for section in applicable:
            for index, segments in enumerate(_segments_for_course(course)):
                blocks.append({
                    "program": course.get("program", ""),
                    "course_id": course.get("id"),
                    "course_code": course.get("course_code", ""),
                    "course_name": course.get("course_name", ""),
                    "year_level": course.get("year_level", ""),
                    "section": section.get("section_name", ""),
                    "has_lab": bool(course.get("has_lab")),
                    "lab_pattern": course.get("lab_pattern") or "",
                    "segments": segments,
                    "total_minutes": sum(seg["minutes"] for seg in segments),
                    "block_index": index,
                })
    return blocks


# ---------------------------------------------------------------------------
# placement context (hard-constraint bookkeeping)
# ---------------------------------------------------------------------------
class PlacementContext:
    def __init__(self, rooms: Sequence[str], existing_schedules: Iterable[dict],
                 instructors: Iterable[dict], qualifications: Iterable[dict],
                 unavailability: Iterable[dict]):
        self.rooms = list(dict.fromkeys(rooms))
        self.instructors = {int(i["id"]): dict(i) for i in instructors}
        self.qualifications: Dict[str, set] = {}
        for q in qualifications:
            key = normalize_code(q.get("course_code", ""))
            self.qualifications.setdefault(key, set()).add(int(q["instructor_id"]))
        self.unavailability: Dict[int, List[dict]] = {}
        for window in unavailability:
            self.unavailability.setdefault(int(window["instructor_id"]), []).append(dict(window))

        self.room_busy: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
        self.instructor_busy: Dict[Tuple[int, str], List[Tuple[int, int]]] = {}
        self.section_busy: Dict[Tuple[str, str, str, str], List[Tuple[int, int]]] = {}
        self.instructor_load: Dict[int, int] = {iid: 0 for iid in self.instructors}
        self.room_load: Dict[str, int] = {room: 0 for room in self.rooms}

        for row in existing_schedules:
            start = parse_minutes(row.get("start_time"))
            end = parse_minutes(row.get("end_time"))
            if start < 0 or end < 0:
                continue
            day = row.get("day")
            room = row.get("room")
            if room:
                self.room_busy.setdefault((day, room), []).append((start, end))
            section_key = (
                row.get("program", ""), row.get("year_level", ""),
                row.get("section", ""), day,
            )
            self.section_busy.setdefault(section_key, []).append((start, end))

    # -- room helpers -----------------------------------------------------
    def available_rooms(self, day: str, start: int, end: int, wanted_type: str,
                        limit: int = 3) -> List[str]:
        free = []
        for room in self.rooms:
            if room_type(room) != wanted_type:
                continue
            busy = self.room_busy.get((day, room), [])
            if any(ranges_overlap(start, end, b_start, b_end) for b_start, b_end in busy):
                continue
            free.append(room)
        free.sort(key=lambda r: (self.room_load.get(r, 0), r))
        return free[:limit]

    # -- instructor helpers ----------------------------------------------
    def qualified_instructors(self, course_code: str) -> List[int]:
        return sorted(self.qualifications.get(normalize_code(course_code), set()))

    def instructor_available(self, instructor_id: int, day: str, start: int, end: int) -> bool:
        busy = self.instructor_busy.get((instructor_id, day), [])
        if any(ranges_overlap(start, end, b_start, b_end) for b_start, b_end in busy):
            return False
        for window in self.unavailability.get(instructor_id, []):
            if not day_within_range(day, window.get("day_from", ""), window.get("day_to", "")):
                continue
            w_start = parse_minutes(window.get("start_time"))
            w_end = parse_minutes(window.get("end_time"))
            if w_start < 0 or w_end < 0:
                continue
            if ranges_overlap(start, end, w_start, w_end):
                return False
        return True

    def section_available(self, program: str, year_level: str, section: str,
                          day: str, start: int, end: int) -> bool:
        busy = self.section_busy.get((program, year_level, section, day), [])
        return not any(ranges_overlap(start, end, b_start, b_end) for b_start, b_end in busy)

    # -- mutation ---------------------------------------------------------
    def commit(self, candidate: dict):
        day = candidate["day"]
        instructor_id = candidate["instructor_id"]
        for segment in candidate["segments"]:
            self.room_busy.setdefault((day, segment["room"]), []).append(
                (segment["start"], segment["end"])
            )
            self.room_load[segment["room"]] = self.room_load.get(segment["room"], 0) + 1
        self.instructor_busy.setdefault((instructor_id, day), []).append(
            (candidate["start"], candidate["end"])
        )
        self.instructor_load[instructor_id] = self.instructor_load.get(instructor_id, 0) + 1
        key = (candidate["program"], candidate["year_level"], candidate["section"], day)
        self.section_busy.setdefault(key, []).append((candidate["start"], candidate["end"]))

    def rollback(self, candidate: dict):
        day = candidate["day"]
        window = (candidate["start"], candidate["end"])
        for segment in candidate["segments"]:
            entries = self.room_busy.get((day, segment["room"]), [])
            seg_window = (segment["start"], segment["end"])
            if seg_window in entries:
                entries.remove(seg_window)
            self.room_load[segment["room"]] = max(0, self.room_load.get(segment["room"], 0) - 1)
        instructor_id = candidate["instructor_id"]
        entries = self.instructor_busy.get((instructor_id, day), [])
        if window in entries:
            entries.remove(window)
        self.instructor_load[instructor_id] = max(0, self.instructor_load.get(instructor_id, 0) - 1)
        section_key = (candidate["program"], candidate["year_level"], candidate["section"], day)
        entries = self.section_busy.get(section_key, [])
        if window in entries:
            entries.remove(window)


# ---------------------------------------------------------------------------
# candidate enumeration + scoring
# ---------------------------------------------------------------------------
def _make_candidate(block: dict, instructor_id: int, day: str, start: int,
                    segments: List[dict], context: PlacementContext) -> dict:
    end = segments[-1]["end"]
    return {
        "program": block["program"],
        "course_id": block["course_id"],
        "course_code": block["course_code"],
        "course_name": block["course_name"],
        "year_level": block["year_level"],
        "section": block["section"],
        "teacher_id": None,
        "teacher_name": context.instructors[instructor_id].get("name", ""),
        "username": "",
        "instructor_id": instructor_id,
        "day": day,
        "start_time": minutes_to_hhmm(start),
        "end_time": minutes_to_hhmm(end),
        "start": start,
        "end": end,
        "room": segments[0]["room"],
        "session_type": segments[0]["session_type"] if len(segments) == 1 else "Mixed",
        "segments": segments,
        "_instructor_load": context.instructor_load.get(instructor_id, 0),
        "_nonpreferred_overlap": 0,
    }


def enumerate_candidates(block: dict, context: PlacementContext) -> List[dict]:
    qualified = context.qualified_instructors(block["course_code"])
    if not qualified:
        return []

    total = block["total_minutes"]
    if DAY_START + total > DAY_END:
        return []

    candidates: List[dict] = []
    for instructor_id in qualified:
        for day in DAYS:
            latest_start = DAY_END - total
            for start in range(DAY_START, latest_start + 1, STEP):
                end = start + total
                if not context.instructor_available(instructor_id, day, start, end):
                    continue
                if not context.section_available(
                    block["program"], block["year_level"], block["section"], day, start, end
                ):
                    continue
                segments, ok = _assign_segments(block, context, day, start)
                if not ok:
                    continue
                candidates.append(_make_candidate(block, instructor_id, day, start, segments, context))
                if len(candidates) >= MAX_CANDIDATES_PER_BLOCK * 4:
                    return candidates
    return candidates


def _assign_segments(block: dict, context: PlacementContext, day: str,
                     block_start: int) -> Tuple[List[dict], bool]:
    """Choose a concrete room for every segment of a block."""
    cursor = block_start
    chosen: List[dict] = []
    for seg in block["segments"]:
        seg_start = cursor
        seg_end = cursor + seg["minutes"]
        wanted = session_room_type(seg["session_type"])
        rooms = context.available_rooms(day, seg_start, seg_end, wanted)
        used = {s["room"] for s in chosen}
        rooms = [r for r in rooms if r not in used]
        if not rooms:
            return [], False
        chosen.append({
            "session_type": seg["session_type"],
            "minutes": seg["minutes"],
            "start": seg_start,
            "end": seg_end,
            "room": rooms[0],
        })
        cursor = seg_end
    return chosen, True


def _deterministic_score(candidate: dict, context: PlacementContext) -> float:
    """Fallback heuristic used when the ML model cannot be trained."""
    score = 100.0
    score -= DAY_INDEX.get(candidate["day"], 0) * 3.0
    score -= (candidate["start"] - DAY_START) / STEP * 0.4
    score -= context.instructor_load.get(candidate["instructor_id"], 0) * 4.0
    key = (candidate["program"], candidate["year_level"], candidate["section"], candidate["day"])
    for busy_start, busy_end in context.section_busy.get(key, []):
        gap = min(abs(candidate["start"] - busy_end), abs(busy_start - candidate["end"]))
        if gap <= 90:
            score += 3.0
            break
    return score


def _score_candidates(candidates: List[dict], context: PlacementContext,
                      scorer) -> List[dict]:
    if not candidates:
        return []
    if scorer is not None:
        scored = scorer(candidates)
        for item in scored:
            item["score"] = float(item.get("ml_probability", 0))
    else:
        scored = []
        for candidate in candidates:
            item = dict(candidate)
            item["score"] = _deterministic_score(item, context)
            scored.append(item)
    scored.sort(key=lambda c: (
        -c["score"],
        DAY_INDEX.get(c["day"], 0),
        c["start"],
        c.get("room", ""),
    ))
    return scored[:MAX_CANDIDATES_PER_BLOCK]


# ---------------------------------------------------------------------------
# block ordering
# ---------------------------------------------------------------------------
def _order_blocks(blocks: List[dict], context: PlacementContext) -> List[dict]:
    def sort_key(block: dict):
        has_lab = 1 if any(s["session_type"] == LAB_SESSION for s in block["segments"]) else 0
        qualified = len(context.qualified_instructors(block["course_code"]))
        return (
            0 if has_lab else 1,
            -block["total_minutes"],
            qualified,
            block["course_code"],
            block["section"],
        )

    return sorted(blocks, key=sort_key)


# ---------------------------------------------------------------------------
# backtracking placement
# ---------------------------------------------------------------------------
def _candidate_still_valid(candidate: dict, context: PlacementContext) -> bool:
    day = candidate["day"]
    for segment in candidate["segments"]:
        busy = context.room_busy.get((day, segment["room"]), [])
        if any(ranges_overlap(segment["start"], segment["end"], b_start, b_end)
               for b_start, b_end in busy):
            return False
    if not context.instructor_available(
        candidate["instructor_id"], day, candidate["start"], candidate["end"]
    ):
        return False
    if not context.section_available(
        candidate["program"], candidate["year_level"], candidate["section"],
        day, candidate["start"], candidate["end"],
    ):
        return False
    return True


def _place_blocks(blocks: List[dict], context: PlacementContext,
                  candidate_cache: Dict[int, List[dict]]) -> Tuple[List[dict], List[dict]]:
    placed: List[dict] = []
    unplaced: List[dict] = []
    steps = {"count": 0}

    def backtrack(index: int) -> bool:
        if index >= len(blocks):
            return True
        if steps["count"] > MAX_STEPS:
            return False

        candidates = candidate_cache.get(index, [])
        for candidate in candidates:
            steps["count"] += 1
            if steps["count"] > MAX_STEPS:
                return False
            if not _candidate_still_valid(candidate, context):
                continue
            context.commit(candidate)
            placed.append(candidate)
            if backtrack(index + 1):
                return True
            placed.pop()
            context.rollback(candidate)

        # No candidate fits: record the block and keep placing the rest.
        unplaced.append(blocks[index])
        if backtrack(index + 1):
            return True
        unplaced.pop()
        return False

    backtrack(0)
    return placed, unplaced


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def generate(courses: Iterable[dict], sections: Iterable[dict], instructors: Iterable[dict],
             qualifications: Iterable[dict], unavailability: Iterable[dict],
             rooms: Sequence[str], existing_schedules: Iterable[dict]) -> dict:
    """Generate a full draft schedule.

    Returns ``{"rows": [...], "report": {...}, "ml": {...}}`` where ``rows`` are
    ready-to-insert ``schedules`` records (``status='Draft'``).
    """
    courses = [dict(c) for c in courses]
    sections = [dict(s) for s in sections]
    instructors = [dict(i) for i in instructors]
    existing_schedules = [dict(s) for s in existing_schedules]

    blocks = expand_blocks(courses, sections)
    if not blocks:
        return {
            "rows": [],
            "report": {
                "total_blocks": 0,
                "placed_blocks": 0,
                "unplaced": [],
                "message": "No courses and sections are configured yet.",
            },
            "ml": {"active": False, "training_schedules": 0, "message": "Nothing to schedule."},
        }

    context = PlacementContext(rooms, existing_schedules, instructors, qualifications, unavailability)

    historical = [s for s in existing_schedules if (s.get("status") or "Published") == "Published"]
    teacher_like = [
        {
            "id": i.get("id"),
            "full_name": i.get("name", ""),
            "username": "",
            "preferred_days": "",
            "available_from": "07:30",
            "available_until": "21:00",
        }
        for i in instructors
    ]
    scorer, ml_meta = build_scorer(historical, teacher_like)

    unordered = _order_blocks(blocks, context)

    candidate_cache: Dict[int, List[dict]] = {}
    for index, block in enumerate(unordered):
        raw = enumerate_candidates(block, context)
        candidate_cache[index] = _score_candidates(raw, context, scorer)

    placed, unplaced = _place_blocks(unordered, context, candidate_cache)

    rows = _placed_to_rows(placed)
    unplaced_report = []
    for block in unplaced:
        reason = "No conflict-free slot is available"
        if not context.qualified_instructors(block["course_code"]):
            reason = "No qualified instructor can teach this course"
        unplaced_report.append({
            "course_code": block["course_code"],
            "course_name": block["course_name"],
            "year_level": block["year_level"],
            "section": block["section"],
            "session_count": len(block["segments"]),
            "reason": reason,
        })

    return {
        "rows": rows,
        "report": {
            "total_blocks": len(blocks),
            "placed_blocks": len(placed),
            "unplaced": unplaced_report,
            "message": f"Placed {len(placed)} of {len(blocks)} scheduled session group(s).",
        },
        "ml": ml_meta,
    }


def _placed_to_rows(placed: List[dict]) -> List[dict]:
    rows: List[dict] = []
    for candidate in placed:
        for segment in candidate["segments"]:
            rows.append({
                "teacher_id": candidate.get("teacher_id"),
                "teacher_name": candidate["teacher_name"],
                "username": candidate.get("username", ""),
                "instructor_id": candidate.get("instructor_id"),
                "subject_code": candidate["course_code"],
                "subject_name": candidate["course_name"],
                "program": candidate["program"],
                "year_level": candidate["year_level"],
                "section": candidate["section"],
                "day": candidate["day"],
                "start_time": minutes_to_hhmm(segment["start"]),
                "end_time": minutes_to_hhmm(segment["end"]),
                "room": segment["room"],
                "status": "Draft",
                "session_type": segment["session_type"],
                "course_id": candidate.get("course_id"),
                "is_generated": 1,
            })
    return rows
