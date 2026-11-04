#!/usr/bin/env python3
"""Telegram-бот: слежка за коллекцией OpenSea. Один файл: бот + трей + автозапуск. Команд нет."""
# ═════════════ трей + автозапуск (Windows) ═════════════
# python bot.py            → иконка в трее, бот фоном, автозапуск включается сам
# python bot.py --console  → бот прямо в консоли (для отладки)
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SELF = Path(__file__).resolve()
BASE = SELF.parent
WORKER_MODE = ("--worker" in sys.argv) or ("--console" in sys.argv) or os.name != "nt"

# ═════════════ список отслеживаемых коллекций (collections.json) ═════════════
# Общий файл для трея и бота: трей меняет его и перезапускает бота.
COLL_FILE = BASE / "collections.json"


def parse_slug(text):
    """'https://opensea.io/collection/foo' или 'foo' → 'foo'; мусор → None."""
    t = (text or "").strip().lower()
    m = re.search(r"opensea\.io/(?:[a-z]{2}/)?collection/([a-z0-9][a-z0-9_-]*)", t)
    if m:
        t = m.group(1)
    return t if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", t) else None


def _default_slug():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return parse_slug(os.getenv("COLLECTION_SLUG", "glhfers")) or "glhfers"


def load_collections():
    if os.getenv("GITHUB_ACTIONS") and os.getenv("COLLECTIONS", "").strip():
        # на GitHub список задаётся переменной репозитория COLLECTIONS (через запятую/пробел/перенос строки)
        out = []
        for x in re.split(r"[,\s]+", os.environ["COLLECTIONS"]):
            sl = parse_slug(x)
            if sl and sl not in out:
                out.append(sl)
        if out:
            return out
    try:
        out = []
        for x in json.loads(COLL_FILE.read_text("utf-8")):
            sl = parse_slug(str(x))
            if sl and sl not in out:
                out.append(sl)
        if out:
            return out
    except (OSError, ValueError, TypeError):
        pass
    return [_default_slug()]


