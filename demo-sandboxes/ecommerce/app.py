"""Demo Sandbox — E-commerce (NAVIGATOR demo2, stream S3).

FastAPI + SQLite, single origin, port 8125. Original branding only:
"ShopSphere Demo Sandbox — not the real site". No real purchases,
no real payments: checkout always returns PREPARED / payment_disabled.
Operator control (X-Operator-Token: demo-operator) changes seller price
or stock in the DB; changes are visible ONLY via normal GET endpoints,
so NAVIGATOR's HTTP monitoring fetch can detect them.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "ecommerce.db")
OPERATOR_TOKEN = "demo-operator"

app = FastAPI(title="ShopSphere Demo Sandbox")

SELLERS = ["TechKart Retail", "GadgetHub India", "WearableWorld", "PrimeDeals"]

# (name, category, base_price_inr, description, specs dict, colour, icon)
PRODUCTS = [
    ("Amazfit Bip 5", "Smartwatches", 5499,
     "1.91-inch HD display smartwatch with Bluetooth calling, 120+ sports modes, "
     "10-day battery life and 5 ATM water resistance. A everyday companion for "
     "fitness tracking, sleep monitoring and smart notifications.",
     {"Display": "1.91\" TFT, 320 x 380 px", "Battery": "300 mAh, up to 10 days",
      "Bluetooth": "BLE 5.2, Bluetooth calling", "Water resistance": "5 ATM",
      "Sensors": "BioTracker PPG heart-rate, SpO2, accelerometer, gyroscope",
      "Sports modes": "120+", "Weight": "26 g (without strap)",
      "Compatibility": "Android 7.0+ / iOS 14.0+", "Warranty": "1 year"},
     "#0f766e", "⌚"),
    ("Amazfit GTR 4", "Smartwatches", 16999,
     "Premium AMOLED smartwatch with dual-band GPS, 150+ sports modes and "
     "14-day battery life. Built for runners and outdoor athletes.",
     {"Display": "1.43\" AMOLED, 466 x 466 px", "Battery": "475 mAh, up to 14 days",
      "GPS": "Dual-band, 6 satellite systems", "Water resistance": "5 ATM",
      "Sports modes": "150+", "Weight": "34 g",
      "Compatibility": "Android 7.0+ / iOS 14.0+", "Warranty": "1 year"},
     "#1e3a5f", "⌚"),
    ("boAt Wave Call 2", "Smartwatches", 1799,
     "Budget Bluetooth-calling smartwatch with a 1.83-inch HD display, "
     "700+ watch faces and IP68 dust and water resistance.",
     {"Display": "1.83\" HD, 240 x 284 px", "Battery": "Up to 7 days",
      "Bluetooth": "BT calling with dial pad", "Water resistance": "IP68",
      "Sports modes": "100+", "Weight": "48 g",
      "Compatibility": "Android / iOS", "Warranty": "1 year"},
     "#7c3aed", "⌚"),
    ("Samsung Galaxy Buds 2", "Audio", 7999,
     "True wireless earbuds with active noise cancellation, 3-mic system and "
     "up to 29 hours of total playtime with the case.",
     {"Driver": "Dynamic 2-way", "ANC": "Yes, active noise cancellation",
      "Battery": "5 h earbuds, 29 h with case", "Bluetooth": "5.2",
      "Water resistance": "IPX7", "Charging": "USB-C, wireless",
      "Weight": "5 g per bud", "Warranty": "1 year"},
     "#0ea5e9", "🎧"),
    ("Sony WH-1000XM5", "Audio", 26990,
     "Industry-leading noise cancelling over-ear headphones with two processors, "
     "eight microphones and up to 30 hours of battery life.",
     {"Driver": "30 mm dynamic", "ANC": "Yes, 2 processors + 8 mics",
      "Battery": "Up to 30 h (NC on)", "Bluetooth": "5.2, LDAC, multipoint",
      "Charging": "USB-C, 3 min = 3 h", "Weight": "250 g",
      "Warranty": "1 year"},
     "#111827", "🎧"),
    ("JBL Flip 6", "Audio", 9999,
     "Portable Bluetooth speaker with powerful sound, IP67 waterproof and "
     "dustproof rating and 12 hours of playtime.",
     {"Output": "30 W RMS", "Battery": "Up to 12 h", "Bluetooth": "5.1",
      "Water resistance": "IP67", "Charging": "USB-C",
      "Weight": "550 g", "Warranty": "1 year"},
     "#ea580c", "🔊"),
    ("Logitech MX Master 3S", "Accessories", 10995,
     "Performance wireless mouse with 8K DPI sensor, quiet clicks and "
     "MagSpeed electromagnetic scrolling. Works on any surface, even glass.",
     {"Sensor": "8K DPI, any-surface tracking", "Buttons": "7, incl. gesture button",
      "Battery": "Up to 70 days", "Connectivity": "Bluetooth / Logi Bolt USB",
      "Weight": "141 g", "Warranty": "1 year"},
     "#334155", "🖱️"),
    ("Anker 65W GaN Charger", "Accessories", 2999,
     "Compact 65W GaN fast charger with 2 USB-C and 1 USB-A ports. Charges a "
     "laptop, phone and earbuds at once.",
     {"Output": "65 W max", "Ports": "2x USB-C, 1x USB-A",
      "Technology": "GaN II", "Input": "100-240 V", "Weight": "120 g",
      "Warranty": "18 months"},
     "#059669", "🔌"),
    ("Samsung Galaxy A55 5G", "Phones", 36999,
     "5G smartphone with a 6.6-inch Super AMOLED 120 Hz display, 50 MP OIS "
     "camera and a 5000 mAh battery.",
     {"Display": "6.6\" Super AMOLED, 120 Hz", "RAM / Storage": "8 GB / 128 GB",
      "Rear camera": "50 MP OIS + 12 MP UW + 5 MP macro",
      "Battery": "5000 mAh, 25 W charging", "Processor": "Exynos 1480",
      "OS": "Android 14, One UI 6.1", "Warranty": "1 year"},
     "#2563eb", "📱"),
    ("Kindle Paperwhite (16 GB)", "E-Readers", 16999,
     "6.8-inch glare-free e-reader with adjustable warm light, weeks of battery "
     "life and 16 GB of storage for thousands of books.",
     {"Display": "6.8\" glare-free, 300 ppi", "Storage": "16 GB",
      "Battery": "Up to 10 weeks", "Light": "Adjustable warm light",
      "Water resistance": "IPX8", "Weight": "205 g", "Warranty": "1 year"},
     "#78350f", "📖"),
    ("Lenovo IdeaPad Slim 3", "Laptops", 52990,
     "Thin 15.6-inch laptop with a 13th-gen Intel Core i5, 16 GB RAM and a "
     "512 GB SSD. Built for work, study and everyday multitasking.",
     {"Display": "15.6\" FHD IPS, 300 nits", "Processor": "Intel Core i5-13420H",
      "RAM / Storage": "16 GB / 512 GB SSD", "Graphics": "Intel UHD",
      "Battery": "Up to 8 h", "Weight": "1.62 kg", "OS": "Windows 11 Home",
      "Warranty": "1 year"},
     "#475569", "💻"),
    ("LG 27\" UltraGear Monitor", "Monitors", 18999,
     "27-inch QHD IPS gaming monitor with 165 Hz refresh rate, 1 ms response "
     "and HDR10 support.",
     {"Display": "27\" QHD IPS, 2560 x 1440", "Refresh rate": "165 Hz",
      "Response": "1 ms GtG", "HDR": "HDR10", "Ports": "HDMI x2, DisplayPort",
      "Panel": "IPS, 99% sRGB", "Warranty": "3 years"},
     "#0891b2", "🖥️"),
    ("Fire TV Stick 4K", "Streaming", 5999,
     "Stream in 4K Ultra HD with Dolby Vision, HDR10+ and Alexa voice remote. "
     "Access Netflix, Prime Video, Disney+ Hotstar and more.",
     {"Resolution": "4K Ultra HD, Dolby Vision", "Audio": "Dolby Atmos",
      "Remote": "Alexa Voice Remote", "Storage": "8 GB", "Wi-Fi": "Wi-Fi 6",
      "Warranty": "1 year"},
     "#b45309", "📺"),
    ("Echo Dot (5th Gen)", "Smart Home", 5499,
     "Smart speaker with Alexa, bigger vibrant sound, and routines to control "
     "your smart home with your voice.",
     {"Speaker": "1.73\" front-firing", "Voice": "Alexa built-in",
      "Connectivity": "Wi-Fi, Bluetooth", "Smart home": "Routines, device control",
      "Warranty": "1 year"},
     "#4f46e5", "🔈"),
]

# seller price offsets (multiplier) and stock per (product_idx, seller_idx)
SELLER_VARIANTS = [
    (1.00, 14), (0.96, 7), (1.04, 22), (0.92, 3),
]


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY, name TEXT, category TEXT, base_price INTEGER,
            description TEXT, specs TEXT, colour TEXT, icon TEXT);
        CREATE TABLE IF NOT EXISTS sellers (
            id INTEGER PRIMARY KEY, product_id INTEGER, name TEXT,
            price INTEGER, stock INTEGER,
            FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER,
            price INTEGER, changed_at TEXT, changed_by TEXT);
        CREATE TABLE IF NOT EXISTS cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            product_id INTEGER, seller_id INTEGER, qty INTEGER,
            UNIQUE(session_id, product_id, seller_id));
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, total INTEGER,
            state TEXT, address TEXT, created_at TEXT);
    """)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        for i, (name, cat, price, desc, specs, colour, icon) in enumerate(PRODUCTS, 1):
            conn.execute(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?)",
                (i, name, cat, price, desc, json.dumps(specs), colour, icon))
            for j, seller in enumerate(SELLERS):
                mult, stock = SELLER_VARIANTS[j]
                # vary stock a little per product so listings look real
                st = max(0, stock + ((i * 3 + j) % 5) - 2)
                p = int(round(price * mult / 10.0) * 10 - 1) if mult != 1.0 else price
                sid = conn.execute(
                    "INSERT INTO sellers (product_id, name, price, stock) VALUES (?,?,?,?)",
                    (i, seller, p, st)).lastrowid
                conn.execute(
                    "INSERT INTO price_history (seller_id, price, changed_at, changed_by) "
                    "VALUES (?,?,?,?)", (sid, p, now, "seed"))
        conn.commit()
    conn.close()


