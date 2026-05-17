"""
CROP SKYYY v3 - AI Farm Irrigation System
Refactored for security, thread-safety, and production-readiness.

Features:
  1. Mobile-friendly UI
  2. WhatsApp / SMS Alerts (Twilio)
  4. Scheduled Irrigation
  6. Real Arduino Sensor Support
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
from flask import Flask, jsonify, request, session

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

# --- API Keys (unchanged from original) ---
OWM_KEY    = "2f125e80f1a3689c2397593b7ad989a0"
OPENAI_KEY = "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"

# --- Twilio WhatsApp / SMS (sign up free at twilio.com) ---
TWILIO_SID   = "YOUR_TWILIO_ACCOUNT_SID"
TWILIO_TOKEN = "YOUR_TWILIO_AUTH_TOKEN"
TWILIO_FROM  = "whatsapp:+14155238886"
ALERT_PHONE  = "+2348012345678"

# --- Arduino ---
ARDUINO_PORT = "COM3"
ARDUINO_BAUD = 9600
USE_ARDUINO  = False   # Set True when Arduino is physically connected

# --- Environment variable overrides (recommended for production) ---
# Set SECRET_KEY in your environment: set SECRET_KEY=your_random_secret
# Set OPENAI_MODEL in your environment: set OPENAI_MODEL=gpt-4o
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
    """
    Return a thread-safe SQLite connection.
    check_same_thread=False is safe here because we use explicit conn.close()
    and never share connection objects between threads.
    WAL mode reduces locking contention from background threads.
    """
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s instead of failing instantly
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # Users
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        phone TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # Sensor + weather logs - includes username for data isolation
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT DEFAULT '',
        city TEXT, moisture INTEGER, pump_on INTEGER,
        advice TEXT, temp REAL, humidity INTEGER,
        rain REAL DEFAULT 0, alert_sent INTEGER DEFAULT 0,
        logged_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # Weather cache
    c.execute("""CREATE TABLE IF NOT EXISTS weather_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city TEXT, temp REAL, humidity INTEGER,
        description TEXT, wind_speed REAL, feels_like REAL,
        pressure INTEGER, clouds INTEGER, rain_1h REAL DEFAULT 0,
        icon TEXT, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # Scheduled irrigation - includes username for data isolation
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

    # Alert settings per user
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
    """Hash password using Werkzeug (bcrypt-style salted hash) if available,
    otherwise fall back to sha256. New registrations always use Werkzeug."""
    if WERKZEUG_OK:
        return generate_password_hash(pw)
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()

def verify_password(pw: str, stored_hash: str) -> bool:
    """Verify password against stored hash. Handles both Werkzeug hashes
    and legacy sha256 hashes transparently."""
    if WERKZEUG_OK:
        # Werkzeug hashes start with 'pbkdf2:' or 'scrypt:'
        if stored_hash.startswith("pbkdf2:") or stored_hash.startswith("scrypt:"):
            return check_password_hash(stored_hash, pw)
        # Legacy sha256 hash - compare directly
        import hashlib
        return hashlib.sha256(pw.encode()).hexdigest() == stored_hash
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest() == stored_hash

# ---------------------------------------------------------------------------
# Timezone Helper
# ---------------------------------------------------------------------------
def utc_ts_to_local(timestamp: int) -> str:
    """Convert a UTC Unix timestamp to local time string (Africa/Lagos)."""
    dt_utc = datetime.utcfromtimestamp(timestamp)
    if PYTZ_OK:
        try:
            tz    = pytz.timezone(TIMEZONE)
            dt_utc = pytz.utc.localize(dt_utc)
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
    """
    Read soil moisture from Arduino or simulate it.

    Arduino sketch should send a number (0-100) followed by newline:
        int sensorPin = A0;
        void setup() { Serial.begin(9600); }
        void loop() {
            int raw = analogRead(sensorPin);
            int pct = map(raw, 1023, 300, 0, 100);
            pct = constrain(pct, 0, 100);
            Serial.println(pct);
            delay(2000);
        }
    """
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
    """
    Send ON/OFF command to Arduino pump relay.

    Arduino should listen for 'PUMP_ON\n' or 'PUMP_OFF\n':
        if (Serial.available()) {
            String cmd = Serial.readStringUntil('\n');
            if (cmd == "PUMP_ON")  digitalWrite(relayPin, HIGH);
            if (cmd == "PUMP_OFF") digitalWrite(relayPin, LOW);
        }
    """
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
_scheduler_started = threading.Event()   # prevents duplicate scheduler threads

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
    """
    Background scheduler - checks active schedules every minute.
    The _scheduler_started Event ensures only ONE instance ever runs,
    even if the module is imported multiple times (e.g. with gunicorn --preload).
    """
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
    """Start the scheduler thread only if it has not been started yet."""
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
        # Sunrise/sunset converted to Nigeria local time (Africa/Lagos)
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

def get_cached_weather() -> dict | None:
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

def gpt_advice(temp, humidity, soil, rain, desc, city) -> str:
    if not ai_client:
        return "Install OpenAI: pip install openai"
    try:
        r = ai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content":
                f"You are AquaMind, AI irrigation advisor for Crop Skyyy.\n"
                f"Farm: {city} | Temp:{temp}C | Humidity:{humidity}% | "
                f"Soil:{soil}% | Rain:{rain}mm | Weather:{desc}\n"
                f"Give 2-3 sentence practical irrigation advice. Include: "
                f"irrigate yes/no, best timing, one water-saving tip."}],
            max_tokens=180,
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
    return HTML_PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="theme-color" content="#1a1005"/>