def save_collections(lst):
    tmp = COLL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(lst, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(COLL_FILE)


def _collection_exists(slug):
    """Быстрая проверка в OpenSea. Если проверить не удалось (сеть, ключ) — считаем, что ок."""
    try:
        import httpx
        r = httpx.get(f"https://api.opensea.io/api/v2/collections/{slug}",
                      headers={"x-api-key": os.environ.get("OPENSEA_API_KEY", ""), "accept": "application/json"},
                      timeout=10)
        return r.status_code != 404
    except Exception:
        return True



def _tail_log(path, kind, n):
    """Последние n строк лога нужного типа ('evt' — события, 'req' — запросы), новые сверху."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            raw = f.read()
    except OSError:
        return []
    out = []
    for b in raw.splitlines():
        try:
            line = b.decode("utf-8")
        except UnicodeDecodeError:
            line = b.decode("cp1251", "replace")
        if not re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line):
            continue
        t, msg = line[11:19], line[24:].strip()
        if msg.startswith(f"INFO {kind} | "):
            out.append(f"{t} {msg[len(kind) + 8:]}")
        elif kind == "evt" and msg.startswith(("ERROR", "WARNING")):
            out.append(f"{t} ⚠ {msg}")
    return out[-n:][::-1]


def _req_rate(path):
    """Сколько запросов к OpenSea было за последние 60 секунд (по bot.req.log)."""
    from datetime import datetime, timedelta

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 60_000))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    cut, n = datetime.now() - timedelta(seconds=60), 0
    for line in raw.splitlines():
        try:
            t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if t >= cut and " req | " in line:
            n += 1
    return n


def _tray_main():
    import socket
    import threading

    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if not pyw.exists():
        pyw = exe

    # запущено из консоли: перезапускаемся без окна и закрываем консоль
    if exe.name.lower() == "python.exe" and pyw != exe:
        subprocess.Popen([str(pyw), str(SELF)], cwd=str(BASE),
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
        print("Бот запущен в трее (иконка у часов). Это окно можно закрыть.")
        return

    import pystray
    from PIL import Image, ImageDraw

    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47831))  # второй экземпляр сразу выходит
    except OSError:
        return

    LOG = BASE / "bot.log"
    REQLOG = BASE / "bot.req.log"
    try:
        errlog = BASE / "bot.err.log"
        if errlog.stat().st_size > 200_000:
            errlog.write_bytes(errlog.read_bytes()[-50_000:])
    except OSError:
        pass
    vbs = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/glhfers-bot.vbs"
    marker = BASE / ".autostart_done"

    def autostart_on():
        vbs.parent.mkdir(parents=True, exist_ok=True)
        vbs.write_text(
            'Set s = CreateObject("WScript.Shell")\r\n'
            f's.CurrentDirectory = "{BASE}"\r\n'
            f's.Run """{pyw}"" ""{SELF}""", 0, False\r\n',
            encoding="utf-16",
        )

    def autostart_off():
        vbs.unlink(missing_ok=True)

    if not marker.exists():  # первый запуск: включаем автозапуск (потом управляется из меню)
        autostart_on()
        marker.touch()

    state = {"proc": None, "stop": False, "icon": None, "dialog": False}
    mtx = threading.Lock()

    def start():
        with mtx:
            p = state["proc"]
            if p and p.poll() is None:
                return
            err = open(BASE / "bot.err.log", "a", encoding="utf-8", buffering=1)
            state["proc"] = subprocess.Popen(
                [str(pyw), str(SELF), "--worker"], cwd=str(BASE),
                stdout=err, stderr=subprocess.STDOUT, creationflags=0x08000000,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )

    def kill():
        with mtx:
            p = state["proc"]
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()

    def watch():
        while not state["stop"]:
            time.sleep(10)
            try:
                ev = _tail_log(LOG, "evt", 1)
                state["icon"].title = ("glhfers-bot: " + ev[0])[:120] if ev else "glhfers-bot"
            except Exception:
                pass
            p = state["proc"]
            if not state["stop"] and p and p.poll() is not None:
                try:
                    state["icon"].notify("Бот упал, перезапускаю. Подробности в логе.", "glhfers-bot")
                except Exception:
                    pass
                start()

    def open_log(icon, item):
        LOG.touch(exist_ok=True)
        os.startfile(str(LOG))

    def restart(icon, item):
        kill()
        start()

    def toggle_autostart(icon, item):
        autostart_off() if vbs.exists() else autostart_on()

    def add_collection(icon, item):
        if state["dialog"]:
            return
        state["dialog"] = True

        def run():
            try:
                import tkinter as tk
                from tkinter import messagebox, simpledialog

                title = "glhfers-bot — добавить коллекцию"
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                try:
                    raw = simpledialog.askstring(title, "Slug коллекции или ссылка на неё в OpenSea:", parent=root)
                    if raw is None:
                        return
                    slug = parse_slug(raw)
                    if not slug:
                        messagebox.showerror(title, "Не похоже на slug коллекции.", parent=root)
                        return
                    cur = load_collections()
                    if slug in cur:
                        messagebox.showinfo(title, f"«{slug}» уже отслеживается.", parent=root)
                        return
                    if not _collection_exists(slug):
                        messagebox.showerror(title, f"Коллекция «{slug}» не найдена на OpenSea.", parent=root)
                        return
                    save_collections(cur + [slug])
                finally:
                    root.destroy()
                restart(None, None)
                icon.update_menu()
            except Exception:
                pass
            finally:
                state["dialog"] = False

        threading.Thread(target=run, daemon=True).start()

    def remove_collection(slug):
        def action(icon, item):
            cur = [x for x in load_collections() if x != slug]
            if not cur:  # последнюю убирать нельзя
                return
            save_collections(cur)
            restart(None, None)
            icon.update_menu()

        return action

    def quit_(icon, item):
        state["stop"] = True
        kill()
        icon.stop()

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(46, 160, 90, 255))
    d.polygon([(32, 14), (50, 38), (38, 38), (38, 50), (26, 50), (26, 38), (14, 38)], fill="white")

    def build_menu():
        def row(text, action=open_log, enabled=True):
            return pystray.MenuItem(text[:95].replace("&", "&&"), action, enabled=enabled)

        header = lambda t: row(t, lambda icon, item: None, enabled=False)
        reqs = _tail_log(REQLOG, "req", 10) or ["запросов пока нет"]
        evs = _tail_log(LOG, "evt", 10) or ["событий пока нет"]
        colls = load_collections()
        coll_menu = pystray.Menu(
            *[pystray.MenuItem(f"✕ Убрать {c}", remove_collection(c), enabled=len(colls) > 1) for c in colls],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("➕ Добавить коллекцию…", add_collection),
        )
        return [
            header(f"— Запросы к OpenSea: {_req_rate(REQLOG)}/мин —"), *[row(r) for r in reqs],
            pystray.Menu.SEPARATOR,
            header("— События —"), *[row(e) for e in evs],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Коллекции ({len(colls)})", coll_menu),
            pystray.MenuItem("Открыть полный лог", open_log),
            pystray.MenuItem("Перезапустить", restart),
            pystray.MenuItem("Автозапуск с Windows", toggle_autostart, checked=lambda item: vbs.exists()),
            pystray.MenuItem("Выход", quit_),
        ]

    icon = pystray.Icon("glhfers-bot", img, "glhfers-bot", menu=pystray.Menu(build_menu))
    state["icon"] = icon
    start()
    threading.Thread(target=watch, daemon=True).start()
    icon.run()


if __name__ == "__main__" and not WORKER_MODE:
    _tray_main()
    sys.exit(0)
# ═════════════ дальше сам бот ═════════════

import asyncio
import collections
import html
import logging
import os
import sqlite3
import statistics
import time

import httpx
from dotenv import load_dotenv
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER = int(os.environ["TELEGRAM_CHAT_ID"])
OS_KEY = os.environ["OPENSEA_API_KEY"]
LEGACY_SLUG = _default_slug()  # коллекция, к которой относятся старые данные в state.db
POLL = float(os.getenv("POLL_SECONDS", "1.0"))  # листинги (снайп): ~60 запросов/мин
SALES_POLL = float(os.getenv("SALES_POLL_SECONDS", "1.7"))  # продажи: ~35 запросов/мин
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "90"))  # жёсткий потолок запросов к OpenSea в минуту
SIGNAL_ALERT_SCORE = int(os.getenv("SIGNAL_ALERT_SCORE", "3"))
SNIPE_DISCOUNT_PCT = float(os.getenv("SNIPE_DISCOUNT_PCT", "10"))
SNIPE_PRICE = float(os.getenv("SNIPE_PRICE", "0")) or None
NOTIFY_NEW_LISTINGS = os.getenv("NOTIFY_NEW_LISTINGS", "1") != "0"  # слать в Telegram и обычные новые листинги, не только снайпы
LISTING_GROUP_OVER = int(os.getenv("LISTING_GROUP_OVER", "5"))  # больше стольких за раз — одним сообщением
LISTING_DEDUP_SEC = int(os.getenv("LISTING_DEDUP_SEC", "21600"))  # тот же токен по той же цене не повторять 6 ч
FLOOR_ALERT_PCT = float(os.getenv("FLOOR_ALERT_PCT", "5"))
COOLDOWN_FLOOR = int(os.getenv("COOLDOWN_FLOOR_S", "300"))    # мин. пауза между алертами про флор
COOLDOWN_SIGNAL = int(os.getenv("COOLDOWN_SIGNAL_S", "1800"))  # мин. пауза между алертами «окно продажи»
SALES_GROUP_OVER = int(os.getenv("SALES_GROUP_OVER", "3"))     # больше N новых продаж за раз — одним сообщением
DB_PATH = os.getenv("DB_PATH", "state.db")
SLUGS = load_collections()
if not COLL_FILE.exists():
    save_collections(SLUGS)


def coll_url(slug):
    return f"https://opensea.io/collection/{slug}"


_FMT = "%(asctime)s %(levelname)s %(message)s"
if "--worker" in sys.argv:
    from logging.handlers import RotatingFileHandler

    logging.basicConfig(level=logging.INFO, format=_FMT, handlers=[
        RotatingFileHandler(BASE / "bot.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")])
else:
    logging.basicConfig(level=logging.INFO, format=_FMT)
log = logging.getLogger("bot")
reqlog = logging.getLogger("req")
if os.getenv("GITHUB_ACTIONS"):
    reqlog.propagate = False  # в логе Actions не нужны ~100 строк «req» в минуту
if "--worker" in sys.argv:
    reqlog.propagate = False
    _h = RotatingFileHandler(BASE / "bot.req.log", maxBytes=300_000, backupCount=1, encoding="utf-8")
    _h.setFormatter(logging.Formatter(_FMT))
    reqlog.addHandler(_h)
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # без шума «Running job…»
logging.getLogger("httpx").setLevel(logging.WARNING)

# ───────────────────────── storage ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
    create table if not exists kv(k text primary key, v text);
    create table if not exists seen(kind text, id text, primary key(kind, id));
    create table if not exists floor(ts integer, price real);
    create table if not exists sales(id text primary key, ts integer, price real, token text);
    """
)
for _t in ("sales", "floor"):  # данные теперь по коллекциям; старые строки — к прежней коллекции
    try:
        db.execute(f"alter table {_t} add column slug text")
    except sqlite3.OperationalError:
        pass
    db.execute(f"update {_t} set slug=? where slug is null", (LEGACY_SLUG,))
