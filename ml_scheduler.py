
"""Machine-learning support for OptiSked AI.

This module ranks conflict-free schedule candidates using a small supervised
learning model built from published schedules already stored in the database.

Training design:
- Positive examples: previously published schedule records.
- Negative examples: deterministic alternative slots generated from those
  schedules that were not historically used.
- Model: RandomForestClassifier from scikit-learn.
- Output: probability that a candidate resembles historically used
  scheduling patterns.

The model is intentionally used *after* the hard constraint engine. It never
overrides mandatory scheduling rules.
"""

from collections import Counter
from copy import deepcopy
from typing import Dict, Iterable, List, Tuple


def _safe_minutes(value: str) -> int:
    try:
        h, m = map(int, str(value).split(":"))
        return h * 60 + m
    except Exception:
        return -1


def _year_num(year_level: str) -> int:
    m = "".join(ch for ch in str(year_level) if ch.isdigit())
    return int(m) if m else 0


def _day_num(day: str) -> int:
    return {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5}.get(str(day), -1)


def _room_type(room: str) -> str:
    room = (room or "").lower()
    if "computer lab" in room:
        return "computer_lab"
    if "lecture room" in room:
        return "lecture_room"
    return "other"


def _teacher_profile(candidate: dict, teachers_by_id: dict, teachers_by_name: dict, teachers_by_username: dict):
    teacher_id = str(candidate.get("teacher_id") or "").strip()
    if teacher_id and teacher_id in teachers_by_id:
        return teachers_by_id[teacher_id]

    name = str(candidate.get("teacher_name") or "").strip().lower()
    if name and name in teachers_by_name:
        return teachers_by_name[name]

    username = str(candidate.get("username") or "").strip().lower()
    if username and username in teachers_by_username:
        return teachers_by_username[username]
    return None


def _frequency_maps(schedules: List[dict]):
    day_freq = Counter(s.get("day", "") for s in schedules)
    room_freq = Counter(s.get("room", "") for s in schedules)
    start_freq = Counter(_safe_minutes(s.get("start_time", "")) for s in schedules)
    program_day_freq = Counter((s.get("program", ""), s.get("day", "")) for s in schedules)
    teacher_day_freq = Counter((str(s.get("teacher_name", "")).lower(), s.get("day", "")) for s in schedules)
    teacher_start_freq = Counter(
        (str(s.get("teacher_name", "")).lower(), _safe_minutes(s.get("start_time", "")))
        for s in schedules
    )
    return day_freq, room_freq, start_freq, program_day_freq, teacher_day_freq, teacher_start_freq


def feature_dict(candidate: dict, schedules: List[dict], teacher_profile=None, frequency_cache=None) -> dict:
    if frequency_cache is None:
        frequency_cache = _frequency_maps(schedules)
    day_freq, room_freq, start_freq, program_day_freq, teacher_day_freq, teacher_start_freq = frequency_cache

    day = str(candidate.get("day", ""))
    room = str(candidate.get("room", ""))
    teacher_name = str(candidate.get("teacher_name", ""))
    start = _safe_minutes(candidate.get("start_time", ""))
    end = _safe_minutes(candidate.get("end_time", ""))
    duration = max(0, end - start) if start >= 0 and end >= 0 else 0

    preferred_days = []
    available_from = "07:30"
    available_until = "17:00"
    if teacher_profile is not None:
        preferred_days = [
            x.strip() for x in str(teacher_profile["preferred_days"] or "").split(",") if x.strip()
        ]
        available_from = teacher_profile["available_from"] or available_from
        available_until = teacher_profile["available_until"] or available_until

    preferred_match = 1 if not preferred_days or day in preferred_days else 0
    within_availability = 1 if (
        start >= _safe_minutes(available_from)
        and end <= _safe_minutes(available_until)
    ) else 0

    # Session type / room-type agreement tells the ranker whether a lab
    # session is being placed in a computer laboratory and whether a lecture
    # is placed in a lecture room.
    session_type = str(candidate.get("session_type", "") or "")
    room_kind = _room_type(room)
    if session_type == "Lab":
        room_matches_type = 1 if room_kind == "computer_lab" else 0
    elif session_type == "Lecture":
        room_matches_type = 1 if room_kind == "lecture_room" else 0
    else:
        room_matches_type = 1

    # Use categorical strings for meaningful one-hot encoding and a few numeric
    # scheduling-pattern features.
    return {
        "day": day,
        "program": str(candidate.get("program", "")),
        "year_level": str(candidate.get("year_level", "")),
        "section": str(candidate.get("section", "")),
        "room_type": room_kind,
        "teacher": teacher_name.lower(),
        "room": room,
        "day_num": _day_num(day),
        "start_minutes": start,
        "duration_minutes": duration,
        "preferred_match": preferred_match,
        "within_availability": within_availability,
        "session_type": session_type,
        "room_matches_type": room_matches_type,
        "instructor_load": int(candidate.get("_instructor_load", 0) or 0),
        "nonpreferred_overlap": int(candidate.get("_nonpreferred_overlap", 0) or 0),
        "day_frequency": day_freq.get(day, 0),
        "room_frequency": room_freq.get(room, 0),
        "start_frequency": start_freq.get(start, 0),
        "program_day_frequency": program_day_freq.get((str(candidate.get("program", "")), day), 0),
        "teacher_day_frequency": teacher_day_freq.get((teacher_name.lower(), day), 0),
        "teacher_start_frequency": teacher_start_freq.get((teacher_name.lower(), start), 0),
    }


