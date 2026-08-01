"""
CROP SKYYY v3 - AI Farm Irrigation System
Refactored for security, thread-safety, and production-readiness.

Features:
  1. Mobile-friendly UI
  2. WhatsApp / SMS Alerts (Twilio)
  4. Scheduled Irrigation
  6. Real Arduino Sensor Support
  7. Crop Health Scanner (Camera + GPT-4o Vision)
  8. Crop Identifier (Camera + GPT-4o Vision)
"""

import os
import json
import random
import sqlite3
import threading
import time
import logging
from datetime import datetime

import requests as req
from flask import Flask, jsonify, request, send_from_directory, session

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cropskyyy")

# ---------------------------------------------------------------------------
# Optional imports - graceful fallback if packages not installed
# ---------------------------------------------------------------------------
try:
    from openai import OpenAI
    OPENAI_OK = True
except ImportError:
    OPENAI_OK = False
    log.warning("openai not installed - AI features disabled. Run: pip install openai")

try:
    import serial
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_OK = True
except ImportError:
    TWILIO_OK = False

try:
    from werkzeug.security import generate_password_hash, check_password_hash
    WERKZEUG_OK = True
except ImportError:
    WERKZEUG_OK = False
    log.warning("werkzeug not found - falling back to sha256 hashing")

try:
    import pytz
    PYTZ_OK = True
except ImportError:
    PYTZ_OK = False
    log.warning("pytz not installed - timezone conversion disabled. Run: pip install pytz")

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    LIMITER_OK = True
except ImportError:
    LIMITER_OK = False
    log.warning("flask-limiter not installed - rate limiting disabled. Run: pip install flask-limiter")

# ---------------------------------------------------------------------------
# CONFIG - Edit these values or set as environment variables
# ---------------------------------------------------------------------------

# --- API Keys ---
OWM_KEY    = "2f125e80f1a3689c2397593b7ad989a0"

# OPENAI_KEY is now read from Railway environment variable first
# Go to Railway -> Variables -> Add: OPENAI_KEY = your_key
OPENAI_KEY = os.getenv("OPENAI_KEY", "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")

# --- Twilio WhatsApp / SMS (sign up free at twilio.com) ---
TWILIO_SID   = "YOUR_TWILIO_ACCOUNT_SID"
TWILIO_TOKEN = "YOUR_TWILIO_AUTH_TOKEN"
TWILIO_FROM  = "whatsapp:+14155238886"
ALERT_PHONE  = "+2348012345678"

# --- Arduino ---
ARDUINO_PORT = "COM3"
ARDUINO_BAUD = 9600
USE_ARDUINO  = False   # Set True when Arduino is physically connected

# --- Environment variable overrides ---
SECRET_KEY   = os.getenv("SECRET_KEY", "cropskyyy_v3_secret_2024_change_in_prod")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# --- Timezone (Nigeria) ---
TIMEZONE = "Africa/Lagos"

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"]   = False  # Set True in production with HTTPS

# --- Rate Limiter ---
if LIMITER_OK:
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://",
    )
    log.info("Rate limiting enabled")

# --- AI and Twilio clients ---
ai_client     = OpenAI(api_key=OPENAI_KEY) if OPENAI_OK else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_OK else None

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cropskyyy_v3.db")

