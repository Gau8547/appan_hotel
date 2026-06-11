from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "hotel_rent.db"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "hotel_rent.db"

# ✅ ADD THIS (IMPORTANT)
BACKUP_DIR = BASE_DIR / "backups"

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 10000))
DEFAULT_ADMIN = "admin"
DEFAULT_PASSWORD = os.environ.get("HOTEL_RENT_ADMIN_PASSWORD", "Admin@12345")
SESSION_HOURS = int(os.environ.get("HOTEL_RENT_SESSION_HOURS", "12"))


ROOM_FIELDS = {
    "roomNo": "room_no",
    "guestName": "guest_name",
    "phone": "phone",
    "bedroomSize": "bedroom_size",
    "rent": "rent",
    "availability": "availability",
    "checkInDate": "check_in_date",
    "checkInTime": "check_in_time",
    "durationMonths": "duration_months",
    "checkoutDate": "checkout_date",
    "checkoutTime": "checkout_time",
    "paymentStatus": "payment_status",
    "lastPaidDate": "last_paid_date",
}
# ONLY SHOWING MODIFIED PARTS CLEANLY INTEGRATED

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def restore_backup():
    latest = BACKUP_DIR / "latest.db"
    if not DB_PATH.exists() and latest.exists():
        import shutil
        shutil.copy(latest, DB_PATH)


def room_to_dict(row: sqlite3.Row) -> dict:
    due = next_due_date(row)
    due_date = parse_date(due)

    # 🔥 AUTO RENT CALCULATION
    check_in = parse_date(row["check_in_date"])
    today = date.today()

    pending_days = 0
    if check_in and row["availability"] != "Available":
        pending_days = max(1, (today - check_in).days)

    pending_amount = pending_days * float(row["rent"])

    pending = row["payment_status"] == "Pending" or bool(due_date and due_date <= today)
    checkout = checkout_due(row)

    return {
        "id": row["id"],
        "roomNo": row["room_no"],
        "guestName": row["guest_name"],
        "phone": row["phone"],
        "bedroomSize": row["bedroom_size"],
        "rent": row["rent"],
        "availability": row["availability"],
        "checkInDate": row["check_in_date"],
        "checkInTime": row["check_in_time"],
        "durationMonths": row["duration_months"],
        "checkoutDate": row["checkout_date"],
        "checkoutTime": row["checkout_time"],
        "paymentStatus": row["payment_status"],
        "lastPaidDate": row["last_paid_date"],
        "computed": {
            "nextDue": due,
            "pending": pending,
            "pendingAmount": pending_amount,
            "checkoutDue": checkout,
        },
    }


# 🔥 MODIFY create_room
def create_room(self) -> None:
    payload = get_json(self)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO rooms (
                room_no, guest_name, phone, bedroom_size, rent, availability,
                check_in_date, check_in_time, duration_months, checkout_date,
                checkout_time, payment_status, last_paid_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            room_values(payload),
        )
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (cursor.lastrowid,)).fetchone()

    # 🔥 AUTO BACKUP
    import shutil
    BACKUP_DIR.mkdir(exist_ok=True)
    shutil.copy(DB_PATH, BACKUP_DIR / "latest.db")

    return send_json(self, room_to_dict(row), HTTPStatus.CREATED)


# 🔥 MODIFY update_room
def update_room(self, path: str) -> None:
    room_id = path_room_id(path)
    payload = get_json(self)
    updates = []
    values = []
    for api_name, db_name in ROOM_FIELDS.items():
        if api_name in payload:
            updates.append(f"{db_name} = ?")
            values.append(clean_value(api_name, payload[api_name]))

    if not updates:
        return send_json(self, {"error": "No valid fields"}, HTTPStatus.BAD_REQUEST)

    values.append(room_id)

    with connect() as conn:
        conn.execute(
            f"UPDATE rooms SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            values,
        )
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()

    if not row:
        return send_json(self, {"error": "Room not found"}, HTTPStatus.NOT_FOUND)

    # 🔥 AUTO BACKUP
    import shutil
    BACKUP_DIR.mkdir(exist_ok=True)
    shutil.copy(DB_PATH, BACKUP_DIR / "latest.db")

    return send_json(self, room_to_dict(row))


# 🔥 FINAL MAIN BLOCK
if __name__ == "__main__":
    BACKUP_DIR.mkdir(exist_ok=True)
    restore_backup()
    init_db()

    server = ThreadingHTTPServer((HOST, PORT), HotelHandler)
    print(f"Server running on {HOST}:{PORT}")
    server.serve_forever()

