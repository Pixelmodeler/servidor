"""
GMBR Social Server
==================
Deploy gratuito: Railway · Render · Fly.io · Koyeb
Requer Python 3.9+ e Flask.

Variáveis de ambiente:
  SECRET_KEY   → chave compartilhada com o launcher (padrão: gmbr-social-2025)
  PORT         → porta HTTP (padrão: 8080)
  DB_PATH      → caminho do SQLite (padrão: /data/social.db  ou  ./social.db)

railway.json / render.yaml já inclusos abaixo como comentários de referência.
"""

import os, sqlite3, time, hashlib, hmac, json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import urllib.request

# ─── Config ──────────────────────────────────────────────────────────────────
SECRET_KEY  = os.environ.get("SECRET_KEY",  "gmbr-social-2025")
PORT        = int(os.environ.get("PORT",    8080))
DB_PATH     = os.environ.get("DB_PATH",     os.path.join(
                  "/data" if os.path.isdir("/data") else ".", "social.db"))

ONLINE_TTL  = 90   # segundos até marcar offline
MSG_LIMIT   = 200  # máximo de msgs por conversa retornadas
_db_lock    = threading.Lock()
ADMIN_KEY   = os.environ.get("ADMIN_KEY", SECRET_KEY + "-admin")

# ─── PIX Manual ───────────────────────────────────────────────────────────────
PIX_KEY       = os.environ.get("PIX_KEY", "")          # Chave PIX
PIX_KEY_TYPE  = os.environ.get("PIX_KEY_TYPE", "email") # email|cpf|telefone|aleatoria
PIX_NAME      = os.environ.get("PIX_NAME", "GMBR Store") # Nome no PIX
PIX_EXPIRE_MIN = int(os.environ.get("PIX_EXPIRE_MIN", "60"))

# ─── Catálogo de itens premium ────────────────────────────────────────────────
# price_brl = preço em centavos (490 = R$4,90)
PREMIUM_ITEMS = [
  {"id":"banner_hologram",  "type":"banner", "label":"🔷 Hologram",   "price_brl":490,  "preview":"linear-gradient(135deg,#001840,#003060,#001840)"},
  {"id":"banner_glitch_ex", "type":"banner", "label":"⚡ Glitch EX",  "price_brl":490,  "preview":"linear-gradient(135deg,#000820,#001040,#000820)"},
  {"id":"banner_sakura",    "type":"banner", "label":"🌸 Sakura",      "price_brl":490,  "preview":"linear-gradient(135deg,#1a0010,#2d0020,#1a0010)"},
  {"id":"banner_thunder",   "type":"banner", "label":"⚡ Thunder God", "price_brl":790,  "preview":"linear-gradient(135deg,#100800,#201000,#100800)"},
  {"id":"banner_deep_void", "type":"banner", "label":"🌑 Deep Void",   "price_brl":990,  "preview":"linear-gradient(135deg,#050005,#0a000f,#050005)"},
  {"id":"av_crown",         "type":"avatar", "label":"👑 Crown",       "price_brl":390,  "icon":"👑"},
  {"id":"av_phoenix",       "type":"avatar", "label":"🔥 Phoenix",     "price_brl":590,  "icon":"🔥"},
  {"id":"av_lightning",     "type":"avatar", "label":"⚡ Lightning",   "price_brl":390,  "icon":"⚡"},
  {"id":"av_shadow",        "type":"avatar", "label":"🌑 Shadow Aura", "price_brl":590,  "icon":"🌑"},
  {"id":"av_diamond",       "type":"avatar", "label":"💎 Diamond",     "price_brl":990,  "icon":"💎"},
  {"id":"bundle_starter",    "type":"bundle", "label":"🎁 Starter Pack",  "price_brl":990,  "icon":"🎁",
   "includes":["banner_hologram","av_crown","av_lightning"]},
  {"id":"bundle_elite",      "type":"bundle", "label":"💎 Elite Pack",    "price_brl":1990, "icon":"💎",
   "includes":["banner_deep_void","banner_thunder","av_diamond","av_phoenix","av_shadow"]},
  {"id":"bundle_nature",     "type":"bundle", "label":"🌿 Nature Pack",   "price_brl":790,  "icon":"🌿",
   "includes":["banner_sakura","banner_arctic","av_ghost"]},
  {"id":"bundle_hacker",     "type":"bundle", "label":"💻 Hacker Pack",   "price_brl":990,  "icon":"💻",
   "includes":["banner_glitch_ex","banner_matrix","av_cyber"]},
  # ── Banners extras ──
  {"id":"banner_crimson",    "type":"banner", "label":"🔴 Crimson Lava",  "price_brl":490,
   "preview":"linear-gradient(135deg,#1a0300,#3d0800,#1a0200)"},
  {"id":"banner_ocean_depth","type":"banner", "label":"🌊 Ocean Depth",   "price_brl":490,
   "preview":"linear-gradient(180deg,#010e18,#001f3a,#010e18)"},
  {"id":"banner_matrix",     "type":"banner", "label":"☢ Matrix",          "price_brl":590,
   "preview":"linear-gradient(135deg,#010a01,#001600,#010a01)"},
  {"id":"banner_galaxy",     "type":"banner", "label":"🌌 Galaxy",          "price_brl":590,
   "preview":"linear-gradient(135deg,#020010,#0a0028,#020010)"},
  {"id":"banner_arctic",     "type":"banner", "label":"🧊 Arctic Storm",   "price_brl":490,
   "preview":"linear-gradient(135deg,#010c18,#002438,#010c18)"},
  {"id":"banner_sunset",     "type":"banner", "label":"🌅 Sunset",          "price_brl":490,
   "preview":"linear-gradient(180deg,#080012,#2d0a00,#080012)"},
  # ── Avatar Effects extras ──
  {"id":"av_ghost",          "type":"avatar", "label":"👻 Ghost",          "price_brl":390,  "icon":"👻"},
  {"id":"av_cyber",          "type":"avatar", "label":"🤖 Cyber Core",     "price_brl":490,  "icon":"🤖"},
  {"id":"av_blood_moon",     "type":"avatar", "label":"🌕 Blood Moon",     "price_brl":590,  "icon":"🌕"},
]
_ITEMS_BY_ID = {it["id"]: it for it in PREMIUM_ITEMS}