try:
    db.execute("alter table seen add column ts integer")
except sqlite3.OperationalError:
    pass
db.execute("update seen set ts=? where ts is null", (int(time.time()),))
db.commit()


def kv_get(k, default=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone()
    return r[0] if r else default


def kv_set(k, v):
    db.execute(
        "insert into kv(k, v) values(?, ?) on conflict(k) do update set v=excluded.v",
        (k, str(v)),
    )
    db.commit()


def kv_del(k):
    db.execute("delete from kv where k=?", (k,))
    db.commit()


def kv_float(k, default=None):
    v = kv_get(k)
    return float(v) if v not in (None, "") else default


# значения по коллекциям хранятся под ключами "slug:имя"
def ckv_get(slug, k, default=None):
    return kv_get(f"{slug}:{k}", default)


def ckv_set(slug, k, v):
    kv_set(f"{slug}:{k}", v)


def ckv_float(slug, k, default=None):
    return kv_float(f"{slug}:{k}", default)


def _migrate_kv():
    """Старые общие ключи (до мульти-коллекций) → ключи прежней коллекции."""
    for k in ("last_sale_ts", "floor", "listings_init", "floor_ref", "hb_ts", "signal_ts",
              "signal_green", "welcomed", "cd_floor", "cd_signal"):
        v = kv_get(k)
        if v is None:
            continue
        new = f"cd_{LEGACY_SLUG}:{k[3:]}" if k.startswith("cd_") else f"{LEGACY_SLUG}:{k}"
        if kv_get(new) is None:
            kv_set(new, v)
        kv_del(k)


_migrate_kv()


def is_seen(kind, id_):
    return db.execute("select 1 from seen where kind=? and id=?", (kind, id_)).fetchone() is not None


def mark_seen(kind, id_):
    db.execute("insert or ignore into seen(kind, id, ts) values(?, ?, ?)", (kind, id_, int(time.time())))
    db.commit()


def cooldown_ok(key, seconds):
    """True, если с прошлого срабатывания key прошло не меньше seconds (и отмечает срабатывание)."""
    now = time.time()
    if now - float(kv_get(f"cd_{key}", 0)) < seconds:
        return False
    kv_set(f"cd_{key}", now)
    return True


def maintenance():
    """Раз в час чистим старое, чтобы state.db не пухла."""
    now = int(time.time())
    if now - int(kv_get("maint_ts", 0)) < 3600:
        return
    kv_set("maint_ts", now)
    db.execute("delete from seen where ts < ?", (now - 14 * 86400,))
    db.execute("delete from sales where ts < ?", (now - 30 * 86400,))
    db.commit()


def save_sale(s, slug):
    cur = db.execute(
        "insert or ignore into sales(id, ts, price, token, slug) values(?, ?, ?, ?, ?)",
        (s["id"], s["ts"], s["price"], s["token"], slug),
    )
    db.commit()
    return cur.rowcount > 0


def record_floor(slug, price):
    now = int(time.time())
    last = db.execute("select max(ts) from floor where slug=?", (slug,)).fetchone()[0] or 0
    if now - last >= 300:
        db.execute("insert into floor(ts, price, slug) values(?, ?, ?)", (now, price, slug))
        db.execute("delete from floor where ts < ?", (now - 30 * 86400,))
        db.commit()


def floor_at(slug, seconds_ago):
    ts = int(time.time()) - seconds_ago
    r = db.execute(
        "select price from floor where slug=? and ts <= ? order by ts desc limit 1", (slug, ts)
    ).fetchone()
    return r[0] if r else None


def sales_since(slug, seconds):
    since = int(time.time()) - seconds
    return [r[0] for r in db.execute("select price from sales where slug=? and ts >= ?", (slug, since))]


# ───────────────────────── OpenSea API ─────────────────────────
class OpenSea:
    def __init__(self):
        self.stamps = collections.deque()
        self.window = 60.0
        self.http = httpx.AsyncClient(
            base_url="https://api.opensea.io/api/v2",
            headers={"x-api-key": OS_KEY, "accept": "application/json"},
            timeout=20,
        )

    async def _throttle(self):
        """Скользящее окно: не больше RATE_LIMIT запросов за минуту, что бы ни творили задачи."""
        while True:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] > self.window:
                self.stamps.popleft()
            if len(self.stamps) < RATE_LIMIT:
                break
            await asyncio.sleep(max(0.05, self.window - (now - self.stamps[0])))
        self.stamps.append(time.monotonic())

    def rate(self):
        now = time.monotonic()
        return sum(1 for t in self.stamps if now - t <= self.window)

    async def get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(3):
            await self._throttle()
            t0 = time.time()
            q = " ".join(f"{k}={str(v)[:12]}" for k, v in params.items())
            what = f"GET {path.replace('/collections/', '/').replace('/collection/', '/')} {q}".strip()
            try:
                r = await self.http.get(path, params=params)
            except httpx.HTTPError as e:
                reqlog.info("req | %s → ✗ %s", what, type(e).__name__)
                raise
            reqlog.info("req | %s → %s (%sмс)", what, r.status_code, int((time.time() - t0) * 1000))
            if r.status_code == 429:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("OpenSea: rate limit (429)")


