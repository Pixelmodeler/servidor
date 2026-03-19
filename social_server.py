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
        """)
        con.commit()
        con.close()
    print(f"[DB] {DB_PATH}")

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

        self._err("Not found", 404)


if __name__ == "__main__":
    init_db()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[GMBR Social] :{PORT}  DB={DB_PATH}  SECRET={'*'*8}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
