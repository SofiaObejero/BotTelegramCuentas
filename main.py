"""
Gestor de cuentas por Telegram.

Mensajes que entiende (sin barra):
  juan cuenta 5000          -> anota 5000 a cuenta de Juan
  juan transf 3000          -> Juan queda pendiente de transferir 3000
  juan pagó 2000            -> descuenta 2000 (primero de transf, si no, de cuenta)
  juan pagó transf 2000     -> descuenta 2000 de lo pendiente de transferencia
  juan pagó                 -> paga todo el saldo (de transf, o de cuenta si no hay transf)
  maria lopez cuenta 1.500  -> los nombres pueden tener varias palabras

Comandos:
  /hoy        total pendiente de transferencia + quién debe
  /resumen    todo lo pendiente, por persona y por tipo
  /cliente juan   movimientos y saldo de una persona
  /deshacer   borra el último movimiento
"""
import os
import re
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
# Esto busca el archivo .env y carga las variables
load_dotenv()

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters,
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
# IDs de Telegram autorizados, separados por coma. Si está vacío, no responde a nadie.
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}
DB = os.environ.get("DB_PATH", "cuentas.db")

PATRON = re.compile(
    r"^(?P<nombre>.+?)\s+(?P<accion>cuenta|transf|transferencia|pag[oó])"
    r"(?:\s+(?P<tipo>cuenta|transf|transferencia))?"
    r"(?:\s+\$?(?P<monto>[\d.,]+))?\s*$",
    re.IGNORECASE,
)


