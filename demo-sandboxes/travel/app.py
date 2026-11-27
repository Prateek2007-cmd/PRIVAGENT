"""Demo Sandbox — Travel booking (fictional data only, no real payment).

FastAPI + SQLite. Serves its own static frontend (single origin).
Payment is DISABLED: checkout always returns 402 payment_disabled and the
booking stays in status PREPARED. Synthetic demo account only.
Run:  uvicorn app:app --host 127.0.0.1 --port 8124
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "travel.db")
OPERATOR_TOKEN = "demo-operator"
DEMO_USER = {"username": "demo", "name": "Demo Traveller", "email": "demo@example.test"}

CITIES = [
    {"code": "HYD", "name": "Hyderabad", "state": "Telangana"},
    {"code": "BLR", "name": "Bengaluru", "state": "Karnataka"},
    {"code": "MAA", "name": "Chennai", "state": "Tamil Nadu"},
    {"code": "BOM", "name": "Mumbai", "state": "Maharashtra"},
    {"code": "DEL", "name": "New Delhi", "state": "Delhi"},
    {"code": "PUNE", "name": "Pune", "state": "Maharashtra"},
    {"code": "VJA", "name": "Vijayawada", "state": "Andhra Pradesh"},
    {"code": "TIR", "name": "Tirupati", "state": "Andhra Pradesh"},
    {"code": "MYS", "name": "Mysuru", "state": "Karnataka"},
    {"code": "CBE", "name": "Coimbatore", "state": "Tamil Nadu"},
    {"code": "COK", "name": "Kochi", "state": "Kerala"},
    {"code": "GOI", "name": "Goa", "state": "Goa"},
]

# (mode, operator, service name/number, from, to, dep, arr, fare INR, seats, amenities)
SEED_OPTIONS = [
    ("train", "Demo Railways", "Demo Express 12639", "HYD", "BLR", "06:30", "14:05", 685, 48, "AC Chair Car, Pantry, Wi-Fi"),
    ("train", "Demo Railways", "Deccan Sunrise 12785", "HYD", "BLR", "08:15", "16:40", 540, 48, "Sleeper, Pantry"),
    ("train", "Demo Railways", "Demo Express 12640", "BLR", "HYD", "07:00", "14:30", 685, 48, "AC Chair Car, Pantry, Wi-Fi"),
    ("bus", "Sunrise Travels", "Sunrise Volvo AC Sleeper", "HYD", "BLR", "21:30", "06:45", 899, 36, "AC Sleeper, Blanket, Charging"),
    ("bus", "Deccan Rider", "Deccan Rider Multi-Axle", "HYD", "BLR", "22:15", "07:30", 749, 40, "AC Semi-Sleeper, Water Bottle"),
    ("bus", "Sunrise Travels", "Sunrise Volvo AC Sleeper", "BLR", "HYD", "21:00", "06:15", 899, 36, "AC Sleeper, Blanket, Charging"),
    ("train", "Demo Railways", "Coromandel Demo 12841", "HYD", "MAA", "07:45", "19:20", 720, 48, "AC Chair Car, Pantry"),
    ("bus", "Kaveri Lines", "Kaveri Lines AC Seater", "HYD", "MAA", "20:30", "07:00", 799, 40, "AC Seater, Charging"),
    ("bus", "Deccan Rider", "Deccan Rider Volvo", "HYD", "BOM", "19:00", "09:30", 1199, 40, "AC Sleeper, Blanket"),
    ("train", "Demo Railways", "Hussain Sagar Demo 12701", "HYD", "BOM", "14:45", "05:10", 890, 48, "Sleeper, Pantry"),
    ("bus", "Kaveri Lines", "Kaveri Lines AC Sleeper", "BLR", "MAA", "22:00", "05:30", 699, 36, "AC Sleeper, Blanket"),
    ("train", "Demo Railways", "Demo Express 16591", "BLR", "MAA", "06:00", "12:45", 495, 48, "AC Chair Car"),
    ("bus", "Sunrise Travels", "Sunrise AC Seater", "HYD", "VJA", "08:00", "13:30", 449, 40, "AC Seater, Charging"),
    ("bus", "Deccan Rider", "Deccan Rider AC Seater", "HYD", "TIR", "21:45", "06:15", 649, 40, "AC Seater, Blanket"),
    ("bus", "Kaveri Lines", "Kaveri Lines Volvo", "BLR", "MYS", "09:30", "12:30", 299, 36, "AC Seater"),
    ("train", "Demo Railways", "Malabar Demo 16347", "BLR", "COK", "18:20", "06:40", 780, 48, "Sleeper, Pantry"),
]


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS options (
        id INTEGER PRIMARY KEY, mode TEXT, operator TEXT, service TEXT,
        from_code TEXT, to_code TEXT, dep_time TEXT, arr_time TEXT,
        base_fare INTEGER, total_seats INTEGER, amenities TEXT, rating REAL);
    CREATE TABLE IF NOT EXISTS inventory (
        option_id INTEGER, date TEXT, seats_available INTEGER,
        fare_override INTEGER, PRIMARY KEY (option_id, date));
    CREATE TABLE IF NOT EXISTS bookings (
        id TEXT PRIMARY KEY, option_id INTEGER, date TEXT, seats TEXT,
        passenger TEXT, fare_total INTEGER, status TEXT, created_at TEXT);
    """)
    if conn.execute("SELECT COUNT(*) FROM options").fetchone()[0] == 0:
        for i, (mode, op, svc, fc, tc, dep, arr, fare, seats, am) in enumerate(SEED_OPTIONS, 1):
            rating = round(3.8 + (i % 9) * 0.1, 1)
            conn.execute(
                "INSERT INTO options VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, mode, op, svc, fc, tc, dep, arr, fare, seats, am, rating))
    conn.commit()
    conn.close()


def ensure_inventory(conn, option_id, date):
    row = conn.execute(
        "SELECT * FROM inventory WHERE option_id=? AND date=?", (option_id, date)).fetchone()
    if row:
        return row
    opt = conn.execute("SELECT total_seats FROM options WHERE id=?", (option_id,)).fetchone()
    if not opt:
        return None
    conn.execute("INSERT INTO inventory VALUES (?,?,?,NULL)",
                 (option_id, date, opt["total_seats"]))
    conn.commit()
    return conn.execute(
        "SELECT * FROM inventory WHERE option_id=? AND date=?", (option_id, date)).fetchone()


def option_dict(conn, opt, date):
    inv = ensure_inventory(conn, opt["id"], date)
    fare = inv["fare_override"] if inv["fare_override"] is not None else opt["base_fare"]
    return {"id": opt["id"], "mode": opt["mode"], "operator": opt["operator"],
            "service": opt["service"], "from_code": opt["from_code"],
            "to_code": opt["to_code"], "dep_time": opt["dep_time"],
            "arr_time": opt["arr_time"], "fare": fare, "base_fare": opt["base_fare"],
            "seats_available": inv["seats_available"], "total_seats": opt["total_seats"],
            "amenities": opt["amenities"], "rating": opt["rating"], "date": date}


def seat_map(conn, option_id, date):
    opt = conn.execute("SELECT total_seats FROM options WHERE id=?", (option_id,)).fetchone()
    if not opt:
        return None
    held = set()
    for b in conn.execute(
            "SELECT seats FROM bookings WHERE option_id=? AND date=? AND status IN ('HELD','PREPARED')",
            (option_id, date)):
        held.update(json.loads(b["seats"]))
    seats = []
    for n in range(1, opt["total_seats"] + 1):
        if n in held:
            status = "held"
        elif n % 7 == 0:  # deterministic pre-booked seats, same for every fresh DB
            status = "booked"
        else:
            status = "available"
        seats.append({"seat_no": n, "label": f"S{n}", "status": status})
    return seats


app = FastAPI(title="Demo Sandbox — Travel")
init_db()


@app.get("/api/v1/health")
def health():
    return {"ok": True, "service": "travel-demo-sandbox", "payment": "disabled"}


@app.get("/api/v1/cities")
def cities(q: str = ""):
    q = q.strip().lower()
    out = [c for c in CITIES if not q or q in c["name"].lower() or q in c["code"].lower()]
    return {"ok": True, "cities": out}


@app.get("/api/v1/search")
def search(from_code: str, to_code: str, date: str, mode: str = "",
           sort: str = "departure", max_price: int = 0, dep_after: str = ""):
    if from_code == to_code:
        raise HTTPException(400, "from and to must differ")
    conn = db()
    rows = conn.execute(
        "SELECT * FROM options WHERE from_code=? AND to_code=? ORDER BY dep_time",
        (from_code.upper(), to_code.upper())).fetchall()
    out = []
    for r in rows:
        d = option_dict(conn, r, date)
        if mode and d["mode"] != mode:
            continue
        if max_price and d["fare"] > max_price:
            continue
        if dep_after and d["dep_time"] < dep_after:
            continue
        out.append(d)
    if sort == "price":
        out.sort(key=lambda x: x["fare"])
    elif sort == "duration":
        out.sort(key=lambda x: x["arr_time"])
    conn.close()
    return {"ok": True, "from_code": from_code.upper(), "to_code": to_code.upper(),
            "date": date, "count": len(out), "options": out}


@app.get("/api/v1/options/{option_id}")
def option_detail(option_id: int, date: str = "2026-12-05"):
    conn = db()
    opt = conn.execute("SELECT * FROM options WHERE id=?", (option_id,)).fetchone()
    if not opt:
        conn.close()
        raise HTTPException(404, "option not found")
    d = option_dict(conn, opt, date)
    d["seats"] = seat_map(conn, option_id, date)
    conn.close()
    return {"ok": True, "option": d}


class HoldReq(BaseModel):
    option_id: int
    date: str
    seats: list[int] = Field(min_length=1, max_length=6)


@app.post("/api/v1/holds")
def create_hold(req: HoldReq):
    conn = db()
    opt = conn.execute("SELECT * FROM options WHERE id=?", (req.option_id,)).fetchone()
    if not opt:
        conn.close()
        raise HTTPException(404, "option not found")
    inv = ensure_inventory(conn, req.option_id, req.date)
    fare = inv["fare_override"] if inv["fare_override"] is not None else opt["base_fare"]
    seats_now = seat_map(conn, req.option_id, req.date)
    by_no = {s["seat_no"]: s["status"] for s in seats_now}
    for s in req.seats:
        if s not in by_no:
            conn.close()
            raise HTTPException(400, f"seat {s} does not exist")
        if by_no[s] != "available":
            conn.close()
            raise HTTPException(409, f"seat {s} is {by_no[s]}")
    if len(req.seats) > inv["seats_available"]:
        conn.close()
        raise HTTPException(409, "not enough seats available")
    bid = "BK-" + uuid.uuid4().hex[:8].upper()
    total = fare * len(req.seats)
    conn.execute(
        "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?)",
        (bid, req.option_id, req.date, json.dumps(req.seats), None, total, "HELD",
         datetime.now(timezone.utc).isoformat()))
    conn.execute("UPDATE inventory SET seats_available=seats_available-? WHERE option_id=? AND date=?",
                 (len(req.seats), req.option_id, req.date))
    conn.commit()
    conn.close()
    return {"ok": True, "booking_id": bid, "status": "HELD",
            "seats": req.seats, "fare_per_seat": fare, "fare_total": total}


class PassengerReq(BaseModel):
    name: str = Field(min_length=2)
    age: int = Field(ge=1, le=120)
    gender: str = ""
    email: str = ""
    phone: str = ""


@app.post("/api/v1/bookings/{booking_id}/passenger")
def set_passenger(booking_id: str, req: PassengerReq):
    conn = db()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "booking not found")
    conn.execute("UPDATE bookings SET passenger=? WHERE id=?",
                 (json.dumps(req.model_dump()), booking_id))
    conn.commit()
    conn.close()
    return {"ok": True, "booking_id": booking_id, "passenger": req.model_dump()}