<title>Crop Skyyy 🌱</title>
<link rel="manifest" href="/manifest.json"/>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Nunito:wght@300;400;600;700&display=swap" rel="stylesheet"/>
<style>
/* -
   CROP SKYYY v3 — Mobile-First Design
- */
:root {
  --earth:#1a1005;--brown:#2d1a08;--clay:#4a2e10;
  --gold:#c8882a;--lgold:#e8b84b;
  --green:#3a7537;--lgreen:#5cb85c;--leaf:#8fd68c;
  --sky:#5b9ecf;--rain:#7ab8e8;--red:#c0392b;
  --cream:#f4ead8;--sand:#d4b483;--grey:#7a6248;
  --text:#ede0c8;--card:rgba(45,26,8,0.88);--bdr:rgba(200,136,42,0.22);
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-bot: env(safe-area-inset-bottom, 0px);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{
  font-family:Nunito,sans-serif;background:var(--earth);color:var(--text);
  min-height:100vh;overflow-x:hidden;
  background-image:
    radial-gradient(ellipse 70% 50% at 15% 5%,rgba(58,117,55,0.3) 0%,transparent 55%),
    radial-gradient(ellipse 50% 70% at 85% 90%,rgba(74,46,16,0.5) 0%,transparent 50%);
  padding-top:var(--safe-top);
  padding-bottom:calc(var(--safe-bot) + 70px); /* space for bottom nav */
}

/* - AUTH SCREEN - */
#authScreen{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}
.abox{
  background:var(--card);backdrop-filter:blur(20px);
  border:1px solid var(--bdr);border-radius:24px;
  padding:2.5rem 2rem;width:100%;max-width:400px;
  box-shadow:0 40px 80px rgba(0,0,0,0.6);animation:up .4s ease both
}
.alogo{text-align:center;margin-bottom:2rem}
.alogo .ic{font-size:3rem;display:block;margin-bottom:.5rem}
.alogo h1{font-family:"Playfair Display",serif;font-size:2rem;color:var(--lgold)}
.alogo p{font-size:.78rem;color:var(--grey)}
.tabs{display:flex;gap:.4rem;margin-bottom:1.5rem}
.tab{flex:1;padding:.55rem;border-radius:10px;border:1px solid var(--bdr);
  background:transparent;color:var(--grey);font-family:inherit;font-size:.88rem;cursor:pointer;transition:.2s}
.tab.on{background:rgba(200,136,42,.15);border-color:var(--gold);color:var(--lgold);font-weight:700}
.field{margin-bottom:.9rem}
.field label{font-size:.7rem;color:var(--grey);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.35rem}
.field input{
  width:100%;padding:.85rem 1rem;border-radius:12px;
  background:rgba(0,0,0,.4);border:1px solid var(--bdr);
  color:var(--text);font-family:inherit;font-size:1rem;outline:none;
  transition:.2s;-webkit-appearance:none;
}
.field input:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(200,136,42,.12)}
.field input::placeholder{color:var(--grey)}
.aerr{background:rgba(192,57,43,.12);border:1px solid rgba(192,57,43,.3);border-radius:10px;
  padding:.6rem .9rem;color:#e07060;font-size:.82rem;margin-bottom:.9rem;display:none}
.asuc{background:rgba(92,184,92,.12);border:1px solid rgba(92,184,92,.3);border-radius:10px;
  padding:.6rem .9rem;color:var(--leaf);font-size:.82rem;margin-bottom:.9rem;display:none}
.abtn{
  width:100%;padding:.9rem;border-radius:12px;
  background:linear-gradient(135deg,var(--green),var(--lgreen));
  border:none;color:white;font-family:inherit;font-size:1rem;
  font-weight:700;cursor:pointer;transition:.2s;-webkit-tap-highlight-color:transparent;
}
.abtn:active{transform:scale(0.98)}

/* - DASHBOARD - */
#dashScreen{display:none}
.wrap{max-width:1280px;margin:0 auto;padding:0 1rem 1rem}

/* - Top Header - */
.topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:1rem 0 .8rem;position:sticky;top:0;z-index:100;
  background:linear-gradient(180deg,var(--earth) 80%,transparent);
  margin-bottom:.5rem;
}
.logo{display:flex;align-items:center;gap:.6rem}
.lmark{width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,var(--green),var(--lgreen));
  display:grid;place-items:center;font-size:1.1rem;
  box-shadow:0 0 14px rgba(92,184,92,.3)}
.ltxt h2{font-family:"Playfair Display",serif;font-size:1.2rem;color:var(--lgold)}
.ltxt span{font-size:.62rem;color:var(--grey)}
.user-chip{display:flex;align-items:center;gap:.4rem;padding:.35rem .8rem;
  background:rgba(200,136,42,.1);border:1px solid var(--bdr);border-radius:100px;font-size:.75rem;color:var(--sand)}

/* - Bottom Navigation (Mobile) - */
.bottom-nav{
  position:fixed;bottom:0;left:0;right:0;z-index:200;
  background:rgba(26,16,5,0.95);backdrop-filter:blur(20px);
  border-top:1px solid var(--bdr);
  display:flex;align-items:center;justify-content:space-around;
  padding:.5rem .5rem calc(.5rem + var(--safe-bot));
}
.bnav-item{
  display:flex;flex-direction:column;align-items:center;gap:.2rem;
  padding:.4rem .8rem;border-radius:12px;cursor:pointer;
  transition:.2s;border:none;background:transparent;color:var(--grey);
  font-family:inherit;font-size:.6rem;font-weight:600;
  -webkit-tap-highlight-color:transparent;
}
.bnav-item.active{color:var(--lgreen);background:rgba(92,184,92,.1)}
.bnav-item .icon{font-size:1.3rem}

/* - Pages - */
.page{display:none;animation:up .3s ease both}
.page.active{display:block}

/* - Cards - */
.card{background:var(--card);border:1px solid var(--bdr);border-radius:18px;padding:1.2rem;backdrop-filter:blur(12px);margin-bottom:1rem}
.clbl{font-size:.62rem;letter-spacing:.13em;text-transform:uppercase;color:var(--grey);font-weight:700;margin-bottom:.8rem}

/* - Grid - */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:.85rem}
@media(min-width:700px){.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem}}

/* - City Bar - */
.cbar{
  display:flex;align-items:center;gap:.6rem;
  background:var(--card);border:1px solid var(--bdr);border-radius:14px;
  padding:.75rem 1rem;margin-bottom:1rem;
}
.cbar input{
  flex:1;background:rgba(0,0,0,.25);border:1px solid var(--bdr);
  color:var(--text);padding:.45rem .8rem;border-radius:100px;
  font-family:inherit;font-size:.9rem;outline:none;
  -webkit-appearance:none;
}
.cbar input:focus{border-color:var(--gold)}