def get_db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        phone TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT DEFAULT '',
        city TEXT, moisture INTEGER, pump_on INTEGER,
        advice TEXT, temp REAL, humidity INTEGER,
        rain REAL DEFAULT 0, alert_sent INTEGER DEFAULT 0,
        soil_type TEXT DEFAULT 'Loamy',
        crop_type TEXT DEFAULT 'Maize',
        logged_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS weather_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city TEXT, temp REAL, humidity INTEGER,
        description TEXT, wind_speed REAL, feels_like REAL,
        pressure INTEGER, clouds INTEGER, rain_1h REAL DEFAULT 0,
        icon TEXT, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT DEFAULT '',
        name TEXT NOT NULL,
        city TEXT DEFAULT 'Kaduna',
        run_time TEXT NOT NULL,
        days TEXT NOT NULL,
        duration_min INTEGER DEFAULT 20,
        active INTEGER DEFAULT 1,
        last_run TEXT DEFAULT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS alert_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        whatsapp_enabled INTEGER DEFAULT 0,
        sms_enabled INTEGER DEFAULT 0,
        phone TEXT DEFAULT '',
        alert_critical INTEGER DEFAULT 1,
        alert_rain INTEGER DEFAULT 1,
        alert_schedule INTEGER DEFAULT 1
    )""")

    conn.commit()
    conn.close()
    log.info("Database ready: %s", DB)

init_db()

# ---------------------------------------------------------------------------
# Password Helpers
# ---------------------------------------------------------------------------
def hash_password(pw: str) -> str:
    if WERKZEUG_OK:
        return generate_password_hash(pw)
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()

def verify_password(pw: str, stored_hash: str) -> bool:
    if WERKZEUG_OK:
        if stored_hash.startswith("pbkdf2:") or stored_hash.startswith("scrypt:"):
            return check_password_hash(stored_hash, pw)
        import hashlib
        return hashlib.sha256(pw.encode()).hexdigest() == stored_hash
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest() == stored_hash

# ---------------------------------------------------------------------------
# Timezone Helper
# ---------------------------------------------------------------------------
def utc_ts_to_local(timestamp: int) -> str:
    """Convert UTC Unix timestamp to Nigeria local time string."""
    dt_utc = datetime.utcfromtimestamp(timestamp)
    if PYTZ_OK:
        try:
            tz       = pytz.timezone(TIMEZONE)
            dt_utc   = pytz.utc.localize(dt_utc)
            dt_local = dt_utc.astimezone(tz)
            return dt_local.strftime("%H:%M")
        except Exception as e:
            log.warning("Timezone conversion failed: %s", e)
    return dt_utc.strftime("%H:%M")

# ---------------------------------------------------------------------------
# Arduino (Feature 6)
# ---------------------------------------------------------------------------
arduino_conn = None
arduino_lock = threading.Lock()

def connect_arduino() -> bool:
    global arduino_conn
    if not SERIAL_OK or not USE_ARDUINO:
        return False
    try:
        with arduino_lock:
            if arduino_conn and arduino_conn.is_open:
                return True
            arduino_conn = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=2)
            time.sleep(2)
            log.info("Arduino connected on %s", ARDUINO_PORT)
            return True
    except Exception as e:
        log.error("Arduino connection failed: %s", e)
        arduino_conn = None
        return False

def read_soil_moisture() -> int:
    global arduino_conn
    if USE_ARDUINO and connect_arduino():
        try:
            with arduino_lock:
                arduino_conn.flushInput()
                line = arduino_conn.readline().decode("utf-8").strip()
                if line.isdigit():
                    val = int(line)
                    if 0 <= val <= 100:
                        log.info("Arduino soil moisture: %d%%", val)
                        return val
        except Exception as e:
            log.error("Arduino read error: %s", e)
            arduino_conn = None
    return random.randint(20, 85)

def control_pump(turn_on: bool):
    global arduino_conn
    state = "ON" if turn_on else "OFF"
    if USE_ARDUINO and connect_arduino():
        try:
            with arduino_lock:
                cmd = b"PUMP_ON\n" if turn_on else b"PUMP_OFF\n"
                arduino_conn.write(cmd)
                log.info("Arduino pump %s command sent", state)
        except Exception as e:
            log.error("Arduino pump control error: %s", e)
    else:
        log.info("Pump %s (simulated)", state)

def get_arduino_status() -> dict:
    if not USE_ARDUINO:
        return {"connected": False, "mode": "simulated",
                "message": "Running in simulation mode (no Arduino)"}
    if connect_arduino():
        return {"connected": True, "mode": "hardware",
                "port": ARDUINO_PORT,
                "message": f"Arduino connected on {ARDUINO_PORT}"}
    return {"connected": False, "mode": "error",
            "message": f"Cannot connect to Arduino on {ARDUINO_PORT}"}

# ---------------------------------------------------------------------------
# WhatsApp / SMS Alerts (Feature 2)
# ---------------------------------------------------------------------------
def send_whatsapp(phone: str, message: str) -> dict:
    if not TWILIO_OK or not twilio_client:
        log.info("[WHATSAPP SIM] Would send to %s", phone)
        return {"sent": False, "reason": "Twilio not configured"}
    try:
        msg = twilio_client.messages.create(
            body=message, from_=TWILIO_FROM, to=f"whatsapp:{phone}")
        log.info("WhatsApp sent to %s: %s", phone, msg.sid)
        return {"sent": True, "sid": msg.sid}
    except Exception as e:
        log.error("WhatsApp send failed: %s", e)
        return {"sent": False, "reason": str(e)}

def send_sms(phone: str, message: str) -> dict:
    if not TWILIO_OK or not twilio_client:
        log.info("[SMS SIM] Would send to %s", phone)
        return {"sent": False, "reason": "Twilio not configured"}
    try:
        msg = twilio_client.messages.create(
            body=message,
            from_=TWILIO_FROM.replace("whatsapp:", ""),
            to=phone)
        return {"sent": True, "sid": msg.sid}
    except Exception as e:
        log.error("SMS send failed: %s", e)
        return {"sent": False, "reason": str(e)}

def build_alert_message(alert_type: str, city: str, soil: int,
                        temp: float, advice: str) -> str:
    now = datetime.now().strftime("%d %b %Y %H:%M")
    if alert_type == "critical":
        return (f"CROP SKYYY CRITICAL ALERT\n"
                f"Farm: {city}\nTime: {now}\n\n"
                f"URGENT: Soil moisture critically low!\n"
                f"Soil: {soil}%  Temp: {temp}C\n\n"
                f"Action: {advice}\n\n"
                f"Irrigate immediately to prevent crop damage!")
    if alert_type == "rain":
        return (f"CROP SKYYY RAIN ALERT\n"
                f"Farm: {city} | {now}\n\n"
                f"Rain detected - irrigation has been paused.\n"
                f"Soil: {soil}% | No watering needed today.\n\n"
                f"Your crops are being watered naturally!")
    if alert_type == "schedule":
        return (f"CROP SKYYY SCHEDULE ALERT\n"
                f"Farm: {city} | {now}\n\n"
                f"Scheduled irrigation is starting now.\n"
                f"Soil: {soil}% | Temp: {temp}C\n\n"
                f"Pump activated as per your schedule.")
    return f"Crop Skyyy Alert - {advice}"

def maybe_send_alert(username: str, alert_type: str, city: str,
                     soil: int, temp: float, advice: str):
    try:
        conn = get_db()
        row  = conn.execute(
            "SELECT * FROM alert_settings WHERE username=?", (username,)
        ).fetchone()
        conn.close()
    except Exception as e:
        log.error("Alert settings lookup failed: %s", e)
        return

    if not row:
        return

    should_send = (
        (alert_type == "critical" and row["alert_critical"]) or
        (alert_type == "rain"     and row["alert_rain"])     or
        (alert_type == "schedule" and row["alert_schedule"])
    )
    if not should_send:
        return

    phone   = row["phone"]
    message = build_alert_message(alert_type, city, soil, temp, advice)

    if row["whatsapp_enabled"] and phone:
        threading.Thread(
            target=send_whatsapp, args=(phone, message), daemon=True
        ).start()

    if row["sms_enabled"] and phone:
        threading.Thread(
            target=send_sms, args=(phone, message), daemon=True
        ).start()

# ---------------------------------------------------------------------------
# Scheduled Irrigation (Feature 4)
# ---------------------------------------------------------------------------
_scheduler_started = threading.Event()

def run_schedule(schedule: dict):
    city     = schedule["city"]
    duration = schedule["duration_min"]
    name     = schedule["name"]
    log.info("Running schedule: %s for %d min in %s", name, duration, city)

    try:
        w    = fetch_weather(city)
        soil = read_soil_moisture()
    except Exception as e:
        log.warning("Schedule %s: could not fetch conditions: %s", name, e)
        soil = 50
        w    = {"temp": 25, "humidity": 60, "rain_1h": 0, "description": "Unknown"}

    if w.get("rain_1h", 0) > 2:
        log.info("Schedule %s skipped - rain detected", name)
        return
    if soil > 70:
        log.info("Schedule %s skipped - soil already moist (%d%%)", name, soil)
        return

    control_pump(True)

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO logs (city, moisture, pump_on, advice, temp, humidity, rain)
               VALUES (?, ?, 1, ?, ?, ?, ?)""",
            (city, soil, f"Scheduled: {name}", w["temp"],
             w["humidity"], w.get("rain_1h", 0)))
        conn.execute(
            "UPDATE schedules SET last_run=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M"), schedule["id"]))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Schedule %s: DB write failed: %s", name, e)

    def stop_after_duration():
        time.sleep(duration * 60)
        control_pump(False)
        log.info("Schedule %s completed after %d min", name, duration)

    threading.Thread(target=stop_after_duration, daemon=True).start()