api = OpenSea()

px = httpx.AsyncClient(timeout=8)
_usd = {"eth": None}


async def refresh_rate():
    """Курс ETH/USD: Coinbase, запасной — CoinGecko. Не использует OpenSea-лимит."""
    for name, url, pick in (
        ("coinbase", "https://api.coinbase.com/v2/prices/ETH-USD/spot", lambda j: float(j["data"]["amount"])),
        ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
         lambda j: float(j["ethereum"]["usd"])),
    ):
        try:
            r = await px.get(url)
            r.raise_for_status()
            _usd["eth"] = pick(r.json())
            reqlog.info("req | GET %s ETH-USD → %s", name, r.status_code)
            return
        except Exception as e:
            reqlog.info("req | GET %s ETH-USD → ✗ %s", name, type(e).__name__)


async def update_rate(ctx):
    await refresh_rate()


def usd(price, sym):
    """' (≈$1,234)' для ETH/WETH, иначе пусто."""
    r = _usd["eth"]
    if r and price is not None and str(sym).upper() in ("ETH", "WETH"):
        return f" (≈${price * r:,.0f})"
    return ""


def fmt(x, nd=4):
    if x is None:
        return "—"
    return f"{x:.{nd}f}".rstrip("0").rstrip(".") or "0"


def short(a):
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "?")