/* - Buttons - */
.btn{
  cursor:pointer;border:none;font-family:inherit;border-radius:100px;font-weight:600;
  transition:.2s;display:inline-flex;align-items:center;justify-content:center;gap:.4rem;
  -webkit-tap-highlight-color:transparent;white-space:nowrap;
}
.btn:active{transform:scale(0.96)}
.bg{background:linear-gradient(135deg,var(--green),var(--lgreen));color:white;padding:.5rem 1rem;font-size:.82rem}
.bg:hover{box-shadow:0 4px 14px rgba(92,184,92,.3)}
.bgh{background:rgba(200,136,42,.08);border:1px solid var(--bdr);color:var(--sand);padding:.45rem .9rem;font-size:.8rem}
.bdr2{background:rgba(192,57,43,.12);border:1px solid rgba(192,57,43,.3);color:#e07060;padding:.4rem .8rem;font-size:.78rem}
.bfull{width:100%;padding:.8rem;font-size:.92rem}

/* - Weather Card - */
.wtemp{font-family:"Playfair Display",serif;font-size:3.5rem;color:var(--cream);line-height:1}
.wtop{display:flex;justify-content:space-between;align-items:flex-start}
.wico{width:64px;height:64px}
.wdesc{color:var(--sand);font-size:.88rem;margin-top:.2rem}
.wloc{font-size:.72rem;color:var(--grey);margin-top:.1rem}
.wstats{display:grid;grid-template-columns:repeat(3,1fr);gap:.4rem;margin-top:.85rem}
.ws{background:rgba(0,0,0,.22);border-radius:10px;padding:.6rem}
.wsl{font-size:.56rem;color:var(--grey);text-transform:uppercase;letter-spacing:.08em}
.wsv{font-size:.9rem;font-weight:700;color:var(--cream);margin-top:.1rem}

/* - Soil Circle - */
.scw{display:flex;justify-content:center;margin:.4rem 0 .85rem}
.sc{width:120px;height:120px;border-radius:50%;display:grid;place-items:center;
  background:conic-gradient(var(--lgreen) var(--p,0deg),rgba(0,0,0,.3) 0deg);
  box-shadow:0 0 20px rgba(92,184,92,.18)}
.sci{width:84px;height:84px;border-radius:50%;background:var(--brown);display:grid;place-items:center;text-align:center}
.spct{font-family:"Playfair Display",serif;font-size:1.5rem;color:var(--leaf)}
.ssub{font-size:.57rem;color:var(--grey)}
.pbadge{display:inline-flex;align-items:center;gap:.4rem;padding:.38rem .85rem;
  border-radius:100px;font-size:.78rem;font-weight:700;margin-top:.4rem}
.pon{background:rgba(92,184,92,.15);border:1px solid rgba(92,184,92,.35);color:var(--leaf)}
.poff{background:rgba(122,98,72,.18);border:1px solid rgba(122,98,72,.3);color:var(--grey)}
.pdot{width:6px;height:6px;border-radius:50%}
.pon .pdot{background:var(--lgreen);animation:blink 1.2s infinite}
.poff .pdot{background:var(--grey)}

/* - Advice - */
.arule{background:rgba(92,184,92,.08);border:1px solid rgba(92,184,92,.2);
  border-radius:12px;padding:.85rem;font-size:.87rem;line-height:1.6;
  color:var(--leaf);font-weight:600;margin-bottom:.7rem}
.agpt{background:rgba(0,0,0,.2);border-radius:12px;padding:.85rem;
  font-size:.82rem;line-height:1.7;color:var(--text);white-space:pre-wrap;min-height:70px}

/* - Forecast - */
.fscroll{display:flex;gap:.5rem;overflow-x:auto;padding-bottom:.4rem;-webkit-overflow-scrolling:touch}
.fscroll::-webkit-scrollbar{height:3px}
.fscroll::-webkit-scrollbar-thumb{background:var(--clay);border-radius:2px}
.fday{flex-shrink:0;width:82px;text-align:center;background:rgba(0,0,0,.2);
  border:1px solid var(--bdr);border-radius:13px;padding:.7rem .3rem}
.fdn{font-size:.6rem;text-transform:uppercase;letter-spacing:.09em;color:var(--grey)}
.fdi{font-size:1.3rem;margin:.28rem 0}
.fdt{font-size:.76rem;color:var(--cream)}
.fdr{font-size:.6rem;color:var(--rain);margin-top:.1rem}

/* - Arduino Status - */
.arduino-badge{display:flex;align-items:center;gap:.5rem;padding:.5rem .8rem;border-radius:10px;font-size:.8rem}
.arduino-ok{background:rgba(92,184,92,.12);border:1px solid rgba(92,184,92,.25);color:var(--leaf)}
.arduino-sim{background:rgba(122,98,72,.15);border:1px solid rgba(122,98,72,.25);color:var(--grey)}
.arduino-err{background:rgba(192,57,43,.12);border:1px solid rgba(192,57,43,.25);color:#e07060}

/* - SCHEDULES PAGE - */
.sched-card{
  background:var(--card);border:1px solid var(--bdr);border-radius:16px;
  padding:1rem;margin-bottom:.75rem;
}
.sched-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem}
.sched-name{font-weight:700;font-size:.95rem;color:var(--cream)}
.sched-meta{font-size:.75rem;color:var(--grey);margin-top:.2rem}
.sched-actions{display:flex;gap:.4rem;margin-top:.7rem;flex-wrap:wrap}
.toggle-switch{position:relative;width:44px;height:24px;cursor:pointer}
.toggle-switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:var(--grey);border-radius:100px;transition:.3s}
.slider::before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;
  background:white;border-radius:50%;transition:.3s}
.toggle-switch input:checked+.slider{background:var(--lgreen)}
.toggle-switch input:checked+.slider::before{left:23px}

/* - Add Schedule Form - */
.form-row{margin-bottom:.85rem}
.form-row label{font-size:.7rem;color:var(--grey);text-transform:uppercase;letter-spacing:.1em;display:block;margin-bottom:.35rem}
.form-row input,.form-row select{
  width:100%;padding:.75rem .9rem;border-radius:12px;
  background:rgba(0,0,0,.35);border:1px solid var(--bdr);
  color:var(--text);font-family:inherit;font-size:.9rem;outline:none;
  -webkit-appearance:none;
}
.form-row input:focus,.form-row select:focus{border-color:var(--gold)}
.form-row select option{background:var(--brown)}
.day-picker{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.4rem}
.day-btn{
  padding:.35rem .7rem;border-radius:8px;font-size:.75rem;font-weight:600;
  border:1px solid var(--bdr);background:transparent;color:var(--grey);
  cursor:pointer;transition:.2s;font-family:inherit;
}
.day-btn.sel{background:rgba(92,184,92,.15);border-color:var(--lgreen);color:var(--leaf)}

/* - ALERTS PAGE - */
.alert-toggle-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:.85rem 0;border-bottom:1px solid rgba(200,136,42,.1);
}
.alert-toggle-row:last-child{border-bottom:none}
.alt-info h4{font-size:.9rem;color:var(--cream);font-weight:600}
.alt-info p{font-size:.75rem;color:var(--grey);margin-top:.15rem}