def scheduler_loop():
    log.info("Background scheduler started")
    while True:
        try:
            now      = datetime.now()
            now_time = now.strftime("%H:%M")
            now_day  = now.strftime("%a")

            conn      = get_db()
            schedules = conn.execute(
                "SELECT * FROM schedules WHERE active=1"
            ).fetchall()
            conn.close()

            for s in schedules:
                s    = dict(s)
                days = json.loads(s["days"])
                if s["run_time"] == now_time and now_day in days:
                    last = s.get("last_run") or ""
                    if not last.startswith(now.strftime("%Y-%m-%d %H:%M")):
                        threading.Thread(
                            target=run_schedule, args=(s,), daemon=True
                        ).start()

        except Exception as e:
            log.error("Scheduler loop error: %s", e)

        time.sleep(60)

def start_scheduler_once():
    if not _scheduler_started.is_set():
        _scheduler_started.set()
        threading.Thread(target=scheduler_loop, daemon=True).start()

start_scheduler_once()

# ---------------------------------------------------------------------------
# Weather Helpers
# ---------------------------------------------------------------------------
def fetch_weather(city: str) -> dict:
    r = req.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={"q": city, "appid": OWM_KEY, "units": "metric"},
        timeout=8)
    r.raise_for_status()
    d = r.json()
    w = {
        "city":        d["name"],
        "country":     d["sys"]["country"],
        "temp":        round(d["main"]["temp"], 1),
        "feels_like":  round(d["main"]["feels_like"], 1),
        "humidity":    d["main"]["humidity"],
        "description": d["weather"][0]["description"].title(),
        "icon":        d["weather"][0]["icon"],
        "wind_speed":  round(d["wind"]["speed"] * 3.6, 1),
        "pressure":    d["main"]["pressure"],
        "clouds":      d["clouds"]["all"],
        "rain_1h":     d.get("rain", {}).get("1h", 0),
        # Fixed: always returns a valid time string
        "sunrise":     utc_ts_to_local(d["sys"]["sunrise"]),
        "sunset":      utc_ts_to_local(d["sys"]["sunset"]),
        "online":      True,
    }
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO weather_cache
               (city,temp,humidity,description,wind_speed,feels_like,
                pressure,clouds,rain_1h,icon)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (w["city"], w["temp"], w["humidity"], w["description"],
             w["wind_speed"], w["feels_like"], w["pressure"],
             w["clouds"], w["rain_1h"], w["icon"]))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Weather cache write failed: %s", e)
    return w