def seller_row(r):
    hist = db().execute(
        "SELECT price, changed_at, changed_by FROM price_history "
        "WHERE seller_id=? ORDER BY id DESC LIMIT 10", (r["id"],)).fetchall()
    return {"id": r["id"], "name": r["name"], "price": r["price"],
            "stock": r["stock"],
            "in_stock": r["stock"] > 0,
            "price_history": [{"price": h["price"], "changed_at": h["changed_at"],
                               "changed_by": h["changed_by"]} for h in hist]}


def product_row(r, with_sellers=False):
    d = {"id": r["id"], "name": r["name"], "category": r["category"],
         "base_price": r["base_price"], "description": r["description"],
         "specs": json.loads(r["specs"]), "colour": r["colour"], "icon": r["icon"]}
    if with_sellers:
        conn = db()
        sellers = conn.execute(
            "SELECT * FROM sellers WHERE product_id=? ORDER BY price ASC",
            (r["id"],)).fetchall()
        conn.close()
        d["sellers"] = [seller_row(s) for s in sellers]
        d["lowest_price"] = min(s["price"] for s in d["sellers"])
        d["in_stock"] = any(s["in_stock"] for s in d["sellers"])
    return d


init_db()


@app.get("/api/v1/health")
def health():
    return {"ok": True, "service": "shopsphere-demo-sandbox", "port": 8125,
            "payment": "disabled", "time_utc": datetime.now(timezone.utc).isoformat()}