/* - CHAT PAGE - */
.chat-wrap{display:flex;flex-direction:column;height:calc(100vh - 180px)}
.cmsgs{
  flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:.6rem;
  padding:.5rem 0;scrollbar-width:thin;scrollbar-color:var(--clay) transparent;
  -webkit-overflow-scrolling:touch;
}
.cmsgs::-webkit-scrollbar{width:3px}
.cmsgs::-webkit-scrollbar-thumb{background:var(--clay);border-radius:3px}
.msg{display:flex;gap:.5rem;max-width:88%}
.msg.u{align-self:flex-end;flex-direction:row-reverse}
.mav{width:28px;height:28px;border-radius:9px;display:grid;place-items:center;font-size:.78rem;flex-shrink:0}
.msg.a .mav{background:linear-gradient(135deg,var(--green),var(--lgreen))}
.msg.u .mav{background:rgba(200,136,42,.15);border:1px solid var(--bdr)}
.mbub{padding:.6rem .85rem;border-radius:13px;font-size:.83rem;line-height:1.65}
.msg.a .mbub{background:rgba(58,117,55,.22);border:1px solid rgba(92,184,92,.14);color:var(--text);border-top-left-radius:4px}
.msg.u .mbub{background:rgba(200,136,42,.11);border:1px solid rgba(200,136,42,.2);color:var(--cream);border-top-right-radius:4px}
.crow{
  display:flex;gap:.5rem;padding:.8rem 0;
  position:sticky;bottom:0;background:var(--earth);
  border-top:1px solid var(--bdr);margin-top:.5rem;
}
.crow input{
  flex:1;background:rgba(0,0,0,.3);border:1px solid var(--bdr);
  color:var(--text);padding:.7rem .9rem;border-radius:100px;
  font-family:inherit;font-size:.88rem;outline:none;
  -webkit-appearance:none;
}
.crow input:focus{border-color:var(--gold)}
.crow input::placeholder{color:var(--grey)}
.qbtns{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.6rem}
.qb{
  font-size:.7rem;padding:.3rem .65rem;
  background:rgba(0,0,0,.22);border:1px solid var(--bdr);
  color:var(--sand);border-radius:100px;cursor:pointer;
  font-family:inherit;transition:.2s;
}
.qb:active{background:rgba(200,136,42,.1)}

/* - History Table - */
.htbl{width:100%;border-collapse:collapse;font-size:.76rem}
.htbl th{text-align:left;padding:.45rem .6rem;color:var(--grey);font-weight:600;border-bottom:1px solid var(--bdr)}
.htbl td{padding:.48rem .6rem;border-bottom:1px solid rgba(200,136,42,.07)}
.htbl tr:last-child td{border-bottom:none}

/* - Status bar - */
.sbar{display:flex;align-items:center;gap:.5rem;font-size:.68rem;color:var(--grey);margin-bottom:.8rem}
.sdot{width:6px;height:6px;border-radius:50%;background:var(--lgreen);animation:blink 2s infinite}

/* - Loading dots - */
.dots span{display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--lgreen);margin:0 2px;animation:bounce 1.1s ease-in-out infinite}
.dots span:nth-child(2){animation-delay:.2s}
.dots span:nth-child(3){animation-delay:.4s}