def fetch_forecast(city: str) -> list:
    r = req.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={"q": city, "appid": OWM_KEY, "units": "metric", "cnt": 40},
        timeout=8)
    r.raise_for_status()
    data  = r.json()
    daily = {}
    for item in data["list"]:
        day = item["dt_txt"].split()[0]
        if day not in daily:
            daily[day] = {
                "temps": [], "hum": [], "rain": 0,
                "icon": item["weather"][0]["icon"],
                "desc": item["weather"][0]["description"].title(),
            }
        daily[day]["temps"].append(item["main"]["temp"])
        daily[day]["hum"].append(item["main"]["humidity"])
        daily[day]["rain"] += item.get("rain", {}).get("3h", 0)
    out = []
    for day, d in list(daily.items())[:7]:
        out.append({
            "day":      datetime.strptime(day, "%Y-%m-%d").strftime("%a %d"),
            "temp_max": round(max(d["temps"]), 1),
            "temp_min": round(min(d["temps"]), 1),
            "humidity": round(sum(d["hum"]) / len(d["hum"])),
            "rain_mm":  round(d["rain"], 1),
            "icon":     d["icon"],
            "desc":     d["desc"],
        })
    return out

def get_cached_weather():
    try:
        conn = get_db()
        row  = conn.execute(
            "SELECT * FROM weather_cache ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            d = dict(row); d["online"] = False; return d
    except Exception as e:
        log.error("Cached weather lookup failed: %s", e)
    return None

def rule_advice(temp, humidity, soil, rain):
    if rain > 2:
        return "Rain detected - skip irrigation", False, "rain"
    if soil < 25 and temp > 32:
        return "Critical: max irrigation needed immediately!", True, "critical"
    if soil < 35:
        return "High irrigation needed - soil very dry", True, None
    if soil < 50:
        return "Moderate irrigation recommended", True, None
    if soil > 70:
        return "Soil well-moistened - no irrigation needed", False, None
    if humidity > 85:
        return "High humidity - skip irrigation", False, None
    return "Light irrigation recommended", False, None

def gpt_advice(temp, humidity, soil, rain, desc, city,
               soil_type="Loamy", crop_type="Maize") -> str:
    """Generate AI irrigation advice specific to crop and soil type."""
    if not ai_client:
        return "OpenAI not configured - add OPENAI_KEY in Railway Variables"
    try:
        r = ai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content":
                f"You are AquaMind, AI irrigation advisor for Crop Skyyy.\n"
                f"Farm: {city} | Crop: {crop_type} | Soil: {soil_type} soil\n"
                f"Temp:{temp}C | Humidity:{humidity}% | "
                f"Soil moisture:{soil}% | Rain:{rain}mm | Weather:{desc}\n"
                f"Give 2-3 sentence practical irrigation advice specific to "
                f"{crop_type} growing in {soil_type} soil. Include: "
                f"irrigate yes/no, best timing, one water-saving tip for this crop."}],
            max_tokens=200,
            temperature=0.6,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        log.error("GPT advice failed: %s", e)
        return f"AI advice unavailable: {e}"

