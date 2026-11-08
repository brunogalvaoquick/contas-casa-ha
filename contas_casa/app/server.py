#!/usr/bin/env python3
"""Contas da Casa — add-on do Home Assistant.

Contas a pagar (com comprovante), entradas de dinheiro e fundos (reserva,
seguro...). Roda pelo ingress do HA, então quem está logado no HA é quem
aparece como "lançado por". Publica sensores no HA para os avisos da manhã.
"""
import asyncio
import calendar
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ") or "America/Sao_Paulo")
except Exception:  # pragma: no cover
    TZ = None

DATA_DIR = Path(os.environ.get("CONTAS_DATA", "/data"))
DB_PATH = DATA_DIR / "contas.db"
COMP_DIR = DATA_DIR / "comprovantes"
STATIC_DIR = Path(__file__).parent / "static"
PORT = int(os.environ.get("CONTAS_PORT", "8099"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API = "http://supervisor/core/api"
INGRESS_IP = "172.30.32.2"
DEV = os.environ.get("CONTAS_DEV") == "1"

log = logging.getLogger("contas")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REMOVIDA = "(removida deste mês)"
EXT_OK = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".heic"}


def hoje() -> date:
    return datetime.now(TZ).date() if TZ else date.today()


def agora_iso() -> str:
    return (datetime.now(TZ) if TZ else datetime.now()).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- banco
SCHEMA = """
CREATE TABLE IF NOT EXISTS recorrentes (
    id INTEGER PRIMARY KEY,
    descricao TEXT NOT NULL,
    categoria TEXT DEFAULT '',
    valor REAL NOT NULL DEFAULT 0,
    dia INTEGER NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1,
    inicio TEXT NOT NULL,
    criado_por TEXT DEFAULT '',
    criado_em TEXT
);
CREATE TABLE IF NOT EXISTS contas (
    id INTEGER PRIMARY KEY,
    descricao TEXT NOT NULL,
    categoria TEXT DEFAULT '',
    valor REAL NOT NULL DEFAULT 0,
    vencimento TEXT NOT NULL,
    obs TEXT DEFAULT '',
    recorrente_id INTEGER,
    parcela TEXT DEFAULT '',
    pago INTEGER NOT NULL DEFAULT 0,
    pago_em TEXT,
    valor_pago REAL,
    pago_por TEXT,
    comprovante TEXT,
    criado_por TEXT DEFAULT '',
    criado_em TEXT
);
CREATE INDEX IF NOT EXISTS ix_contas_venc ON contas(vencimento);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contas_rec ON contas(recorrente_id, vencimento)
    WHERE recorrente_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS entradas (
    id INTEGER PRIMARY KEY,
    descricao TEXT NOT NULL,
    categoria TEXT DEFAULT '',
    valor REAL NOT NULL,
    data TEXT NOT NULL,
    obs TEXT DEFAULT '',
    comprovante TEXT,
    criado_por TEXT DEFAULT '',
    criado_em TEXT
);
CREATE TABLE IF NOT EXISTS fundos (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    meta REAL DEFAULT 0,
    cor TEXT DEFAULT '#2e7d32',
    criado_por TEXT DEFAULT '',
    criado_em TEXT
);
CREATE TABLE IF NOT EXISTS acessos (
    user_id TEXT PRIMARY KEY,
    nome TEXT DEFAULT '',
    login TEXT DEFAULT '',
    permitido INTEGER NOT NULL DEFAULT 0,
    visto_em TEXT
);
CREATE TABLE IF NOT EXISTS movimentos (
    id INTEGER PRIMARY KEY,
    fundo_id INTEGER NOT NULL REFERENCES fundos(id) ON DELETE CASCADE,
    tipo TEXT NOT NULL CHECK (tipo IN ('aporte','retirada')),
    valor REAL NOT NULL,
    data TEXT NOT NULL,
    descricao TEXT DEFAULT '',
    criado_por TEXT DEFAULT '',
    criado_em TEXT
);
"""


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COMP_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)
        if not con.execute("SELECT 1 FROM fundos LIMIT 1").fetchone():
            for nome, cor in (("Fundo reserva", "#2e7d32"), ("Fundo seguro", "#1565c0")):
                con.execute(
                    "INSERT INTO fundos(nome, cor, criado_em) VALUES (?,?,?)",
                    (nome, cor, agora_iso()),
                )


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def dia_no_mes(ano: int, mes: int, dia: int) -> date:
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def add_meses(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def gerar_recorrentes(con, ate: date | None = None):
    """Cria as contas mensais das recorrentes até o fim do mês seguinte."""
    if ate is None:
        ate = add_meses(hoje(), 2) - timedelta(days=1)
    for r in rows(con.execute("SELECT * FROM recorrentes WHERE ativo=1")):
        ini = date.fromisoformat(r["inicio"])
        m = date(ini.year, ini.month, 1)
        while m <= ate:
            venc = dia_no_mes(m.year, m.month, r["dia"])
            if venc >= ini:
                con.execute(
                    """INSERT OR IGNORE INTO contas
                       (descricao, categoria, valor, vencimento, recorrente_id, criado_por, criado_em)
                       VALUES (?,?,?,?,?,?,?)""",
                    (r["descricao"], r["categoria"], r["valor"], venc.isoformat(), r["id"],
                     r["criado_por"], agora_iso()),
                )
            m = add_meses(m, 1)


# --------------------------------------------------------------------------- util
def usuario(req: web.Request) -> str:
    return (req.headers.get("X-Remote-User-Display-Name")
            or req.headers.get("X-Remote-User-Name")
            or ("Teste" if DEV else "Alguém"))


def num(v, campo="valor", obrigatorio=True):
    if v is None or str(v).strip() == "":
        if obrigatorio:
            raise web.HTTPBadRequest(text=f"{campo} obrigatório")
        return None
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        raise web.HTTPBadRequest(text=f"{campo} inválido")


def data_ok(v, campo="data"):
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except Exception:
        raise web.HTTPBadRequest(text=f"{campo} inválida")


def mes_param(req) -> tuple[date, date]:
    m = req.query.get("mes") or hoje().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        raise web.HTTPBadRequest(text="mes inválido")
    ini = date(int(m[:4]), int(m[5:]), 1)
    fim = add_meses(ini, 1) - timedelta(days=1)
    return ini, fim


async def salvar_arquivo(campo) -> str | None:
    if campo is None or not getattr(campo, "filename", None):
        return None
    ext = Path(campo.filename).suffix.lower() or ".jpg"
    if ext not in EXT_OK:
        raise web.HTTPBadRequest(text="Tipo de arquivo não aceito (use foto ou PDF)")
    nome = f"{hoje().strftime('%Y%m')}_{uuid.uuid4().hex[:12]}{ext}"
    destino = COMP_DIR / nome
    dados = campo.file.read()
    destino.write_bytes(dados)
    return nome


def apagar_arquivo(nome):
    if nome:
        try:
            (COMP_DIR / Path(nome).name).unlink()
        except FileNotFoundError:
            pass


def ok(dados=None):
    return web.json_response(dados if dados is not None else {"ok": True})


# --------------------------------------------------------------------------- middleware
# --------------------------------------------------------------------------- usuários
# Administradores do HA sempre entram e veem a tela ⚙ Usuários.
# Os demais usuários só entram se um admin liberar (tabela "acessos").
_cache_usuarios = {"t": 0.0, "lista": None}


async def usuarios_ha():
    """Lista os usuários do HA (websocket config/auth/list, com o token do Supervisor)."""
    import time
    if _cache_usuarios["lista"] is not None and time.monotonic() - _cache_usuarios["t"] < 60:
        return _cache_usuarios["lista"]
    lista = None
    if SUPERVISOR_TOKEN:
        try:
            async with ClientSession(timeout=ClientTimeout(total=10)) as s:
                async with s.ws_connect("http://supervisor/core/websocket") as ws:
                    await ws.receive_json()  # auth_required
                    await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
                    if (await ws.receive_json()).get("type") == "auth_ok":
                        await ws.send_json({"id": 1, "type": "config/auth/list"})
                        r = await ws.receive_json()
                        if r.get("success"):
                            lista = [
                                {"id": u["id"], "nome": u.get("name") or u.get("username") or "?",
                                 "login": u.get("username") or "",
                                 "admin": "system-admin" in (u.get("group_ids") or []) or u.get("is_owner", False)}
                                for u in r["result"]
                                if not u.get("system_generated") and u.get("is_active", True)]
        except Exception as e:
            log.warning("Não consegui listar os usuários do HA: %s", e)
    _cache_usuarios.update(t=time.monotonic(), lista=lista)
    return lista


def registrar_visto(req):
    uid = req.headers.get("X-Remote-User-Id")
    if not uid:
        return
    with db() as con:
        con.execute("""INSERT INTO acessos(user_id, nome, login, visto_em) VALUES (?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET nome=excluded.nome, login=excluded.login,
                       visto_em=excluded.visto_em""",
                    (uid, req.headers.get("X-Remote-User-Display-Name") or "",
                     req.headers.get("X-Remote-User-Name") or "", agora_iso()))


async def perfil(req):
    """-> (é_admin, pode_entrar)"""
    if DEV:
        return True, True
    uid = req.headers.get("X-Remote-User-Id")
    if not uid:
        return False, False
    registrar_visto(req)
    lista = await usuarios_ha()
    with db() as con:
        if lista is not None:
            admin = any(u["id"] == uid and u["admin"] for u in lista)
        else:
            # sem acesso à lista do HA: o primeiro usuário que abriu o app é o admin
            p = con.execute("SELECT user_id FROM acessos ORDER BY rowid LIMIT 1").fetchone()
            admin = bool(p and p[0] == uid)
        a = con.execute("SELECT permitido FROM acessos WHERE user_id=?", (uid,)).fetchone()
    return admin, admin or bool(a and a[0])


SEM_ACESSO = """<!DOCTYPE html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font-family:sans-serif;text-align:center;padding:60px 20px;color:#555">
<div style="font-size:48px">🔒</div><h2>Sem acesso</h2>
<p>Peça para um administrador liberar seu usuário em ⚙ Usuários, dentro das Contas da Casa.</p>
<p><button onclick="location.reload()" style="padding:10px 18px;font-size:16px">Tentar de novo</button></p>
</body></html>"""


@web.middleware
async def guarda(req: web.Request, handler):
    if not DEV and req.remote not in (INGRESS_IP, "127.0.0.1"):
        return web.Response(status=403, text="Acesse pelo Home Assistant")
    admin, pode = await perfil(req)
    req["admin"] = admin
    if not pode:
        if req.path.startswith("/api/"):
            return web.json_response({"erro": "Sem acesso"}, status=403)
        return web.Response(status=403, text=SEM_ACESSO, content_type="text/html")
    try:
        resp = await handler(req)
    except web.HTTPException as e:
        if e.status >= 400 and req.path.startswith("/api/"):
            return web.json_response({"erro": e.text or e.reason}, status=e.status)
        raise
    if req.method in ("POST", "PUT", "DELETE") and req.path.startswith("/api/"):
        req.app["publicar"].set()
    return resp


# --------------------------------------------------------------------------- páginas
async def index(req):
    return web.FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})