/* - Toast Notification - */
.toast{
  position:fixed;bottom:90px;left:50%;transform:translateX(-50%) translateY(20px);
  background:rgba(45,26,8,0.96);border:1px solid var(--bdr);border-radius:14px;
  padding:.75rem 1.2rem;font-size:.83rem;color:var(--text);
  box-shadow:0 8px 32px rgba(0,0,0,.5);z-index:999;
  opacity:0;transition:all .3s;pointer-events:none;white-space:nowrap;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.success{border-color:rgba(92,184,92,.4);color:var(--leaf)}
.toast.error{border-color:rgba(192,57,43,.4);color:#e07060}

/* - Animations - */
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes bounce{0%,80%,100%{transform:scale(0)}40%{transform:scale(1)}}
@keyframes up{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}

/* - Desktop upgrades - */
@media(min-width:768px){
  body{padding-bottom:1rem}
  .bottom-nav{display:none}
  .desktop-nav{
    display:flex!important;align-items:center;gap:.5rem;
    background:var(--card);border:1px solid var(--bdr);
    border-radius:100px;padding:.4rem;margin-bottom:1.5rem;
  }
  .dnav-btn{
    padding:.5rem 1.2rem;border-radius:100px;border:none;background:transparent;
    color:var(--grey);font-family:inherit;font-size:.85rem;font-weight:600;
    cursor:pointer;transition:.2s;
  }
  .dnav-btn.active{background:rgba(92,184,92,.15);color:var(--lgreen)}
  .g2{grid-template-columns:1fr 1fr}
  .g3{grid-template-columns:1fr 1fr 1fr}
}
.desktop-nav{display:none}
</style>
</head>
<body>

<!-- - AUTH SCREEN - -->
<div id="authScreen">
  <div class="abox">
    <div class="alogo">
      <span class="ic">🌱</span>
      <h1>Crop Skyyy</h1>
      <p>AI-Powered Farm Irrigation System</p>
    </div>
    <div class="tabs">
      <button class="tab on" id="t1" onclick="swTab(0)">Login</button>
      <button class="tab"    id="t2" onclick="swTab(1)">Register</button>
    </div>
    <div class="field"><label>Username</label>
      <input id="au" placeholder="Enter username" autocomplete="username"/></div>
    <div class="field"><label>Password</label>
      <input id="ap" type="password" placeholder="Enter password"
        autocomplete="current-password" onkeydown="if(event.key==='Enter')doAuth()"/></div>
    <div id="regExtra" style="display:none">
      <div class="field"><label>Phone (for alerts, optional)</label>
        <input id="aph" placeholder="+2348012345678" type="tel"/></div>
    </div>
    <div class="aerr" id="ae"></div>
    <div class="asuc" id="as"></div>
    <button class="abtn" id="ab" onclick="doAuth()">Login</button>
  </div>
</div>

<!-- - DASHBOARD - -->
<div id="dashScreen">
<div class="wrap">

  <!-- Top Bar -->
  <div class="topbar">
    <div class="logo">
      <div class="lmark">🌱</div>
      <div class="ltxt"><h2>Crop Skyyy</h2><span>AI Irrigation System</span></div>
    </div>
    <div style="display:flex;align-items:center;gap:.5rem">
      <div class="user-chip">👤 <span id="uname">--</span></div>
      <button class="btn bdr2" onclick="logout()" style="padding:.35rem .7rem;font-size:.72rem">Out</button>
    </div>
  </div>

  <!-- Desktop Nav -->
  <div class="desktop-nav" id="desktopNav">
    <button class="dnav-btn active" onclick="goPage('home',this)">🌾 Dashboard</button>
    <button class="dnav-btn" onclick="goPage('schedules',this)">⏰ Schedules</button>
    <button class="dnav-btn" onclick="goPage('alerts',this)">🔔 Alerts</button>
    <button class="dnav-btn" onclick="goPage('chat',this)">💬 Chat</button>
    <button class="dnav-btn" onclick="goPage('history',this)">📋 History</button>
  </div>

  <!-- - PAGE: HOME - -->
  <div class="page active" id="page-home">
    <div class="sbar"><div class="sdot" id="sd"></div><span id="st">Ready — enter your city below</span></div>
    <div class="cbar">
      <span style="font-size:.68rem;color:var(--grey);white-space:nowrap">📍 City</span>
      <input id="ci" value="Kaduna" placeholder="Enter your farm city…"/>
      <button class="btn bg" onclick="getData()" style="flex-shrink:0">🌾 Get Data</button>
    </div>

    <!-- Arduino Status -->
    <div id="arduinoStatus" class="arduino-badge arduino-sim" style="margin-bottom:1rem">
      🔌 <span id="arduinoMsg">Checking sensor…</span>
    </div>

    <!-- Weather + Soil (2 col) -->
    <div class="g2" style="margin-bottom:1rem">
      <div class="card">
        <div class="clbl">Weather</div>
        <div class="wtop">
          <div>
            <div class="wtemp" id="wt">--°</div>
            <div class="wdesc" id="wd">--</div>
            <div class="wloc" id="wl">Enter city</div>
          </div>
          <img id="wi" class="wico" src="" alt="" style="display:none"/>
        </div>
        <div class="wstats">
          <div class="ws"><div class="wsl">Humidity</div><div class="wsv" id="wh">--%</div></div>
          <div class="ws"><div class="wsl">Wind</div><div class="wsv" id="ww">--</div></div>
          <div class="ws"><div class="wsl">Rain 1h</div><div class="wsv" id="wr">--</div></div>
          <div class="ws"><div class="wsl">Feels</div><div class="wsv" id="wf">--°</div></div>
          <div class="ws"><div class="wsl">Pressure</div><div class="wsv" id="wp">--</div></div>
          <div class="ws"><div class="wsl">Clouds</div><div class="wsv" id="wc">--%</div></div>
        </div>
      </div>

      <div class="card" style="text-align:center">
        <div class="clbl">Soil Moisture</div>
        <div class="scw">
          <div class="sc" id="sc" style="--p:0deg">
            <div class="sci">
              <div class="spct" id="sp">--%</div>
              <div class="ssub">Moisture</div>
            </div>
          </div>
        </div>
        <div id="pb" class="pbadge poff" style="justify-content:center">
          <span class="pdot"></span><span id="pt">Pump OFF</span>
        </div>
        <div style="font-size:.7rem;color:var(--grey);margin-top:.45rem" id="ss">Waiting…</div>
      </div>
    </div>

    <!-- AI Advice -->
    <div class="card">
      <div class="clbl">🤖 AI Irrigation Advice</div>
      <div class="arule" id="ar"><span style="color:var(--grey);font-style:italic">Tap Get Data to see advice</span></div>
      <div class="clbl" style="margin-top:.5rem">GPT-4o Deep Analysis</div>
      <div class="agpt" id="ag"><span style="color:var(--grey);font-style:italic">AI analysis loads after fetching data…</span></div>
    </div>

    <!-- 7-Day Forecast -->
    <div class="card">
      <div class="clbl">7-Day Forecast</div>
      <div class="fscroll" id="fc">
        <span style="color:var(--grey);font-size:.82rem">Load data to see forecast</span>
      </div>
    </div>
  </div><!-- /home -->

  <!-- - PAGE: SCHEDULES - -->
  <div class="page" id="page-schedules">
    <div class="card">
      <div class="clbl">⏰ Add New Schedule</div>
      <div class="form-row"><label>Schedule Name</label>
        <input id="sName" placeholder="e.g. Morning Irrigation"/></div>
      <div class="g2" style="gap:.6rem;margin-bottom:.85rem">
        <div class="form-row" style="margin:0"><label>Time</label>
          <input id="sTime" type="time" value="06:00"/></div>
        <div class="form-row" style="margin:0"><label>Duration (min)</label>
          <input id="sDur" type="number" value="20" min="1" max="120"/></div>
      </div>
      <div class="form-row"><label>City</label>
        <input id="sCity" placeholder="Kaduna"/></div>
      <div class="form-row">
        <label>Repeat Days</label>
        <div class="day-picker" id="dayPicker">
          <button class="day-btn sel" data-day="Mon">Mon</button>
          <button class="day-btn" data-day="Tue">Tue</button>
          <button class="day-btn sel" data-day="Wed">Wed</button>
          <button class="day-btn" data-day="Thu">Thu</button>
          <button class="day-btn sel" data-day="Fri">Fri</button>
          <button class="day-btn" data-day="Sat">Sat</button>
          <button class="day-btn" data-day="Sun">Sun</button>
        </div>
      </div>
      <button class="btn bg bfull" onclick="addSchedule()">➕ Add Schedule</button>
    </div>

    <div class="clbl" style="padding:0 .2rem">Your Schedules</div>
    <div id="schedList"><div style="color:var(--grey);font-size:.85rem;padding:.5rem">No schedules yet</div></div>
  </div><!-- /schedules -->

  <!-- - PAGE: ALERTS - -->
  <div class="page" id="page-alerts">
    <div class="card">
      <div class="clbl">🔔 Alert Settings</div>
      <p style="font-size:.8rem;color:var(--grey);margin-bottom:1.2rem;line-height:1.6">
        Get WhatsApp or SMS alerts when your crops need urgent attention.
        Powered by Twilio (free trial available at twilio.com).
      </p>

      <div class="form-row">
        <label>Your Phone Number</label>
        <input id="alertPhone" placeholder="+2348012345678" type="tel"/>
      </div>

      <div class="alert-toggle-row">
        <div class="alt-info">
          <h4>📱 WhatsApp Alerts</h4>
          <p>Receive alerts via WhatsApp message</p>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="waEnabled"/>
          <span class="slider"></span>
        </label>
      </div>

      <div class="alert-toggle-row">
        <div class="alt-info">
          <h4>💬 SMS Alerts</h4>
          <p>Receive alerts via plain text message</p>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="smsEnabled"/>
          <span class="slider"></span>
        </label>
      </div>

      <div class="clbl" style="margin-top:1.2rem">Alert Triggers</div>

      <div class="alert-toggle-row">
        <div class="alt-info">
          <h4>🚨 Critical Soil Alert</h4>
          <p>Soil below 25% in hot weather</p>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="alertCrit" checked/>
          <span class="slider"></span>
        </label>
      </div>

      <div class="alert-toggle-row">
        <div class="alt-info">
          <h4>🌧 Rain Detected Alert</h4>
          <p>Heavy rain pauses irrigation automatically</p>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="alertRain" checked/>
          <span class="slider"></span>
        </label>
      </div>

      <div class="alert-toggle-row">
        <div class="alt-info">
          <h4>⏰ Schedule Started Alert</h4>
          <p>Notified when scheduled irrigation runs</p>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="alertSched" checked/>
          <span class="slider"></span>
        </label>
      </div>

      <div style="display:flex;gap:.6rem;margin-top:1.2rem;flex-wrap:wrap">
        <button class="btn bg" style="flex:1" onclick="saveAlerts()">💾 Save Settings</button>
        <button class="btn bgh" style="flex:1" onclick="testAlert()">📤 Send Test Message</button>
      </div>

      <div style="margin-top:1.2rem;background:rgba(0,0,0,.2);border-radius:12px;padding:1rem">
        <div style="font-size:.7rem;color:var(--grey);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.5rem">Setup Guide</div>
        <div style="font-size:.78rem;color:var(--sand);line-height:1.8">
          1. Sign up at <strong style="color:var(--lgold)">twilio.com</strong> (free)<br>
          2. Get your Account SID and Auth Token<br>
          3. Open <strong style="color:var(--lgold)">app.py</strong> and paste them in the CONFIG section<br>
          4. For WhatsApp: join Twilio's sandbox by sending a WhatsApp to <strong style="color:var(--lgold)">+1 415 523 8886</strong><br>
          5. Enter your phone number above and save
        </div>
      </div>
    </div>
  </div><!-- /alerts -->

  <!-- - PAGE: CHAT - -->
  <div class="page" id="page-chat">
    <div class="chat-wrap">
      <div class="qbtns">
        <button class="qb" onclick="qa('Should I water today?')">Water today?</button>
        <button class="qb" onclick="qa('Rain and irrigation tips')">Rain tips</button>
        <button class="qb" onclick="qa('Best time to water in hot weather')">Hot weather</button>
        <button class="qb" onclick="qa('How to save water on my farm')">Save water</button>
        <button class="qb" onclick="qa('What crops suit dry climate?')">Dry crops</button>
      </div>
      <div class="cmsgs" id="cm">
        <div class="msg a">
          <div class="mav">🌱</div>
          <div class="mbub">Hello! I'm <strong>AquaMind</strong> — your AI farm advisor. I know your local weather and soil conditions. Ask me anything about irrigation, crops, or your farm! 🌾</div>
        </div>
      </div>
      <div class="crow">
        <input id="ci2" placeholder="Ask about irrigation, crops, soil…" onkeydown="if(event.key==='Enter')sendChat()"/>
        <button class="btn bg" onclick="sendChat()">➤</button>
      </div>
    </div>
  </div><!-- /chat -->

  <!-- - PAGE: HISTORY - -->
  <div class="page" id="page-history">
    <div class="card">
      <div class="clbl">📋 Sensor & Irrigation History</div>
      <div style="overflow-x:auto">
        <table class="htbl">
          <thead><tr><th>Time</th><th>Soil</th><th>Pump</th><th>Temp</th><th>Advice</th></tr></thead>
          <tbody id="hbody"><tr><td colspan="5" style="color:var(--grey);padding:.8rem">No logs yet</td></tr></tbody>
        </table>
      </div>
    </div>
  </div><!-- /history -->

</div><!-- /.wrap -->

<!-- Bottom Navigation (Mobile) -->
<nav class="bottom-nav">
  <button class="bnav-item active" onclick="goPage('home',this)" data-page="home">
    <span class="icon">🌾</span>Dashboard
  </button>
  <button class="bnav-item" onclick="goPage('schedules',this)" data-page="schedules">
    <span class="icon">⏰</span>Schedules
  </button>
  <button class="bnav-item" onclick="goPage('alerts',this)" data-page="alerts">
    <span class="icon">🔔</span>Alerts
  </button>
  <button class="bnav-item" onclick="goPage('chat',this)" data-page="chat">
    <span class="icon">💬</span>Chat
  </button>
  <button class="bnav-item" onclick="goPage('history',this)" data-page="history">
    <span class="icon">📋</span>History
  </button>
</nav>
</div><!-- /#dashScreen -->

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
// -
//  CROP SKYYY v3 — Frontend JavaScript
// -
let authMode='login', chatHist=[], city='Kaduna';
const $=id=>document.getElementById(id);

function toast(msg, type='success', dur=3000) {
  const t=$('toast'); t.textContent=msg;
  t.className='toast show '+(type==='error'?'error':type==='success'?'success':'');
  setTimeout(()=>t.className='toast',dur);
}

// - Auth -
function swTab(n) {
  authMode=n?'register':'login';
  $('t1').classList.toggle('on',n===0);
  $('t2').classList.toggle('on',n===1);
  $('ab').textContent=n?'Create Account':'Login';
  $('regExtra').style.display=n?'block':'none';
  clrAuth();
}
function clrAuth(){['ae','as'].forEach(id=>{$(id).style.display='none'})}
function errAuth(m){const e=$('ae');e.textContent=m;e.style.display='block';$('as').style.display='none'}
function sucAuth(m){const s=$('as');s.textContent=m;s.style.display='block';$('ae').style.display='none'}

async function doAuth() {
  clrAuth();
  const u=$('au').value.trim(), p=$('ap').value.trim();
  const ph=$('aph')?.value.trim()||'';
  if(!u){errAuth('Please enter a username');return}
  if(!p){errAuth('Please enter a password');return}
  const btn=$('ab'); btn.disabled=true;
  btn.textContent=authMode==='login'?'Logging in…':'Creating account…';
  try {
    const r=await fetch('/api/'+authMode,{method:'POST',
      headers:{'Content-Type':'application/json'},credentials:'same-origin',
      body:JSON.stringify({username:u,password:p,phone:ph})});
    const d=await r.json();
    if(!r.ok){errAuth(d.error||'Something went wrong');return}
    if(authMode==='register'){sucAuth('✅ Account created!');setTimeout(()=>goApp(d.username||u),700)}
    else goApp(d.username||u);
  } catch(e){errAuth('Connection error: '+e.message)}
  finally{btn.disabled=false;btn.textContent=authMode==='login'?'Login':'Create Account'}
}

function goApp(u) {
  $('uname').textContent=u;
  $('authScreen').style.display='none';
  $('dashScreen').style.display='block';
  checkArduino();
  loadAlertSettings();
  loadSchedules();
  loadHistory();
}
async function logout(){
  await fetch('/api/logout',{method:'POST',credentials:'same-origin'});
  location.reload();
}

// - Page Navigation -
function goPage(name, btn) {
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  $('page-'+name).classList.add('active');
  // Mobile nav
  document.querySelectorAll('.bnav-item').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  // Desktop nav
  document.querySelectorAll('.dnav-btn').forEach(b=>b.classList.remove('active'));
  const desktopBtn=document.querySelector(`.dnav-btn[onclick*="${name}"]`);
  if(desktopBtn) desktopBtn.classList.add('active');
  if(name==='history') loadHistory();
  if(name==='schedules') loadSchedules();
}

// - Farm Data (Home) -
function setStatus(m,ok=true){$('st').textContent=m;$('sd').style.background=ok?'var(--lgreen)':'var(--red)'}

async function getData() {
  city=$('ci').value.trim()||'Kaduna';
  setStatus('Fetching live data…');
  $('ar').innerHTML='<div class="dots"><span></span><span></span><span></span></div>';
  $('ag').innerHTML='<div class="dots"><span></span><span></span><span></span></div>';
  try {
    const r=await fetch('/api/farm-data',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({city})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||'Failed');
    renderWeather(d.weather);
    renderSoil(d.soil,d.pump_on);
    $('ar').textContent=d.advice;
    $('ag').textContent=d.gpt||'AI advice unavailable';
    loadForecast();
    // Arduino status
    if(d.arduino) renderArduino(d.arduino);
    setStatus('✅ Live data for '+(d.weather.city||city)+' · '+new Date().toLocaleTimeString());
  } catch(e) {
    setStatus('⚠ Error: '+e.message,false);
    $('ar').innerHTML='<span style="color:var(--red)">'+e.message+'</span>';
    $('ag').textContent='';
    toast(e.message,'error');
  }
}

function renderWeather(w) {
  $('wt').textContent=w.temp+'°C';
  $('wd').textContent=w.description;
  $('wl').textContent=(w.city||city)+(w.country?', '+w.country:'')+' · ☀'+w.sunrise+' 🌙'+w.sunset;
  $('wh').textContent=w.humidity+'%';
  $('ww').textContent=w.wind_speed+' km/h';
  $('wr').textContent=w.rain_1h+' mm';
  $('wf').textContent=w.feels_like+'°C';
  $('wp').textContent=w.pressure+' hPa';
  $('wc').textContent=w.clouds+'%';
  if(w.icon){const i=$('wi');i.src='https://openweathermap.org/img/wn/'+w.icon+'@2x.png';i.style.display='block'}
}

function renderSoil(m,pump) {
  $('sp').textContent=m+'%';
  $('sc').style.setProperty('--p',m*3.6+'deg');
  const b=$('pb');b.className='pbadge '+(pump?'pon':'poff');
  $('pt').textContent=pump?'Pump ON 🚰':'Pump OFF ❌';
  let s='Normal';
  if(m<25)s='⚠️ Critical — very dry';
  else if(m<40)s='💧 Dry — needs water';
  else if(m<60)s='✅ Good moisture';
  else if(m<75)s='💧 Well watered';
  else s='🌊 Saturated';
  $('ss').textContent=s;
}

// - FEATURE 6: Arduino -
async function checkArduino() {
  try {
    const r=await fetch('/api/arduino/status',{credentials:'same-origin'});
    const d=await r.json();
    renderArduino(d);
  } catch(e) {}
}

function renderArduino(d) {
  const el=$('arduinoStatus');
  if(d.connected) {
    el.className='arduino-badge arduino-ok';
    el.innerHTML='🔌 Arduino Connected — '+d.port+' (Live soil readings)';
  } else if(d.mode==='simulated') {
    el.className='arduino-badge arduino-sim';
    el.innerHTML='🔌 Simulation mode — set USE_ARDUINO=True in app.py for real sensor';
  } else {
    el.className='arduino-badge arduino-err';
    el.innerHTML='⚠️ '+d.message;
  }
}

async function testPump() {
  toast('Testing pump (2 second pulse)…');
  try {
    await fetch('/api/arduino/test',{method:'POST',credentials:'same-origin'});
    toast('Pump test complete!','success');
  } catch(e) { toast('Pump test failed: '+e.message,'error'); }
}

// - Forecast -
async function loadForecast() {
  try {
    const r=await fetch('/api/weather?city='+encodeURIComponent(city),{credentials:'same-origin'});
    const d=await r.json();
    if(d.forecast&&d.forecast.length) {
      const em=ic=>{
        if(!ic)return'🌤';
        if(ic.startsWith('01'))return'☀️';
        if(ic.startsWith('02'))return'🌤';
        if(ic.startsWith('03')||ic.startsWith('04'))return'☁️';
        if(ic.startsWith('09')||ic.startsWith('10'))return'🌧';
        if(ic.startsWith('11'))return'⛈';
        if(ic.startsWith('13'))return'❄️';
        return'🌡';
      };
      $('fc').innerHTML=d.forecast.map(f=>
        `<div class="fday">
          <div class="fdn">${f.day}</div>
          <div class="fdi">${em(f.icon)}</div>
          <div class="fdt">${f.temp_max}° / ${f.temp_min}°</div>
          <div class="fdr">💧 ${f.rain_mm}mm</div>
        </div>`).join('');
    }
  } catch(e) {}
}

// - FEATURE 4: Schedules -
function getSelectedDays() {
  return [...document.querySelectorAll('.day-btn.sel')].map(b=>b.dataset.day);
}

document.querySelectorAll('.day-btn').forEach(b=>{
  b.addEventListener('click',()=>b.classList.toggle('sel'));
});

async function addSchedule() {
  const name=$('sName').value.trim();
  const time=$('sTime').value;
  const dur=parseInt($('sDur').value)||20;
  const scity=$('sCity').value.trim()||city||'Kaduna';
  const days=getSelectedDays();
  if(!name){toast('Please enter a schedule name','error');return}
  if(!days.length){toast('Please select at least one day','error');return}
  try {
    const r=await fetch('/api/schedules',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,run_time:time,duration_min:dur,city:scity,days})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error);
    toast('✅ Schedule "'+name+'" created!','success');
    $('sName').value='';
    loadSchedules();
  } catch(e){toast(e.message,'error')}
}