def esc(s):
    return html.escape(str(s))


def money(p):
    return int(p["value"]) / 10 ** int(p["decimals"]), p.get("currency", "")


def parse_sale(ev, slug):
    pay = ev.get("payment") or {}
    qty = max(int(ev.get("quantity") or 1), 1)
    price = int(pay.get("quantity", 0)) / 10 ** int(pay.get("decimals", 18)) / qty
    nft = ev.get("nft") or {}
    tid = nft.get("identifier", "?")
    ts = int(ev.get("event_timestamp") or ev.get("closing_date") or time.time())
    return {
        "id": f"{ev.get('transaction', '')}:{tid}:{ts}",
        "ts": ts,
        "price": price,
        "sym": pay.get("symbol", ""),
        "token": tid,
        "name": nft.get("name") or f"#{tid}",
        "url": nft.get("opensea_url") or coll_url(slug),
        "buyer": ev.get("buyer"),
        "seller": ev.get("seller"),
    }


def parse_listing(l, slug):
    price, sym = money(l["price"]["current"])
    tid, contract = "?", ""
    try:
        off = l["protocol_data"]["parameters"]["offer"][0]
        tid, contract = off["identifierOrCriteria"], off["token"]
    except Exception:
        pass
    chain = l.get("chain", "ethereum")
    url = f"https://opensea.io/assets/{chain}/{contract}/{tid}" if contract else coll_url(slug)
    return {"id": l["order_hash"], "price": price, "sym": sym, "token": tid, "url": url}


def parse_offer(o):
    p = o.get("price") or {}
    p = p.get("current", p)
    total, sym = money(p)
    qty = 1
    try:
        qty = max(int(o["protocol_data"]["parameters"]["consideration"][0]["startAmount"]), 1)
    except Exception:
        pass
    return {"price": total / qty, "sym": sym, "qty": qty}


async def get_listings(slug, limit=50):
    d = await api.get(f"/listings/collection/{slug}/best", limit=limit)
    return sorted((parse_listing(x, slug) for x in d.get("listings", [])), key=lambda x: x["price"])


async def get_offers(slug):
    d = await api.get(f"/offers/collection/{slug}")
    return sorted((parse_offer(x) for x in d.get("offers", [])), key=lambda x: -x["price"])


async def fetch_sales(slug, after=None, max_pages=1):
    out, nxt = [], None
    for _ in range(max_pages):
        d = await api.get(
            f"/events/collection/{slug}", event_type="sale", after=after, limit=50, next=nxt
        )
        out += [parse_sale(e, slug) for e in d.get("asset_events", [])]
        nxt = d.get("next")
        if not nxt:
            break
    return out


