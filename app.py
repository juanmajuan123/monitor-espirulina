"""
Fotobiorreactor de espirulina - servidor para Render

- Recibe las lecturas del ESP32 y le contesta qué hacer con los relés.
- Página con lecturas, vista 3D, controles (con contraseña) y gráficas.
- Bitácora de volumen, medio y nutrientes agregados.

Variables de entorno en Render:
  API_KEY         clave que también va en el ESP32 (obligatoria)
  PANEL_PASSWORD  contraseña para usar los botones de la página (obligatoria para controlar)
  DATABASE_URL    (opcional) Postgres de Render; sin ella usa SQLite, que se borra al reiniciar
"""
import csv
import hmac
import io
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API_KEY = os.environ.get("API_KEY", "").strip()
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()
SQLITE_PATH = os.environ.get("SQLITE_PATH", os.path.join(BASE, "reactor.db"))
USE_PG = DATABASE_URL.startswith("postgres")
if USE_PG:
    import psycopg  # noqa: E402

PH = "%s" if USE_PG else "?"
CAMPOS = ["t_reactor", "t_frente", "h_frente", "t_atras", "h_atras"]
MAX_PUNTOS = 1500

CONFIG_DEFAULT = {
    "luz_modo": "auto",          # auto | on | off
    "bomba_modo": "auto",
    "luz_inicio": None,          # segundos UTC en que empezó el ciclo de luz (empieza encendida)
    "bomba_inicio": None,        # segundos UTC en que empezó el ciclo de burbujeo (empieza burbujeando)
    "luz_horas_on": 12,
    "luz_horas_off": 12,
    "bomba_min_on": 15,
    "bomba_min_ciclo": 60,
    "bomba_solo_con_luz": True,
    "t_corte": 36.0,
    "t_reanuda": 33.0,
}


# ---------------- Base de datos ----------------
def conectar():
    if USE_PG:
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect(SQLITE_PATH)