async function loadSchedules() {
  try {
    const r=await fetch('/api/schedules',{credentials:'same-origin'});
    const list=await r.json();
    const el=$('schedList');
    if(!list.length){el.innerHTML='<div style="color:var(--grey);font-size:.85rem;padding:.5rem">No schedules yet — add one above!</div>';return}
    el.innerHTML=list.map(s=>`
      <div class="sched-card">
        <div class="sched-header">
          <div>
            <div class="sched-name">${s.name}</div>
            <div class="sched-meta">⏰ ${s.run_time} · ${s.days.join(', ')} · ${s.duration_min} min · 📍 ${s.city}</div>
            ${s.last_run?`<div class="sched-meta">Last run: ${s.last_run}</div>`:''}
          </div>
          <label class="toggle-switch">
            <input type="checkbox" ${s.active?'checked':''} onchange="toggleSched(${s.id},this.checked)"/>
            <span class="slider"></span>
          </label>
        </div>
        <div class="sched-actions">
          <button class="btn bg" onclick="runNow(${s.id})" style="font-size:.75rem;padding:.38rem .8rem">▶ Run Now</button>
          <button class="btn bdr2" onclick="deleteSched(${s.id})" style="font-size:.75rem;padding:.35rem .75rem">🗑 Delete</button>
        </div>
      </div>`).join('');
  } catch(e){}
}