def _get_store_items(con=None):
    close = False
    if con is None: con = _db(); close = True
    rows = con.execute("SELECT * FROM store_items WHERE active=1 ORDER BY sort_order,id").fetchall()
    if close: pass
    return [{"id":r["id"],"type":r["type"],"label":r["label"],"icon":r["icon"],
             "price_brl":r["price_brl"],"preview":r["preview"],
             "includes":json.loads(r["includes"] or "[]")} for r in rows]

def _get_item_by_id(item_id, con=None):
    close = False
    if con is None: con = _db(); close = True
    row = con.execute("SELECT * FROM store_items WHERE id=? AND active=1",(item_id,)).fetchone()
    if close: pass
    if not row: return None
    return {"id":row["id"],"type":row["type"],"label":row["label"],"icon":row["icon"],
            "price_brl":row["price_brl"],"preview":row["preview"],
            "includes":json.loads(row["includes"] or "[]")}

def _pix_brcode(pix_key, pix_name, amount_brl, order_id):
    """Gera BR Code PIX (EMV/QR) para pagamento."""
    def tlv(tag, value): return f"{tag:02d}{len(value):02d}{value}"
    def crc16(data):
        crc = 0xFFFF
        for b in data.encode():
            crc ^= b << 8
            for _ in range(8): crc = (crc<<1)^0x1021 if crc&0x8000 else crc<<1
        return crc & 0xFFFF
    ma = tlv(0,"BR.GOV.BCB.PIX") + tlv(1, pix_key)
    info = f"GMBR-{order_id}"[:25]
    base = (tlv(0,"01") + tlv(26,ma) + tlv(52,"0000") + tlv(53,"986") +
            tlv(54,f"{amount_brl:.2f}") + tlv(58,"BR") +
            tlv(59,pix_name[:25]) + tlv(60,"SAO PAULO") +
            tlv(62,tlv(5,info)) + "6304")
    return base + f"{crc16(base):04X}"
# ─── DB com connection pool por thread ───────────────────────────────────────
import threading as _threading
_db_local = _threading.local()

def _db():
    """Retorna conexão SQLite reutilizável por thread (thread-local pool)."""
    con = getattr(_db_local, 'con', None)
    if con is None:
        con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        con.row_factory = sqlite3.Row
        # WAL mode: leituras não bloqueiam escritas, escritas não bloqueiam leituras
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA cache_size=-8000")  # 8MB cache por conexão
        con.execute("PRAGMA temp_store=MEMORY")
        _db_local.con = con
    return con