async def eu(req):
    return ok({"usuario": usuario(req), "hoje": hoje().isoformat(), "admin": req["admin"]})


async def listar_acessos(req):
    if not req["admin"]:
        raise web.HTTPForbidden(text="Só administradores")
    lista = await usuarios_ha()
    with db() as con:
        vistos = {r["user_id"]: dict(r) for r in con.execute("SELECT * FROM acessos ORDER BY rowid")}
    if lista is None:  # fallback: só quem já abriu o app; o primeiro é o admin
        lista = [{"id": k, "nome": v["nome"] or v["login"], "login": v["login"], "admin": i == 0}
                 for i, (k, v) in enumerate(vistos.items())]
    eu_id = req.headers.get("X-Remote-User-Id")
    return ok([{**u, "permitido": bool(u["admin"] or (vistos.get(u["id"]) or {}).get("permitido")),
                "eu": u["id"] == eu_id} for u in sorted(lista, key=lambda u: (not u["admin"], u["nome"].lower()))])


async def mudar_acesso(req):
    if not req["admin"]:
        raise web.HTTPForbidden(text="Só administradores")
    d = await req.json()
    uid = str(d.get("id") or "")
    if not uid:
        raise web.HTTPBadRequest(text="id obrigatório")
    with db() as con:
        con.execute("""INSERT INTO acessos(user_id, nome, login, permitido) VALUES (?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET permitido=excluded.permitido""",
                    (uid, d.get("nome") or "", d.get("login") or "", 1 if d.get("permitido") else 0))
    return ok()