async function toggleSched(id,active) {
  await fetch('/api/schedules/'+id,{method:'PATCH',credentials:'same-origin',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({active:active?1:0})});
  toast(active?'Schedule enabled ✅':'Schedule paused ⏸');
}

async function deleteSched(id) {
  if(!confirm('Delete this schedule?')) return;
  await fetch('/api/schedules/'+id,{method:'DELETE',credentials:'same-origin'});
  toast('Schedule deleted');
  loadSchedules();
}

async function runNow(id) {
  try {
    const r=await fetch('/api/schedules/'+id+'/run',{method:'POST',credentials:'same-origin'});
    const d=await r.json();
    toast(d.message||'Running now!','success');
  } catch(e){toast(e.message,'error')}
}

// - FEATURE 2: Alerts -
async function loadAlertSettings() {
  try {
    const r=await fetch('/api/alerts/settings',{credentials:'same-origin'});
    const d=await r.json();
    $('alertPhone').value=d.phone||'';
    $('waEnabled').checked=!!d.whatsapp_enabled;
    $('smsEnabled').checked=!!d.sms_enabled;
    $('alertCrit').checked=d.alert_critical!==0;
    $('alertRain').checked=d.alert_rain!==0;
    $('alertSched').checked=d.alert_schedule!==0;
  } catch(e){}
}

