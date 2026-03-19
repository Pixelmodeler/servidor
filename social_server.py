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

# ─── Stripe ───────────────────────────────────────────────────────────────────
STRIPE_SECRET  = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# ─── Catálogo de itens premium ────────────────────────────────────────────────
# price_id = Stripe Price ID (crie no dashboard.stripe.com)
# price_brl = preço em centavos (490 = R$4,90)
PREMIUM_ITEMS = [
  {"id":"banner_hologram",  "type":"banner", "label":"🔷 Hologram",   "price_id":"", "price_brl":490,  "preview":"linear-gradient(135deg,#001840,#003060,#001840)"},
  {"id":"banner_glitch_ex", "type":"banner", "label":"⚡ Glitch EX",  "price_id":"", "price_brl":490,  "preview":"linear-gradient(135deg,#000820,#001040,#000820)"},
  {"id":"banner_sakura",    "type":"banner", "label":"🌸 Sakura",      "price_id":"", "price_brl":490,  "preview":"linear-gradient(135deg,#1a0010,#2d0020,#1a0010)"},
  {"id":"banner_thunder",   "type":"banner", "label":"⚡ Thunder God", "price_id":"", "price_brl":790,  "preview":"linear-gradient(135deg,#100800,#201000,#100800)"},
  {"id":"banner_deep_void", "type":"banner", "label":"🌑 Deep Void",   "price_id":"", "price_brl":990,  "preview":"linear-gradient(135deg,#050005,#0a000f,#050005)"},
  {"id":"av_crown",         "type":"avatar", "label":"👑 Crown",       "price_id":"", "price_brl":390,  "icon":"👑"},
  {"id":"av_phoenix",       "type":"avatar", "label":"🔥 Phoenix",     "price_id":"", "price_brl":590,  "icon":"🔥"},
  {"id":"av_lightning",     "type":"avatar", "label":"⚡ Lightning",   "price_id":"", "price_brl":390,  "icon":"⚡"},
  {"id":"av_shadow",        "type":"avatar", "label":"🌑 Shadow Aura", "price_id":"", "price_brl":590,  "icon":"🌑"},
  {"id":"av_diamond",       "type":"avatar", "label":"💎 Diamond",     "price_id":"", "price_brl":990,  "icon":"💎"},
  {"id":"bundle_starter",   "type":"bundle", "label":"🎁 Starter Pack","price_id":"", "price_brl":990,  "icon":"🎁",
   "includes":["banner_hologram","av_crown","av_lightning"]},
  {"id":"bundle_elite",     "type":"bundle", "label":"💎 Elite Pack",  "price_id":"", "price_brl":1990, "icon":"💎",
   "includes":["banner_deep_void","banner_thunder","av_diamond","av_phoenix","av_shadow"]},
]
_ITEMS_BY_ID = {it["id"]: it for it in PREMIUM_ITEMS}
# ─── DB ──────────────────────────────────────────────────────────────────────
def _db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
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
        """)
        con.commit()
        con.close()
    print(f"[DB] {DB_PATH}")
    # Load stored price_ids into memory
    try:
        con = _db()
        con.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        row = con.execute("SELECT value FROM config WHERE key='price_ids'").fetchone()
        if row:
            price_ids = json.loads(row[0])
            for item_id, price_id in price_ids.items():
                if item_id in _ITEMS_BY_ID:
                    _ITEMS_BY_ID[item_id]["price_id"] = price_id
        con.close()
    except Exception as e:
        print(f"[DB] price_ids load: {e}")

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
                con.close()
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
                con.close()
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
                con.close()
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
                con.close()
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
                con.close()
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
                con.close()
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
            if gid and _check_sig(gid, sig):
                with _db_lock:
                    con = _db(); cur = con.cursor()
                    rows = cur.execute(
                        "SELECT item_id FROM purchases WHERE gmbr_id=? AND status='paid'", (gid,)).fetchall()
                    con.close()
                owned = [r["item_id"] for r in rows]
            # Expand bundle ownership
            owned_set = set(owned)
            for iid in list(owned_set):
                it = _ITEMS_BY_ID.get(iid,{})
                if it.get("type") == "bundle":
                    owned_set.update(it.get("includes",[]))
            items_out = []
            for it in PREMIUM_ITEMS:
                entry = dict(it)
                entry["owned"] = it["id"] in owned_set
                entry.pop("price_id", None)  # don't leak stripe price ids
                items_out.append(entry)
            self._ok({"ok": True, "items": items_out, "owned": list(owned_set)}); return

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
                con.close()
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
                con.commit(); con.close()
            self._ok({"ok": True}); return

        # ── Heartbeat ──────────────────────────────────────────────────────────
        if p == "/api/heartbeat":
            gid = self._auth(b)
            if not gid: self._err("auth", 401); return
            with _db_lock:
                con = _db()
                con.execute("UPDATE users SET last_seen=? WHERE gmbr_id=?",
                            (time.time(), gid))
                con.commit(); con.close()
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
                    con.commit(); con.close()
                    self._ok({"ok": True, "auto_accepted": True}); return
                cur.execute("INSERT INTO friends (a,b,status,ts) VALUES (?,?,'pending',?)",
                            (gid, target, time.time()))
                con.commit()
                tu = _user(cur, target)
                con.close()
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
                con.close()
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
                con.commit(); con.close()
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
                con.commit(); con.close()
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
                con.close()
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
                con.commit(); con.close()
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
                con.commit(); con.close()
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
                con.commit(); con.close()
            self._ok({"ok": True}); return

        # ── Admin: kick (force offline) ────────────────────────────────────────
        if p == "/api/admin/kick":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid = b.get("gmbr_id","").strip().upper()
            if not gid: self._err("gmbr_id required"); return
            with _db_lock:
                con = _db()
                con.execute("UPDATE users SET last_seen=0 WHERE gmbr_id=?", (gid,))
                con.commit(); con.close()
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
                con.commit(); con.close()
            self._ok({"ok": True, "granted": item_id, "to": gid}); return

        # ── Admin: revoke item ─────────────────────────────────────────────────
        if p == "/api/admin/premium/revoke":
            if not _check_admin(b): self._err("forbidden", 403); return
            gid     = b.get("gmbr_id","").strip().upper()
            item_id = b.get("item_id","").strip()
            with _db_lock:
                con = _db()
                con.execute("DELETE FROM purchases WHERE gmbr_id=? AND item_id=?", (gid, item_id))
                con.commit(); con.close()
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
                con.commit(); con.close()
            # Update in-memory catalog
            for item_id, price_id in price_ids.items():
                if item_id in _ITEMS_BY_ID:
                    _ITEMS_BY_ID[item_id]["price_id"] = price_id
            self._ok({"ok": True}); return

        # ── Stripe: create checkout session ────────────────────────────────────
        if p == "/api/premium/checkout":
            gid     = self._auth(b)
            if not gid: self._err("auth", 401); return
            item_id  = b.get("item_id","").strip()
            # Accept price_id directly (sent by app.py) OR fall back to catalog
            price_id = b.get("price_id","").strip() or _ITEMS_BY_ID.get(item_id,{}).get("price_id","").strip()
            if not item_id: self._err("item_id required"); return
            if not STRIPE_SECRET: self._err("Pagamentos não configurados — defina STRIPE_SECRET_KEY no Railway"); return
            if not price_id: self._err("Price ID não configurado para este item. Configure no Painel Admin → Loja."); return

            item = _ITEMS_BY_ID.get(item_id, {"id": item_id, "price_brl": 0})

            # Create Stripe Checkout Session via API
            try:
                payload = "&".join([
                    "mode=payment",
                    f"line_items[0][price]={price_id}",
                    "line_items[0][quantity]=1",
                    f"metadata[gmbr_id]={gid}",
                    f"metadata[item_id]={item_id}",
                    "success_url=https://gmbrlauncher.com/payment_success",
                    "cancel_url=https://gmbrlauncher.com/payment_cancel",
                ])
                req = urllib.request.Request(
                    "https://api.stripe.com/v1/checkout/sessions",
                    data=payload.encode(),
                    headers={
                        "Authorization": f"Bearer {STRIPE_SECRET}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    session = json.loads(r.read().decode())

                session_id   = session["id"]
                checkout_url = session["url"]

                # Record pending purchase
                with _db_lock:
                    con = _db(); now = time.time()
                    con.execute("""INSERT INTO purchases (gmbr_id,item_id,stripe_session,status,amount_brl,created_at)
                        VALUES (?,?,?,'pending',?,?)
                        ON CONFLICT(gmbr_id,item_id) DO UPDATE SET
                            stripe_session=excluded.stripe_session, status='pending', created_at=excluded.created_at
                    """, (gid, item_id, session_id, item["price_brl"], now))
                    con.commit(); con.close()

                self._ok({"ok": True, "checkout_url": checkout_url, "session_id": session_id}); return
            except Exception as e:
                self._err(f"Stripe error: {e}"); return

        # ── Stripe: webhook ────────────────────────────────────────────────────
        if p == "/api/premium/webhook":
            # Read raw body for signature verification
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            sig_header = self.headers.get("Stripe-Signature","")

            if STRIPE_WEBHOOK:
                # Verify signature
                try:
                    ts_part  = [s for s in sig_header.split(",") if s.startswith("t=")]
                    sig_part = [s for s in sig_header.split(",") if s.startswith("v1=")]
                    if not ts_part or not sig_part:
                        self._err("invalid signature", 400); return
                    ts  = ts_part[0][2:]
                    signed_payload = f"{ts}.".encode() + raw
                    expected = hmac.new(STRIPE_WEBHOOK.encode(), signed_payload, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, sig_part[0][3:]):
                        self._err("signature mismatch", 400); return
                except Exception as e:
                    self._err(f"webhook verify: {e}", 400); return

            try:
                event = json.loads(raw.decode())
            except:
                self._err("invalid json", 400); return

            if event.get("type") == "checkout.session.completed":
                sess = event["data"]["object"]
                meta = sess.get("metadata", {})
                gid     = meta.get("gmbr_id","").upper()
                item_id = meta.get("item_id","")
                session_id = sess.get("id","")
                if gid and item_id:
                    with _db_lock:
                        con = _db(); now = time.time()
                        con.execute("""UPDATE purchases SET status='paid', paid_at=?
                            WHERE gmbr_id=? AND item_id=? AND stripe_session=?
                        """, (now, gid, item_id, session_id))
                        # Also insert if not found (safety)
                        con.execute("""INSERT OR IGNORE INTO purchases
                            (gmbr_id,item_id,stripe_session,status,amount_brl,created_at,paid_at)
                            VALUES (?,?,?,'paid',0,?,?)
                        """, (gid, item_id, session_id, now, now))
                        con.commit(); con.close()
                    print(f"[PREMIUM] ✓ {gid} purchased {item_id}")

            self._ok({"ok": True}); return

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
                con.close()
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
