"""
BetAI - Yapay Zekâ Destekli Futbol Analiz ve Bahis Asistanı
TEK DOSYA sürümü - GitHub + Railway ile telefondan kurulum için hazırlandı.

Nasıl çalıştırılır: README.md dosyasındaki adımları takip et.
"""
import os
import json
import time
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import google.generativeai as genai

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# ============================================================
# AYARLAR
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_MODE = os.getenv("API_FOOTBALL_MODE", "direct")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

DAILY_UPDATE_HOUR = int(os.getenv("DAILY_UPDATE_HOUR", "8"))
DAILY_UPDATE_MINUTE = int(os.getenv("DAILY_UPDATE_MINUTE", "0"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/betai.db")

if API_FOOTBALL_MODE == "rapidapi":
    API_FOOTBALL_BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"
    API_FOOTBALL_HEADERS = {
        "x-rapidapi-key": API_FOOTBALL_KEY,
        "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
    }
else:
    API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
    API_FOOTBALL_HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# Dokümandaki ligler -> API-Football lig ID eşlemesi.
# NOT: Bir ligde maç görünmezse /leagues?name=... ile ID'yi doğrulayın.
LEAGUES = {
    "sup": {"id": 203, "name": "🇹🇷 Süper Lig"},
    "epl": {"id": 39, "name": "🇬🇧 Premier League"},
    "laliga": {"id": 140, "name": "🇪🇸 La Liga"},
    "bund": {"id": 78, "name": "🇩🇪 Bundesliga"},
    "seriea": {"id": 135, "name": "🇮🇹 Serie A"},
    "ligue1": {"id": 61, "name": "🇫🇷 Ligue 1"},
    "eredivisie": {"id": 88, "name": "🇳🇱 Eredivisie"},
    "portekiz": {"id": 94, "name": "🇵🇹 Portekiz Ligi"},
    "belcika": {"id": 144, "name": "🇧🇪 Belçika Pro Ligi"},
    "suudi": {"id": 307, "name": "🇸🇦 Suudi Arabistan Ligi"},
    "ucl": {"id": 2, "name": "🏆 UEFA Şampiyonlar Ligi"},
    "uel": {"id": 3, "name": "🏆 Avrupa Ligi"},
    "uecl": {"id": 848, "name": "🏆 Konferans Ligi"},
    "hazirlik": {"id": 10, "name": "🌍 Hazırlık Maçları"},
}

BET_STATUS = {"pending": "⏳ Bekleniyor", "won": "✅ Kazandı", "lost": "❌ Kaybetti"}

# ============================================================
# VERİTABANI (SQLite - tek dosya, sunucu kurulumu gerektirmez)
# ============================================================


def _ensure_dir():
    d = os.path.dirname(DATABASE_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                joined_at TEXT NOT NULL,
                notify_daily INTEGER DEFAULT 1,
                notify_before_match INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                match_desc TEXT NOT NULL,
                bet_type TEXT NOT NULL,
                odd REAL,
                stake REAL,
                note TEXT,
                status TEXT DEFAULT 'pending',
                match_date TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fav_type TEXT NOT NULL,
                fav_ext_id INTEGER,
                fav_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS match_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fixture_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def get_or_create_user(telegram_id: int, username):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (telegram_id, username, joined_at) VALUES (?, ?, ?)",
            (telegram_id, username, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def set_notification_pref(telegram_id: int, daily=None, before_match=None):
    with get_conn() as conn:
        if daily is not None:
            conn.execute("UPDATE users SET notify_daily = ? WHERE telegram_id = ?", (int(daily), telegram_id))
        if before_match is not None:
            conn.execute(
                "UPDATE users SET notify_before_match = ? WHERE telegram_id = ?",
                (int(before_match), telegram_id),
            )


def get_all_users_for_notification(kind: str):
    col = "notify_daily" if kind == "daily" else "notify_before_match"
    with get_conn() as conn:
        rows = conn.execute(f"SELECT telegram_id FROM users WHERE {col} = 1").fetchall()
        return [r["telegram_id"] for r in rows]


def add_bet(user_db_id, match_desc, bet_type, odd, stake, note, match_date):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO bets (user_id, match_desc, bet_type, odd, stake, note, match_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_db_id, match_desc, bet_type, odd, stake, note, match_date, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def list_bets(user_db_id, status=None):
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM bets WHERE user_id = ? AND status = ? ORDER BY id DESC", (user_db_id, status)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bets WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user_db_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def update_bet_status(bet_id, status):
    with get_conn() as conn:
        conn.execute("UPDATE bets SET status = ? WHERE id = ?", (status, bet_id))


def delete_bet(bet_id, user_db_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM bets WHERE id = ? AND user_id = ?", (bet_id, user_db_id))


def add_favorite(user_db_id, fav_type, fav_ext_id, fav_name):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM favorites WHERE user_id=? AND fav_type=? AND fav_name=?",
            (user_db_id, fav_type, fav_name),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO favorites (user_id, fav_type, fav_ext_id, fav_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_db_id, fav_type, fav_ext_id, fav_name, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def list_favorites(user_db_id, fav_type=None):
    with get_conn() as conn:
        if fav_type:
            rows = conn.execute(
                "SELECT * FROM favorites WHERE user_id=? AND fav_type=? ORDER BY id DESC", (user_db_id, fav_type)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM favorites WHERE user_id=? ORDER BY id DESC", (user_db_id,)).fetchall()
        return [dict(r) for r in rows]


def add_match_note(user_db_id, fixture_id, note):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO match_notes (user_id, fixture_id, note, created_at) VALUES (?, ?, ?, ?)",
            (user_db_id, fixture_id, note, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_performance_stats(user_db_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM bets WHERE user_id=? AND status != 'pending'", (user_db_id,)).fetchall()

    total = len(rows)
    won = sum(1 for r in rows if r["status"] == "won")
    lost = sum(1 for r in rows if r["status"] == "lost")
    success_rate = (won / total * 100) if total else 0.0

    by_type = {}
    for r in rows:
        t = r["bet_type"] or "Diğer"
        by_type.setdefault(t, {"total": 0, "won": 0})
        by_type[t]["total"] += 1
        if r["status"] == "won":
            by_type[t]["won"] += 1

    best_type, best_rate = None, -1
    worst_type, worst_rate = None, 101
    for t, d in by_type.items():
        rate = (d["won"] / d["total"] * 100) if d["total"] else 0
        if rate > best_rate:
            best_type, best_rate = t, rate
        if rate < worst_rate:
            worst_type, worst_rate = t, rate

    return {
        "total": total,
        "won": won,
        "lost": lost,
        "success_rate": round(success_rate, 1),
        "best_bet_type": best_type,
        "best_bet_type_rate": round(best_rate, 1) if best_type else None,
        "worst_bet_type": worst_type,
        "worst_bet_type_rate": round(worst_rate, 1) if worst_type else None,
    }


def cache_get(key, max_age_seconds):
    with get_conn() as conn:
        row = conn.execute("SELECT data, updated_at FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
    if not row:
        return None
    age = (datetime.utcnow() - datetime.fromisoformat(row["updated_at"])).total_seconds()
    if age > max_age_seconds:
        return None
    return row["data"]


def cache_set(key, data):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO api_cache (cache_key, data, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
            (key, data, datetime.utcnow().isoformat()),
        )


# ============================================================
# API-FOOTBALL (kalıcı önbellekli)
# ============================================================

FIXTURES_TTL = 60 * 60 * 20
MATCH_DATA_TTL = 60 * 60 * 6


def _cache_key(endpoint, params):
    return endpoint + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def _api_get(endpoint, params, ttl_seconds):
    key = _cache_key(endpoint, params)
    cached = cache_get(key, ttl_seconds)
    if cached is not None:
        return json.loads(cached)
    url = f"{API_FOOTBALL_BASE_URL}/{endpoint}"
    try:
        resp = requests.get(url, headers=API_FOOTBALL_HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"error": str(e), "response": []}
    cache_set(key, json.dumps(data))
    return data


def get_fixtures_by_league(league_id, day=None, season=None):
    day = day or date.today()
    season = season or day.year
    data = _api_get("fixtures", {"league": league_id, "season": season, "date": day.isoformat()}, FIXTURES_TTL)
    return data.get("response", [])


def get_all_fixtures_today(day=None):
    """O günün DÜNYADAKİ TÜM maçlarını (her lig, her ülke) tek API isteğiyle çeker."""
    day = day or date.today()
    data = _api_get("fixtures", {"date": day.isoformat()}, FIXTURES_TTL)
    fixtures = data.get("response", [])
    # En yakın saatten en uzağa sırala
    def _sort_key(f):
        try:
            return f["fixture"]["date"]
        except Exception:
            return ""
    return sorted(fixtures, key=_sort_key)


def get_fixture_details(fixture_id):
    data = _api_get("fixtures", {"id": fixture_id}, MATCH_DATA_TTL)
    resp = data.get("response", [])
    return resp[0] if resp else None


def get_fixture_statistics(fixture_id):
    data = _api_get("fixtures/statistics", {"fixture": fixture_id}, MATCH_DATA_TTL)
    return data.get("response", [])


def get_team_statistics(team_id, league_id, season=None):
    season = season or date.today().year
    data = _api_get("teams/statistics", {"team": team_id, "league": league_id, "season": season}, MATCH_DATA_TTL)
    return data.get("response", {})


def get_head_to_head(team1_id, team2_id, last=5):
    data = _api_get("fixtures/headtohead", {"h2h": f"{team1_id}-{team2_id}", "last": last}, MATCH_DATA_TTL)
    return data.get("response", [])


def get_injuries(team_id, league_id, season=None):
    season = season or date.today().year
    data = _api_get("injuries", {"team": team_id, "league": league_id, "season": season}, MATCH_DATA_TTL)
    return data.get("response", [])


def get_fixture_odds(fixture_id):
    """Bir maç için bahis oranlarını çeker (birden fazla bahis şirketi dönebilir)."""
    data = _api_get("odds", {"fixture": fixture_id}, MATCH_DATA_TTL)
    return data.get("response", [])


def format_odds_summary(odds_response, max_bookmakers=3):
    """Oran verisini okunabilir kısa bir metne çevirir (1X2, 2.5 Alt/Üst, KG Var gibi ana pazarlar)."""
    if not odds_response:
        return None
    entry = odds_response[0]
    bookmakers = entry.get("bookmakers", [])[:max_bookmakers]
    if not bookmakers:
        return None

    wanted_markets = {
        "Match Winner": "🏆 Maç Sonucu (MS1-X-MS2)",
        "Goals Over/Under": "⚽ Alt/Üst 2.5",
        "Both Teams Score": "🥅 Karşılıklı Gol",
    }

    lines = []
    for bm in bookmakers:
        bm_name = bm.get("name", "?")
        lines.append(f"*{bm_name}*")
        for bet in bm.get("bets", []):
            bet_name = bet.get("name")
            if bet_name not in wanted_markets:
                continue
            values = bet.get("values", [])
            vals_text = ", ".join(f"{v['value']}: {v['odd']}" for v in values[:3])
            lines.append(f"  {wanted_markets[bet_name]} → {vals_text}")
        lines.append("")
    return "\n".join(lines).strip() or None


def sync_all_leagues_fixtures(leagues):
    calls_made = 0
    for key, league in leagues.items():
        key_str = _cache_key(
            "fixtures", {"league": league["id"], "season": date.today().year, "date": date.today().isoformat()}
        )
        if cache_get(key_str, FIXTURES_TTL) is not None:
            continue
        get_fixtures_by_league(league["id"])
        calls_made += 1
    return calls_made


def format_fixture_summary(fixture):
    teams = fixture.get("teams", {})
    home = teams.get("home", {}).get("name", "?")
    away = teams.get("away", {}).get("name", "?")
    ts = fixture.get("fixture", {}).get("date")
    saat = "?"
    if ts:
        try:
            saat = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            pass
    return f"{home} vs {away}  🕒 {saat}"


# ============================================================
# YAPAY ZEKA (Google Gemini)
# ============================================================

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

AI_SYSTEM_PROMPT = (
    "Sen BetAI adlı bir Telegram botunun futbol analiz asistanısın. "
    "Sana verilen istatistiksel verilere dayanarak Türkçe, net ve yapılandırılmış "
    "bir maç analizi yazıyorsun. Kurallar:\n"
    "1) Kesin sonuç iddia etme, 'muhtemel', 'olası' gibi ifadeler kullan.\n"
    "2) Takım formunu, eksik oyuncuları, ev sahibi avantajını ve gol istatistiklerini yorumla.\n"
    "3) En sonunda 'Güven Puanı: X.X / 10' şeklinde bir satır ekle.\n"
    "4) Kısa bir 'Risk Seviyesi: Düşük/Orta/Yüksek' satırı ekle.\n"
    "5) Sonuna, bunun mali tavsiye olmadığını ve bahsin risk içerdiğini hatırlatan tek cümlelik bir not ekle."
)

_ai_model = None


def _get_ai_model():
    global _ai_model
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY tanımlı değil. Railway'de Variables kısmına ekleyin.")
    if _ai_model is None:
        _ai_model = genai.GenerativeModel(model_name="gemini-flash-latest", system_instruction=AI_SYSTEM_PROMPT)
    return _ai_model


def ai_analyze_match(context_data, user_question=None):
    model = _get_ai_model()
    prompt = f"Maç verileri:\n{context_data}\n\n"
    prompt += f"Kullanıcının sorusu: {user_question}\n" if user_question else "Bu maçı analiz et.\n"
    return model.generate_content(prompt).text


def ai_free_chat(context_data, user_message, history=None):
    model = _get_ai_model()
    gemini_history = []
    for h in (history or []):
        role = "model" if h["role"] == "assistant" else "user"
        gemini_history.append({"role": role, "parts": [h["content"]]})
    chat = model.start_chat(history=gemini_history)
    prompt = f"Güncel maç/istatistik verileri:\n{context_data}\n\nSoru: {user_message}"
    return chat.send_message(prompt).text


# ============================================================
# KLAVYELER (MENÜLER)
# ============================================================


def main_menu():
    rows = [
        [InlineKeyboardButton("⚽ Günün Maçları", callback_data="menu:matches")],
        [InlineKeyboardButton("🌍 Tüm Maçlar (Bugün, Filtreli)", callback_data="menu:allmatches")],
        [InlineKeyboardButton("📊 İstatistik Merkezi", callback_data="menu:stats")],
        [InlineKeyboardButton("🤖 AI Analiz", callback_data="menu:ai")],
        [InlineKeyboardButton("📝 Bahis Ajandam", callback_data="menu:journal")],
        [InlineKeyboardButton("⭐ Favoriler", callback_data="menu:favorites")],
        [InlineKeyboardButton("📈 Performansım", callback_data="menu:performance")],
        [InlineKeyboardButton("🔔 Bildirimler", callback_data="menu:notifications")],
        [InlineKeyboardButton("⚙️ Ayarlar", callback_data="menu:settings")],
    ]
    return InlineKeyboardMarkup(rows)


def league_list_menu():
    rows = []
    items = list(LEAGUES.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(info["name"], callback_data=f"league:{key}") for key, info in items[i:i + 2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def fixtures_menu(fixtures):
    rows = []
    for f in fixtures[:15]:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        rows.append([InlineKeyboardButton(f"{home} vs {away}", callback_data=f"match:{fid}")])
    if not rows:
        rows.append([InlineKeyboardButton("Bugün maç bulunamadı", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅️ Ligler", callback_data="menu:matches")])
    return InlineKeyboardMarkup(rows)


def match_detail_menu(fixture_id):
    rows = [
        [InlineKeyboardButton("📊 İstatistik", callback_data=f"match:{fixture_id}:stats")],
        [InlineKeyboardButton("💰 Oranlar", callback_data=f"match:{fixture_id}:odds")],
        [InlineKeyboardButton("🤖 AI Analizi", callback_data=f"match:{fixture_id}:ai")],
        [InlineKeyboardButton("⭐ Favorilere Ekle", callback_data=f"match:{fixture_id}:fav")],
        [InlineKeyboardButton("📝 Not Al", callback_data=f"match:{fixture_id}:note")],
        [InlineKeyboardButton("➕ Ajandaya Ekle", callback_data=f"match:{fixture_id}:addbet")],
        [InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(rows)


def journal_menu(bets):
    rows = []
    for b in bets[:10]:
        status_emoji = BET_STATUS.get(b["status"], b["status"])
        rows.append(
            [InlineKeyboardButton(f"{b['match_desc'][:20]} | {b['bet_type']} | {status_emoji}", callback_data=f"bet:{b['id']}")]
        )
    rows.append([InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def bet_detail_menu(bet_id):
    rows = [
        [
            InlineKeyboardButton("✅ Kazandı", callback_data=f"betstatus:{bet_id}:won"),
            InlineKeyboardButton("❌ Kaybetti", callback_data=f"betstatus:{bet_id}:lost"),
        ],
        [InlineKeyboardButton("🗑 Sil", callback_data=f"betdelete:{bet_id}")],
        [InlineKeyboardButton("⬅️ Ajandam", callback_data="menu:journal")],
    ]
    return InlineKeyboardMarkup(rows)


def notifications_menu(notify_daily, notify_before_match):
    rows = [
        [InlineKeyboardButton(f"{'✅' if notify_daily else '⬜️'} Günlük Maç Bildirimi", callback_data="notify:toggle:daily")],
        [InlineKeyboardButton(f"{'✅' if notify_before_match else '⬜️'} Maç Öncesi Bildirim (30dk)", callback_data="notify:toggle:before_match")],
        [InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(rows)


def all_matches_menu(fixtures, page=0, page_size=12, filtered=False):
    start = page * page_size
    chunk = fixtures[start:start + page_size]
    rows = []
    for f in chunk:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        league = f["league"]["name"]
        ts = f["fixture"].get("date", "")
        saat = ts[11:16] if len(ts) > 16 else "?"
        rows.append([InlineKeyboardButton(f"🕒{saat} {home} - {away} ({league[:15]})", callback_data=f"match:{fid}")])
    if not rows:
        rows.append([InlineKeyboardButton("Sonuç bulunamadı", callback_data="noop")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Önceki", callback_data=f"allmatches_page:{page-1}"))
    if start + page_size < len(fixtures):
        nav.append(InlineKeyboardButton("Sonraki ▶️", callback_data=f"allmatches_page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🔍 Metinle Filtrele (lig/takım adı)", callback_data="allmatches:filter")])
    rows.append([InlineKeyboardButton("🤖 AI ile Filtrele/Sırala", callback_data="allmatches:aifilter")])
    if filtered:
        rows.append([InlineKeyboardButton("♻️ Filtreyi Temizle", callback_data="allmatches:clear")])
    rows.append([InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def back_home_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu:home")]])


# ============================================================
# HANDLER'LAR
# ============================================================

WELCOME_TEXT = (
    "🏠 *BetAI'ye Hoş Geldin!*\n\n"
    "Güncel futbol verilerini analiz eden, yapay zeka ile sohbet edebildiğin ve kendi bahis "
    "geçmişini takip edebildiğin kişisel asistanın.\n\n"
    "⚠️ _Bu bot kesin sonuç garanti etmez, sadece veriye dayalı analiz sunar. Bahis risk içerir, "
    "lütfen bilinçli oynayın._\n\nAşağıdaki menüden başlayabilirsin:"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu(), parse_mode="Markdown")


async def show_home(update, context):
    query = update.callback_query
    context.user_data.pop("mode", None)
    context.user_data.pop("pending_fixture", None)
    await query.edit_message_text("🏠 *Ana Menü*\nBir seçenek belirle:", reply_markup=main_menu(), parse_mode="Markdown")


async def show_leagues(update, context):
    query = update.callback_query
    await query.edit_message_text("⚽ *Günün Maçları*\nBir lig seç:", reply_markup=league_list_menu(), parse_mode="Markdown")


async def show_fixtures(update, context, league_key):
    query = update.callback_query
    league = LEAGUES.get(league_key)
    if not league:
        await query.edit_message_text("Lig bulunamadı.", reply_markup=back_home_menu())
        return
    await query.edit_message_text(f"⏳ {league['name']} için bugünkü maçlar yükleniyor...")
    fixtures = get_fixtures_by_league(league["id"])
    text = f"{league['name']}\nBugünkü maçlar:" if fixtures else f"{league['name']}\nBugün planlanmış maç bulunamadı."
    await query.edit_message_text(text, reply_markup=fixtures_menu(fixtures))
    context.chat_data.setdefault("fixtures_cache", {})
    for f in fixtures:
        context.chat_data["fixtures_cache"][f["fixture"]["id"]] = f


async def show_all_matches(update, context, page=0):
    query = update.callback_query
    await query.edit_message_text("⏳ Bugünün tüm dünya genelindeki maçları yükleniyor (bu biraz sürebilir)...")
    fixtures = get_all_fixtures_today()
    context.chat_data["all_fixtures"] = fixtures
    context.chat_data["all_fixtures_filtered"] = None
    context.chat_data.setdefault("fixtures_cache", {})
    for f in fixtures:
        context.chat_data["fixtures_cache"][f["fixture"]["id"]] = f
    context.chat_data["all_matches_page"] = page
    text = f"🌍 *Bugünün Tüm Maçları*\nToplam {len(fixtures)} maç bulundu. En yakın saatten sıralı:"
    await query.edit_message_text(text, reply_markup=all_matches_menu(fixtures, page), parse_mode="Markdown")


async def show_all_matches_page(update, context, page):
    query = update.callback_query
    fixtures = context.chat_data.get("all_fixtures_filtered") or context.chat_data.get("all_fixtures") or []
    filtered = context.chat_data.get("all_fixtures_filtered") is not None
    context.chat_data["all_matches_page"] = page
    await query.edit_message_reply_markup(reply_markup=all_matches_menu(fixtures, page, filtered=filtered))


async def prompt_match_filter(update, context):
    query = update.callback_query
    context.user_data["mode"] = "awaiting_match_filter"
    await query.edit_message_text(
        "🔍 Lig, ülke veya takım adı yaz (örn: `Süper Lig`, `Uganda`, `Real Madrid`).\n"
        "Yazdığın metni içeren tüm maçları göstereceğim.",
        reply_markup=back_home_menu(),
        parse_mode="Markdown",
    )


async def handle_match_filter_text(update, context):
    query_text = update.message.text.strip().lower()
    fixtures = context.chat_data.get("all_fixtures") or []
    if not fixtures:
        await update.message.reply_text("Önce '🌍 Tüm Maçlar' menüsünü bir kez açman lazım. Ana menüden dene.")
        context.user_data.pop("mode", None)
        return

    filtered = []
    for f in fixtures:
        league = f["league"]["name"].lower()
        country = f["league"].get("country", "").lower()
        home = f["teams"]["home"]["name"].lower()
        away = f["teams"]["away"]["name"].lower()
        haystack = f"{league} {country} {home} {away}"
        if query_text in haystack:
            filtered.append(f)

    context.chat_data["all_fixtures_filtered"] = filtered
    context.chat_data["all_matches_page"] = 0
    context.user_data.pop("mode", None)

    text = f"🔍 *'{update.message.text.strip()}'* için {len(filtered)} maç bulundu:"
    await update.message.reply_text(text, reply_markup=all_matches_menu(filtered, 0, filtered=True), parse_mode="Markdown")


async def clear_match_filter(update, context):
    query = update.callback_query
    context.chat_data["all_fixtures_filtered"] = None
    context.chat_data["all_matches_page"] = 0
    fixtures = context.chat_data.get("all_fixtures") or []
    await query.edit_message_text(
        f"🌍 *Bugünün Tüm Maçları*\nToplam {len(fixtures)} maç:", reply_markup=all_matches_menu(fixtures, 0), parse_mode="Markdown"
    )


async def prompt_ai_filter(update, context):
    query = update.callback_query
    context.user_data["mode"] = "awaiting_ai_filter"
    await query.edit_message_text(
        "🤖 Bugünün maçları arasında ne arıyorsun, yaz. Örnekler:\n"
        "• _En önemli/dikkat çekici 5 maç hangisi?_\n"
        "• _Uganda liglerindeki maçları listele_\n"
        "• _Saat 20:00'den sonraki Avrupa maçları hangileri?_\n\n"
        "⚠️ Not: Gerçek bahis oranı verisi yok, yapay zeka lig önemine/bilinirliğe göre yorumlar.",
        reply_markup=back_home_menu(),
        parse_mode="Markdown",
    )


async def handle_ai_filter_text(update, context):
    fixtures = context.chat_data.get("all_fixtures") or []
    if not fixtures:
        await update.message.reply_text("Önce '🌍 Tüm Maçlar' menüsünü bir kez açman lazım. Ana menüden dene.")
        context.user_data.pop("mode", None)
        return

    await update.message.reply_text("🤖 Düşünüyorum...")

    # Prompt'u çok büyütmemek için ilk 250 maçı kompakt tek satırlık formatta gönder
    lines = []
    for f in fixtures[:250]:
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        league = f["league"]["name"]
        country = f["league"].get("country", "")
        ts = f["fixture"].get("date", "")
        saat = ts[11:16] if len(ts) > 16 else "?"
        lines.append(f"{saat} | {league} ({country}) | {home} vs {away}")
    context_text = "\n".join(lines)

    try:
        answer = ai_free_chat(
            context_data=f"Bugünün tüm maçları (saat | lig (ülke) | ev sahibi vs deplasman):\n{context_text}",
            user_message=update.message.text,
        )
    except Exception as e:
        answer = f"⚠️ {e}"

    context.user_data.pop("mode", None)
    await update.message.reply_text(answer[:4000])


async def show_match_detail(update, context, fixture_id):
    query = update.callback_query
    await query.edit_message_text("⏳ Maç detayları yükleniyor...")
    fixture = get_fixture_details(fixture_id)
    if not fixture:
        await query.edit_message_text("Maç bulunamadı.", reply_markup=back_home_menu())
        return
    context.chat_data.setdefault("fixtures_cache", {})[fixture_id] = fixture
    teams = fixture["teams"]
    fx = fixture["fixture"]
    venue = fx.get("venue", {}) or {}
    text = (
        f"⚽ *{teams['home']['name']}* 🆚 *{teams['away']['name']}*\n\n"
        f"📅 Tarih: {fx.get('date', '?')[:10]}\n"
        f"🕒 Saat: {fx.get('date', '?')[11:16]}\n"
        f"🏟 Stadyum: {venue.get('name', 'Bilinmiyor')}\n"
        f"👨 Hakem: {fx.get('referee') or 'Bilinmiyor'}\n"
    )
    await query.edit_message_text(text, reply_markup=match_detail_menu(fixture_id), parse_mode="Markdown")


def _fixture_context_text(fixture_id, chat_data):
    fixture = chat_data.get("fixtures_cache", {}).get(fixture_id) or get_fixture_details(fixture_id)
    if not fixture:
        return "Bu maç için veri bulunamadı."
    teams = fixture["teams"]
    league_id = fixture["league"]["id"]
    season = fixture["league"]["season"]
    home_id, away_id = teams["home"]["id"], teams["away"]["id"]
    lines = [f"Maç: {teams['home']['name']} vs {teams['away']['name']}"]

    stats = get_fixture_statistics(fixture_id)
    if stats:
        for team_stat in stats:
            name = team_stat.get("team", {}).get("name")
            values = team_stat.get("statistics", [])
            summary = ", ".join(f"{v['type']}: {v['value']}" for v in values if v.get("value") is not None)
            lines.append(f"- {name}: {summary}")

    if get_head_to_head(home_id, away_id, last=5):
        lines.append("Son karşılaşma verisi mevcut.")

    try:
        home_stats = get_team_statistics(home_id, league_id, season)
        away_stats = get_team_statistics(away_id, league_id, season)
        if home_stats:
            lines.append(f"{teams['home']['name']} son form: {home_stats.get('form')}")
        if away_stats:
            lines.append(f"{teams['away']['name']} son form: {away_stats.get('form')}")
    except Exception:
        pass

    inj_h = get_injuries(home_id, league_id, season)
    inj_a = get_injuries(away_id, league_id, season)
    if inj_h or inj_a:
        lines.append(f"Sakat/cezalı sayısı - Ev sahibi: {len(inj_h)}, Deplasman: {len(inj_a)}")

    odds_text = format_odds_summary(get_fixture_odds(fixture_id))
    if odds_text:
        lines.append("Bahis oranları (bilgi amaçlı, kesinlik garantisi değildir):")
        lines.append(odds_text)

    return "\n".join(lines)


async def show_stats(update, context, fixture_id):
    query = update.callback_query
    await query.edit_message_text("⏳ İstatistikler getiriliyor...")
    stats = get_fixture_statistics(fixture_id)
    if not stats:
        text = "Bu maç için henüz detaylı istatistik yayınlanmadı."
    else:
        parts = []
        for team_stat in stats:
            name = team_stat.get("team", {}).get("name", "?")
            parts.append(f"*{name}*")
            for v in team_stat.get("statistics", []):
                if v.get("value") is not None:
                    parts.append(f"  • {v['type']}: {v['value']}")
        text = "\n".join(parts)
    await query.edit_message_text(text[:4000], reply_markup=match_detail_menu(fixture_id), parse_mode="Markdown")


async def show_odds(update, context, fixture_id):
    query = update.callback_query
    await query.edit_message_text("⏳ Oranlar getiriliyor...")
    odds_response = get_fixture_odds(fixture_id)
    text = format_odds_summary(odds_response)
    if not text:
        text = (
            "Bu maç için henüz oran verisi yok (genelde maça yaklaştıkça yayınlanır) "
            "ya da bu bahis şirketi/pazar API-Football tarafında mevcut değil."
        )
    await query.edit_message_text(text[:4000], reply_markup=match_detail_menu(fixture_id), parse_mode="Markdown")


async def run_ai_analysis(update, context, fixture_id):
    query = update.callback_query
    await query.edit_message_text("🤖 Yapay zeka analiz ediyor, birkaç saniye sürebilir...")
    context_text = _fixture_context_text(fixture_id, context.chat_data)
    try:
        analysis = ai_analyze_match(context_text)
    except Exception as e:
        analysis = f"⚠️ {e}"
    await query.edit_message_text(analysis[:4000], reply_markup=match_detail_menu(fixture_id))


async def add_favorite_from_match(update, context, fixture_id):
    query = update.callback_query
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    fixture = context.chat_data.get("fixtures_cache", {}).get(fixture_id) or get_fixture_details(fixture_id)
    if fixture:
        home, away = fixture["teams"]["home"]["name"], fixture["teams"]["away"]["name"]
        add_favorite(user_db_id, "team", fixture["teams"]["home"]["id"], home)
        add_favorite(user_db_id, "team", fixture["teams"]["away"]["id"], away)
        await query.answer(f"{home} ve {away} favorilere eklendi!", show_alert=True)
    else:
        await query.answer("Maç verisi bulunamadı.", show_alert=True)


async def prompt_note(update, context, fixture_id):
    query = update.callback_query
    context.user_data["mode"] = "awaiting_note"
    context.user_data["pending_fixture"] = fixture_id
    await query.edit_message_text("📝 Bu maç için notunu yaz ve gönder.", reply_markup=back_home_menu())


async def prompt_add_bet(update, context, fixture_id):
    query = update.callback_query
    context.user_data["mode"] = "awaiting_bet"
    context.user_data["pending_fixture"] = fixture_id
    await query.edit_message_text(
        "📝 Bahsini şu formatta yaz:\n`Bahis Türü | Oran | Miktar | Not`\n\nÖrnek: `MS1 | 1.85 | 100 | Ev sahibi güçlü`",
        reply_markup=back_home_menu(),
        parse_mode="Markdown",
    )


async def show_journal(update, context):
    query = update.callback_query
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    bets = list_bets(user_db_id)
    text = "📝 *Bahis Ajandam*\n\nKayıtlı bahisler:" if bets else (
        "📝 *Bahis Ajandam*\n\nHenüz kayıtlı bahsin yok. Bir maça girip '➕ Ajandaya Ekle' ile ekleyebilirsin."
    )
    await query.edit_message_text(text, reply_markup=journal_menu(bets), parse_mode="Markdown")


async def show_bet_detail(update, context, bet_id):
    query = update.callback_query
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    bet = next((b for b in list_bets(user_db_id) if b["id"] == bet_id), None)
    if not bet:
        await query.edit_message_text("Kayıt bulunamadı.", reply_markup=back_home_menu())
        return
    text = (
        f"⚽ {bet['match_desc']}\n🎯 Bahis: {bet['bet_type']}\n💰 Oran: {bet['odd'] or '-'}\n"
        f"💵 Miktar: {bet['stake'] or '-'}\n📝 Not: {bet['note'] or '-'}\n📌 Durum: {bet['status']}\n"
    )
    await query.edit_message_text(text, reply_markup=bet_detail_menu(bet_id))


async def handle_update_bet_status(update, context, bet_id, status):
    query = update.callback_query
    update_bet_status(bet_id, status)
    await query.answer("Durum güncellendi!")
    await show_bet_detail(update, context, bet_id)


async def handle_delete_bet(update, context, bet_id):
    query = update.callback_query
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    delete_bet(bet_id, user_db_id)
    await query.answer("Silindi.")
    await show_journal(update, context)


async def handle_bet_text_input(update, context):
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    fixture_id = context.user_data.get("pending_fixture")
    fixture = context.chat_data.get("fixtures_cache", {}).get(fixture_id) if fixture_id else None
    match_desc = format_fixture_summary(fixture) if fixture else (f"Maç #{fixture_id}" if fixture_id else "Bilinmeyen maç")

    parts = [p.strip() for p in update.message.text.split("|")]
    bet_type = parts[0] if parts and parts[0] else "Belirtilmedi"
    odd = stake = note = None
    try:
        if len(parts) > 1 and parts[1]:
            odd = float(parts[1].replace(",", "."))
    except ValueError:
        pass
    try:
        if len(parts) > 2 and parts[2]:
            stake = float(parts[2].replace(",", "."))
    except ValueError:
        pass
    if len(parts) > 3 and parts[3]:
        note = parts[3]

    add_bet(user_db_id, match_desc, bet_type, odd, stake, note, date.today().isoformat())
    context.user_data.pop("mode", None)
    context.user_data.pop("pending_fixture", None)
    await update.message.reply_text(f"✅ Ajandaya eklendi:\n{match_desc}\n🎯 {bet_type}")


async def show_favorites(update, context):
    query = update.callback_query
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    favs = list_favorites(user_db_id)
    if not favs:
        text = "⭐ *Favoriler*\n\nHenüz favori eklemedin. Bir maç sayfasından takım favorileyebilirsin."
    else:
        lines = ["⭐ *Favoriler*\n"]
        for f in favs:
            emoji = {"team": "⚽", "league": "🏆", "player": "👤"}.get(f["fav_type"], "•")
            lines.append(f"{emoji} {f['fav_name']}")
        text = "\n".join(lines)
    await query.edit_message_text(text, reply_markup=back_home_menu(), parse_mode="Markdown")


async def show_performance(update, context):
    query = update.callback_query
    user = update.effective_user
    user_db_id = get_or_create_user(user.id, user.username)
    stats = get_performance_stats(user_db_id)
    if stats["total"] == 0:
        text = "📈 *Performansım*\n\nHenüz sonuçlanmış bahis yok."
    else:
        text = (
            f"📈 *Performansım*\n\nToplam bahis: {stats['total']}\n✅ Kazanan: {stats['won']}\n"
            f"❌ Kaybeden: {stats['lost']}\n📊 Başarı yüzdesi: %{stats['success_rate']}\n\n"
            f"🏆 En başarılı tür: {stats['best_bet_type']} (%{stats['best_bet_type_rate']})\n"
            f"⚠️ En düşük başarılı tür: {stats['worst_bet_type']} (%{stats['worst_bet_type_rate']})\n"
        )
    await query.edit_message_text(text, reply_markup=back_home_menu(), parse_mode="Markdown")


async def show_notifications(update, context):
    query = update.callback_query
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    with get_conn() as conn:
        row = conn.execute("SELECT notify_daily, notify_before_match FROM users WHERE telegram_id=?", (user.id,)).fetchone()
    daily = bool(row["notify_daily"]) if row else True
    before = bool(row["notify_before_match"]) if row else True
    await query.edit_message_text("🔔 *Bildirim Ayarları*", reply_markup=notifications_menu(daily, before), parse_mode="Markdown")


async def toggle_notification(update, context, kind):
    query = update.callback_query
    user = update.effective_user
    with get_conn() as conn:
        row = conn.execute("SELECT notify_daily, notify_before_match FROM users WHERE telegram_id=?", (user.id,)).fetchone()
    current_daily = bool(row["notify_daily"]) if row else True
    current_before = bool(row["notify_before_match"]) if row else True
    if kind == "daily":
        set_notification_pref(user.id, daily=not current_daily)
    else:
        set_notification_pref(user.id, before_match=not current_before)
    await query.answer("Güncellendi!")
    await show_notifications(update, context)


async def show_settings(update, context):
    query = update.callback_query
    text = "⚙️ *Ayarlar*\n\nBildirim tercihlerini '🔔 Bildirimler' menüsünden yönetebilirsin."
    await query.edit_message_text(text, reply_markup=back_home_menu(), parse_mode="Markdown")


async def enter_ai_mode(update, context):
    query = update.callback_query
    context.user_data["mode"] = "ai_chat"
    context.user_data["ai_history"] = []
    text = (
        "🤖 *AI Analiz Modu*\n\nBana futbolla ilgili soru sorabilirsin, örneğin:\n"
        "• _Bugün en güvenilir 5 maç hangisi?_\n• _Liverpool Arsenal analiz et_\n\nÇıkmak için Ana Menü'ye dön."
    )
    await query.edit_message_text(text, reply_markup=back_home_menu(), parse_mode="Markdown")


async def handle_ai_chat_text(update, context):
    user_message = update.message.text
    history = context.user_data.get("ai_history", [])
    await update.message.reply_text("🤖 Düşünüyorum...")
    try:
        answer = ai_free_chat(
            context_data="(Genel soru - spesifik maç verisi yok, gerekirse kullanıcıdan takım/lig ismi iste.)",
            user_message=user_message,
            history=history,
        )
    except Exception as e:
        answer = f"⚠️ {e}"
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": answer})
    context.user_data["ai_history"] = history[-10:]
    await update.message.reply_text(answer[:4000])


# ============================================================
# ZAMANLANMIŞ GÖREVLER
# ============================================================


async def morning_sync(app=None):
    calls = sync_all_leagues_fixtures(LEAGUES)
    logger.info("Sabah senkronu tamamlandı, %d API isteği yapıldı.", calls)


async def send_daily_update(app):
    await morning_sync()
    users = get_all_users_for_notification("daily")
    for telegram_id in users:
        try:
            await app.bot.send_message(
                chat_id=telegram_id,
                text="🌅 *Günaydın!* Bugünün maçları hazır. ⚽",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Bildirim gönderilemedi (%s): %s", telegram_id, e)


async def check_favorite_matches(app):
    now = datetime.utcnow()
    window_start, window_end = now + timedelta(minutes=25), now + timedelta(minutes=35)
    with get_conn() as conn:
        users = conn.execute("SELECT id, telegram_id FROM users WHERE notify_before_match = 1").fetchall()
    for user in users:
        favs = list_favorites(user["id"], fav_type="team")
        fav_ids = {f["fav_ext_id"] for f in favs if f["fav_ext_id"]}
        if not fav_ids:
            continue
        for league in LEAGUES.values():
            for f in get_fixtures_by_league(league["id"]):
                home_id, away_id = f["teams"]["home"]["id"], f["teams"]["away"]["id"]
                if home_id not in fav_ids and away_id not in fav_ids:
                    continue
                try:
                    match_time = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, KeyError):
                    continue
                if window_start <= match_time <= window_end:
                    try:
                        await app.bot.send_message(
                            chat_id=user["telegram_id"],
                            text=f"🔔 Favori takımının maçına 30 dakika kaldı!\n{format_fixture_summary(f)}",
                        )
                    except Exception as e:
                        logger.warning("Favori bildirimi gönderilemedi: %s", e)


def setup_scheduler(app):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(morning_sync, args=[app])
    scheduler.add_job(send_daily_update, "cron", hour=DAILY_UPDATE_HOUR, minute=DAILY_UPDATE_MINUTE, args=[app])
    scheduler.add_job(check_favorite_matches, "interval", minutes=10, args=[app])
    scheduler.start()
    return scheduler


# ============================================================
# ROUTER'LAR VE ANA ÇALIŞTIRMA
# ============================================================


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "noop":
        return
    if data == "menu:home":
        await show_home(update, context); return
    if data in ("menu:matches", "menu:stats"):
        await show_leagues(update, context); return
    if data == "menu:allmatches":
        await show_all_matches(update, context, 0); return
    if data.startswith("allmatches_page:"):
        await show_all_matches_page(update, context, int(data.split(":")[1])); return
    if data == "allmatches:filter":
        await prompt_match_filter(update, context); return
    if data == "allmatches:aifilter":
        await prompt_ai_filter(update, context); return
    if data == "allmatches:clear":
        await clear_match_filter(update, context); return
    if data == "menu:ai":
        await enter_ai_mode(update, context); return
    if data == "menu:journal":
        await show_journal(update, context); return
    if data == "menu:favorites":
        await show_favorites(update, context); return
    if data == "menu:performance":
        await show_performance(update, context); return
    if data == "menu:notifications":
        await show_notifications(update, context); return
    if data == "menu:settings":
        await show_settings(update, context); return

    if data.startswith("league:"):
        await show_fixtures(update, context, data.split(":", 1)[1]); return

    if data.startswith("match:"):
        parts = data.split(":")
        fixture_id = int(parts[1])
        action = parts[2] if len(parts) > 2 else None
        if action is None:
            await show_match_detail(update, context, fixture_id)
        elif action == "stats":
            await show_stats(update, context, fixture_id)
        elif action == "odds":
            await show_odds(update, context, fixture_id)
        elif action == "ai":
            await run_ai_analysis(update, context, fixture_id)
        elif action == "fav":
            await add_favorite_from_match(update, context, fixture_id)
        elif action == "note":
            await prompt_note(update, context, fixture_id)
        elif action == "addbet":
            await prompt_add_bet(update, context, fixture_id)
        return

    if data.startswith("bet:"):
        await show_bet_detail(update, context, int(data.split(":")[1])); return
    if data.startswith("betstatus:"):
        _, bet_id, status = data.split(":")
        await handle_update_bet_status(update, context, int(bet_id), status); return
    if data.startswith("betdelete:"):
        await handle_delete_bet(update, context, int(data.split(":")[1])); return
    if data.startswith("notify:toggle:"):
        await toggle_notification(update, context, data.split(":")[2]); return


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if mode == "ai_chat":
        await handle_ai_chat_text(update, context)
    elif mode == "awaiting_match_filter":
        await handle_match_filter_text(update, context)
    elif mode == "awaiting_ai_filter":
        await handle_ai_filter_text(update, context)
    elif mode == "awaiting_note":
        user = update.effective_user
        user_db_id = get_or_create_user(user.id, user.username)
        fixture_id = context.user_data.get("pending_fixture")
        if fixture_id:
            add_match_note(user_db_id, fixture_id, update.message.text)
            await update.message.reply_text("📝 Notun kaydedildi.")
        context.user_data.pop("mode", None)
        context.user_data.pop("pending_fixture", None)
    elif mode == "awaiting_bet":
        await handle_bet_text_input(update, context)
    else:
        await update.message.reply_text("Ne yapmak istediğini anlayamadım. /start yazarak ana menüye dönebilirsin.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN tanımlı değil. Railway'de Variables kısmını kontrol edin.")

    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    setup_scheduler(app)

    logger.info("BetAI botu başlatılıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
