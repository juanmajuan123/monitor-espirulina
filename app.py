"""
Monitor del fotobiorreactor de espirulina
Servidor Flask para Render: recibe lecturas del ESP32 y muestra gráficas en tiempo real.

Base de datos:
  - Si existe la variable de entorno DATABASE_URL (Postgres de Render), usa Postgres.
  - Si no, usa SQLite (en Render gratis se BORRA cuando el servicio se reinicia o se vuelve a desplegar).
"""
import csv
import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
API_KEY = os.environ.get("API_KEY", "").strip()
SQLITE_PATH = os.environ.get("SQLITE_PATH", "lecturas.db")
USE_PG = DATABASE_URL.startswith("postgres")

if USE_PG:
    import psycopg  # noqa: E402

PH = "%s" if USE_PG else "?"   # marcador de parámetros según la base
CAMPOS = ["t_reactor", "t_frente", "h_frente", "t_atras", "h_atras"]
MAX_PUNTOS = 1500               # máximo de puntos que se mandan a la gráfica


# ---------------- Base de datos ----------------
def conectar():
    if USE_PG:
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect(SQLITE_PATH)


def iniciar_db():
    id_col = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with conectar() as con:
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS lecturas (
                id {id_col},
                ts TEXT NOT NULL,
                t_reactor REAL,
                t_frente REAL,
                h_frente REAL,
                t_atras REAL,
                h_atras REAL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_lecturas_ts ON lecturas (ts)")


def ahora_iso(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def a_numero(valor):
    """Convierte a float o devuelve None si viene vacío, null o es inválido."""
    try:
        if valor is None:
            return None
        n = float(valor)
        if n != n or n < -60 or n > 150:   # NaN o fuera de rango razonable
            return None
        return round(n, 2)
    except (TypeError, ValueError):
        return None


def consultar(horas):
    corte = ahora_iso(datetime.now(timezone.utc) - timedelta(hours=horas))
    with conectar() as con:
        filas = con.execute(
            f"SELECT ts, {', '.join(CAMPOS)} FROM lecturas WHERE ts >= {PH} ORDER BY ts",
            (corte,),
        ).fetchall()
    return [dict(zip(["ts"] + CAMPOS, f)) for f in filas]


iniciar_db()


# ---------------- Rutas ----------------
@app.get("/")
def tablero():
    return render_template("index.html")


@app.post("/api/lecturas")
def recibir_lectura():
    if API_KEY and request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify(ok=False, error="clave incorrecta"), 401

    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify(ok=False, error="se esperaba JSON"), 400

    valores = [a_numero(datos.get(c)) for c in CAMPOS]
    if all(v is None for v in valores):
        return jsonify(ok=False, error="sin datos válidos"), 400

    with conectar() as con:
        con.execute(
            f"INSERT INTO lecturas (ts, {', '.join(CAMPOS)}) VALUES ({', '.join([PH] * 6)})",
            [ahora_iso()] + valores,
        )
    return jsonify(ok=True)


@app.get("/api/lecturas")
def historial():
    try:
        horas = float(request.args.get("horas", 6))
    except ValueError:
        horas = 6
    horas = max(0.1, min(horas, 24 * 31))

    filas = consultar(horas)
    if len(filas) > MAX_PUNTOS:                     # reduce puntos para que la gráfica no se trabe
        paso = len(filas) // MAX_PUNTOS + 1
        filas = filas[::paso] + ([filas[-1]] if (len(filas) - 1) % paso else [])
    return jsonify(filas)


@app.get("/api/ultima")
def ultima():
    with conectar() as con:
        f = con.execute(
            f"SELECT ts, {', '.join(CAMPOS)} FROM lecturas ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return jsonify(dict(zip(["ts"] + CAMPOS, f)) if f else None)


@app.get("/descargar.csv")
def descargar():
    try:
        horas = float(request.args.get("horas", 24 * 7))
    except ValueError:
        horas = 24 * 7
    filas = consultar(max(0.1, min(horas, 24 * 31)))

    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(["fecha_utc", "temp_reactor_C", "temp_frente_C", "hum_frente_%",
                       "temp_atras_C", "hum_atras_%"])
    for f in filas:
        escritor.writerow([f["ts"]] + [f[c] for c in CAMPOS])

    return Response(
        salida.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=lecturas_espirulina.csv"},
    )


@app.get("/salud")
def salud():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