async function saveAlerts() {
  const data={
    phone:$('alertPhone').value.trim(),
    whatsapp_enabled:$('waEnabled').checked?1:0,
    sms_enabled:$('smsEnabled').checked?1:0,
    alert_critical:$('alertCrit').checked?1:0,
    alert_rain:$('alertRain').checked?1:0,
    alert_schedule:$('alertSched').checked?1:0,
  };
  try {
    const r=await fetch('/api/alerts/settings',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    toast(d.message||'Settings saved!','success');
  } catch(e){toast(e.message,'error')}
}

async function testAlert() {
  const phone=$('alertPhone').value.trim();
  if(!phone){toast('Enter your phone number first','error');return}
  toast('Sending test message…');
  try {
    const r=await fetch('/api/alerts/test',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
    const d=await r.json();
    toast('Test sent! Check your WhatsApp 📱','success');
  } catch(e){toast(e.message,'error')}
}

// - History -
async function loadHistory() {
  try {
    const r=await fetch('/api/logs',{credentials:'same-origin'});
    const rows=await r.json();
    if(!rows.length) return;
    $('hbody').innerHTML=rows.map(r=>`
      <tr>
        <td style="color:var(--grey);font-size:.68rem">${(r.logged_at||'--').slice(0,16)}</td>
        <td>${r.moisture}%</td>
        <td style="color:${r.pump_on?'var(--leaf)':'var(--grey)'}">${r.pump_on?'🚰 ON':'❌ OFF'}</td>
        <td>${r.temp||'--'}°</td>
        <td style="font-size:.68rem;color:var(--sand)">${(r.advice||'').slice(0,30)}</td>
      </tr>`).join('');
  } catch(e){}
}

// - Chat -
function addMsg(role,html) {
  const cm=$('cm'),d=document.createElement('div');
  d.className='msg '+(role==='ai'?'a':'u');
  d.innerHTML='<div class="mav">'+(role==='ai'?'🌱':'👤')+'</div><div class="mbub">'+html+'</div>';
  cm.appendChild(d); cm.scrollTop=cm.scrollHeight; return d;
}
const esc=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmt=t=>esc(t).replace(/\\*\\*(.*?)\\*\\*/g,'<strong>$1</strong>').replace(/\\n/g,'<br>');
function qa(t){$('ci2').value=t;sendChat()}
async function sendChat(){
  const inp=$('ci2'),txt=inp.value.trim();if(!txt)return;inp.value='';
  addMsg('user',esc(txt)); chatHist.push({role:'user',content:txt});
  const ld=addMsg('ai','<div class="dots"><span></span><span></span><span></span></div>');
  try {
    const r=await fetch('/api/chat',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:txt,city,history:chatHist.slice(-6)})});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    ld.querySelector('.mbub').innerHTML=fmt(d.reply);
    chatHist.push({role:'assistant',content:d.reply});
  } catch(e){
    ld.querySelector('.mbub').innerHTML='<span style="color:var(--red)">Error: '+e.message+'</span>';
  }
}

// - Auto session check -
(async()=>{
  try {
    const r=await fetch('/api/me',{credentials:'same-origin'});
    const d=await r.json();
    if(d.in) goApp(d.username);
  } catch(e){}
})();
</script>
</body>
</html>
"""

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
    body = request.get_json(force=True, silent=True) or {}
    city = body.get("city", "Kaduna").strip()
    user = session.get("user", "")

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
        soil, w.get("rain_1h", 0), w.get("description", ""), city)

    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO logs(username,city,moisture,pump_on,advice,temp,humidity,rain)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user, city, soil, int(pump_on), advice,
             w.get("temp", 0), w.get("humidity", 0), w.get("rain_1h", 0)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Farm data DB write failed: %s", e)

    if alert_type and user:
        maybe_send_alert(user, alert_type, city, soil, w.get("temp", 25), advice)

    return jsonify({
        "weather": w,    "soil":    soil,
        "pump_on": pump_on, "advice":  advice,
        "gpt":     ai_text, "arduino": get_arduino_status(),
    })

@app.route("/api/logs")
def api_logs():
    user = session.get("user", "")
    try:
        conn = get_db()
        # Return only this user's logs for data isolation
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
# Routes - Schedules (Feature 4) - user-isolated
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
        # Only allow update of own schedules
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
    """Test pump with a 2-second pulse. Runs in background so it does not
    block the HTTP response."""
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
    d    = request.get_json(force=True, silent=True) or {}
    msg  = d.get("message", "").strip()
    hist = d.get("history", [])
    city = d.get("city", "Kaduna")

    if not msg:
        return jsonify({"error": "Empty message"}), 400
    if not ai_client:
        return jsonify({"reply": "Install OpenAI: pip install openai"})

    ctx = ""
    try:
        w   = fetch_weather(city)
        ctx = (f"Current weather in {city}: {w['temp']}C, {w['description']}, "
               f"Humidity:{w['humidity']}%, Rain:{w.get('rain_1h', 0)}mm.")
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
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Railway sets the PORT environment variable automatically
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