# ───────────────────────── сигнал «пора продавать» ─────────────────────────
async def compute_signal(slug):
    ls = await get_listings(slug)
    if not ls:
        return None
    floor, sym = ls[0]["price"], ls[0]["sym"]
    score, notes = 0, []

    def add(delta, text):
        nonlocal score
        score += delta
        notes.append(("🟢 " if delta > 0 else "🔴 " if delta < 0 else "⚪ ") + text)

    # 1. тренд флора за 24ч (по собственной истории бота)
    f24 = floor_at(slug, 86400)
    if f24:
        ch = (floor / f24 - 1) * 100
        add(1 if ch >= 3 else -1 if ch <= -3 else 0, f"флор за 24ч: {ch:+.1f}%")
    else:
        notes.append("⚪ история флора за 24ч ещё копится")

    # 2. спрос: продажи за 24ч против среднего за 7д (из stats OpenSea)
    try:
        st = await api.get(f"/collections/{slug}/stats")
        iv = {i["interval"]: i for i in st.get("intervals", [])}
        s1 = (iv.get("one_day") or {}).get("sales")
        s7 = (iv.get("seven_day") or {}).get("sales")
        if s1 is not None and s7:
            avg = s7 / 7
            ratio = s1 / avg if avg else 0
            add(1 if ratio >= 1.3 else -1 if ratio <= 0.6 else 0,
                f"продаж за 24ч: {s1} (среднее в день за 7д: {avg:.1f})")
    except Exception:
        log.exception("stats failed")

    # 3. средняя цена последних продаж против флора
    sold = sales_since(slug, 86400)
    median24 = statistics.median(sold) if len(sold) >= 3 else None
    if median24:
        prem = (median24 / floor - 1) * 100
        add(1 if prem >= 5 else -1 if prem <= -5 else 0,
            f"медиана продаж за 24ч: {fmt(median24)} ({prem:+.0f}% к флору)")

    # 4. поддержка бидами
    best_offer = None
    try:
        offers = await get_offers(slug)
        if offers:
            best_offer = offers[0]["price"]
            r = best_offer / floor
            add(1 if r >= 0.9 else -1 if r < 0.7 else 0,
                f"лучший коллекшен-оффер: {fmt(best_offer)} ({r * 100:.0f}% от флора)")
    except Exception:
        log.exception("offers failed")

    # 5. предложение у флора (насколько «тонкая» полка)
    near = sum(1 for x in ls if x["price"] <= floor * 1.10)
    add(1 if near <= 3 else -1 if near >= 10 else 0,
        f"листингов в пределах +10% от флора: {near}{'+' if near >= len(ls) else ''}")

    return {"floor": floor, "sym": sym, "score": score, "notes": notes,
            "median24": median24, "best_offer": best_offer, "listings": ls}


def verdict(score):
    if score >= SIGNAL_ALERT_SCORE:
        return "🟢 Хорошее окно для продажи"
    if score >= 1:
        return "🟡 Нейтрально — можно ждать лучшего момента"
    return "🔴 Слабый момент — лучше подождать (или ставить у флора, если нужно быстро)"


def signal_text(sig):
    fl, sym = sig["floor"], sig["sym"]
    ls = sig["listings"]
    target = max(fl, sig["median24"] or fl)
    lines = [f"{verdict(sig['score'])}  (скор {sig['score']:+d})", ""] + sig["notes"] + [""]
    lines.append(f"Флор: <b>{fmt(fl)} {sym}</b>{usd(fl, sym)}")
    lines.append(f"Быстро продать: ≈ {fmt(fl * 0.99)} {sym} (чуть ниже флора)")
    if sig["score"] >= 2:
        nxt = ls[2]["price"] if len(ls) > 2 else target
        lines.append(f"Выгодно выставить: {fmt(target)}–{fmt(max(target, nxt))} {sym}")
    else:
        lines.append(f"Ориентир на ожидание: ≥ {fmt(target)} {sym}")
    lines.append("<i>Это эвристика по данным OpenSea, не финсовет. Комиссии/роялти вычитай сам.</i>")
    return "\n".join(lines)


# ───────────────────────── тексты алертов ─────────────────────────
def sale_text(s, floor):
    prem = f" ({(s['price'] / floor - 1) * 100:+.0f}% к флору)" if floor else ""
    return (
        f"💸 <b>Продажа {esc(s['name'])}</b>\n"
        f"Цена: <b>{fmt(s['price'])} {esc(s['sym'])}</b>{usd(s['price'], s['sym'])}{prem}\n"
        + (f"Флор: <b>{fmt(floor)} {esc(s['sym'])}</b>{usd(floor, s['sym'])}\n" if floor else "")
        + f"{short(s['seller'])} → {short(s['buyer'])}\n"
        f"<a href=\"{s['url']}\">OpenSea</a>"
    )


def snipe_text(l, floor):
    d = f" ({(l['price'] / floor - 1) * 100:+.0f}% к флору)" if floor else ""
    return (
        f"🎯 <b>СНАЙП: #{esc(l['token'])}</b>\n"
        f"Цена: <b>{fmt(l['price'])} {esc(l['sym'])}</b>{usd(l['price'], l['sym'])}{d}\n"
        + (f"Флор: <b>{fmt(floor)} {esc(l['sym'])}</b>{usd(floor, l['sym'])}\n" if floor else "")
        + f"<a href=\"{l['url']}\">Купить на OpenSea</a>"
    )