@app.get("/api/v1/bookings/{booking_id}")
def get_booking(booking_id: str):
    conn = db()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "booking not found")
    return {"ok": True, "booking": {"id": row["id"], "option_id": row["option_id"],
            "date": row["date"], "seats": json.loads(row["seats"]),
            "passenger": json.loads(row["passenger"]) if row["passenger"] else None,
            "fare_total": row["fare_total"], "status": row["status"],
            "created_at": row["created_at"]}}


@app.post("/api/v1/bookings/{booking_id}/checkout")
def checkout(booking_id: str):
    """Payment is DISABLED in this demo sandbox. The booking is only PREPARED."""
    conn = db()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "booking not found")
    if not row["passenger"]:
        conn.close()
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "passenger_required", "state": row["status"]})
    conn.execute("UPDATE bookings SET status='PREPARED' WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    return JSONResponse(status_code=402, content={
        "ok": False, "error": "payment_disabled", "state": "PREPARED",
        "booking_id": booking_id,
        "message": "Demo sandbox: payment is disabled. Booking prepared only — no charge, no ticket issued."})


class LoginReq(BaseModel):
    username: str
    password: str = ""


@app.post("/api/v1/auth/login")
def login(req: LoginReq):
    # Synthetic demo account only; any password accepted for the demo user.
    if req.username.lower() not in ("demo", "demo@example.test"):
        raise HTTPException(401, "unknown demo user — use username 'demo'")
    return {"ok": True, "user": DEMO_USER, "token": "demo-session-token",
            "note": "Synthetic demo account. No real credentials."}


