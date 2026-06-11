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
BACKUP_DIR = BASE_DIR / "backups"
HOST = os.environ.get("HOTEL_RENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOTEL_RENT_PORT", "8765"))
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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, digest_hex)


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_no TEXT NOT NULL DEFAULT '',
                guest_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                bedroom_size TEXT NOT NULL DEFAULT 'Studio',
                rent REAL NOT NULL DEFAULT 0,
                availability TEXT NOT NULL DEFAULT 'Available',
                check_in_date TEXT NOT NULL DEFAULT '',
                check_in_time TEXT NOT NULL DEFAULT '',
                duration_months INTEGER NOT NULL DEFAULT 1,
                checkout_date TEXT NOT NULL DEFAULT '',
                checkout_time TEXT NOT NULL DEFAULT '12:00',
                payment_status TEXT NOT NULL DEFAULT 'Paid',
                last_paid_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE CASCADE
            );
            """
        )

        admin_count = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        if admin_count == 0:
            salt, digest = hash_password(DEFAULT_PASSWORD)
            conn.execute(
                "INSERT INTO admins (username, password_salt, password_hash) VALUES (?, ?, ?)",
                (DEFAULT_ADMIN, salt, digest),
            )

        room_count = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        if room_count == 0:
            conn.executemany(
                """
                INSERT INTO rooms (
                    room_no, guest_name, phone, bedroom_size, rent, availability,
                    check_in_date, check_in_time, duration_months, checkout_date,
                    checkout_time, payment_status, last_paid_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "101",
                        "Mohammed Ali",
                        "+971 50 123 4567",
                        "Studio",
                        2800,
                        "Occupied",
                        "2026-05-20",
                        "14:00",
                        1,
                        "2026-06-20",
                        "12:00",
                        "Pending",
                        "",
                    ),
                    (
                        "102",
                        "",
                        "",
                        "One Bedroom",
                        3200,
                        "Available",
                        "",
                        "",
                        1,
                        "",
                        "12:00",
                        "Paid",
                        "",
                    ),
                    (
                        "201",
                        "Ravi Kumar",
                        "+971 55 222 8899",
                        "Two Bedroom",
                        4600,
                        "Occupied",
                        "2026-04-08",
                        "15:00",
                        3,
                        "2026-07-08",
                        "12:00",
                        "Paid",
                        "2026-06-08",
                    ),
                ],
            )


def parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def add_months(source: date, months: int) -> date:
    month = source.month - 1 + max(months, 1)
    year = source.year + month // 12
    month = month % 12 + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(source.day, month_lengths[month - 1])
    return date(year, month, day)


def next_due_date(row: sqlite3.Row) -> str:
    start = parse_date(row["check_in_date"])
    if not start or row["availability"] == "Available":
        return ""
    last_paid = parse_date(row["last_paid_date"])
    due = start
    for offset in range(60):
        due = add_months(start, offset)
        if not last_paid or due > last_paid:
            break
    return due.isoformat()


def checkout_due(row: sqlite3.Row) -> bool:
    if row["availability"] == "Available" or not row["checkout_date"]:
        return False
    checkout_time = row["checkout_time"] or "12:00"
    try:
        due_at = datetime.strptime(f"{row['checkout_date']} {checkout_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        due_at = datetime.strptime(f"{row['checkout_date']} 12:00", "%Y-%m-%d %H:%M")
    return datetime.now() >= due_at


def room_to_dict(row: sqlite3.Row) -> dict:
    due = next_due_date(row)
    due_date = parse_date(due)
    pending = row["payment_status"] == "Pending" or bool(due_date and due_date <= date.today())
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
            "checkoutDue": checkout,
        },
    }