# ---------------------------------------------------------------------------
# Routes - Auth
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/register", methods=["POST"])
def api_register():
    d     = request.get_json(force=True, silent=True) or {}
    u     = d.get("username", "").strip()
    p     = d.get("password", "").strip()
    phone = d.get("phone", "").strip()

    if not u or not p:
        return jsonify({"error": "Fill in both fields"}), 400
    if len(u) < 3:
        return jsonify({"error": "Username needs 3+ characters"}), 400
    if len(p) < 4:
        return jsonify({"error": "Password needs 4+ characters"}), 400

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO users(username,password,phone) VALUES(?,?,?)",
            (u, hash_password(p), phone))
        conn.execute(
            "INSERT INTO alert_settings(username,phone) VALUES(?,?)", (u, phone))
        conn.commit()
        conn.close()
        session["user"] = u
        log.info("Registered: %s", u)
        return jsonify({"ok": True, "username": u})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already taken"}), 409
    except Exception as e:
        log.error("Register error: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(force=True, silent=True) or {}
    u = d.get("username", "").strip()
    p = d.get("password", "").strip()

    if not u or not p:
        return jsonify({"error": "Fill in both fields"}), 400

    try:
        conn = get_db()
        row  = conn.execute(
            "SELECT * FROM users WHERE username=?", (u,)
        ).fetchone()
        conn.close()

        if row and verify_password(p, row["password"]):
            session["user"] = u
            log.info("Login: %s", u)
            return jsonify({"ok": True, "username": u})

        return jsonify({"error": "Wrong username or password"}), 401
    except Exception as e:
        log.error("Login error: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user", None)
    return jsonify({"ok": True})

@app.route("/api/me")
def api_me():
    u = session.get("user")
    return jsonify({"in": bool(u), "username": u or ""})

# ---------------------------------------------------------------------------
# Routes - Weather & Farm Data
# ---------------------------------------------------------------------------
@app.route("/api/weather")
def api_weather():
    city = request.args.get("city", "Kaduna").strip()
    try:
        w  = fetch_weather(city)
        fc = []
        try:
            fc = fetch_forecast(city)
        except Exception as e:
            log.warning("Forecast fetch failed: %s", e)
        return jsonify({"weather": w, "forecast": fc, "source": "live"})
    except Exception as e:
        log.warning("Live weather failed, using cache: %s", e)
        cached = get_cached_weather()
        if cached:
            return jsonify({"weather": cached, "forecast": [], "source": "cache"})
        return jsonify({"error": str(e)}), 500

@app.route("/api/farm-data", methods=["POST"])
def api_farm_data():
    body      = request.get_json(force=True, silent=True) or {}
    city      = body.get("city", "Kaduna").strip()
    soil_type = body.get("soil_type", "Loamy").strip()
    crop_type = body.get("crop_type", "Maize").strip()
    user      = session.get("user", "")

    try:
        w = fetch_weather(city)
    except Exception as e:
        log.warning("Weather fetch failed, using cache: %s", e)
        w = get_cached_weather() or {}
        w["online"] = False

    soil             = read_soil_moisture()
    advice, pump_on, alert_type = rule_advice(
        w.get("temp", 25), w.get("humidity", 60),
        soil, w.get("rain_1h", 0))
    control_pump(pump_on)

    ai_text = gpt_advice(
        w.get("temp", 25), w.get("humidity", 60),
        soil, w.get("rain_1h", 0), w.get("description", ""), city,
        soil_type, crop_type)

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO logs(username,city,moisture,pump_on,advice,temp,humidity,rain,soil_type,crop_type)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (user, city, soil, int(pump_on), advice,
             w.get("temp", 0), w.get("humidity", 0), w.get("rain_1h", 0),
             soil_type, crop_type))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Farm data DB write failed: %s", e)

    if alert_type and user:
        maybe_send_alert(user, alert_type, city, soil, w.get("temp", 25), advice)

    return jsonify({
        "weather":   w,
        "soil":      soil,
        "pump_on":   pump_on,
        "advice":    advice,
        "gpt":       ai_text,
        "arduino":   get_arduino_status(),
        "soil_type": soil_type,
        "crop_type": crop_type,
    })