def _alternative_negatives(schedule: dict, all_schedules: List[dict]) -> List[dict]:
    negatives = []
    base = deepcopy(schedule)
    positive_keys = {
        (
            str(s.get("teacher_name", "")).strip().lower(),
            s.get("day"),
            s.get("start_time"),
            s.get("end_time"),
            str(s.get("room", "")).strip().lower(),
        )
        for s in all_schedules
    }

    def add_negative(candidate):
        key = (
            str(candidate.get("teacher_name", "")).strip().lower(),
            candidate.get("day"),
            candidate.get("start_time"),
            candidate.get("end_time"),
            str(candidate.get("room", "")).strip().lower(),
        )
        if key not in positive_keys:
            negatives.append(candidate)
    original_start = _safe_minutes(base.get("start_time", ""))
    original_end = _safe_minutes(base.get("end_time", ""))
    duration = max(30, original_end - original_start)

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    current_day = base.get("day")
    for day in days:
        if day != current_day:
            n = deepcopy(base)
            n["day"] = day
            add_negative(n)
            if len(negatives) >= 2:
                break

    # Nearby time alternatives teach the model that historically used time
    # bands are more representative of the institution's patterns.
    for shift in (30, -30, 60, -60):
        new_start = original_start + shift
        new_end = new_start + duration
        if 450 <= new_start and new_end <= 1260:
            n = deepcopy(base)
            n["start_time"] = f"{new_start // 60:02d}:{new_start % 60:02d}"
            n["end_time"] = f"{new_end // 60:02d}:{new_end % 60:02d}"
            add_negative(n)

    # Room alternatives provide another historical-pattern dimension.
    rooms = [s.get("room", "") for s in all_schedules if s.get("room")]
    seen = []
    for room in rooms:
        if room != base.get("room") and room not in seen:
            n = deepcopy(base)
            n["room"] = room
            add_negative(n)
            seen.append(room)
            if len(seen) >= 2:
                break

    # De-duplicate identical negatives.
    unique = []
    seen_keys = set()
    for n in negatives:
        key = (n.get("day"), n.get("start_time"), n.get("end_time"), n.get("room"))
        if key == (base.get("day"), base.get("start_time"), base.get("end_time"), base.get("room")):
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(n)
    return unique[:6]


def train_and_rank(candidates: List[dict], schedules: Iterable[dict], teachers: Iterable[dict] = ()):
    """Return ranked candidate dicts and ML metadata.

    The target is historical-pattern membership: published schedules are
    positive examples, and nearby alternative slots not historically used are
    negative examples. This is a small, explainable model suitable for the
    prototype and is retrained from the current verified schedule dataset.
    """
    schedules = [dict(s) for s in schedules]
    candidates = [dict(c) for c in candidates]
    teachers = [dict(t) for t in teachers]

    if not candidates:
        return [], {
            "active": False,
            "algorithm": "Random Forest Classifier",
            "training_schedules": len(schedules),
            "training_samples": 0,
            "message": "No candidates available for ML ranking.",
        }

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.feature_extraction import DictVectorizer
    except Exception as exc:
        # The application still works with the hard constraint engine if the
        # package has not yet been installed. Deployment requirements include it.
        return candidates, {
            "active": False,
            "algorithm": "Random Forest Classifier",
            "training_schedules": len(schedules),
            "training_samples": 0,
            "message": f"Scikit-learn is unavailable: {exc}",
        }

    teachers_by_id = {str(t.get("id")): t for t in teachers if t.get("id") is not None}
    teachers_by_name = {str(t.get("full_name", "")).strip().lower(): t for t in teachers}
    teachers_by_username = {str(t.get("username", "")).strip().lower(): t for t in teachers}
    freq = _frequency_maps(schedules)

    X_dicts = []
    y = []

    # Positive examples = observed/published schedules.
    for s in schedules:
        profile = _teacher_profile(s, teachers_by_id, teachers_by_name, teachers_by_username)
        X_dicts.append(feature_dict(s, schedules, profile, freq))
        y.append(1)

        # Negative examples = nearby alternatives that were not historically used.
        for n in _alternative_negatives(s, schedules):
            profile = _teacher_profile(n, teachers_by_id, teachers_by_name, teachers_by_username)
            X_dicts.append(feature_dict(n, schedules, profile, freq))
            y.append(0)

    # Make sure a tiny first dataset still has both classes.
    if not X_dicts or len(set(y)) < 2:
        return candidates, {
            "active": False,
            "algorithm": "Random Forest Classifier",
            "training_schedules": len(schedules),
            "training_samples": len(X_dicts),
            "message": "More published scheduling examples are needed to train the ML ranker.",
        }

    vectorizer = DictVectorizer(sparse=False)
    X = vectorizer.fit_transform(X_dicts)

    model = RandomForestClassifier(
        n_estimators=120,
        max_depth=8,
        random_state=42,
        class_weight="balanced",
    )
    model.fit(X, y)

    ranked = []
    for candidate in candidates:
        profile = _teacher_profile(candidate, teachers_by_id, teachers_by_name, teachers_by_username)
        features = feature_dict(candidate, schedules, profile, freq)
        matrix = vectorizer.transform([features])
        probability = float(model.predict_proba(matrix)[0][1])

        # Small proximity component keeps recommendations from jumping far
        # away when the learned scores are otherwise very close.
        original_start = _safe_minutes(candidate.get("_original_start", candidate.get("start_time", "")))
        original_day = _day_num(candidate.get("_original_day", candidate.get("day", "")))
        current_start = _safe_minutes(candidate.get("start_time", ""))
        current_day = _day_num(candidate.get("day", ""))
        distance = abs(current_start - original_start) + (abs(current_day - original_day) * 180)
        proximity = max(0.0, 1.0 - min(distance, 900) / 900.0)

        combined = (0.85 * probability) + (0.15 * proximity)
        item = dict(candidate)
        item["ml_score"] = round(combined * 100)
        item["ml_probability"] = round(probability * 100)
        item.pop("_original_start", None)
        item.pop("_original_day", None)
        item["reason"] = (
            f"ML suitability {item['ml_score']}% based on historical scheduling patterns; "
            "the slot also passed all hard scheduling constraints."
        )
        ranked.append(item)

    ranked.sort(key=lambda x: (-x["ml_score"], x.get("day", ""), x.get("start_time", ""), x.get("room", "")))

    return ranked, {
        "active": True,
        "algorithm": "Random Forest Classifier",
        "training_schedules": len(schedules),
        "training_samples": len(X_dicts),
        "message": "ML ranking is active. Recommendations are ranked from historically published scheduling patterns.",
    }