def get_json(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def send_json(handler: SimpleHTTPRequestHandler, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def current_admin_id(handler: SimpleHTTPRequestHandler) -> int | None:
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    if not token:
        return None
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        session = conn.execute(
            "SELECT admin_id FROM sessions WHERE token_hash = ? AND expires_at >= ?",
            (token_digest(token), now_iso()),
        ).fetchone()
    return int(session["admin_id"]) if session else None


def authorized(handler: SimpleHTTPRequestHandler) -> bool:
    return current_admin_id(handler) is not None


class HotelHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/hotel-rent-admin.html"
            return super().do_GET()
        if parsed.path == "/api/rooms":
            if not authorized(self):
                return send_json(self, {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
            with connect() as conn:
                rows = conn.execute("SELECT * FROM rooms ORDER BY room_no, id").fetchall()
            return send_json(self, [room_to_dict(row) for row in rows])
        if parsed.path == "/api/export":
            if not authorized(self):
                return send_json(self, {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return self.export_csv()
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            return self.login()
        if not authorized(self):
            return send_json(self, {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
        if parsed.path == "/api/logout":
            return self.logout()
        if parsed.path == "/api/change-password":
            return self.change_password()
        if parsed.path == "/api/backup":
            return self.backup_database()
        if parsed.path == "/api/rooms":
            return self.create_room()
        if parsed.path.endswith("/paid"):
            return self.set_payment(parsed.path, "Paid")
        if parsed.path.endswith("/pending"):
            return self.set_payment(parsed.path, "Pending")
        return send_json(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not authorized(self):
            return send_json(self, {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
        if parsed.path.startswith("/api/rooms/"):
            return self.update_room(parsed.path)
        return send_json(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not authorized(self):
            return send_json(self, {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
        if parsed.path.startswith("/api/rooms/"):
            room_id = path_room_id(parsed.path)
            with connect() as conn:
                conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
            return send_json(self, {"ok": True})
        return send_json(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def login(self) -> None:
        payload = get_json(self)
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        with connect() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if not admin or not verify_password(password, admin["password_salt"], admin["password_hash"]):
            return send_json(self, {"error": "Invalid username or password"}, HTTPStatus.UNAUTHORIZED)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now() + timedelta(hours=SESSION_HOURS)
        with connect() as conn:
            conn.execute(
                "INSERT INTO sessions (admin_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (admin["id"], token_digest(token), expires_at.replace(microsecond=0).isoformat(sep=" ")),
            )
        return send_json(self, {"token": token, "username": username, "expiresAt": expires_at.isoformat()})

    def logout(self) -> None:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if token:
            with connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
        return send_json(self, {"ok": True})

    def change_password(self) -> None:
        admin_id = current_admin_id(self)
        if not admin_id:
            return send_json(self, {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
        payload = get_json(self)
        current_password = str(payload.get("currentPassword", ""))
        new_password = str(payload.get("newPassword", ""))
        if len(new_password) < 10:
            return send_json(self, {"error": "New password must be at least 10 characters."}, HTTPStatus.BAD_REQUEST)
        with connect() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if not admin or not verify_password(current_password, admin["password_salt"], admin["password_hash"]):
                return send_json(self, {"error": "Current password is wrong."}, HTTPStatus.UNAUTHORIZED)
            salt, digest = hash_password(new_password)
            conn.execute(
                "UPDATE admins SET password_salt = ?, password_hash = ? WHERE id = ?",
                (salt, digest, admin_id),
            )
            conn.execute("DELETE FROM sessions WHERE admin_id = ? AND token_hash != ?", (admin_id, token_digest(self.headers.get("Authorization", "").removeprefix("Bearer ").strip())))
        return send_json(self, {"ok": True})

    def backup_database(self) -> None:
        BACKUP_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = BACKUP_DIR / f"hotel_rent_backup_{stamp}.db"
        with connect() as source:
            with sqlite3.connect(backup_path) as target:
                source.backup(target)
        return send_json(self, {"ok": True, "file": str(backup_path.name), "folder": str(BACKUP_DIR)})

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
        return send_json(self, room_to_dict(row), HTTPStatus.CREATED)

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
        return send_json(self, room_to_dict(row))

    def set_payment(self, path: str, status: str) -> None:
        room_id = path_room_id(path.replace("/paid", "").replace("/pending", ""))
        last_paid = date.today().isoformat() if status == "Paid" else ""
        with connect() as conn:
            conn.execute(
                "UPDATE rooms SET payment_status = ?, last_paid_date = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, last_paid, room_id),
            )
            row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if not row:
            return send_json(self, {"error": "Room not found"}, HTTPStatus.NOT_FOUND)
        return send_json(self, room_to_dict(row))

    def export_csv(self) -> None:
        with connect() as conn:
            rows = conn.execute("SELECT * FROM rooms ORDER BY room_no, id").fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        headers = [
            "Room No",
            "Guest Name",
            "Phone",
            "Bedroom Size",
            "Monthly Rent AED",
            "Availability",
            "Check In Date",
            "Check In Time",
            "Duration Months",
            "Checkout Date",
            "Checkout Time",
            "Payment Status",
            "Last Paid Date",
            "Next Due",
            "Rent Pending",
            "Checkout Due",
        ]
        writer.writerow(headers)
        for row in rows:
            room = room_to_dict(row)
            writer.writerow(
                [
                    room["roomNo"],
                    room["guestName"],
                    room["phone"],
                    room["bedroomSize"],
                    room["rent"],
                    room["availability"],
                    room["checkInDate"],
                    room["checkInTime"],
                    room["durationMonths"],
                    room["checkoutDate"],
                    room["checkoutTime"],
                    room["paymentStatus"],
                    room["lastPaidDate"],
                    room["computed"]["nextDue"],
                    "Yes" if room["computed"]["pending"] else "No",
                    "Yes" if room["computed"]["checkoutDue"] else "No",
                ]
            )

        body = output.getvalue().encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=hotel-rent-export.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def path_room_id(path: str) -> int:
    try:
        return int(path.strip("/").split("/")[2])
    except (IndexError, ValueError):
        return 0


def clean_value(field: str, value) -> str | int | float:
    if field == "rent":
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0
    if field == "durationMonths":
        try:
            return max(1, int(value or 1))
        except (TypeError, ValueError):
            return 1
    return str(value or "").strip()


def room_values(payload: dict) -> tuple:
    return (
        clean_value("roomNo", payload.get("roomNo", "")),
        clean_value("guestName", payload.get("guestName", "")),
        clean_value("phone", payload.get("phone", "")),
        clean_value("bedroomSize", payload.get("bedroomSize", "Studio")),
        clean_value("rent", payload.get("rent", 0)),
        clean_value("availability", payload.get("availability", "Available")),
        clean_value("checkInDate", payload.get("checkInDate", "")),
        clean_value("checkInTime", payload.get("checkInTime", "")),
        clean_value("durationMonths", payload.get("durationMonths", 1)),
        clean_value("checkoutDate", payload.get("checkoutDate", "")),
        clean_value("checkoutTime", payload.get("checkoutTime", "12:00")),
        clean_value("paymentStatus", payload.get("paymentStatus", "Paid")),
        clean_value("lastPaidDate", payload.get("lastPaidDate", "")),
    )


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), HotelHandler)
    print(f"Hotel Rent Admin running at http://{HOST}:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    print(f"Default login: {DEFAULT_ADMIN} / {DEFAULT_PASSWORD}")
    server.serve_forever()