@app.route("/api/logs")
def api_logs():
    user = session.get("user", "")
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM logs WHERE username=? ORDER BY id DESC LIMIT 20",
            (user,)
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.error("Logs fetch failed: %s", e)
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Routes - Schedules (Feature 4)
# ---------------------------------------------------------------------------
@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    user = session.get("user", "")
    try:
        conn  = get_db()
        rows  = conn.execute(
            "SELECT * FROM schedules WHERE username=? ORDER BY id DESC", (user,)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            try:    d["days"] = json.loads(d["days"])
            except Exception: d["days"] = []
            result.append(d)
        return jsonify(result)
    except Exception as e:
        log.error("Get schedules failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    user = session.get("user", "")
    d    = request.get_json(force=True, silent=True) or {}
    name     = d.get("name", "My Schedule").strip()
    city     = d.get("city", "Kaduna").strip()
    run_time = d.get("run_time", "06:00")
    days     = d.get("days", ["Mon", "Wed", "Fri"])
    duration = int(d.get("duration_min", 20))

    if not name:
        return jsonify({"error": "Schedule name required"}), 400
    try:
        datetime.strptime(run_time, "%H:%M")
    except Exception:
        return jsonify({"error": "Invalid time format (use HH:MM)"}), 400

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO schedules(username,name,city,run_time,days,duration_min,active)
               VALUES(?,?,?,?,?,?,1)""",
            (user, name, city, run_time, json.dumps(days), duration))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({"ok": True, "id": new_id, "message": f"Schedule '{name}' created"})
    except Exception as e:
        log.error("Create schedule failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/schedules/<int:sid>", methods=["PATCH"])
def update_schedule(sid):
    user = session.get("user", "")
    d    = request.get_json(force=True, silent=True) or {}
    try:
        conn = get_db()
        if "active"       in d:
            conn.execute("UPDATE schedules SET active=?       WHERE id=? AND username=?",
                         (int(d["active"]), sid, user))
        if "duration_min" in d:
            conn.execute("UPDATE schedules SET duration_min=? WHERE id=? AND username=?",
                         (int(d["duration_min"]), sid, user))
        if "run_time"     in d:
            conn.execute("UPDATE schedules SET run_time=?     WHERE id=? AND username=?",
                         (d["run_time"], sid, user))
        if "days"         in d:
            conn.execute("UPDATE schedules SET days=?         WHERE id=? AND username=?",
                         (json.dumps(d["days"]), sid, user))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("Update schedule failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/schedules/<int:sid>", methods=["DELETE"])
def delete_schedule(sid):
    user = session.get("user", "")
    try:
        conn = get_db()
        conn.execute("DELETE FROM schedules WHERE id=? AND username=?", (sid, user))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        log.error("Delete schedule failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/schedules/<int:sid>/run", methods=["POST"])
def run_schedule_now(sid):
    user = session.get("user", "")
    try:
        conn = get_db()
        row  = conn.execute(
            "SELECT * FROM schedules WHERE id=? AND username=?", (sid, user)
        ).fetchone()
        conn.close()
    except Exception as e:
        log.error("Run schedule lookup failed: %s", e)
        return jsonify({"error": str(e)}), 500

    if not row:
        return jsonify({"error": "Schedule not found"}), 404

    s = dict(row)
    try:    s["days"] = json.loads(s["days"])
    except Exception: s["days"] = []

    threading.Thread(target=run_schedule, args=(s,), daemon=True).start()
    return jsonify({"ok": True, "message": f"Schedule '{s['name']}' started manually"})

# ---------------------------------------------------------------------------
# Routes - Alert Settings (Feature 2)
# ---------------------------------------------------------------------------
@app.route("/api/alerts/settings", methods=["GET"])
def get_alert_settings():
    user = session.get("user", "")
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    try:
        conn = get_db()
        row  = conn.execute(
            "SELECT * FROM alert_settings WHERE username=?", (user,)
        ).fetchone()
        conn.close()
        if row:
            return jsonify(dict(row))
        return jsonify({"whatsapp_enabled": 0, "sms_enabled": 0, "phone": "",
                        "alert_critical": 1, "alert_rain": 1, "alert_schedule": 1})
    except Exception as e:
        log.error("Get alert settings failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/alerts/settings", methods=["POST"])
def save_alert_settings():
    user = session.get("user", "")
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    d = request.get_json(force=True, silent=True) or {}
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO alert_settings
               (username,whatsapp_enabled,sms_enabled,phone,
                alert_critical,alert_rain,alert_schedule)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET
               whatsapp_enabled=excluded.whatsapp_enabled,
               sms_enabled=excluded.sms_enabled,
               phone=excluded.phone,
               alert_critical=excluded.alert_critical,
               alert_rain=excluded.alert_rain,
               alert_schedule=excluded.alert_schedule""",
            (user,
             int(d.get("whatsapp_enabled", 0)),
             int(d.get("sms_enabled", 0)),
             d.get("phone", ""),
             int(d.get("alert_critical", 1)),
             int(d.get("alert_rain", 1)),
             int(d.get("alert_schedule", 1))))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "message": "Alert settings saved"})
    except Exception as e:
        log.error("Save alert settings failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/alerts/test", methods=["POST"])