def build_scorer(schedules: Iterable[dict], teachers: Iterable[dict] = ()):
    """Train the historical-pattern ranker once and return a scoring callable.

    The AI schedule generator evaluates thousands of candidate placements.
    Retraining the Random Forest for each one would be wasteful, so this helper
    fits the model a single time and returns:

        (score_fn, meta)

    where ``score_fn(candidates)`` returns the same candidate dicts annotated
    with ``ml_probability`` (0-100). When the model cannot be trained (no
    history or scikit-learn unavailable) it returns ``(None, meta)`` so the
    caller can fall back to a deterministic heuristic.
    """
    schedules = [dict(s) for s in schedules]
    teachers = [dict(t) for t in teachers]

    meta = {
        "active": False,
        "algorithm": "Random Forest Classifier",
        "training_schedules": len(schedules),
        "training_samples": 0,
        "message": "",
    }

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.feature_extraction import DictVectorizer
    except Exception as exc:
        meta["message"] = f"Scikit-learn is unavailable: {exc}"
        return None, meta

    teachers_by_id = {str(t.get("id")): t for t in teachers if t.get("id") is not None}
    teachers_by_name = {str(t.get("full_name", "")).strip().lower(): t for t in teachers}
    teachers_by_username = {str(t.get("username", "")).strip().lower(): t for t in teachers}
    freq = _frequency_maps(schedules)

    X_dicts = []
    y = []
    for s in schedules:
        profile = _teacher_profile(s, teachers_by_id, teachers_by_name, teachers_by_username)
        X_dicts.append(feature_dict(s, schedules, profile, freq))
        y.append(1)
        for n in _alternative_negatives(s, schedules):
            profile = _teacher_profile(n, teachers_by_id, teachers_by_name, teachers_by_username)
            X_dicts.append(feature_dict(n, schedules, profile, freq))
            y.append(0)

    meta["training_samples"] = len(X_dicts)
    if not X_dicts or len(set(y)) < 2:
        meta["message"] = "More published scheduling examples are needed to train the ML ranker."
        return None, meta

    vectorizer = DictVectorizer(sparse=False)
    X = vectorizer.fit_transform(X_dicts)
    model = RandomForestClassifier(
        n_estimators=120,
        max_depth=8,
        random_state=42,
        class_weight="balanced",
    )
    model.fit(X, y)

    def score_fn(candidates):
        if not candidates:
            return []
        features = []
        for candidate in candidates:
            profile = _teacher_profile(candidate, teachers_by_id, teachers_by_name, teachers_by_username)
            features.append(feature_dict(candidate, schedules, profile, freq))
        matrix = vectorizer.transform(features)
        probabilities = model.predict_proba(matrix)
        scored = []
        for candidate, row in zip(candidates, probabilities):
            item = dict(candidate)
            item["ml_probability"] = round(float(row[1]) * 100)
            scored.append(item)
        return scored

    meta.update({
        "active": True,
        "message": "ML placement is active. Sessions are ranked from historically published scheduling patterns.",
    })
    return score_fn, meta