def new_listing_text(l, floor):
    d = f" ({(l['price'] / floor - 1) * 100:+.0f}% к флору)" if floor else ""
    return (
        f"🆕 <b>Новый листинг #{esc(l['token'])}</b>\n"
        f"Цена: <b>{fmt(l['price'])} {esc(l['sym'])}</b>{usd(l['price'], l['sym'])}{d}\n"
        + (f"Флор: <b>{fmt(floor)} {esc(l['sym'])}</b>{usd(floor, l['sym'])}\n" if floor else "")
        + f"<a href=\"{l['url']}\">Открыть на OpenSea</a>"
    )


_listing_sent = {}  # (slug, токен, цена) -> когда уже слали; гасит повторы, когда продавец переставляет ордер


def _listing_dup(slug, l):
    now = time.time()
    for k in [k for k, t in _listing_sent.items() if now - t > LISTING_DEDUP_SEC]:
        del _listing_sent[k]
    key = (slug, l["token"], round(l["price"], 8))
    if key in _listing_sent:
        return True
    _listing_sent[key] = now
    return False


def evt(msg, slug=None):
    """Строка для трея: что бот делает прямо сейчас."""
    if slug and len(SLUGS) > 1:
        msg = f"[{slug}] {msg}"
    log.info("evt | %s", msg)