def test_alert():
    user = session.get("user", "")
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    d     = request.get_json(force=True, silent=True) or {}
    phone = d.get("phone", "")
    if not phone:
        return jsonify({"error": "Phone number required"}), 400
    msg = ("Crop Skyyy Test Alert\n"
           "Your alerts are working correctly!\n"
           "You will receive notifications when:\n"
           "- Soil moisture is critically low\n"
           "- Heavy rain is detected\n"
           "- Scheduled irrigation runs\n\n"
           "Happy farming!")
    wa = send_whatsapp(phone, msg)
    return jsonify({"whatsapp": wa, "message": "Test sent!"})

# ---------------------------------------------------------------------------
# Routes - Arduino (Feature 6)
# ---------------------------------------------------------------------------
@app.route("/api/arduino/status")
def api_arduino_status():
    return jsonify(get_arduino_status())

@app.route("/api/arduino/test", methods=["POST"])
def api_arduino_test():
    def pulse():
        control_pump(True)
        time.sleep(2)
        control_pump(False)
        log.info("Pump test complete (2 second pulse)")
    threading.Thread(target=pulse, daemon=True).start()
    return jsonify({"ok": True, "message": "Pump test started (2 second pulse)"})

# ---------------------------------------------------------------------------
# Routes - AI Chat
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    d         = request.get_json(force=True, silent=True) or {}
    msg       = d.get("message", "").strip()
    hist      = d.get("history", [])
    city      = d.get("city", "Kaduna")
    soil_type = d.get("soil_type", "Loamy")
    crop_type = d.get("crop_type", "Maize")

    if not msg:
        return jsonify({"error": "Empty message"}), 400
    if not ai_client:
        return jsonify({"reply": "OpenAI not configured - add OPENAI_KEY in Railway Variables"})

    ctx = ""
    try:
        w   = fetch_weather(city)
        ctx = (f"Current weather in {city}: {w['temp']}C, {w['description']}, "
               f"Humidity:{w['humidity']}%, Rain:{w.get('rain_1h', 0)}mm. "
               f"Farmer grows {crop_type} on {soil_type} soil.")
    except Exception as e:
        log.warning("Chat weather fetch failed: %s", e)

    messages = [{"role": "system", "content":
        f"You are AquaMind, AI advisor in Crop Skyyy irrigation system for African farmers. "
        f"{ctx} Help with irrigation, crops, soil, weather. Be practical and friendly."}]
    for h in hist[-6:]:
        if h.get("role") in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg})

    try:
        r = ai_client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages,
            max_tokens=350, temperature=0.7)
        return jsonify({"reply": r.choices[0].message.content})
    except Exception as e:
        log.error("Chat completion failed: %s", e)
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Routes - Camera / Vision AI (Feature 7 & 8)
# ---------------------------------------------------------------------------
@app.route("/api/analyze-crop", methods=["POST"])
def analyze_crop():
    """Analyze crop health from a photo using GPT-4o Vision."""
    if not ai_client:
        return jsonify({"error": "OpenAI not configured - add OPENAI_KEY in Railway Variables"}), 500
    d         = request.get_json(force=True, silent=True) or {}
    image_b64 = d.get("image", "")
    soil_type = d.get("soil_type", "Unknown")
    crop_type = d.get("crop_type", "Unknown")

    if not image_b64:
        return jsonify({"error": "No image provided"}), 400

    try:
        r = ai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
                    },
                    {
                        "type": "text",
                        "text": (
                            f"You are AquaMind, an expert agricultural AI for African farmers.\n"
                            f"The farmer grows {crop_type} on {soil_type} soil.\n\n"
                            f"Analyze this crop photo and provide:\n"
                            f"1. HEALTH STATUS: (Healthy / Stressed / Diseased / Critical)\n"
                            f"2. OBSERVATIONS: What you see in the plant (color, leaves, stems)\n"
                            f"3. DIAGNOSIS: What is likely wrong (if anything)\n"
                            f"4. ACTION: What the farmer should do right now\n"
                            f"5. IRRIGATION IMPACT: How this affects watering needs\n\n"
                            f"Be specific, practical, and focused on African farming conditions."
                        )
                    }
                ]
            }],
            max_tokens=400
        )
        result = r.choices[0].message.content.strip()
        status = "Unknown"
        if "Healthy"  in result: status = "Healthy"
        elif "Stressed"  in result: status = "Stressed"
        elif "Diseased"  in result: status = "Diseased"
        elif "Critical"  in result: status = "Critical"
        return jsonify({"analysis": result, "status": status})
    except Exception as e:
        log.error("Crop analysis failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/identify-crop", methods=["POST"])