def iniciar_db():
    id_col = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with conectar() as con:
        con.execute(f"""CREATE TABLE IF NOT EXISTS lecturas (
            id {id_col}, ts TEXT NOT NULL,
            t_reactor REAL, t_frente REAL, h_frente REAL, t_atras REAL, h_atras REAL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_lecturas_ts ON lecturas (ts)")
        con.execute("CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT)")
        con.execute(f"""CREATE TABLE IF NOT EXISTS bitacora (
            id {id_col}, ts TEXT NOT NULL, tipo TEXT NOT NULL,
            volumen_l REAL, medio_ml REAL, nutritivo_ml REAL, cosecha_ml REAL,
            secchi_cm REAL, ph REAL, nota TEXT)""")


def leer_ajuste(clave, default=None):
    with conectar() as con:
        fila = con.execute(f"SELECT valor FROM ajustes WHERE clave = {PH}", (clave,)).fetchone()
    return json.loads(fila[0]) if fila else default


def guardar_ajuste(clave, valor):
    with conectar() as con:
        con.execute(
            f"INSERT INTO ajustes (clave, valor) VALUES ({PH}, {PH}) "
            "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
            (clave, json.dumps(valor)),
        )


def config():
    c = dict(CONFIG_DEFAULT)
    c.update(leer_ajuste("config", {}) or {})
    return c


def ahora_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def a_numero(valor, lo=-60, hi=150):
    try:
        if valor is None or valor == "":
            return None
        n = float(valor)
        if n != n or n < lo or n > hi:
            return None
        return round(n, 2)
    except (TypeError, ValueError):
        return None


def consultar(horas):
    corte = ahora_iso(datetime.now(timezone.utc) - timedelta(hours=horas))
    with conectar() as con:
        filas = con.execute(
            f"SELECT ts, {', '.join(CAMPOS)} FROM lecturas WHERE ts >= {PH} ORDER BY ts", (corte,)
        ).fetchall()
    return [dict(zip(["ts"] + CAMPOS, f)) for f in filas]


def guardar_lectura(datos):
    valores = [a_numero(datos.get(c)) for c in CAMPOS]
    if all(v is None for v in valores):
        return False
    with conectar() as con:
        con.execute(
            f"INSERT INTO lecturas (ts, {', '.join(CAMPOS)}) VALUES ({', '.join([PH] * 6)})",
            [ahora_iso()] + valores,
        )
    return True


BIT_CAMPOS = ["ts", "tipo", "volumen_l", "medio_ml", "nutritivo_ml", "cosecha_ml", "secchi_cm", "ph", "nota"]


def bitacora(limite=None):
    sql = f"SELECT {', '.join(BIT_CAMPOS)} FROM bitacora ORDER BY ts DESC, id DESC"
    if limite:
        sql += f" LIMIT {int(limite)}"
    with conectar() as con:
        filas = con.execute(sql).fetchall()
    return [dict(zip(BIT_CAMPOS, f)) for f in filas]


def volumen_actual():
    ult = bitacora(1)
    return ult[0]["volumen_l"] if ult else None


iniciar_db()


# ---------------- Seguridad ----------------
def clave_esp_ok():
    return not API_KEY or hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY)


def clave_panel_ok():
    return bool(PANEL_PASSWORD) and hmac.compare_digest(
        request.headers.get("X-Panel-Clave", ""), PANEL_PASSWORD
    )


def sin_permiso():
    if not PANEL_PASSWORD:
        return jsonify(ok=False, error="Falta configurar PANEL_PASSWORD en Render"), 403
    return jsonify(ok=False, error="Contraseña incorrecta"), 401


# ---------------- Estado completo para la página ----------------
def estado():
    return {
        "ahora": int(time.time()),
        "config": config(),
        "dispositivo": leer_ajuste("dispositivo"),
        "volumen_l": volumen_actual(),
        "bitacora": bitacora(6),
        "protegido": bool(PANEL_PASSWORD),
    }


# ---------------- Página ----------------
@app.get("/")
def pagina():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/salud")
def salud():
    return "ok"


# ---------------- ESP32 ----------------
@app.post("/api/esp32/sync")
def esp32_sync():
    """El ESP32 llama cada 5 s: manda su estado (y lecturas) y recibe la configuración."""
    if not clave_esp_ok():
        return jsonify(ok=False, error="clave incorrecta"), 401
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify(ok=False, error="se esperaba JSON"), 400

    if d.get("guardar"):
        guardar_lectura(d)

    c = config()
    cambio = False
    # Si el servidor perdió los ciclos (reinicio de Render), adopta los del ESP32
    for k in ("luz_inicio", "bomba_inicio"):
        v = d.get(k)
        if c.get(k) is None and isinstance(v, (int, float)) and v > 1_600_000_000:
            c[k] = int(v)
            cambio = True
    if cambio:
        guardar_ajuste("config", c)

    guardar_ajuste("dispositivo", {
        "visto": int(time.time()),
        "luz": bool(d.get("luz")),
        "bomba": bool(d.get("bomba")),
        "corte": bool(d.get("corte")),
        "hora_ok": bool(d.get("hora_ok")),
        "uptime": d.get("uptime"),
        "rssi": d.get("rssi"),
        "t_reactor": a_numero(d.get("t_reactor")),
    })
    return jsonify(ok=True, ahora=int(time.time()), config=c)


@app.post("/api/lecturas")          # compatibilidad con el código anterior del ESP32
def recibir_lectura():
    if not clave_esp_ok():
        return jsonify(ok=False, error="clave incorrecta"), 401
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not guardar_lectura(d):
        return jsonify(ok=False, error="sin datos válidos"), 400
    return jsonify(ok=True)


# ---------------- Lecturas ----------------
@app.get("/api/lecturas")
def historial():
    try:
        horas = float(request.args.get("horas", 6))
    except ValueError:
        horas = 6
    filas = consultar(max(0.1, min(horas, 24 * 31)))
    if len(filas) > MAX_PUNTOS:
        paso = len(filas) // MAX_PUNTOS + 1
        filas = filas[::paso] + ([filas[-1]] if (len(filas) - 1) % paso else [])
    return jsonify(filas)


@app.get("/api/ultima")
def ultima():
    with conectar() as con:
        f = con.execute(f"SELECT ts, {', '.join(CAMPOS)} FROM lecturas ORDER BY ts DESC LIMIT 1").fetchone()
    return jsonify(dict(zip(["ts"] + CAMPOS, f)) if f else None)


@app.get("/descargar.csv")
def descargar():
    try:
        horas = float(request.args.get("horas", 24 * 7))
    except ValueError:
        horas = 24 * 7
    filas = consultar(max(0.1, min(horas, 24 * 31)))
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["fecha_utc", "temp_reactor_C", "temp_frente_C", "hum_frente_%", "temp_atras_C", "hum_atras_%"])
    for f in filas:
        w.writerow([f["ts"]] + [f[c] for c in CAMPOS])
    return Response(salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lecturas_espirulina.csv"})


# ---------------- Estado y controles ----------------
@app.get("/api/estado")
def api_estado():
    return jsonify(estado())


@app.post("/api/verificar")
def verificar():
    if not clave_panel_ok():
        return sin_permiso()
    return jsonify(ok=True)


@app.post("/api/control")
def control():
    if not clave_panel_ok():
        return sin_permiso()
    d = request.get_json(silent=True) or {}
    accion = d.get("accion")
    c = config()
    ahora = int(time.time())

    if accion in ("luz_modo", "bomba_modo"):
        if d.get("valor") not in ("auto", "on", "off"):
            return jsonify(ok=False, error="modo inválido"), 400
        c[accion] = d["valor"]
    elif accion == "reset_luz":
        c["luz_inicio"] = ahora          # la luz empieza su fase encendida desde ahora
        c["luz_modo"] = "auto"
    elif accion == "reset_bomba":
        c["bomba_inicio"] = ahora        # el burbujeo empieza desde ahora
        c["bomba_modo"] = "auto"
    elif accion == "ajustes":
        v = d.get("valores") or {}
        limites = {"luz_horas_on": (1, 23), "luz_horas_off": (1, 23), "bomba_min_on": (1, 120),
                   "bomba_min_ciclo": (5, 240), "t_corte": (30, 40), "t_reanuda": (25, 38)}
        for k, (lo, hi) in limites.items():
            n = a_numero(v.get(k), lo, hi)
            if n is not None:
                c[k] = n
        if "bomba_solo_con_luz" in v:
            c["bomba_solo_con_luz"] = bool(v["bomba_solo_con_luz"])
        if c["bomba_min_on"] >= c["bomba_min_ciclo"]:
            return jsonify(ok=False, error="El burbujeo debe durar menos que el ciclo"), 400
        if c["t_reanuda"] >= c["t_corte"]:
            return jsonify(ok=False, error="La temperatura para volver a prender debe ser menor que la de corte"), 400
    else:
        return jsonify(ok=False, error="acción desconocida"), 400

    guardar_ajuste("config", c)
    return jsonify(ok=True, estado=estado())


# ---------------- Bitácora ----------------
@app.get("/api/bitacora")
def api_bitacora():
    return jsonify(bitacora(200))


@app.post("/api/bitacora")
def nueva_bitacora():
    if not clave_panel_ok():
        return sin_permiso()
    d = request.get_json(silent=True) or {}
    tipo = d.get("tipo")
    if tipo not in ("agregado", "medicion", "cosecha", "volumen"):
        return jsonify(ok=False, error="tipo inválido"), 400

    medio = a_numero(d.get("medio_ml"), 0, 100000) or 0
    nutr = a_numero(d.get("nutritivo_ml"), 0, 10000) or 0
    cosecha = a_numero(d.get("cosecha_ml"), 0, 100000) or 0
    actual = volumen_actual()

    if tipo == "volumen":
        vol = a_numero(d.get("volumen_l"), 0, 1000)
        if vol is None:
            return jsonify(ok=False, error="Escribe el volumen en litros"), 400
    elif actual is None:
        return jsonify(ok=False, error="Primero registra el volumen actual del cultivo"), 400
    elif tipo == "agregado":
        if not medio and not nutr:
            return jsonify(ok=False, error="Escribe cuánto medio o nutritivo agregaste"), 400
        vol = actual + (medio + nutr) / 1000
    elif tipo == "cosecha":
        if not cosecha:
            return jsonify(ok=False, error="Escribe cuánto cultivo cosechaste"), 400
        vol = max(0, actual - cosecha / 1000)
    else:
        vol = actual

    with conectar() as con:
        con.execute(
            f"INSERT INTO bitacora ({', '.join(BIT_CAMPOS)}) VALUES ({', '.join([PH] * 9)})",
            [ahora_iso(), tipo, round(vol, 3), medio or None, nutr or None, cosecha or None,
             a_numero(d.get("secchi_cm"), 0, 100), a_numero(d.get("ph"), 0, 14),
             (str(d.get("nota") or "").strip()[:300] or None)],
        )
    return jsonify(ok=True, estado=estado())


@app.get("/bitacora.csv")
def bitacora_csv():
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["fecha_utc", "tipo", "volumen_L", "medio_mL", "nutritivo_mL", "cosecha_mL", "secchi_cm", "pH", "nota"])
    for f in reversed(bitacora()):
        w.writerow([f[k] for k in BIT_CAMPOS])
    return Response(salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bitacora_espirulina.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