@app.get("/api/v1/categories")
def categories():
    conn = db()
    rows = conn.execute("SELECT DISTINCT category FROM products ORDER BY category").fetchall()
    conn.close()
    return {"ok": True, "categories": [r["category"] for r in rows]}


@app.get("/api/v1/products")
def list_products(q: str = "", category: str = ""):
    conn = db()
    sql = "SELECT * FROM products WHERE 1=1"
    params = []
    if q:
        sql += " AND (name LIKE ? OR category LIKE ? OR description LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if category:
        sql += " AND category=?"
        params.append(category)
    sql += " ORDER BY name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {"ok": True, "count": len(rows),
            "products": [product_row(r, with_sellers=True) for r in rows]}


@app.get("/api/v1/products/{pid}")
def get_product(pid: int):
    conn = db()
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, "product not found")
    return {"ok": True, "product": product_row(r, with_sellers=True)}


class CartAdd(BaseModel):
    session_id: str = Field(min_length=1)
    product_id: int
    seller_id: int
    qty: int = Field(default=1, ge=1, le=10)


class CartUpdate(BaseModel):
    session_id: str = Field(min_length=1)
    product_id: int
    seller_id: int
    qty: int = Field(ge=0, le=10)


def cart_for(session_id: str):
    conn = db()
    rows = conn.execute(
        "SELECT ci.*, p.name AS pname, p.category, s.name AS sname, s.price, s.stock "
        "FROM cart_items ci JOIN products p ON p.id=ci.product_id "
        "JOIN sellers s ON s.id=ci.seller_id WHERE ci.session_id=?",
        (session_id,)).fetchall()
    conn.close()
    items = [{"product_id": r["product_id"], "product_name": r["pname"],
              "category": r["category"], "seller_id": r["seller_id"],
              "seller_name": r["sname"], "unit_price": r["price"],
              "qty": r["qty"], "line_total": r["price"] * r["qty"],
              "stock": r["stock"]} for r in rows]
    return {"ok": True, "session_id": session_id, "items": items,
            "total": sum(i["line_total"] for i in items),
            "count": sum(i["qty"] for i in items)}


@app.get("/api/v1/cart")
def get_cart(session_id: str):
    return cart_for(session_id)