# ---------- base de datos ----------
def db():
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS mov (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               nombre TEXT NOT NULL,
               tipo TEXT NOT NULL,          -- 'cuenta' o 'transf'
               monto REAL NOT NULL,         -- positivo = debe, negativo = pago
               fecha TEXT NOT NULL
           )"""
    )
    return con


def saldo(con, nombre, tipo):
    r = con.execute(
        "SELECT COALESCE(SUM(monto),0) FROM mov WHERE nombre=? AND tipo=?",
        (nombre, tipo),
    ).fetchone()
    return round(r[0], 2)


def agregar(con, nombre, tipo, monto):
    con.execute(
        "INSERT INTO mov (nombre, tipo, monto, fecha) VALUES (?,?,?,?)",
        (nombre, tipo, monto, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()


# ---------- utilidades ----------
def plata(x):
    return "$" + f"{x:,.0f}".replace(",", ".")


def parse_monto(s):
    s = s.strip()
    if "," in s:                     # 1.500,50
        s = s.replace(".", "").replace(",", ".")
    else:                            # 1.500 -> 1500
        s = s.replace(".", "")
    return float(s)


def tipo_norm(t):
    return "transf" if t.lower().startswith("transf") else "cuenta"


def autorizado(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ALLOWED


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        await update.message.reply_text(f"Tu ID es {update.effective_user.id}. Agregalo a ALLOWED_IDS.")
        return
    await update.message.reply_text(__doc__)


async def mensaje(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    m = PATRON.match(update.message.text.strip())
    if not m:
        await update.message.reply_text("No entendí. Ejemplos:\njuan cuenta 5000\njuan transf 3000\njuan pagó 2000")
        return

    nombre = m["nombre"].strip().lower()
    accion = m["accion"].lower()
    monto = parse_monto(m["monto"]) if m["monto"] else None
    con = db()
    bonito = nombre.title()

    if accion in ("cuenta", "transf", "transferencia"):
        if monto is None or monto <= 0:
            await update.message.reply_text("Falta el monto. Ej: juan cuenta 5000")
            return
        tipo = tipo_norm(accion)
        agregar(con, nombre, tipo, monto)
        etiqueta = "a cuenta" if tipo == "cuenta" else "pendiente de transferencia"
        await update.message.reply_text(
            f"✅ {bonito}: +{plata(monto)} {etiqueta}\nSaldo {tipo}: {plata(saldo(con, nombre, tipo))}"
        )
        return

    # pagos
    if m["tipo"]:
        tipo = tipo_norm(m["tipo"])
    else:
        tipo = "transf" if saldo(con, nombre, "transf") > 0 else "cuenta"
    actual = saldo(con, nombre, tipo)
    if actual <= 0:
        await update.message.reply_text(f"{bonito} no tiene saldo en {tipo}.")
        return
    if monto is None:
        monto = actual
    if monto > actual:
        await update.message.reply_text(
            f"⚠️ {bonito} solo debe {plata(actual)} en {tipo}, no {plata(monto)}."
        )
        return
    agregar(con, nombre, tipo, -monto)
    resto = saldo(con, nombre, tipo)
    estado = "saldado 🎉" if resto <= 0 else f"quedan {plata(resto)}"
    await update.message.reply_text(f"💸 {bonito} pagó {plata(monto)} ({tipo}): {estado}")


def filas_pendientes(con, tipo=None):
    q = "SELECT nombre, tipo, ROUND(SUM(monto),2) s FROM mov"
    params = ()
    if tipo:
        q += " WHERE tipo=?"
        params = (tipo,)
    q += " GROUP BY nombre, tipo HAVING s > 0 ORDER BY nombre"
    return con.execute(q, params).fetchall()


async def hoy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    con = db()
    filas = filas_pendientes(con, "transf")
    if not filas:
        await update.message.reply_text("No hay transferencias pendientes 👌")
        return
    lineas = [f"• {n.title()}: {plata(s)}" for n, _, s in filas]
    total = sum(s for _, _, s in filas)
    await update.message.reply_text("Pendiente de transferencia:\n" + "\n".join(lineas) + f"\n\nTOTAL: {plata(total)}")


async def resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    con = db()
    filas = filas_pendientes(con)
    if not filas:
        await update.message.reply_text("Todo saldado 👌")
        return
    out, tot = [], {"cuenta": 0, "transf": 0}
    for n, t, s in filas:
        out.append(f"• {n.title()} ({t}): {plata(s)}")
        tot[t] += s
    out.append(f"\nA cuenta: {plata(tot['cuenta'])}\nPendiente transf: {plata(tot['transf'])}")
    await update.message.reply_text("\n".join(out))


async def cliente(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    nombre = " ".join(ctx.args).strip().lower()
    if not nombre:
        await update.message.reply_text("Uso: /cliente juan")
        return
    con = db()
    movs = con.execute(
        "SELECT fecha, tipo, monto FROM mov WHERE nombre=? ORDER BY id DESC LIMIT 15", (nombre,)
    ).fetchall()
    if not movs:
        await update.message.reply_text("No encontré a esa persona.")
        return
    lineas = [f"{f[:10]} {t}: {'+' if mto > 0 else ''}{plata(mto)}" for f, t, mto in movs]
    await update.message.reply_text(
        f"{nombre.title()}\nA cuenta: {plata(saldo(con, nombre, 'cuenta'))}\n"
        f"Transf: {plata(saldo(con, nombre, 'transf'))}\n\nÚltimos movimientos:\n" + "\n".join(lineas)
    )


async def deshacer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    con = db()
    r = con.execute("SELECT id, nombre, tipo, monto FROM mov ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        await update.message.reply_text("No hay nada para deshacer.")
        return
    con.execute("DELETE FROM mov WHERE id=?", (r[0],))
    con.commit()
    await update.message.reply_text(f"↩️ Borré: {r[1].title()} {r[2]} {plata(r[3])}")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "ayuda"], start))
    app.add_handler(CommandHandler("hoy", hoy))
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("cliente", cliente))
    app.add_handler(CommandHandler("deshacer", deshacer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje))
    app.run_polling()


if __name__ == "__main__":
    print("El bot de Telegram se está ejecutando correctamente...")
    main()