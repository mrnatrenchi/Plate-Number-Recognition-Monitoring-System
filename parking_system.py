import re
import sqlite3
import hashlib
import secrets
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

DATABASE_PATH = Path("results") / "smart_parking.db"
VERIFY_REQUIRED_COUNT = 3
DUPLICATE_COOLDOWN_SECONDS = 20
PENDING_MAX_AGE_MINUTES = 2
CAR_RATE_PER_HOUR = 10
MOTOR_RATE_PER_HOUR = 5
CAR_MAX_FEE = 80
MOTOR_MAX_FEE = 40
_recent_detections = {}


def _connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS parking_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_number TEXT NOT NULL,
                vehicle_type TEXT NOT NULL,
                guard_name TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                fee_amount INTEGER NOT NULL DEFAULT 0,
                duration_minutes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guard_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guard_name TEXT NOT NULL,
                shift_start TEXT NOT NULL,
                shift_end TEXT,
                status TEXT NOT NULL,
                notes TEXT,
                expected_revenue INTEGER DEFAULT 0,
                actual_cash INTEGER DEFAULT 0,
                flagged INTEGER DEFAULT 0,
                end_notes TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guard_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guard_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'guard',
                profile_picture TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_number TEXT NOT NULL,
                vehicle_type TEXT NOT NULL,
                action TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guard_name TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                notes TEXT NOT NULL,
                reported_at TEXT NOT NULL
            )
            """
        )
        
        event_columns = [row["name"] for row in connection.execute("PRAGMA table_info(parking_events)").fetchall()]
        if "fee_amount" not in event_columns:
            connection.execute("ALTER TABLE parking_events ADD COLUMN fee_amount INTEGER NOT NULL DEFAULT 0")
        if "duration_minutes" not in event_columns:
            connection.execute("ALTER TABLE parking_events ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 0")
            
        account_columns = [row["name"] for row in connection.execute("PRAGMA table_info(guard_accounts)").fetchall()]
        if "role" not in account_columns:
            connection.execute("ALTER TABLE guard_accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'guard'")
        if "profile_picture" not in account_columns:
            connection.execute("ALTER TABLE guard_accounts ADD COLUMN profile_picture TEXT")
            
        shift_columns = [row["name"] for row in connection.execute("PRAGMA table_info(guard_shifts)").fetchall()]
        if "notes" not in shift_columns:
            connection.execute("ALTER TABLE guard_shifts ADD COLUMN notes TEXT")
        if "expected_revenue" not in shift_columns:
            connection.execute("ALTER TABLE guard_shifts ADD COLUMN expected_revenue INTEGER DEFAULT 0")
            connection.execute("ALTER TABLE guard_shifts ADD COLUMN actual_cash INTEGER DEFAULT 0")
            connection.execute("ALTER TABLE guard_shifts ADD COLUMN flagged INTEGER DEFAULT 0")
        if "end_notes" not in shift_columns:
            connection.execute("ALTER TABLE guard_shifts ADD COLUMN end_notes TEXT")
        connection.commit()


def normalize_plate(plate_number):
    return re.sub(r"[^A-Z0-9]", "", (plate_number or "").upper())

def _hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}:{digest}"

def _verify_password(password, password_hash):
    salt, digest = password_hash.split(":", 1)
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest() == digest

def create_guard_account(guard_name, username, password, role="guard"):
    init_db()
    guard_name = (guard_name or "").strip()
    username = (username or "").strip().lower()
    password = password or ""
    role = role if role in {"admin", "guard"} else "guard"
    if not guard_name or not username or not password:
        raise ValueError("Name, username, and password are required")
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute(
            "INSERT INTO guard_accounts (guard_name, username, password_hash, role, profile_picture, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guard_name, username, _hash_password(password), role, None, now),
        )
        connection.commit()
    return authenticate_guard(username, password)

def authenticate_guard(username, password):
    init_db()
    username = (username or "").strip().lower()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM guard_accounts WHERE username = ?", (username,)).fetchone()
    if row and _verify_password(password or "", row["password_hash"]):
        return {
            "id": row["id"],
            "guard_name": row["guard_name"],
            "username": row["username"],
            "role": row["role"],
            "profile_picture": row["profile_picture"],
        }
    return None

def get_guard_account(account_id):
    init_db()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM guard_accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None

def get_guard_accounts():
    init_db()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM guard_accounts ORDER BY created_at DESC, id DESC").fetchall()
    return [dict(row) for row in rows]

def update_guard_account(account_id, guard_name, username, password=None, role=None, profile_picture=None):
    init_db()
    guard_name = (guard_name or "").strip()
    username = (username or "").strip().lower()
    role = role if role in {"admin", "guard"} else None
    if not guard_name or not username:
        raise ValueError("Name and username are required")
    with _connect() as connection:
        current = connection.execute("SELECT * FROM guard_accounts WHERE id = ?", (account_id,)).fetchone()
        if not current:
            return None
        if password:
            password_hash = _hash_password(password)
            connection.execute(
                "UPDATE guard_accounts SET guard_name = ?, username = ?, password_hash = ?, role = COALESCE(?, role), profile_picture = COALESCE(?, profile_picture) WHERE id = ?",
                (guard_name, username, password_hash, role, profile_picture, account_id),
            )
        else:
            connection.execute(
                "UPDATE guard_accounts SET guard_name = ?, username = ?, role = COALESCE(?, role), profile_picture = COALESCE(?, profile_picture) WHERE id = ?",
                (guard_name, username, role, profile_picture, account_id),
            )
        connection.commit()
    return get_guard_account(account_id)

def delete_guard_account(account_id):
    init_db()
    with _connect() as connection:
        connection.execute("DELETE FROM guard_accounts WHERE id = ?", (account_id,))
        connection.commit()

def calculate_fee(vehicle_type, duration_minutes):
    normalized_type = (vehicle_type or "").strip().lower()
    duration_minutes = max(int(duration_minutes or 0), 1)
    hours = (duration_minutes + 59) // 60
    if normalized_type in {"mc", "motor", "motorcycle"}:
        return min(hours * MOTOR_RATE_PER_HOUR, MOTOR_MAX_FEE)
    return min(hours * CAR_RATE_PER_HOUR, CAR_MAX_FEE)

def set_guard_on_duty(guard_name, notes=""):
    init_db()
    guard_name = (guard_name or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as connection:
        active = connection.execute("SELECT * FROM guard_shifts WHERE status = 'on-duty' ORDER BY shift_start DESC LIMIT 1").fetchone()
        if active and active["guard_name"] == guard_name:
            return dict(active)
        connection.execute("UPDATE guard_shifts SET shift_end = ?, status = 'off-duty' WHERE status = 'on-duty'", (now,))
        connection.execute(
            "INSERT INTO guard_shifts (guard_name, shift_start, status, notes) VALUES (?, ?, 'on-duty', ?)",
            (guard_name, now, notes)
        )
        connection.commit()
    return get_guard_on_duty()

def get_shift_revenue(guard_name, shift_start):
    init_db()
    with _connect() as connection:
        rev = connection.execute(
            "SELECT COALESCE(SUM(fee_amount), 0) as total FROM parking_events WHERE guard_name = ? AND status = 'exited' AND exit_time >= ?",
            (guard_name, shift_start)
        ).fetchone()["total"]
    return rev

def end_guard_shift(guard_name, actual_cash, end_notes=""):
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    actual_cash = int(actual_cash or 0)
    
    with _connect() as connection:
        shift = connection.execute("SELECT * FROM guard_shifts WHERE status = 'on-duty' AND guard_name = ?", (guard_name,)).fetchone()
        if not shift:
            return None
            
        expected_revenue = connection.execute(
            "SELECT COALESCE(SUM(fee_amount), 0) as total FROM parking_events WHERE guard_name = ? AND status = 'exited' AND exit_time >= ?",
            (guard_name, shift["shift_start"])
        ).fetchone()["total"]
        
        # 0 = OK, 1 = Flagged, 2 = Settled
        flagged = 1 if expected_revenue != actual_cash else 0
        
        connection.execute(
            "UPDATE guard_shifts SET shift_end = ?, status = 'off-duty', expected_revenue = ?, actual_cash = ?, flagged = ?, end_notes = ? WHERE id = ?", 
            (now, expected_revenue, actual_cash, flagged, end_notes, shift["id"])
        )
        connection.commit()
        return {
            "shift_end": now,
            "flagged": flagged,
            "expected_revenue": expected_revenue,
            "actual_cash": actual_cash
        }

def settle_guard_shift(shift_id):
    init_db()
    with _connect() as connection:
        # Change flagged status from 1 (Flagged) to 2 (Settled)
        connection.execute("UPDATE guard_shifts SET flagged = 2 WHERE id = ?", (shift_id,))
        connection.commit()

def report_incident(guard_name, incident_type, notes):
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute(
            "INSERT INTO incidents (guard_name, incident_type, notes, reported_at) VALUES (?, ?, ?, ?)",
            (guard_name, incident_type, notes, now)
        )
        connection.commit()

def get_guard_on_duty(guard_name=None):
    init_db()
    with _connect() as connection:
        if guard_name:
            row = connection.execute("SELECT * FROM guard_shifts WHERE status = 'on-duty' AND guard_name = ? ORDER BY shift_start DESC LIMIT 1", (guard_name,)).fetchone()
        else:
            row = connection.execute("SELECT * FROM guard_shifts WHERE status = 'on-duty' ORDER BY shift_start DESC LIMIT 1").fetchone()
    return dict(row) if row else None

def _current_guard_name():
    guard = get_guard_on_duty()
    return guard["guard_name"] if guard else "No guard assigned"

def _active_event_for_plate(connection, plate_number):
    return connection.execute("SELECT * FROM parking_events WHERE plate_number = ? AND status = 'inside' ORDER BY entry_time DESC LIMIT 1", (plate_number,)).fetchone()

def create_pending_detection(plate_number, vehicle_type):
    init_db()
    plate_number = normalize_plate(plate_number)
    vehicle_type = vehicle_type or "Unknown"
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as connection:
        active = _active_event_for_plate(connection, plate_number)
        action = "exit" if active else "entry"
        existing = connection.execute(
            "SELECT * FROM pending_detections WHERE plate_number = ? AND action = ? AND status = 'pending' ORDER BY detected_at DESC LIMIT 1",
            (plate_number, action),
        ).fetchone()
        if existing:
            return {"created": False, "reason": "already_pending", "pending_id": existing["id"], "action": action}
        cursor = connection.execute(
            "INSERT INTO pending_detections (plate_number, vehicle_type, action, detected_at, status) VALUES (?, ?, ?, ?, 'pending')",
            (plate_number, vehicle_type, action, now),
        )
        connection.commit()
    return {"created": True, "pending_id": cursor.lastrowid, "plate_number": plate_number, "vehicle_type": vehicle_type, "action": action, "detected_at": now}

def verify_and_record_detection(plate_number, vehicle_type, guard_name=None):
    plate_number = normalize_plate(plate_number)
    if not plate_number: return {"verified": False, "reason": "empty_plate"}
    now = datetime.now()
    record = _recent_detections.setdefault(plate_number, {"items": deque(maxlen=VERIFY_REQUIRED_COUNT), "last_saved": None})
    record["items"].append(now)
    if len(record["items"]) < VERIFY_REQUIRED_COUNT: return {"verified": False, "count": len(record["items"]), "required": VERIFY_REQUIRED_COUNT}
    if record["last_saved"] and now - record["last_saved"] < timedelta(seconds=DUPLICATE_COOLDOWN_SECONDS): return {"verified": False, "reason": "cooldown", "count": len(record["items"]), "required": VERIFY_REQUIRED_COUNT}
    record["last_saved"] = now
    return {"verified": True, **create_pending_detection(plate_number, vehicle_type)}

def get_pending_detections():
    init_db()
    cutoff = (datetime.now() - timedelta(minutes=PENDING_MAX_AGE_MINUTES)).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("UPDATE pending_detections SET status = 'dismissed' WHERE status = 'pending' AND detected_at < ?", (cutoff,))
        connection.commit()
        return [dict(row) for row in connection.execute("SELECT * FROM pending_detections WHERE status = 'pending' ORDER BY detected_at ASC").fetchall()]

def confirm_pending_detection(pending_id, confirmed_plate, guard_name):
    init_db()
    confirmed_plate = normalize_plate(confirmed_plate)
    guard_name = guard_name or _current_guard_name()
    now = datetime.now()
    now_text = now.isoformat(timespec="seconds")
    with _connect() as connection:
        pending = connection.execute("SELECT * FROM pending_detections WHERE id = ? AND status = 'pending'", (pending_id,)).fetchone()
        active = _active_event_for_plate(connection, confirmed_plate)
        if active:
            entry_time = datetime.fromisoformat(active["entry_time"])
            duration_minutes = max(int((now - entry_time).total_seconds() // 60), 1)
            fee_amount = calculate_fee(active["vehicle_type"], duration_minutes)
            connection.execute(
                "UPDATE parking_events SET exit_time = ?, fee_amount = ?, duration_minutes = ?, status = 'exited' WHERE id = ?",
                (now_text, fee_amount, duration_minutes, active["id"]),
            )
            action = "exit"
            vehicle_type = active["vehicle_type"]
        else:
            vehicle_type = pending["vehicle_type"]
            fee_amount = 0
            duration_minutes = 0
            connection.execute(
                "INSERT INTO parking_events (plate_number, vehicle_type, guard_name, entry_time, fee_amount, duration_minutes, status) VALUES (?, ?, ?, ?, 0, 0, 'inside')",
                (confirmed_plate, vehicle_type, guard_name, now_text),
            )
            action = "entry"
        connection.execute("UPDATE pending_detections SET status = 'confirmed' WHERE id = ?", (pending_id,))
        connection.commit()
    return {"action": action, "plate_number": confirmed_plate, "vehicle_type": vehicle_type, "guard_name": guard_name, "fee_amount": fee_amount, "duration_minutes": duration_minutes, "timestamp": now_text}

def reject_pending_detection(pending_id):
    init_db()
    with _connect() as connection:
        connection.execute("UPDATE pending_detections SET status = 'rejected' WHERE id = ?", (pending_id,))
        connection.commit()

def dismiss_pending_detection(pending_id):
    init_db()
    with _connect() as connection:
        connection.execute("UPDATE pending_detections SET status = 'dismissed' WHERE id = ?", (pending_id,))
        connection.commit()

def manual_timeout_parking_event(event_id, guard_name=None):
    init_db()
    now = datetime.now()
    now_text = now.isoformat(timespec="seconds")
    with _connect() as connection:
        event = connection.execute("SELECT * FROM parking_events WHERE id = ? AND status = 'inside'", (event_id,)).fetchone()
        if not event:
            return None
        entry_time = datetime.fromisoformat(event["entry_time"])
        duration_minutes = max(int((now - entry_time).total_seconds() // 60), 1)
        fee_amount = calculate_fee(event["vehicle_type"], duration_minutes)
        connection.execute(
            "UPDATE parking_events SET exit_time = ?, fee_amount = ?, duration_minutes = ?, status = 'exited' WHERE id = ?",
            (now_text, fee_amount, duration_minutes, event_id),
        )
        connection.commit()
    return {
        "action": "exit",
        "plate_number": event["plate_number"],
        "vehicle_type": event["vehicle_type"],
        "guard_name": guard_name or event["guard_name"],
        "fee_amount": fee_amount,
        "duration_minutes": duration_minutes,
        "timestamp": now_text,
    }

def delete_parking_event(event_id):
    init_db()
    with _connect() as connection:
        connection.execute("DELETE FROM parking_events WHERE id = ?", (event_id,))
        connection.commit()

def get_dashboard_data():
    init_db()
    today = datetime.now().date().isoformat()
    with _connect() as connection:
        inside_count = connection.execute("SELECT COUNT(*) AS total FROM parking_events WHERE status = 'inside'").fetchone()["total"]
        vehicle_counts_rows = connection.execute("SELECT LOWER(vehicle_type) AS vehicle_type, COUNT(*) AS total FROM parking_events WHERE status = 'inside' GROUP BY LOWER(vehicle_type)").fetchall()
        history = connection.execute("SELECT * FROM parking_events WHERE status = 'exited' ORDER BY exit_time DESC LIMIT 100").fetchall()
        active = connection.execute("SELECT * FROM parking_events WHERE status = 'inside' ORDER BY entry_time DESC").fetchall()
        rejected = connection.execute("SELECT * FROM pending_detections WHERE status = 'rejected' ORDER BY detected_at DESC LIMIT 50").fetchall()
        guards = connection.execute("SELECT * FROM guard_shifts ORDER BY shift_start DESC LIMIT 50").fetchall()
        accounts = connection.execute("SELECT * FROM guard_accounts ORDER BY created_at DESC, id DESC").fetchall()
        incidents = connection.execute("SELECT * FROM incidents ORDER BY reported_at DESC LIMIT 100").fetchall()
        total_sales_today = connection.execute(
            "SELECT COALESCE(SUM(fee_amount), 0) AS total FROM parking_events WHERE status = 'exited' AND date(exit_time) = ?", (today,)
        ).fetchone()["total"]
        guard_shift_sales = connection.execute(
            """
            SELECT gs.id, gs.guard_name, gs.shift_start, gs.shift_end, gs.status, gs.notes, 
                   gs.expected_revenue, gs.actual_cash, gs.flagged, gs.end_notes,
                   COALESCE(SUM(pe.fee_amount), 0) AS live_sales,
                   COUNT(pe.id) AS vehicle_count
            FROM guard_shifts gs
            LEFT JOIN parking_events pe
              ON pe.guard_name = gs.guard_name
             AND pe.status = 'exited'
             AND pe.exit_time >= gs.shift_start
             AND (gs.shift_end IS NULL OR pe.exit_time <= gs.shift_end)
            GROUP BY gs.id
            ORDER BY gs.shift_start DESC
            LIMIT 50
            """
        ).fetchall()
        vehicle_counts_map = {row["vehicle_type"]: row["total"] for row in vehicle_counts_rows}
        vehicle_counts = [
            {"vehicle_type": "car", "total": vehicle_counts_map.get("car", 0)},
            {"vehicle_type": "mc", "total": vehicle_counts_map.get("mc", 0)},
        ]
        for vehicle_type, total in vehicle_counts_map.items():
            if vehicle_type not in {"car", "mc"}:
                vehicle_counts.append({"vehicle_type": vehicle_type, "total": total})
    return {
        "inside_count": inside_count,
        "total_sales_today": total_sales_today,
        "vehicle_counts": vehicle_counts,
        "history_events": [dict(row) for row in history],
        "active_vehicles": [dict(row) for row in active],
        "rejected_detections": [dict(row) for row in rejected],
        "guard_on_duty": get_guard_on_duty(),
        "guard_shifts": [dict(row) for row in guards],
        "guard_shift_sales": [dict(row) for row in guard_shift_sales],
        "guard_accounts": [dict(row) for row in accounts],
        "pending_detections": get_pending_detections(),
        "incidents": [dict(row) for row in incidents],
    }

init_db()