def _require_operator(x_operator_token: str = Header(default="")):
    if x_operator_token != OPERATOR_TOKEN:
        raise HTTPException(403, "invalid operator token")


class FareReq(BaseModel):
    option_id: int
    date: str
    new_fare: int = Field(gt=0)


@app.post("/api/v1/operator/fare")
def operator_fare(req: FareReq, x_operator_token: str = Header(default="")):
    _require_operator(x_operator_token)
    conn = db()
    if not conn.execute("SELECT 1 FROM options WHERE id=?", (req.option_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "option not found")
    ensure_inventory(conn, req.option_id, req.date)
    conn.execute("UPDATE inventory SET fare_override=? WHERE option_id=? AND date=?",
                 (req.new_fare, req.option_id, req.date))
    conn.commit()
    conn.close()
    return {"ok": True, "option_id": req.option_id, "date": req.date, "fare": req.new_fare}


class AvailReq(BaseModel):
    option_id: int
    date: str
    seats_available: int = Field(ge=0)


@app.post("/api/v1/operator/availability")
def operator_availability(req: AvailReq, x_operator_token: str = Header(default="")):
    _require_operator(x_operator_token)
    conn = db()
    if not conn.execute("SELECT 1 FROM options WHERE id=?", (req.option_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "option not found")
    ensure_inventory(conn, req.option_id, req.date)
    conn.execute("UPDATE inventory SET seats_available=? WHERE option_id=? AND date=?",
                 (req.seats_available, req.option_id, req.date))
    conn.commit()
    conn.close()
    return {"ok": True, "option_id": req.option_id, "date": req.date,
            "seats_available": req.seats_available}


app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"), html=True), name="static")
