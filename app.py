import os, io, hmac, hashlib, sqlite3, uuid, requests, threading, smtplib, html
from datetime import datetime
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.lib.colors import HexColor, white
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from email.message import EmailMessage
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature
from flask import Flask, jsonify, request, Response, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://tickets-hubs.onrender.com").rstrip("/")
CORS(app, origins=[origin.strip().rstrip("/") for origin in FRONTEND_URL.split(",") if origin.strip()])
SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
CURRENCY = os.getenv("PAYSTACK_CURRENCY", "KES")
SITE_URL = os.getenv("SITE_URL", "https://tickets-hubs.onrender.com")
PAYSTACK = "https://api.paystack.co"
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "tickets.db")
UPLOADS = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

CATEGORIES = [
    {"id": "sports", "name": "Sports"},
    {"id": "music", "name": "Music"},
    {"id": "festivals", "name": "Festivals"},
    {"id": "tech", "name": "Tech and business"},
    {"id": "comedy", "name": "Comedy and theatre"},
]

def E(id, title, category, venue, city, date, price, capacity, blurb, featured=False):
    return dict(id=id, title=title, category=category, venue=venue, city=city,
                date=date, price=price, capacity=capacity, blurb=blurb, featured=featured)

EVENTS = [
    E(1, "Nairobi City Derby", "sports", "Kasarani Stadium", "Nairobi", "2026-12-06", 1500, 420,
      "The biggest football night of the season, with every seat close to the pitch.", True),
    E(2, "Sunset Afrobeats Live", "music", "Uhuru Gardens", "Nairobi", "2026-12-12", 3000, 180,
      "Five artists, one open-air stage, and a skyline finish.", True),
    E(3, "Nairobi Tech Summit 2027", "tech", "KICC", "Nairobi", "2027-02-18", 4500, 260,
      "Two days of talks on AI, fintech and geospatial technology.", True),
    E(4, "Naivasha Food and Music Fest", "festivals", "Lake Naivasha", "Naivasha", "2026-12-20", 2500, 600,
      "Street food, live bands and lakeside camping for the weekend."),
    E(5, "Laugh Out Loud Comedy Night", "comedy", "Westlands", "Nairobi", "2026-11-28", 1800, 120,
      "Six stand-up comedians in one packed room."),
    E(6, "Rugby Sevens Showdown", "sports", "RFUEA Ground", "Nairobi", "2026-12-13", 1000, 800,
      "Fast, loud and family friendly, with matches all day."),
    E(7, "Jazz at the Gardens", "music", "Karura Forest", "Nairobi", "2027-01-09", 2200, 150,
      "An unhurried evening of live jazz among the trees."),
    E(8, "Startup Pitch Night", "tech", "Westlands", "Nairobi", "2026-11-26", 800, 90,
      "Ten founders, five minutes each, and a room full of investors."),
]
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
KEY = os.getenv("APP_SECRET") or (ADMIN_PASSWORD + SECRET + "-techpoint")
tokens = URLSafeTimedSerializer(KEY)
dl = URLSafeTimedSerializer(KEY, salt="ticket")

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        ref TEXT PRIMARY KEY, event_id INTEGER, name TEXT, email TEXT,
        qty INTEGER, amount INTEGER, status TEXT DEFAULT 'pending')""")
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, category TEXT, venue TEXT, city TEXT,
        date TEXT, price INTEGER, capacity INTEGER, blurb TEXT, featured INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""")
    if not c.execute("SELECT 1 FROM events").fetchone():
        for e in EVENTS:
            c.execute("INSERT INTO events (id,title,category,venue,city,date,price,capacity,blurb,featured) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (e["id"], e["title"], e["category"], e["venue"], e["city"], e["date"], e["price"], e["capacity"], e["blurb"], int(e["featured"])))

with db() as c:
    try:
        c.execute("ALTER TABLE events ADD COLUMN image TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

def row_event(r):
    d = dict(r)
    d["featured"], d["active"] = bool(d["featured"]), bool(d["active"])
    d["image"] = d.get("image") or ""
    return d

def all_events(active_only=True):
    with db() as c:
        q = "SELECT * FROM events" + (" WHERE active=1" if active_only else "") + " ORDER BY date"
        return [row_event(r) for r in c.execute(q)]

def get_event(i):
    with db() as c:
        r = c.execute("SELECT * FROM events WHERE id=?", (i,)).fetchone()
    return row_event(r) if r else None

def sold(event_id):
    with db() as c:
        return c.execute("SELECT COALESCE(SUM(qty),0) n FROM orders WHERE event_id=? AND status='paid'",
                         (event_id,)).fetchone()["n"]

def public(e):
    return {**e, "left": max(e["capacity"] - sold(e["id"]), 0)}

def err(msg, code=400):
    return jsonify(error=msg), code

def auth():
    return {"Authorization": f"Bearer {SECRET}"}

CAT_COLORS = {"sports": "#1D9E75", "music": "#7F77DD", "festivals": "#EF9F27", "tech": "#00B8D9", "comedy": "#E4572E"}

def make_ticket_pdf(o):
    e = get_event(o["event_id"])
    W, H = 612, 252
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    cover = None
    if e["image"].startswith("/uploads/"):
        try:
            cover = ImageReader(Image.open(os.path.join(UPLOADS, os.path.basename(e["image"]))).convert("RGB"))
        except Exception:
            cover = None
    navy, muted = HexColor("#0B1F3A"), HexColor("#5B6B80")
    try:
        day = datetime.strptime(e["date"], "%Y-%m-%d").strftime("%a %d %b %Y")
    except ValueError:
        day = e["date"]
    for n in range(1, o["qty"] + 1):
        code = f'{o["ref"]}-{n}'
        c.setFillColor(white)
        c.rect(0, 0, W, H, fill=1, stroke=0)
        c.saveState()
        p = c.beginPath()
        p.rect(0, 0, 220, H)
        c.clipPath(p, stroke=0, fill=0)
        if cover:
            iw, ih = cover.getSize()
            k = max(220 / iw, H / ih)
            c.drawImage(cover, (220 - iw * k) / 2, (H - ih * k) / 2, iw * k, ih * k)
        else:
            c.setFillColor(HexColor(CAT_COLORS.get(e["category"], "#00B8D9")))
            c.rect(0, 0, 220, H, fill=1, stroke=0)
        c.setFillColor(HexColor("#061224"))
        c.setFillAlpha(0.8)
        c.rect(0, 0, 220, 34, fill=1, stroke=0)
        c.setFillAlpha(1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(14, 13, "TechPoint Solutions")
        c.restoreState()
        c.setStrokeColor(HexColor("#DCE3EA"))
        c.setDash(4, 4)
        c.line(470, 14, 470, H - 14)
        c.setDash()
        c.setFillColor(muted)
        c.setFont("Helvetica", 10)
        c.drawString(244, H - 30, "ADMIT ONE")
        c.setFillColor(navy)
        c.setFont("Helvetica-Bold", 19)
        y = H - 58
        for line in simpleSplit(e["title"], "Helvetica-Bold", 19, 215)[:2]:
            c.drawString(244, y, line)
            y -= 23
        c.setFont("Helvetica", 11)
        c.setFillColor(muted)
        for line in (day, f'{e["venue"]}, {e["city"]}', f'Holder: {o["name"]}', f'Ticket {n} of {o["qty"]}'):
            c.drawString(244, y - 8, line)
            y -= 17
        wd = QrCodeWidget(code)
        b = wd.getBounds()
        d = Drawing(120, 120, transform=[120 / (b[2] - b[0]), 0, 0, 120 / (b[3] - b[1]), 0, 0])
        d.add(wd)
        renderPDF.draw(d, c, 486, 92)
        c.setFillColor(navy)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(546, 76, code)
        c.setFont("Helvetica", 8)
        c.setFillColor(muted)
        c.drawCentredString(546, 62, "Scan or show at the gate")
        c.showPage()
    c.save()
    return buf.getvalue()

def send_receipt(o):
    host = os.getenv("SMTP_HOST")
    if not host:
        print("SMTP not configured, receipt skipped for", o["ref"])
        return
    e = get_event(o["event_id"])
    total = f'KES {o["amount"] / 100:,.0f}'
    esc = html.escape
    msg = EmailMessage()
    msg["Subject"] = f'Your tickets for {e["title"]} ({o["ref"]})'
    msg["From"] = os.getenv("MAIL_FROM") or os.getenv("SMTP_USER")
    msg["To"] = o["email"]
    msg.set_content(
        f'Hi {o["name"]},\n\nThank you for your purchase. Show this reference at the gate.\n\n'
        f'Reference: {o["ref"]}\nEvent: {e["title"]}\nDate: {e["date"]}\nVenue: {e["venue"]}, {e["city"]}\n'
        f'Tickets: {o["qty"]}\nTotal paid: {total}\n\nTechPoint Solutions')
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#0B1F3A">
<div style="background:#061224;color:#fff;padding:20px 24px;border-radius:12px 12px 0 0"><strong style="font-size:18px">TechPoint <span style="color:#00B8D9">Solutions</span></strong></div>
<div style="border:1px solid #DCE3EA;border-top:0;padding:24px;border-radius:0 0 12px 12px">
<p>Hi {esc(o["name"])}, your payment is confirmed. Your ticket is attached as a PDF. Show it at the gate.</p>
<p style="background:#E0F7FB;padding:14px;border-radius:8px;font-size:20px;font-weight:bold;letter-spacing:1px;text-align:center">{o["ref"]}</p>
<table style="width:100%;font-size:15px;line-height:1.8"><tr><td>Event</td><td align="right"><b>{esc(e["title"])}</b></td></tr>
<tr><td>Date</td><td align="right">{e["date"]}</td></tr><tr><td>Venue</td><td align="right">{esc(e["venue"])}, {esc(e["city"])}</td></tr>
<tr><td>Tickets</td><td align="right">{o["qty"]}</td></tr><tr><td>Total paid</td><td align="right"><b>{total}</b></td></tr></table></div></div>""", subtype="html")
    try:
        msg.add_attachment(make_ticket_pdf(o), maintype="application", subtype="pdf", filename=f'TechPoint-{o["ref"]}.pdf')
    except Exception as x:
        print("Ticket PDF failed:", x)
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        with (smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)) as s:
            if port != 465:
                s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as x:
        print("Receipt email failed:", x)

def settle(data):
    ref = data.get("reference")
    paid_now = False
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()
        if not o:
            return None
        if data.get("status") == "success" and data.get("amount") == o["amount"] \
                and data.get("currency", CURRENCY) == CURRENCY:
            paid_now = o["status"] != "paid"
            c.execute("UPDATE orders SET status='paid' WHERE ref=?", (ref,))
        elif data.get("status") in ("failed", "abandoned") and o["status"] != "paid":
            c.execute("UPDATE orders SET status=? WHERE ref=?", (data["status"], ref))
        res = dict(c.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone())
    if paid_now:
        threading.Thread(target=send_receipt, args=(res,), daemon=True).start()
    return res

@app.get("/api/categories")
def categories():
    return jsonify(CATEGORIES)

@app.get("/api/events")
def events():
    cat = request.args.get("category")
    return jsonify([public(e) for e in all_events() if not cat or e["category"] == cat])

@app.post("/api/checkout")
def checkout():
    b = request.get_json(force=True, silent=True) or {}
    e = get_event(b.get("event_id"))
    name, email = (b.get("name") or "").strip(), (b.get("email") or "").strip()
    try:
        qty = int(b.get("qty", 1))
    except (TypeError, ValueError):
        qty = 0
    if not e or not e["active"]:
        return err("Event not found.", 404)
    if not name or "@" not in email:
        return err("Enter your name and a valid email address.")
    if not 1 <= qty <= 10:
        return err("Choose between 1 and 10 tickets.")
    if public(e)["left"] < qty:
        return err("Not enough tickets left for this event.")
    if not SECRET:
        return err("Payments are not set up yet. Add PAYSTACK_SECRET_KEY to backend/.env.", 500)
    ref = "TP-" + uuid.uuid4().hex[:10].upper()
    amount = e["price"] * qty * 100
    try:
        j = requests.post(f"{PAYSTACK}/transaction/initialize", headers=auth(), timeout=20, json={
            "email": email, "amount": amount, "currency": CURRENCY, "reference": ref,
            "callback_url": SITE_URL,
            "metadata": {"event_id": e["id"], "event": e["title"], "name": name, "qty": qty},
        }).json()
    except requests.RequestException:
        return err("Could not reach Paystack. Try again in a moment.", 502)
    if not j.get("status"):
        return err(j.get("message", "Paystack could not start the payment."), 502)
    with db() as c:
        c.execute("INSERT INTO orders (ref,event_id,name,email,qty,amount) VALUES (?,?,?,?,?,?)",
                  (ref, e["id"], name, email, qty, amount))
    return jsonify(authorization_url=j["data"]["authorization_url"], reference=ref)

@app.get("/api/verify/<ref>")
def verify(ref):
    try:
        j = requests.get(f"{PAYSTACK}/transaction/verify/{ref}", headers=auth(), timeout=20).json()
    except requests.RequestException:
        return err("Could not reach Paystack. Try again in a moment.", 502)
    if not j.get("status"):
        return err(j.get("message", "Payment not found."), 404)
    o = settle(j["data"])
    if not o:
        return err("Order not found.", 404)
    link = f"/api/tickets/{ref}.pdf?t={dl.dumps(ref)}" if o["status"] == "paid" else None
    return jsonify(order=o, event=get_event(o["event_id"])["title"], download=link)

@app.post("/api/paystack/webhook")
def webhook():
    sig = request.headers.get("x-paystack-signature", "")
    digest = hmac.new(SECRET.encode(), request.get_data(), hashlib.sha512).hexdigest()
    if not SECRET or not hmac.compare_digest(sig, digest):
        return "", 401
    evt = request.get_json(silent=True) or {}
    if evt.get("event") == "charge.success":
        settle(evt["data"])
    return "", 200

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        try:
            tokens.loads(request.headers.get("Authorization", "").replace("Bearer ", ""), max_age=43200)
        except BadSignature:
            return err("Please sign in again.", 401)
        return f(*a, **k)
    return w

@app.post("/api/admin/login")
def admin_login():
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if not ADMIN_PASSWORD:
        return err("Set ADMIN_PASSWORD in backend/.env to enable the dashboard.", 500)
    if not hmac.compare_digest(pw, ADMIN_PASSWORD):
        return err("Wrong password.", 401)
    return jsonify(token=tokens.dumps("admin"))

@app.get("/api/admin/summary")
@admin_required
def admin_summary():
    with db() as c:
        t = c.execute("SELECT COALESCE(SUM(amount),0) rev, COALESCE(SUM(qty),0) tix, COUNT(*) n FROM orders WHERE status='paid'").fetchone()
        unpaid = c.execute("SELECT COUNT(*) n FROM orders WHERE status!='paid'").fetchone()["n"]
        rev = {r["event_id"]: r["r"] for r in c.execute("SELECT event_id, SUM(amount) r FROM orders WHERE status='paid' GROUP BY event_id")}
    events = [{**public(e), "sold": sold(e["id"]), "revenue": rev.get(e["id"], 0)} for e in all_events(False)]
    return jsonify(revenue=t["rev"], tickets=t["tix"], paid_orders=t["n"], unpaid_orders=unpaid, events=events)

@app.get("/api/admin/orders")
@admin_required
def admin_orders():
    with db() as c:
        rows = c.execute("SELECT o.*, e.title event FROM orders o LEFT JOIN events e ON e.id=o.event_id ORDER BY o.rowid DESC LIMIT 300").fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/admin/orders/<ref>/resend")
@admin_required
def admin_resend(ref):
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE ref=? AND status='paid'", (ref,)).fetchone()
    if not o:
        return err("Only paid orders can be resent.", 404)
    threading.Thread(target=send_receipt, args=(dict(o),), daemon=True).start()
    return jsonify(ok=True)

def clean(b):
    img = (b.get("image") or "").strip()
    if img and not img.startswith(("/uploads/", "http://", "https://")):
        return None
    try:
        d = {"image": img, "title": b["title"].strip(), "category": b["category"], "venue": b["venue"].strip(), "city": b["city"].strip(),
             "date": b["date"], "price": int(b["price"]), "capacity": int(b["capacity"]), "blurb": (b.get("blurb") or "").strip(),
             "featured": int(bool(b.get("featured"))), "active": int(bool(b.get("active", True)))}
    except (KeyError, ValueError, AttributeError, TypeError):
        return None
    if not d["title"] or not d["date"] or d["category"] not in {c["id"] for c in CATEGORIES} or d["price"] < 0 or d["capacity"] < 1:
        return None
    return d

@app.post("/api/admin/events")
@admin_required
def admin_create_event():
    d = clean(request.get_json(silent=True) or {})
    if not d:
        return err("Fill in the title, date, category, price and capacity.")
    with db() as c:
        cur = c.execute(f"INSERT INTO events ({','.join(d)}) VALUES ({','.join('?' * len(d))})", tuple(d.values()))
    return jsonify(id=cur.lastrowid)

@app.put("/api/admin/events/<int:i>")
@admin_required
def admin_update_event(i):
    d = clean(request.get_json(silent=True) or {})
    if not d or not get_event(i):
        return err("Fill in the title, date, category, price and capacity.")
    with db() as c:
        c.execute(f"UPDATE events SET {','.join(k + '=?' for k in d)} WHERE id=?", (*d.values(), i))
    return jsonify(ok=True)

@app.get("/api/tickets/<ref>.pdf")
def ticket_pdf(ref):
    try:
        ok = dl.loads(request.args.get("t", ""), max_age=60 * 60 * 24 * 365) == ref
    except BadSignature:
        ok = False
    if not ok:
        return err("This ticket link is not valid.", 403)
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE ref=? AND status='paid'", (ref,)).fetchone()
    if not o:
        return err("Ticket not found, or the payment is not confirmed yet.", 404)
    return Response(make_ticket_pdf(dict(o)), mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="TechPoint-{ref}.pdf"'})

@app.get("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOADS, name)

@app.post("/api/admin/upload")
@admin_required
def admin_upload():
    f = request.files.get("file")
    ext = os.path.splitext(f.filename or "")[1].lower() if f else ""
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return err("Upload a JPG, PNG or WebP image.")
    try:
        Image.open(f.stream).verify()
    except Exception:
        return err("That file is not a valid image.")
    f.stream.seek(0)
    name = uuid.uuid4().hex + ext
    f.save(os.path.join(UPLOADS, name))
    return jsonify(url=f"/uploads/{name}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