def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _db_lock:
        con = _db()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            gmbr_id      TEXT PRIMARY KEY,
            name         TEXT    NOT NULL DEFAULT '',
            display_name TEXT    DEFAULT '',
            avatar       TEXT    DEFAULT '',
            bio          TEXT    DEFAULT '',
            banner_type  TEXT    DEFAULT 'color',
            banner_val   TEXT    DEFAULT '',
            banner_color TEXT    DEFAULT '',
            avatar_effect TEXT   DEFAULT 'none',
            created_at   REAL    DEFAULT 0,
            last_seen    REAL    DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS friends (
            a      TEXT NOT NULL,
            b      TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            ts     REAL DEFAULT 0,
            PRIMARY KEY (a, b)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id   TEXT    NOT NULL,
            from_id   TEXT    NOT NULL,
            from_name TEXT    DEFAULT '',
            text      TEXT    NOT NULL,
            ts        REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, ts);
        CREATE INDEX IF NOT EXISTS idx_users_ls  ON users(last_seen);
        CREATE TABLE IF NOT EXISTS punishments (
            gmbr_id    TEXT NOT NULL,
            type       TEXT NOT NULL,  -- 'ban' | 'mute'
            reason     TEXT DEFAULT '',
            expires_at REAL DEFAULT 0, -- 0 = permanente
            created_at REAL DEFAULT 0,
            PRIMARY KEY (gmbr_id, type)
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            gmbr_id        TEXT NOT NULL,
            item_id        TEXT NOT NULL,
            stripe_session TEXT DEFAULT '',
            status         TEXT DEFAULT 'pending',
            amount_brl     INTEGER DEFAULT 0,
            created_at     REAL DEFAULT 0,
            paid_at        REAL DEFAULT 0,
            UNIQUE(gmbr_id, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_purchases_gmbr     ON purchases(gmbr_id);
        CREATE INDEX IF NOT EXISTS idx_purchases_session  ON purchases(stripe_session);
        CREATE INDEX IF NOT EXISTS idx_purchases_status   ON purchases(status);
        CREATE INDEX IF NOT EXISTS idx_purchases_gmbr_status ON purchases(gmbr_id, status);
        CREATE INDEX IF NOT EXISTS idx_friends_a ON friends(a, status);
        CREATE INDEX IF NOT EXISTS idx_friends_b ON friends(b, status);
        CREATE INDEX IF NOT EXISTS idx_users_gmbr ON users(gmbr_id);
        CREATE INDEX IF NOT EXISTS idx_punishments_gmbr ON punishments(gmbr_id, type);
        CREATE TABLE IF NOT EXISTS pix_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS store_items (
            id         TEXT PRIMARY KEY,
            type       TEXT NOT NULL DEFAULT 'banner',
            label      TEXT NOT NULL DEFAULT '',
            icon       TEXT NOT NULL DEFAULT '🎁',
            price_brl  INTEGER NOT NULL DEFAULT 490,
            preview    TEXT NOT NULL DEFAULT '',
            includes   TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0,
            active     INTEGER NOT NULL DEFAULT 1
        );
        """)
        con.commit()
    # Ativa WAL na conexão principal
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.close()
    except: pass
    print(f"[DB] {DB_PATH}")
    # Seed store_items se vazio
    try:
        con = _db()
        count = con.execute("SELECT COUNT(*) FROM store_items").fetchone()[0]
        if count == 0:
            for i, it in enumerate(PREMIUM_ITEMS):
                con.execute("""INSERT OR IGNORE INTO store_items
                    (id,type,label,icon,price_brl,preview,includes,sort_order,active)
                    VALUES (?,?,?,?,?,?,?,?,1)""", (
                    it["id"], it.get("type","banner"), it.get("label",it["id"]),
                    it.get("icon","🎁"), it["price_brl"],
                    it.get("preview",""), json.dumps(it.get("includes",[])), i
                ))
            con.commit()
            print(f"[DB] Seeded {len(PREMIUM_ITEMS)} store items")
    except Exception as e:
        print(f"[DB] store_items seed: {e}")

# ─── Auth ─────────────────────────────────────────────────────────────────────
def _check_sig(gmbr_id: str, sig: str) -> bool:
    """Launcher assina: hmac(SECRET_KEY, gmbr_id, sha256) → hex"""
    expected = hmac.new(
        SECRET_KEY.encode(), gmbr_id.upper().encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig.lower())

def _mk_sig(gmbr_id: str) -> str:
    return hmac.new(
        SECRET_KEY.encode(), gmbr_id.upper().encode(), hashlib.sha256
    ).hexdigest()

def _check_admin(b: dict) -> bool:
    return hmac.compare_digest(b.get("admin_key",""), ADMIN_KEY)

def _is_banned(cur, gmbr_id: str) -> bool:
    now = time.time()
    row = cur.execute(
        "SELECT 1 FROM punishments WHERE gmbr_id=? AND type='ban' AND (expires_at=0 OR expires_at>?)",
        (gmbr_id.upper(), now)).fetchone()
    return bool(row)

def _is_muted(cur, gmbr_id: str) -> bool:
    now = time.time()
    row = cur.execute(
        "SELECT 1 FROM punishments WHERE gmbr_id=? AND type='mute' AND (expires_at=0 OR expires_at>?)",
        (gmbr_id.upper(), now)).fetchone()
    return bool(row)

# ─── Helpers ─────────────────────────────────────────────────────────────────
def _conv(a: str, b: str) -> str:
    return "__".join(sorted([a.upper(), b.upper()]))

def _user(cur, gmbr_id: str):
    return cur.execute("SELECT * FROM users WHERE gmbr_id=?",
                       (gmbr_id.upper(),)).fetchone()

def _friends_of(cur, gmbr_id: str):
    gid = gmbr_id.upper()
    rows = cur.execute(
        "SELECT * FROM friends WHERE (a=? OR b=?) AND status='accepted'",
        (gid, gid)).fetchall()
    return [r["b"] if r["a"]==gid else r["a"] for r in rows]

def _pending_recv(cur, gmbr_id: str):
    gid = gmbr_id.upper()
    rows = cur.execute(
        "SELECT a FROM friends WHERE b=? AND status='pending'", (gid,)).fetchall()
    return [r["a"] for r in rows]

def _pending_sent(cur, gmbr_id: str):
    gid = gmbr_id.upper()
    rows = cur.execute(
        "SELECT b FROM friends WHERE a=? AND status='pending'", (gid,)).fetchall()
    return [r["b"] for r in rows]

def _fmt_user(row) -> dict:
    if not row: return {}
    return {
        "gmbr_id":      row["gmbr_id"],
        "name":         row["display_name"] or row["name"],
        "display_name": row["display_name"],
        "avatar":       row["avatar"],
        "bio":          row["bio"],
        "banner_type":  row["banner_type"],
        "banner_val":   row["banner_val"],
        "banner_color": row["banner_color"],
        "avatar_effect":row["avatar_effect"],
        "online":       (time.time() - (row["last_seen"] or 0)) < ONLINE_TTL,
        "created_at":   row["created_at"],
    }

# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # silencioso

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n: return {}
        try: return json.loads(self.rfile.read(n).decode())
        except: return {}

    def _ok(self, data: dict, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, code=400):
        self._ok({"ok": False, "error": msg}, code)

    def _auth(self, b: dict):
        """Valida gmbr_id + sig no body. Retorna gmbr_id ou None."""
        gid = b.get("gmbr_id","").strip().upper()
        sig = b.get("sig","").strip()
        if not gid or not sig: return None
        if not _check_sig(gid, sig): return None
        return gid

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # ── Ping ──────────────────────────────────────────────────────────────
        if p == "/api/ping":
            self._ok({"ok": True, "ts": time.time()}); return

        # ── Profile by gmbr_id ────────────────────────────────────────────────
        if p.startswith("/api/profile/"):
            gid = unquote(p[len("/api/profile/"):]).upper()
            with _db_lock:
                con = _db(); cur = con.cursor()
                row = _user(cur, gid)
            if not row: self._err("Usuário não encontrado", 404); return
            self._ok({"ok": True, "user": _fmt_user(row)}); return

        # ── Friends list ──────────────────────────────────────────────────────
        if p == "/api/friends":
            gid = q.get("gmbr_id","").upper()
            sig = q.get("sig","")
            if not gid or not _check_sig(gid, sig):
                self._err("auth", 401); return
            with _db_lock:
                con = _db(); cur = con.cursor()
                fids   = _friends_of(cur, gid)
                precvs = _pending_recv(cur, gid)
                psents = _pending_sent(cur, gid)
                def _fu(g):
                    r = _user(cur, g)
                    return _fmt_user(r) if r else {"gmbr_id": g, "name": "?"}
                result = {
                    "ok": True,
                    "friends":      [_fu(g) for g in fids],
                    "pending_recv": [_fu(g) for g in precvs],
                    "pending_sent": [_fu(g) for g in psents],
                }
            self._ok(result); return

        # ── DM history ────────────────────────────────────────────────────────
        if p == "/api/dm":
            gid   = q.get("gmbr_id","").upper()
            sig   = q.get("sig","")
            other = q.get("other","").upper()
            since = float(q.get("since", 0))
            if not gid or not _check_sig(gid, sig):
                self._err("auth", 401); return
            if not other: self._err("other required"); return
            conv = _conv(gid, other)
            with _db_lock:
                con = _db(); cur = con.cursor()
                # Verify friendship
                fr = cur.execute(
                    "SELECT 1 FROM friends WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND status='accepted'",
                    (gid, other, other, gid)).fetchone()
                if not fr:
                    con.close(); self._err("Não são amigos", 403); return
                rows = cur.execute(
                    "SELECT * FROM messages WHERE conv_id=? AND ts>? ORDER BY ts ASC LIMIT ?",
                    (conv, since, MSG_LIMIT)).fetchall()
            msgs = [{"id": r["id"], "from": r["from_id"], "from_name": r["from_name"],
                     "text": r["text"], "ts": r["ts"]} for r in rows]
            self._ok({"ok": True, "messages": msgs}); return

        # ── Online users ──────────────────────────────────────────────────────
        if p == "/api/online":
            gid = q.get("gmbr_id","").upper()
            sig = q.get("sig","")
            if not gid or not _check_sig(gid, sig):
                self._err("auth", 401); return
            cutoff = time.time() - ONLINE_TTL
            with _db_lock:
                con = _db(); cur = con.cursor()
                rows = cur.execute(
                    "SELECT * FROM users WHERE last_seen>?", (cutoff,)).fetchall()
            self._ok({"ok": True,
                      "online": [_fmt_user(r) for r in rows if r["gmbr_id"]!=gid]}); return

        # ── My punishment status ──────────────────────────────────────────────
        if p == "/api/my-status":
            gid = q.get("gmbr_id","").upper()
            sig = q.get("sig","")
            if not gid or not _check_sig(gid, sig):
                self._err("auth", 401); return
            now = time.time()
            with _db_lock:
                con = _db(); cur = con.cursor()
                ban = cur.execute(
                    "SELECT * FROM punishments WHERE gmbr_id=? AND type='ban' AND (expires_at=0 OR expires_at>?)",
                    (gid, now)).fetchone()
                mute = cur.execute(
                    "SELECT * FROM punishments WHERE gmbr_id=? AND type='mute' AND (expires_at=0 OR expires_at>?)",
                    (gid, now)).fetchone()
            self._ok({
                "ok": True,
                "banned": bool(ban),
                "ban_reason":   ban["reason"]   if ban  else "",
                "ban_expires":  ban["expires_at"]  if ban  else 0,
                "muted":  bool(mute),
                "mute_reason":  mute["reason"]  if mute else "",
                "mute_expires": mute["expires_at"] if mute else 0,
            }); return

        # ── Admin: list all users ─────────────────────────────────────────────
        if p == "/api/admin/users":
            if not _check_admin(q): self._err("forbidden", 403); return
            cutoff = time.time() - ONLINE_TTL
            with _db_lock:
                con = _db(); cur = con.cursor()
                rows = cur.execute("SELECT * FROM users ORDER BY last_seen DESC").fetchall()
                puns = cur.execute("SELECT * FROM punishments WHERE expires_at=0 OR expires_at>?",
                                   (time.time(),)).fetchall()
            pun_map = {}
            for pun in puns:
                pun_map.setdefault(pun["gmbr_id"], []).append({
                    "type": pun["type"], "reason": pun["reason"],
                    "expires_at": pun["expires_at"]
                })
            users_out = []
            for r in rows:
                u = dict(_fmt_user(r))
                u["punishments"] = pun_map.get(r["gmbr_id"], [])
                u["online"] = (time.time() - (r["last_seen"] or 0)) < ONLINE_TTL
                users_out.append(u)
            self._ok({"ok": True, "users": users_out}); return

        # ── Premium: catalog ──────────────────────────────────────────────────
        if p == "/api/premium/catalog":
            gid = q.get("gmbr_id","").upper()
            sig = q.get("sig","")
            owned = []
            with _db_lock:
                con = _db(); cur = con.cursor()
                if gid and _check_sig(gid, sig):
                    rows = cur.execute("SELECT item_id FROM purchases WHERE gmbr_id=? AND status='paid'",(gid,)).fetchall()
                    owned = [r["item_id"] for r in rows]
                items_db = _get_store_items(con)
            owned_set = set(owned)
            for iid in list(owned_set):
                it = next((x for x in items_db if x["id"]==iid),{})
                if it.get("type")=="bundle": owned_set.update(it.get("includes",[]))
            items_out = [dict(it, owned=it["id"] in owned_set) for it in items_db]
            self._ok({"ok":True,"items":items_out,"owned":list(owned_set)}); return

        # ── Premium: my items ─────────────────────────────────────────────────
        if p == "/api/premium/my-items":
            gid = q.get("gmbr_id","").upper()
            sig = q.get("sig","")
            if not gid or not _check_sig(gid, sig):
                self._err("auth", 401); return
            with _db_lock:
                con = _db(); cur = con.cursor()
                rows = cur.execute(
                    "SELECT item_id FROM purchases WHERE gmbr_id=? AND status='paid'", (gid,)).fetchall()
            owned = set(r["item_id"] for r in rows)
            # Expand bundles
            for iid in list(owned):
                it = _ITEMS_BY_ID.get(iid,{})
                if it.get("type") == "bundle":
                    owned.update(it.get("includes",[]))
            self._ok({"ok": True, "owned": list(owned)}); return

        self._err("Not found", 404)

    def do_POST(self):
        b = self._body()
        parsed = urlparse(self.path)
        p = parsed.path.rstrip("/")

        # ── Sync profile (called on login + profile save) ──────────────────────
        if p == "/api/sync":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            now = time.time()
            with _db_lock:
                con = _db(); cur = con.cursor()
                if _is_banned(cur, gid):
                    con.close(); self._err("Conta banida", 403); return
                existing = _user(cur, gid)
                created  = existing["created_at"] if existing else now
                cur.execute("""
                    INSERT INTO users (gmbr_id,name,display_name,avatar,bio,
                        banner_type,banner_val,banner_color,avatar_effect,created_at,last_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(gmbr_id) DO UPDATE SET
                        name=excluded.name, display_name=excluded.display_name,
                        avatar=excluded.avatar, bio=excluded.bio,
                        banner_type=excluded.banner_type, banner_val=excluded.banner_val,
                        banner_color=excluded.banner_color, avatar_effect=excluded.avatar_effect,
                        last_seen=excluded.last_seen
                """, (
                    gid,
                    b.get("name",""),
                    b.get("display_name","") or b.get("name",""),
                    b.get("avatar","")[:500],
                    b.get("bio","")[:300],
                    b.get("banner_type","color"),
                    b.get("banner_val","")[:100],
                    b.get("banner_color","")[:50],
                    b.get("avatar_effect","none")[:50],
                    created, now,
                ))
                con.commit();
            self._ok({"ok": True}); return

        # ── Heartbeat ──────────────────────────────────────────────────────────
        if p == "/api/heartbeat":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            with _db_lock:
                con = _db()
                con.execute("UPDATE users SET last_seen=? WHERE gmbr_id=?",
                            (time.time(), gid))
                con.commit();
            self._ok({"ok": True}); return

        # ── Friend request ─────────────────────────────────────────────────────
        if p == "/api/friend/request":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            target = b.get("target_gmbr_id","").strip().upper()
            if not target: self._err("target_gmbr_id required"); return
            if target == gid: self._err("Não pode adicionar a si mesmo"); return
            with _db_lock:
                con = _db(); cur = con.cursor()
                if not _user(cur, target):
                    con.close(); self._err(f"Usuário #{target} não encontrado"); return
                # Check existing
                ex = cur.execute(
                    "SELECT * FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)",
                    (gid, target, target, gid)).fetchone()
                if ex:
                    if ex["status"] == "accepted":
                        con.close(); self._err("Já são amigos"); return
                    if ex["a"] == gid:
                        con.close(); self._err("Solicitação já enviada"); return
                    # They already sent us a request → auto-accept
                    cur.execute("UPDATE friends SET status='accepted' WHERE a=? AND b=?",
                                (target, gid))
                    con.commit();
                    self._ok({"ok": True, "auto_accepted": True}); return
                cur.execute("INSERT INTO friends (a,b,status,ts) VALUES (?,?,'pending',?)",
                            (gid, target, time.time()))
                con.commit()
                tu = _user(cur, target)
            name = (tu["display_name"] or tu["name"]) if tu else target
            self._ok({"ok": True, "name": name}); return

        # ── Accept friend ──────────────────────────────────────────────────────
        if p == "/api/friend/accept":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            sender = b.get("sender_gmbr_id","").strip().upper()
            if not sender: self._err("sender_gmbr_id required"); return
            with _db_lock:
                con = _db(); cur = con.cursor()
                ex = cur.execute(
                    "SELECT * FROM friends WHERE a=? AND b=? AND status='pending'",
                    (sender, gid)).fetchone()
                if not ex:
                    con.close(); self._err("Nenhuma solicitação pendente"); return
                cur.execute("UPDATE friends SET status='accepted' WHERE a=? AND b=?",
                            (sender, gid))
                con.commit()
                su = _user(cur, sender)
            name = (su["display_name"] or su["name"]) if su else sender
            self._ok({"ok": True, "name": name}); return

        # ── Decline friend ─────────────────────────────────────────────────────
        if p == "/api/friend/decline":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            sender = b.get("sender_gmbr_id","").strip().upper()
            with _db_lock:
                con = _db()
                con.execute("DELETE FROM friends WHERE a=? AND b=? AND status='pending'",
                            (sender, gid))
                con.commit();
            self._ok({"ok": True}); return

        # ── Remove friend ──────────────────────────────────────────────────────
        if p == "/api/friend/remove":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            other = b.get("other_gmbr_id","").strip().upper()
            with _db_lock:
                con = _db()
                con.execute(
                    "DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)",
                    (gid, other, other, gid))
                con.commit();
            self._ok({"ok": True}); return

        # ── Send DM ────────────────────────────────────────────────────────────
        if p == "/api/dm/send":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            other = b.get("to","").strip().upper()
            text  = b.get("text","").strip()[:1000]
            if not other or not text: self._err("to + text required"); return
            conv = _conv(gid, other)
            with _db_lock:
                con = _db(); cur = con.cursor()
                if _is_muted(cur, gid):
                    con.close(); self._err("Você está silenciado e não pode enviar mensagens", 403); return
                fr = cur.execute(
                    "SELECT 1 FROM friends WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND status='accepted'",
                    (gid, other, other, gid)).fetchone()
                if not fr:
                    con.close(); self._err("Não são amigos", 403); return
                now = time.time()
                cur.execute(
                    "INSERT INTO messages (conv_id,from_id,from_name,text,ts) VALUES (?,?,?,?,?)",
                    (conv, gid, b.get("from_name",""), text, now))
                con.commit()
                rows = cur.execute(
                    "SELECT * FROM messages WHERE conv_id=? ORDER BY ts ASC LIMIT ?",
                    (conv, MSG_LIMIT)).fetchall()
            msgs = [{"id": r["id"], "from": r["from_id"], "from_name": r["from_name"],
                     "text": r["text"], "ts": r["ts"]} for r in rows]
            self._ok({"ok": True, "messages": msgs}); return

        # ── Admin: ban user ────────────────────────────────────────────────────
        if p == "/api/admin/ban":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid      = b.get("gmbr_id","").strip().upper()
            reason   = b.get("reason","")[:200]
            duration = int(b.get("duration", 0))  # minutos, 0=permanente
            if not gid: self._err("gmbr_id required"); return
            expires  = (time.time() + duration * 60) if duration > 0 else 0
            with _db_lock:
                con = _db()
                con.execute("""
                    INSERT INTO punishments (gmbr_id,type,reason,expires_at,created_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(gmbr_id,type) DO UPDATE SET
                        reason=excluded.reason, expires_at=excluded.expires_at, created_at=excluded.created_at
                """, (gid, "ban", reason, expires, time.time()))
                con.commit();
            self._ok({"ok": True, "gmbr_id": gid, "expires_at": expires}); return

        # ── Admin: mute user ───────────────────────────────────────────────────
        if p == "/api/admin/mute":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid      = b.get("gmbr_id","").strip().upper()
            reason   = b.get("reason","")[:200]
            duration = int(b.get("duration", 60))  # minutos, 0=permanente
            if not gid: self._err("gmbr_id required"); return
            expires  = (time.time() + duration * 60) if duration > 0 else 0
            with _db_lock:
                con = _db()
                con.execute("""
                    INSERT INTO punishments (gmbr_id,type,reason,expires_at,created_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(gmbr_id,type) DO UPDATE SET
                        reason=excluded.reason, expires_at=excluded.expires_at, created_at=excluded.created_at
                """, (gid, "mute", reason, expires, time.time()))
                con.commit();
            self._ok({"ok": True, "gmbr_id": gid, "expires_at": expires}); return

        # ── Admin: unban/unmute user ───────────────────────────────────────────
        if p == "/api/admin/pardon":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid  = b.get("gmbr_id","").strip().upper()
            ptype = b.get("type","ban")  # 'ban' | 'mute' | 'all'
            if not gid: self._err("gmbr_id required"); return
            with _db_lock:
                con = _db()
                if ptype == "all":
                    con.execute("DELETE FROM punishments WHERE gmbr_id=?", (gid,))
                else:
                    con.execute("DELETE FROM punishments WHERE gmbr_id=? AND type=?", (gid, ptype))
                con.commit();
            self._ok({"ok": True}); return

        # ── Admin: kick (force offline) ────────────────────────────────────────
        if p == "/api/admin/kick":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid = b.get("gmbr_id","").strip().upper()
            if not gid: self._err("gmbr_id required"); return
            with _db_lock:
                con = _db()
                con.execute("UPDATE users SET last_seen=0 WHERE gmbr_id=?", (gid,))
                con.commit();
            self._ok({"ok": True}); return

        # ── Admin: grant item manually (sem pagamento) ─────────────────────────
        if p == "/api/admin/premium/grant":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid     = b.get("gmbr_id","").strip().upper()
            item_id = b.get("item_id","").strip()
            if not gid or not item_id: self._err("gmbr_id + item_id required"); return
            if item_id not in _ITEMS_BY_ID: self._err(f"Item '{item_id}' não existe"); return
            with _db_lock:
                con = _db(); now = time.time()
                con.execute("""INSERT INTO purchases (gmbr_id,item_id,stripe_session,status,amount_brl,created_at,paid_at)
                    VALUES (?,?,'admin','paid',0,?,?)
                    ON CONFLICT(gmbr_id,item_id) DO UPDATE SET status='paid', paid_at=excluded.paid_at
                """, (gid, item_id, now, now))
                con.commit();
            self._ok({"ok": True, "granted": item_id, "to": gid}); return

        # ── Admin: revoke item ─────────────────────────────────────────────────
        if p == "/api/admin/premium/revoke":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid     = b.get("gmbr_id","").strip().upper()
            item_id = b.get("item_id","").strip()
            with _db_lock:
                con = _db()
                con.execute("DELETE FROM purchases WHERE gmbr_id=? AND item_id=?", (gid, item_id))
                con.commit();
            self._ok({"ok": True}); return

        # ── Admin: update price_ids in catalog ─────────────────────────────────
        if p == "/api/admin/catalog-update":
            if not _check_admin(b): self._err("forbidden", 403); return
            price_ids = b.get("price_ids", {})
            with _db_lock:
                con = _db()
                # Store as a single JSON config row
                con.execute("""CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)""")
                con.execute("INSERT INTO config (key,value) VALUES ('price_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(price_ids),))
                con.commit();
            # Update in-memory catalog
            for item_id, price_id in price_ids.items():
                if item_id in _ITEMS_BY_ID:
                    _ITEMS_BY_ID[item_id]["price_id"] = price_id
            self._ok({"ok": True}); return

        # ── PIX Manual: gerar pedido ──────────────────────────────────────────
        if p == "/api/premium/checkout":
            gid     = self._auth(b)
            if not gid: self._err("auth", 401); return
            item_id = b.get("item_id","").strip()
            if not item_id: self._err("item_id required"); return
            item = _get_item_by_id(item_id)
            if not item: self._err(f"Item '{item_id}' não existe"); return
            with _db_lock:
                con = _db()
                def _cfg(k):
                    r = con.execute("SELECT value FROM pix_config WHERE key=?",(k,)).fetchone()
                    return r["value"] if r else ""
                pix_key  = _cfg("pix_key")  or PIX_KEY
                pix_name = _cfg("pix_name") or PIX_NAME
            if not pix_key: self._err("PIX não configurado. Configure no Painel Admin → Loja."); return
            amount_brl = round(item["price_brl"]/100, 2)
            label      = item.get("label", item_id)
            now        = time.time()
            expire_at  = int(now + PIX_EXPIRE_MIN*60)
            import hashlib as _hs
            order_id   = _hs.md5(f"{gid}{item_id}{int(now)}".encode()).hexdigest()[:12].upper()
            pix_copy   = _pix_brcode(pix_key, pix_name, amount_brl, order_id)
            pix_qr_b64 = ""
            try:
                import qrcode, io, base64 as _b64
                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=2)
                qr.add_data(pix_copy); qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO(); img.save(buf, format="PNG")
                pix_qr_b64 = _b64.b64encode(buf.getvalue()).decode()
            except: pass
            with _db_lock:
                con = _db(); 
                con.execute("""INSERT INTO purchases (gmbr_id,item_id,stripe_session,status,amount_brl,created_at)
                    VALUES (?,?,?,'pending',?,?)
                    ON CONFLICT(gmbr_id,item_id) DO UPDATE SET
                        stripe_session=excluded.stripe_session,status='pending',created_at=excluded.created_at
                """, (gid, item_id, order_id, item["price_brl"], now))
                con.commit();
            print(f"[PIX] Pedido {order_id} — {gid} → {item_id} R${amount_brl:.2f}")
            self._ok({"ok":True,"session_id":order_id,"pix_copy":pix_copy,"pix_qr":pix_qr_b64,
                      "pix_key":pix_key,"pix_name":pix_name,"amount":amount_brl,
                      "expires_at":expire_at,"label":label,"order_id":order_id}); return

        # ── PIX Manual: webhook placeholder ───────────────────────────────────
        if p == "/api/premium/webhook":
            self._ok({"ok": True}); return

        # ── Admin: pedidos pendentes ───────────────────────────────────────────
        if p == "/api/admin/store/pending":
            if not _check_admin(b): self._err("forbidden",403); return
            with _db_lock:
                con = _db(); cur = con.cursor()
                rows = cur.execute("""SELECT p.*,u.display_name,u.name as uname FROM purchases p
                    LEFT JOIN users u ON u.gmbr_id=p.gmbr_id
                    WHERE p.status='pending' ORDER BY p.created_at DESC LIMIT 100""").fetchall()
            pending = [{"id":r["id"],"gmbr_id":r["gmbr_id"],
                "name":r["display_name"] or r["uname"] or r["gmbr_id"],
                "item_id":r["item_id"],"order_id":r["stripe_session"],
                "amount_brl":r["amount_brl"],"created_at":r["created_at"]} for r in rows]
            self._ok({"ok":True,"pending":pending}); return

        # ── Admin: confirmar PIX ───────────────────────────────────────────────
        if p == "/api/admin/store/confirm":
            if not _check_admin(b): self._err("forbidden",403); return
            pid = b.get("id")
            if not pid: self._err("id required"); return
            with _db_lock:
                con = _db(); now = time.time()
                row = con.execute("SELECT * FROM purchases WHERE id=?",(pid,)).fetchone()
                if not row: con.close(); self._err("Pedido não encontrado"); return
                con.execute("UPDATE purchases SET status='paid',paid_at=? WHERE id=?",(now,pid))
                con.commit();
            print(f"[PIX] ✓ Confirmado #{pid} — {row['gmbr_id']} → {row['item_id']}")
            self._ok({"ok":True,"gmbr_id":row["gmbr_id"],"item_id":row["item_id"]}); return

        # ── Admin: rejeitar pedido ─────────────────────────────────────────────
        if p == "/api/admin/store/reject":
            if not _check_admin(b): self._err("forbidden",403); return
            pid = b.get("id")
            if not pid: self._err("id required"); return
            with _db_lock:
                con = _db()
                con.execute("DELETE FROM purchases WHERE id=? AND status='pending'",(pid,))
                con.commit();
            self._ok({"ok":True}); return

        # ── Admin: salvar chave PIX ────────────────────────────────────────────
        if p == "/api/admin/store/pix-config":
            if not _check_admin(b): self._err("forbidden",403); return
            with _db_lock:
                con = _db()
                for k in ["pix_key","pix_key_type","pix_name"]:
                    con.execute("INSERT INTO pix_config (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (k, b.get(k,"").strip()))
                con.commit();
            self._ok({"ok":True}); return

        # ── Admin: ler chave PIX ───────────────────────────────────────────────
        if p == "/api/admin/store/pix-config-get":
            if not _check_admin(b): self._err("forbidden",403); return
            with _db_lock:
                con = _db()
                rows = con.execute("SELECT key,value FROM pix_config").fetchall()
            cfg = {r["key"]:r["value"] for r in rows}
            self._ok({"ok":True,
                "pix_key":cfg.get("pix_key",PIX_KEY),
                "pix_key_type":cfg.get("pix_key_type",PIX_KEY_TYPE),
                "pix_name":cfg.get("pix_name",PIX_NAME)}); return

        # ── Admin: CRUD itens da loja ──────────────────────────────────────────
        if p == "/api/admin/store/items":
            if not _check_admin(b): self._err("forbidden",403); return
            with _db_lock:
                con = _db()
                rows = con.execute("SELECT * FROM store_items ORDER BY sort_order,id").fetchall()
            items = [{"id":r["id"],"type":r["type"],"label":r["label"],"icon":r["icon"],
                "price_brl":r["price_brl"],"preview":r["preview"],
                "includes":json.loads(r["includes"] or "[]"),
                "sort_order":r["sort_order"],"active":bool(r["active"])} for r in rows]
            self._ok({"ok":True,"items":items}); return

        if p in ("/api/admin/store/save", "/api/admin/store/create"):
            if not _check_admin(b): self._err("forbidden",403); return
            iid   = b.get("id","").strip().lower().replace(" ","_")
            label = b.get("label","").strip()[:80]
            if not iid or not label: self._err("id e label obrigatórios"); return
            price = int(b.get("price_brl",490))
            if price < 100: self._err("price_brl mínimo 100"); return
            with _db_lock:
                con = _db()
                con.execute("""INSERT INTO store_items (id,type,label,icon,price_brl,preview,includes,sort_order,active)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET type=excluded.type,label=excluded.label,
                    icon=excluded.icon,price_brl=excluded.price_brl,preview=excluded.preview,
                    includes=excluded.includes,sort_order=excluded.sort_order,active=excluded.active""",
                    (iid,b.get("type","banner"),label,b.get("icon","🎁"),price,
                     b.get("preview","")[:200],json.dumps(b.get("includes",[])),
                     int(b.get("sort_order",99)),1 if b.get("active",True) else 0))
                con.commit();
            self._ok({"ok":True,"id":iid}); return

        if p == "/api/admin/store/delete":
            if not _check_admin(b): self._err("forbidden",403); return
            iid = b.get("id","").strip()
            if not iid: self._err("id required"); return
            with _db_lock:
                con = _db()
                con.execute("UPDATE store_items SET active=0 WHERE id=?",(iid,))
                con.commit();
            self._ok({"ok":True}); return

        if p == "/api/admin/store/reorder":
            if not _check_admin(b): self._err("forbidden",403); return
            with _db_lock:
                con = _db()
                for i,iid in enumerate(b.get("order",[])):
                    con.execute("UPDATE store_items SET sort_order=? WHERE id=?",(i,iid))
                con.commit();
            self._ok({"ok":True}); return


        # ── Premium: check session status (polling from launcher) ──────────────
        if p == "/api/premium/check-session":
            gid        = self._auth(b)
            if not gid: self._err("auth", 401); return
            session_id = b.get("session_id","").strip()
            if not session_id: self._err("session_id required"); return
            with _db_lock:
                con = _db(); cur = con.cursor()
                row = cur.execute(
                    "SELECT * FROM purchases WHERE stripe_session=? AND gmbr_id=?",
                    (session_id, gid)).fetchone()
            if not row:
                self._ok({"ok": True, "status": "not_found"}); return
            self._ok({"ok": True, "status": row["status"], "item_id": row["item_id"]}); return

        self._err("Not found", 404)


if __name__ == "__main__":
    init_db()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[GMBR Social] :{PORT}  DB={DB_PATH}  SECRET={'*'*8}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