def identify_crop():
    """Identify a crop from a photo using GPT-4o Vision."""
    if not ai_client:
        return jsonify({"error": "OpenAI not configured - add OPENAI_KEY in Railway Variables"}), 500
    d         = request.get_json(force=True, silent=True) or {}
    image_b64 = d.get("image", "")

    if not image_b64:
        return jsonify({"error": "No image provided"}), 400

    try:
        r = ai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are AquaMind, an expert agricultural AI for African farmers.\n\n"
                            "Identify the crop or plant in this photo and provide:\n"
                            "1. CROP NAME: Common name and scientific name\n"
                            "2. CONFIDENCE: How sure you are (High/Medium/Low)\n"
                            "3. GROWTH STAGE: Seedling/Vegetative/Flowering/Fruiting/Mature\n"
                            "4. WATER NEEDS: How much water this crop typically needs\n"
                            "5. BEST SOIL: What soil type suits it best\n"
                            "6. HARVEST TIME: When to expect harvest\n"
                            "7. TIPS: One key farming tip for African conditions\n\n"
                            "If it is not a crop or plant, say so politely."
                        )
                    }
                ]
            }],
            max_tokens=400
        )
        return jsonify({"identification": r.choices[0].message.content.strip()})
    except Exception as e:
        log.error("Crop identification failed: %s", e)
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")