# --------------------------------------------------------------------------- contas
async def listar_contas(req):
    ini, fim = mes_param(req)
    with db() as con:
        gerar_recorrentes(con)
        contas = rows(con.execute(
            "SELECT * FROM contas WHERE vencimento BETWEEN ? AND ? AND NOT (pago=1 AND valor=0 AND obs=?)"
            " ORDER BY pago, vencimento, id",
            (ini.isoformat(), fim.isoformat(), REMOVIDA)))
        atrasadas = rows(con.execute(
            "SELECT * FROM contas WHERE pago=0 AND vencimento < ? ORDER BY vencimento",
            (ini.isoformat(),)))
    return ok({"contas": contas, "atrasadas_meses_anteriores": atrasadas})


async def criar_conta(req):
    d = await req.json()
    desc = (d.get("descricao") or "").strip()
    if not desc:
        raise web.HTTPBadRequest(text="Descrição obrigatória")
    valor = num(d.get("valor"))
    venc = date.fromisoformat(data_ok(d.get("vencimento"), "vencimento"))
    cat = (d.get("categoria") or "").strip()
    obs = (d.get("obs") or "").strip()
    quem = usuario(req)
    parcelas = int(d.get("parcelas") or 1)
    with db() as con:
        if d.get("recorrente"):
            cur = con.execute(
                """INSERT INTO recorrentes(descricao, categoria, valor, dia, inicio, criado_por, criado_em)
                   VALUES (?,?,?,?,?,?,?)""",
                (desc, cat, valor, venc.day, venc.isoformat(), quem, agora_iso()))
            gerar_recorrentes(con)
            return ok({"recorrente_id": cur.lastrowid})
        parcelas = max(1, min(parcelas, 120))
        for i in range(parcelas):
            m = add_meses(venc, i)
            v = dia_no_mes(m.year, m.month, venc.day)
            con.execute(
                """INSERT INTO contas(descricao, categoria, valor, vencimento, obs, parcela, criado_por, criado_em)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (desc, cat, valor, v.isoformat(), obs,
                 f"{i + 1}/{parcelas}" if parcelas > 1 else "", quem, agora_iso()))
    return ok()


async def editar_conta(req):
    cid = int(req.match_info["id"])
    d = await req.json()
    with db() as con:
        c = con.execute("SELECT * FROM contas WHERE id=?", (cid,)).fetchone()
        if not c:
            raise web.HTTPNotFound(text="Conta não encontrada")
        desc = (d.get("descricao") or c["descricao"]).strip()
        valor = num(d.get("valor"), obrigatorio=False)
        valor = c["valor"] if valor is None else valor
        venc = data_ok(d["vencimento"], "vencimento") if d.get("vencimento") else c["vencimento"]
        cat = d.get("categoria", c["categoria"])
        obs = d.get("obs", c["obs"])
        con.execute("UPDATE contas SET descricao=?, valor=?, vencimento=?, categoria=?, obs=? WHERE id=?",
                    (desc, valor, venc, cat, obs, cid))
        # "mudar também os próximos meses" para contas fixas
        if d.get("aplicar_futuras") and c["recorrente_id"]:
            dia = date.fromisoformat(venc).day
            con.execute("UPDATE recorrentes SET descricao=?, valor=?, categoria=?, dia=? WHERE id=?",
                        (desc, valor, cat, dia, c["recorrente_id"]))
            futuras = rows(con.execute(
                "SELECT id, vencimento FROM contas WHERE recorrente_id=? AND pago=0 AND vencimento > ?",
                (c["recorrente_id"], c["vencimento"])))
            for f in futuras:
                fv = date.fromisoformat(f["vencimento"])
                nv = dia_no_mes(fv.year, fv.month, dia).isoformat()
                con.execute("UPDATE OR IGNORE contas SET descricao=?, valor=?, categoria=?, vencimento=? WHERE id=?",
                            (desc, valor, cat, nv, f["id"]))
    return ok()


async def apagar_conta(req):
    cid = int(req.match_info["id"])
    futuras = req.query.get("futuras") == "1"
    with db() as con:
        c = con.execute("SELECT * FROM contas WHERE id=?", (cid,)).fetchone()
        if not c:
            raise web.HTTPNotFound(text="Conta não encontrada")
        if futuras and c["recorrente_id"]:
            con.execute("UPDATE recorrentes SET ativo=0 WHERE id=?", (c["recorrente_id"],))
            for f in rows(con.execute(
                    "SELECT id, comprovante FROM contas WHERE recorrente_id=? AND pago=0 AND vencimento >= ?",
                    (c["recorrente_id"], c["vencimento"]))):
                apagar_arquivo(f["comprovante"])
                con.execute("DELETE FROM contas WHERE id=?", (f["id"],))
        if c["recorrente_id"] and not futuras:
            # marca como "pulada": guarda valor 0 e paga, para não ser recriada
            con.execute("UPDATE contas SET pago=1, valor=0, valor_pago=0, pago_em=?, pago_por=?, obs=? WHERE id=?",
                        (hoje().isoformat(), usuario(req), REMOVIDA, cid))
        else:
            apagar_arquivo(c["comprovante"])
            con.execute("DELETE FROM contas WHERE id=?", (cid,))
    return ok()


async def pagar_conta(req):
    cid = int(req.match_info["id"])
    form = await req.post()
    with db() as con:
        c = con.execute("SELECT * FROM contas WHERE id=?", (cid,)).fetchone()
        if not c:
            raise web.HTTPNotFound(text="Conta não encontrada")
        valor = num(form.get("valor_pago"), "valor pago", obrigatorio=False)
        valor = c["valor"] if valor is None else valor
        data = data_ok(form.get("data") or hoje().isoformat())
        arq = await salvar_arquivo(form.get("arquivo"))
        if arq and c["comprovante"]:
            apagar_arquivo(c["comprovante"])
        con.execute(
            "UPDATE contas SET pago=1, pago_em=?, valor_pago=?, pago_por=?, comprovante=COALESCE(?, comprovante) WHERE id=?",
            (data, valor, usuario(req), arq, cid))
    return ok()


async def comprovante_conta(req):
    """Anexa (ou troca) o comprovante de uma conta já paga."""
    cid = int(req.match_info["id"])
    form = await req.post()
    arq = await salvar_arquivo(form.get("arquivo"))
    if not arq:
        raise web.HTTPBadRequest(text="Nenhum arquivo")
    with db() as con:
        c = con.execute("SELECT comprovante FROM contas WHERE id=?", (cid,)).fetchone()
        if not c:
            apagar_arquivo(arq)
            raise web.HTTPNotFound(text="Conta não encontrada")
        apagar_arquivo(c["comprovante"])
        con.execute("UPDATE contas SET comprovante=? WHERE id=?", (arq, cid))
    return ok()


async def desfazer_pagamento(req):
    cid = int(req.match_info["id"])
    with db() as con:
        c = con.execute("SELECT comprovante FROM contas WHERE id=?", (cid,)).fetchone()
        if not c:
            raise web.HTTPNotFound(text="Conta não encontrada")
        apagar_arquivo(c["comprovante"])
        con.execute("UPDATE contas SET pago=0, pago_em=NULL, valor_pago=NULL, pago_por=NULL, comprovante=NULL WHERE id=?",
                    (cid,))
    return ok()


async def ver_comprovante(req):
    nome = Path(req.match_info["nome"]).name
    p = COMP_DIR / nome
    if not p.is_file():
        raise web.HTTPNotFound(text="Arquivo não encontrado")
    return web.FileResponse(p)


# --------------------------------------------------------------------------- entradas
async def listar_entradas(req):
    ini, fim = mes_param(req)
    with db() as con:
        return ok(rows(con.execute(
            "SELECT * FROM entradas WHERE data BETWEEN ? AND ? ORDER BY data DESC, id DESC",
            (ini.isoformat(), fim.isoformat()))))


async def criar_entrada(req):
    form = await req.post()
    desc = (form.get("descricao") or "").strip()
    if not desc:
        raise web.HTTPBadRequest(text="Descrição obrigatória")
    valor = num(form.get("valor"))
    data = data_ok(form.get("data") or hoje().isoformat())
    arq = await salvar_arquivo(form.get("arquivo"))
    with db() as con:
        con.execute(
            "INSERT INTO entradas(descricao, categoria, valor, data, obs, comprovante, criado_por, criado_em) VALUES (?,?,?,?,?,?,?,?)",
            (desc, (form.get("categoria") or "").strip(), valor, data,
             (form.get("obs") or "").strip(), arq, usuario(req), agora_iso()))
    return ok()


async def apagar_entrada(req):
    eid = int(req.match_info["id"])
    with db() as con:
        e = con.execute("SELECT comprovante FROM entradas WHERE id=?", (eid,)).fetchone()
        if e:
            apagar_arquivo(e["comprovante"])
            con.execute("DELETE FROM entradas WHERE id=?", (eid,))
    return ok()


# --------------------------------------------------------------------------- fundos
def saldos_fundos(con):
    return rows(con.execute("""
        SELECT f.*,
          COALESCE(SUM(CASE WHEN m.tipo='aporte' THEN m.valor ELSE -m.valor END), 0) AS saldo
        FROM fundos f LEFT JOIN movimentos m ON m.fundo_id = f.id
        GROUP BY f.id ORDER BY f.id"""))


async def listar_fundos(req):
    with db() as con:
        return ok(saldos_fundos(con))


async def criar_fundo(req):
    d = await req.json()
    nome = (d.get("nome") or "").strip()
    if not nome:
        raise web.HTTPBadRequest(text="Nome obrigatório")
    with db() as con:
        con.execute("INSERT INTO fundos(nome, meta, cor, criado_por, criado_em) VALUES (?,?,?,?,?)",
                    (nome, num(d.get("meta"), obrigatorio=False) or 0, d.get("cor") or "#2e7d32",
                     usuario(req), agora_iso()))
    return ok()


async def editar_fundo(req):
    fid = int(req.match_info["id"])
    d = await req.json()
    with db() as con:
        f = con.execute("SELECT * FROM fundos WHERE id=?", (fid,)).fetchone()
        if not f:
            raise web.HTTPNotFound(text="Fundo não encontrado")
        meta = num(d.get("meta"), obrigatorio=False)
        con.execute("UPDATE fundos SET nome=?, meta=?, cor=? WHERE id=?",
                    ((d.get("nome") or f["nome"]).strip(), f["meta"] if meta is None else meta,
                     d.get("cor") or f["cor"], fid))
    return ok()


async def apagar_fundo(req):
    fid = int(req.match_info["id"])
    with db() as con:
        con.execute("DELETE FROM fundos WHERE id=?", (fid,))
    return ok()


async def movimentos_fundo(req):
    fid = int(req.match_info["id"])
    with db() as con:
        return ok(rows(con.execute(
            "SELECT * FROM movimentos WHERE fundo_id=? ORDER BY data DESC, id DESC", (fid,))))


async def criar_movimento(req):
    fid = int(req.match_info["id"])
    d = await req.json()
    tipo = d.get("tipo")
    if tipo not in ("aporte", "retirada"):
        raise web.HTTPBadRequest(text="Tipo inválido")
    with db() as con:
        if not con.execute("SELECT 1 FROM fundos WHERE id=?", (fid,)).fetchone():
            raise web.HTTPNotFound(text="Fundo não encontrado")
        con.execute(
            "INSERT INTO movimentos(fundo_id, tipo, valor, data, descricao, criado_por, criado_em) VALUES (?,?,?,?,?,?,?)",
            (fid, tipo, abs(num(d.get("valor"))), data_ok(d.get("data") or hoje().isoformat()),
             (d.get("descricao") or "").strip(), usuario(req), agora_iso()))
    return ok()


async def apagar_movimento(req):
    with db() as con:
        con.execute("DELETE FROM movimentos WHERE id=?", (int(req.match_info["id"]),))
    return ok()


# --------------------------------------------------------------------------- resumo
def resumo_mes(con, ini: date, fim: date):
    a, b = ini.isoformat(), fim.isoformat()
    c = con.execute("""SELECT
          COALESCE(SUM(valor),0) total,
          COALESCE(SUM(CASE WHEN pago=1 THEN COALESCE(valor_pago, valor) END),0) pago,
          COALESCE(SUM(CASE WHEN pago=0 THEN valor END),0) a_pagar,
          COUNT(CASE WHEN pago=0 THEN 1 END) n_a_pagar
        FROM contas WHERE vencimento BETWEEN ? AND ?""", (a, b)).fetchone()
    ent = con.execute("SELECT COALESCE(SUM(valor),0) FROM entradas WHERE data BETWEEN ? AND ?",
                      (a, b)).fetchone()[0]
    mov = con.execute("""SELECT
          COALESCE(SUM(CASE WHEN tipo='aporte' THEN valor END),0),
          COALESCE(SUM(CASE WHEN tipo='retirada' THEN valor END),0)
        FROM movimentos WHERE data BETWEEN ? AND ?""", (a, b)).fetchone()
    vencidas = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(valor),0) FROM contas WHERE pago=0 AND vencimento < ?",
        (hoje().isoformat(),)).fetchone()
    return {
        "contas_total": round(c["total"], 2),
        "contas_pago": round(c["pago"], 2),
        "contas_a_pagar": round(c["a_pagar"], 2),
        "n_a_pagar": c["n_a_pagar"],
        "entradas": round(ent, 2),
        "aportes": round(mov[0], 2),
        "retiradas": round(mov[1], 2),
        # dinheiro que entrou menos o que já foi pago e guardado nos fundos
        "sobra": round(ent - c["pago"] - mov[0] + mov[1], 2),
        "vencidas_n": vencidas[0],
        "vencidas_total": round(vencidas[1], 2),
    }


async def resumo(req):
    ini, fim = mes_param(req)
    with db() as con:
        gerar_recorrentes(con)
        r = resumo_mes(con, ini, fim)
        r["fundos"] = saldos_fundos(con)
        r["categorias"] = rows(con.execute(
            """SELECT COALESCE(NULLIF(categoria,''),'Sem categoria') categoria, SUM(valor) total
               FROM contas WHERE vencimento BETWEEN ? AND ? AND valor > 0
               GROUP BY 1 ORDER BY 2 DESC""", (ini.isoformat(), fim.isoformat())))
    return ok(r)


async def categorias(req):
    with db() as con:
        cs = [r[0] for r in con.execute(
            "SELECT DISTINCT categoria FROM contas WHERE categoria<>'' UNION SELECT DISTINCT categoria FROM entradas WHERE categoria<>''")]
    base = ["Moradia", "Luz", "Água", "Internet", "Telefone", "Mercado", "Cartão", "Escola",
            "Saúde", "Carro", "Combustível", "Impostos", "Lazer", "Outros"]
    return ok(sorted(set(base) | set(cs)))


# --------------------------------------------------------------------------- fala
UNID = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez",
        "onze", "doze", "treze", "quatorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove"]


def reais_falado(v: float) -> str:
    """280.5 -> '280 reais e 50 centavos' (o TTS lê os números)."""
    v = round(v or 0, 2)
    r = int(v)
    c = int(round((v - r) * 100))
    partes = []
    if r:
        partes.append(f"{r} {'real' if r == 1 else 'reais'}")
    if c:
        partes.append(f"{c} {'centavo' if c == 1 else 'centavos'}")
    return " e ".join(partes) or "zero reais"


def lista_falada(itens):
    frases = [f"{i['descricao']}, {reais_falado(i['valor'])}" for i in itens]
    if len(frases) <= 1:
        return "".join(frases)
    return "; ".join(frases[:-1]) + "; e " + frases[-1]


def texto_manha(hj, amanha, vencidas):
    partes = []
    if hj:
        partes.append(("Hoje vence: " if len(hj) == 1 else f"Hoje vencem {len(hj)} contas: ")
                      + lista_falada(hj) + ".")
    if vencidas:
        n = len(vencidas)
        partes.append((f"Tem uma conta vencida: " if n == 1 else f"Tem {n} contas vencidas: ")
                      + lista_falada(vencidas[:5]) + ("." if n <= 5 else ", e outras."))
    if amanha:
        partes.append(("Amanhã vence: " if len(amanha) == 1 else f"Amanhã vencem {len(amanha)} contas: ")
                      + lista_falada(amanha) + ".")
    return " ".join(partes) if partes else "Nenhuma conta para pagar hoje."


# --------------------------------------------------------------------------- HA
def slug(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "fundo"


def estados_ha():
    hj = hoje()
    with db() as con:
        gerar_recorrentes(con)
        pega = lambda sql, *a: [  # noqa: E731
            {"id": r["id"], "descricao": r["descricao"], "valor": r["valor"], "vencimento": r["vencimento"]}
            for r in rows(con.execute(sql, a))]
        l_hoje = pega("SELECT * FROM contas WHERE pago=0 AND valor>0 AND vencimento=? ORDER BY id", hj.isoformat())
        l_amanha = pega("SELECT * FROM contas WHERE pago=0 AND valor>0 AND vencimento=? ORDER BY id",
                        (hj + timedelta(days=1)).isoformat())
        l_venc = pega("SELECT * FROM contas WHERE pago=0 AND valor>0 AND vencimento<? ORDER BY vencimento", hj.isoformat())
        l_semana = pega("SELECT * FROM contas WHERE pago=0 AND valor>0 AND vencimento BETWEEN ? AND ? ORDER BY vencimento",
                        hj.isoformat(), (hj + timedelta(days=7)).isoformat())
        ini = date(hj.year, hj.month, 1)
        r = resumo_mes(con, ini, add_meses(ini, 1) - timedelta(days=1))
        fundos = saldos_fundos(con)

    soma = lambda l: round(sum(i["valor"] for i in l), 2)  # noqa: E731
    texto = texto_manha(l_hoje, l_amanha, l_venc)
    dinheiro = {"unit_of_measurement": "BRL", "device_class": "monetary", "state_class": "total"}
    est = {
        "sensor.contas_casa_hoje": (len(l_hoje), {
            "friendly_name": "Contas que vencem hoje", "icon": "mdi:calendar-alert",
            "contas": l_hoje, "total": soma(l_hoje), "texto": texto,
            "texto_amanha": texto_manha([], l_amanha, []) if l_amanha else ""}),
        "sensor.contas_casa_amanha": (len(l_amanha), {
            "friendly_name": "Contas que vencem amanhã", "icon": "mdi:calendar-arrow-right",
            "contas": l_amanha, "total": soma(l_amanha)}),
        "sensor.contas_casa_vencidas": (len(l_venc), {
            "friendly_name": "Contas vencidas", "icon": "mdi:alert-circle",
            "contas": l_venc, "total": soma(l_venc)}),
        "sensor.contas_casa_proximos_7_dias": (len(l_semana), {
            "friendly_name": "Contas nos próximos 7 dias", "icon": "mdi:calendar-week",
            "contas": l_semana, "total": soma(l_semana)}),
        "sensor.contas_casa_a_pagar_mes": (r["contas_a_pagar"], {
            **dinheiro, "friendly_name": "Contas a pagar no mês", "icon": "mdi:cash-clock",
            "quantidade": r["n_a_pagar"], "total_mes": r["contas_total"], "pago": r["contas_pago"]}),
        "sensor.contas_casa_entradas_mes": (r["entradas"], {
            **dinheiro, "friendly_name": "Entradas do mês", "icon": "mdi:cash-plus"}),
        "sensor.contas_casa_sobra_mes": (r["sobra"], {
            **dinheiro, "friendly_name": "Sobra do mês", "icon": "mdi:scale-balance",
            "state_class": "measurement"}),
    }
    for f in fundos:
        est[f"sensor.fundo_{re.sub(r'^fundo_', '', slug(f['nome']))}"] = (round(f["saldo"], 2), {
            **dinheiro, "friendly_name": f["nome"], "icon": "mdi:piggy-bank",
            "meta": f["meta"], "state_class": "measurement"})
    return est


async def publicar_ha(app):
    if not SUPERVISOR_TOKEN:
        log.info("Sem SUPERVISOR_TOKEN: sensores do HA desativados")
        return
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    ev: asyncio.Event = app["publicar"]
    async with ClientSession(timeout=ClientTimeout(total=15), headers=headers) as s:
        while True:
            try:
                for ent, (estado, attrs) in estados_ha().items():
                    async with s.post(f"{HA_API}/states/{ent}",
                                      json={"state": estado, "attributes": attrs}) as r:
                        if r.status >= 400:
                            log.warning("HA recusou %s: %s", ent, r.status)
            except Exception as e:  # HA reiniciando, etc.
                log.warning("Falha ao publicar no HA: %s", e)
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=300)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass


async def iniciar_tarefas(app):
    app["tarefas"].append(asyncio.create_task(publicar_ha(app)))


async def parar_tarefas(app):
    for t in app["tarefas"]:
        t.cancel()


async def hoje_api(req):
    return ok({k: {"estado": v[0], **v[1]} for k, v in estados_ha().items()})


# --------------------------------------------------------------------------- app
def criar_app():
    init_db()
    app = web.Application(middlewares=[guarda], client_max_size=25 * 1024 * 1024)
    app["publicar"] = asyncio.Event()
    app["tarefas"] = []
    r = app.router
    r.add_get("/", index)
    r.add_get("/api/eu", eu)
    r.add_get("/api/resumo", resumo)
    r.add_get("/api/acessos", listar_acessos)
    r.add_post("/api/acessos", mudar_acesso)
    r.add_get("/api/categorias", categorias)
    r.add_get("/api/hoje", hoje_api)
    r.add_get("/api/contas", listar_contas)
    r.add_post("/api/contas", criar_conta)
    r.add_put("/api/contas/{id:\\d+}", editar_conta)
    r.add_delete("/api/contas/{id:\\d+}", apagar_conta)
    r.add_post("/api/contas/{id:\\d+}/pagar", pagar_conta)
    r.add_post("/api/contas/{id:\\d+}/comprovante", comprovante_conta)
    r.add_post("/api/contas/{id:\\d+}/desfazer", desfazer_pagamento)
    r.add_get("/api/comprovante/{nome}", ver_comprovante)
    r.add_get("/api/entradas", listar_entradas)
    r.add_post("/api/entradas", criar_entrada)
    r.add_delete("/api/entradas/{id:\\d+}", apagar_entrada)
    r.add_get("/api/fundos", listar_fundos)
    r.add_post("/api/fundos", criar_fundo)
    r.add_put("/api/fundos/{id:\\d+}", editar_fundo)
    r.add_delete("/api/fundos/{id:\\d+}", apagar_fundo)
    r.add_get("/api/fundos/{id:\\d+}/movimentos", movimentos_fundo)
    r.add_post("/api/fundos/{id:\\d+}/movimentos", criar_movimento)
    r.add_delete("/api/movimentos/{id:\\d+}", apagar_movimento)
    r.add_static("/static", STATIC_DIR)
    app.on_startup.append(iniciar_tarefas)
    app.on_cleanup.append(parar_tarefas)
    return app


if __name__ == "__main__":
    log.info("Contas da Casa iniciando na porta %s (dados em %s)", PORT, DATA_DIR)
    web.run_app(criar_app(), host="0.0.0.0", port=PORT, access_log=None)