@app.post("/api/v1/cart/add")
def add_cart(body: CartAdd):
    conn = db()
    s = conn.execute("SELECT * FROM sellers WHERE id=? AND product_id=?",
                     (body.seller_id, body.product_id)).fetchone()
    if not s:
        conn.close()
        raise HTTPException(400, "seller does not sell this product")
    if s["stock"] <= 0:
        conn.close()
        raise HTTPException(400, "out of stock")
    cur = conn.execute(
        "SELECT qty FROM cart_items WHERE session_id=? AND product_id=? AND seller_id=?",
        (body.session_id, body.product_id, body.seller_id)).fetchone()
    new_qty = (cur["qty"] if cur else 0) + body.qty
    if new_qty > s["stock"]:
        conn.close()
        raise HTTPException(400, f"only {s['stock']} in stock")
    conn.execute(
        "INSERT INTO cart_items (session_id, product_id, seller_id, qty) VALUES (?,?,?,?) "
        "ON CONFLICT(session_id, product_id, seller_id) DO UPDATE SET qty=excluded.qty",
        (body.session_id, body.product_id, body.seller_id, new_qty))
    conn.commit()
    conn.close()
    return cart_for(body.session_id)


@app.post("/api/v1/cart/update")
def update_cart(body: CartUpdate):
    conn = db()
    if body.qty == 0:
        conn.execute("DELETE FROM cart_items WHERE session_id=? AND product_id=? AND seller_id=?",
                     (body.session_id, body.product_id, body.seller_id))
    else:
        s = conn.execute("SELECT stock FROM sellers WHERE id=?", (body.seller_id,)).fetchone()
        if not s or body.qty > s["stock"]:
            conn.close()
            raise HTTPException(400, "qty exceeds stock")
        conn.execute("UPDATE cart_items SET qty=? WHERE session_id=? AND product_id=? AND seller_id=?",
                     (body.qty, body.session_id, body.product_id, body.seller_id))
    conn.commit()
    conn.close()
    return cart_for(body.session_id)


class Checkout(BaseModel):
    session_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    city: str = Field(min_length=1)
    pincode: str = Field(min_length=1)


@app.post("/api/v1/checkout")
def checkout(body: Checkout):
    cart = cart_for(body.session_id)
    if not cart["items"]:
        raise HTTPException(400, "cart is empty")
    conn = db()
    conn.execute(
        "INSERT INTO orders (session_id, total, state, address, created_at) VALUES (?,?,?,?,?)",
        (body.session_id, cart["total"], "PREPARED",
         json.dumps({"name": body.name, "address": body.address,
                     "city": body.city, "pincode": body.pincode}),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    # Payment is DISABLED in this demo sandbox — never a success.
    return JSONResponse(status_code=402, content={
        "ok": False, "error": "payment_disabled", "state": "PREPARED",
        "message": "Demo sandbox: payment is disabled. Order prepared only, "
                   "no charge was made and no order was placed.",
        "order_summary": {"items": cart["items"], "total": cart["total"],
                          "name": body.name, "city": body.city}})


def _operator(request: Request, x_operator_token: str | None):
    if x_operator_token != OPERATOR_TOKEN:
        raise HTTPException(401, "invalid operator token")


class PriceChange(BaseModel):
    seller_id: int
    price: int = Field(gt=0)


class StockChange(BaseModel):
    seller_id: int
    stock: int = Field(ge=0)


@app.post("/api/v1/operator/price")
def op_price(body: PriceChange, request: Request,
             x_operator_token: str | None = Header(default=None)):
    _operator(request, x_operator_token)
    conn = db()
    s = conn.execute("SELECT * FROM sellers WHERE id=?", (body.seller_id,)).fetchone()
    if not s:
        conn.close()
        raise HTTPException(404, "seller not found")
    conn.execute("UPDATE sellers SET price=? WHERE id=?", (body.price, body.seller_id))
    conn.execute("INSERT INTO price_history (seller_id, price, changed_at, changed_by) "
                 "VALUES (?,?,?,?)",
                 (body.seller_id, body.price,
                  datetime.now(timezone.utc).isoformat(), "operator"))
    conn.commit()
    conn.close()
    return {"ok": True, "seller_id": body.seller_id,
            "old_price": s["price"], "new_price": body.price}


@app.post("/api/v1/operator/stock")
def op_stock(body: StockChange, request: Request,
             x_operator_token: str | None = Header(default=None)):
    _operator(request, x_operator_token)
    conn = db()
    s = conn.execute("SELECT * FROM sellers WHERE id=?", (body.seller_id,)).fetchone()
    if not s:
        conn.close()
        raise HTTPException(404, "seller not found")
    conn.execute("UPDATE sellers SET stock=? WHERE id=?", (body.stock, body.seller_id))
    conn.commit()
    conn.close()
    return {"ok": True, "seller_id": body.seller_id,
            "old_stock": s["stock"], "new_stock": body.stock}


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8125)