async def notify(ctx, text, slug):
    evt("📨 " + re.sub(r"<[^>]+>", "", text).replace("\n", " · ")[:110], slug)
    if len(SLUGS) > 1:
        text = f"📦 <b>{esc(slug)}</b>\n" + text
    await ctx.bot.send_message(OWNER, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ───────────────────────── фоновые проверки ─────────────────────────
# Каждая задача получает slug коллекции через ctx.job.data
async def check_sales(ctx, slug):
    last = int(ckv_get(slug, "last_sale_ts", 0))
    if not last:  # первый запуск: наполняем историю без алертов
        for s in await fetch_sales(slug, after=int(time.time()) - 7 * 86400, max_pages=4):
            save_sale(s, slug)
        last = db.execute("select coalesce(max(ts), ?) from sales where slug=?",
                          (int(time.time()), slug)).fetchone()[0]
        ckv_set(slug, "last_sale_ts", last)
        evt(f"история продаж загружена: {len(sales_since(slug, 7 * 86400))} за 7д", slug)
        return
    new = [s for s in await fetch_sales(slug, after=last, max_pages=4) if save_sale(s, slug)]
    new.sort(key=lambda s: s["ts"])
    floor = ckv_float(slug, "floor")
    if len(new) > SALES_GROUP_OVER:
        prices = [x["price"] for x in new]
        await notify(ctx, (
            f"💸 <b>{len(new)} продаж подряд</b>\n"
            f"Цены: {fmt(min(prices))}–{fmt(max(prices))} {esc(new[0]['sym'])}, медиана {fmt(statistics.median(prices))}"
            + (f"\nФлор: {fmt(floor)} {esc(new[0]['sym'])}{usd(floor, new[0]['sym'])}" if floor else "")), slug)
    else:
        for s in new:
            await notify(ctx, sale_text(s, floor), slug)
    if new:
        ckv_set(slug, "last_sale_ts", max(s["ts"] for s in new))


async def check_listings(ctx, slug):
    ls = await get_listings(slug)
    if not ls:
        return
    first_run = ckv_get(slug, "listings_init") is None
    prev_floor = ckv_float(slug, "floor")
    snipe_abs, disc = SNIPE_PRICE, SNIPE_DISCOUNT_PCT
    fresh = []
    for l in ls:
        if is_seen("listing", l["id"]):
            continue
        mark_seen("listing", l["id"])
        if not first_run:
            fresh.append(l)
    for l in fresh:
        d = f" ({(l['price'] / prev_floor - 1) * 100:+.0f}% к флору)" if prev_floor else ""
        evt(f"новый листинг #{l['token']} — {fmt(l['price'])} {l['sym']}{d}", slug)
    fresh = [l for l in fresh if not _listing_dup(slug, l)]
    snipes = [l for l in fresh
              if (snipe_abs is not None and l["price"] <= snipe_abs)
              or (prev_floor and l["price"] <= prev_floor * (1 - disc / 100))]
    for l in snipes:
        await notify(ctx, snipe_text(l, prev_floor), slug)
    rest = [l for l in fresh if l not in snipes]
    if NOTIFY_NEW_LISTINGS and rest:
        if len(rest) > LISTING_GROUP_OVER:
            top = sorted(rest, key=lambda x: x["price"])[:LISTING_GROUP_OVER]
            lines = "\n".join(
                f"• <a href=\"{x['url']}\">#{esc(x['token'])}</a> — {fmt(x['price'])} {esc(x['sym'])}" for x in top)
            await notify(ctx, f"🆕 <b>{len(rest)} новых листингов</b>\nСамые дешёвые:\n{lines}", slug)
        else:
            for l in rest:
                await notify(ctx, new_listing_text(l, prev_floor), slug)
    if first_run:
        ckv_set(slug, "listings_init", 1)

    floor = ls[0]["price"]
    sym = ls[0]["sym"]
    if first_run:
        evt(f"листинги загружены: {len(ls)}, флор {fmt(floor)} {sym}", slug)
    elif prev_floor is not None and abs(floor - prev_floor) > 1e-12:
        evt(f"флор {fmt(prev_floor)} → {fmt(floor)} {sym}", slug)
    if prev_floor != floor:
        ckv_set(slug, "floor", floor)
    record_floor(slug, floor)

    now = time.time()
    if now - float(ckv_get(slug, "hb_ts", 0)) >= 300:  # раз в 5 минут: «живой»
        ckv_set(slug, "hb_ts", now)
        evt(f"✓ работаю: флор {fmt(floor)} {sym}, листингов в топ-50: {len(ls)}, "
            f"продаж за 24ч: {len(sales_since(slug, 86400))}, запросов/мин: {api.rate()}"
            + (f", ETH ${_usd['eth']:,.0f}" if _usd["eth"] else ""), slug)

    ref = ckv_float(slug, "floor_ref")
    if ref is None:
        ckv_set(slug, "floor_ref", floor)
        return
    pct = (floor / ref - 1) * 100
    if abs(pct) >= FLOOR_ALERT_PCT and cooldown_ok(f"{slug}:floor", COOLDOWN_FLOOR):
        ckv_set(slug, "floor_ref", floor)
        await notify(
            ctx,
            f"{'📈' if pct > 0 else '📉'} Флор {fmt(ref)} → <b>{fmt(floor)} {esc(ls[0]['sym'])}</b>{usd(floor, ls[0]['sym'])} ({pct:+.1f}%)",
            slug,
        )


async def check_signal(ctx, slug):
    maintenance()
    now = time.time()
    if now - float(ckv_get(slug, "signal_ts", 0)) < 600:
        return
    ckv_set(slug, "signal_ts", now)
    sig = await compute_signal(slug)
    if not sig:
        return
    evt(f"сигнал продажи: {verdict(sig['score']).split(' — ')[0]} (скор {sig['score']:+d})", slug)
    green = sig["score"] >= SIGNAL_ALERT_SCORE
    was_green = ckv_get(slug, "signal_green") == "1"
    if not green:
        ckv_set(slug, "signal_green", "0")
    elif not was_green and cooldown_ok(f"{slug}:signal", COOLDOWN_SIGNAL):
        ckv_set(slug, "signal_green", "1")
        await notify(ctx, "🔔 <b>Окно для продажи открылось</b>\n\n" + signal_text(sig), slug)


def _safe(step, per_collection=True):
    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await (step(ctx, ctx.job.data) if per_collection else step(ctx))
        except Exception:
            log.exception("%s failed", step.__name__)

    return job


# ───────────────────────── запуск ─────────────────────────
async def on_start(app: Application):
    await refresh_rate()
    for slug in SLUGS:
        if ckv_get(slug, "welcomed"):
            continue
        try:
            ls = await get_listings(slug)
            fl = f"{fmt(ls[0]['price'])} {ls[0]['sym']}{usd(ls[0]['price'], ls[0]['sym'])}" if ls else "—"
            await app.bot.send_message(
                OWNER,
                f"✅ Слежу за <b>{esc(slug)}</b>. Флор: {fl}\n"
                "Пришлю: продажи, снайп-листинги, движение флора и момент, когда выгодно продавать.",
                parse_mode=ParseMode.HTML,
            )
            ckv_set(slug, "welcomed", 1)
        except Exception:
            log.exception("startup message failed for %s", slug)


def main():
    app = Application.builder().token(TOKEN).post_init(on_start).build()
    jq = app.job_queue  # независимые задачи на каждую коллекцию, запросы идут вразнобой, а не пачкой
    n = len(SLUGS)  # общий лимит запросов делим на все коллекции: интервалы растут пропорционально
    for i, slug in enumerate(SLUGS):
        jq.run_repeating(_safe(check_listings), interval=POLL * n, first=2 + i * POLL, data=slug)
        jq.run_repeating(_safe(check_sales), interval=SALES_POLL * n, first=4 + i * SALES_POLL, data=slug)
        jq.run_repeating(_safe(check_signal), interval=60, first=15 + i * 7, data=slug)
    jq.run_repeating(_safe(update_rate, per_collection=False), interval=60, first=1)
    evt("слежу за: " + ", ".join(SLUGS))
    app.run_polling()


if __name__ == "__main__":
    main()
