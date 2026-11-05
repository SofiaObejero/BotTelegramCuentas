"""
Gestor de cuentas por Telegram (versión simple).

Cada persona tiene hasta dos cuentas: "cuenta" (lo que debe) y "transf" (lo que debe transferir).
Cada cuenta es una lista de deudas con descripción, más lo que ya abonó.
Cuando una cuenta llega a $0, su lista se borra.
"""
import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

load_dotenv()  # lee el archivo .env y carga las variables (TELEGRAM_TOKEN)

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = {7410439541, 5399353387}  # IDs de Telegram que pueden usar el bot
DB = os.environ.get("DB_PATH", "cuentas.db")

AYUDA = """Anotar (la descripción es opcional):
  juan cta 5000 pan y leche       -> Juan debe 5000 a cuenta
  juan trans 3000 pedido martes   -> Juan debe transferir 3000

Abonar:
  juan pagó 2000          -> abona 2000 de lo que debe a cuenta
  juan pagó               -> abona TODO lo que debe a cuenta
  juan transfirió 3000    -> abona 3000 de lo que debe transferir
  juan transfirió         -> abona TODO lo que debe transferir
Cuando una cuenta queda en $0, su lista se borra.

Comandos:
  /hoy                    lo pendiente de transferir, con detalle
  /resumen                todo lo pendiente, con detalle
  /cliente juan           solo los totales de Juan
  /clienteresumen juan    la lista de Juan con su total
  /borrar                 borra el último movimiento
"""

# Cada palabra que el bot entiende -> (tipo de cuenta que toca, +1 suma deuda / -1 resta deuda)
# ("cuenta" y "transf" son los nombres internos que se guardan en la base: no se cambian)
ACCIONES = {
    "cta": ("cuenta", +1),
    "trans": ("transf", +1),
    "pagó": ("cuenta", -1),
    "pago": ("cuenta", -1),
    "transfirió": ("transf", -1),
    "transfirio": ("transf", -1),
}

# Textos que se muestran según el tipo de cuenta
NOMBRE_TIPO = {"cuenta": "a cuenta", "transf": "a transferir"}
TEXTO_TOTAL = {"cuenta": "Total que adeuda", "transf": "Total a transferir"}


# ---------- base de datos ----------
def abrir_db():
    con = sqlite3.connect(DB)

    # Tabla principal. monto positivo = deuda, monto negativo = abono.
    con.execute(
        """CREATE TABLE IF NOT EXISTS mov (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               nombre TEXT NOT NULL,
               tipo TEXT NOT NULL,
               monto REAL NOT NULL,
               fecha TEXT NOT NULL,
               descripcion TEXT
           )"""
    )
    # Si la tabla venía de una versión anterior (sin descripción), se agrega la columna.
    columnas = [fila[1] for fila in con.execute("PRAGMA table_info(mov)").fetchall()]
    if "descripcion" not in columnas:
        con.execute("ALTER TABLE mov ADD COLUMN descripcion TEXT")

    # Copia del ÚLTIMO cierre (cuando alguien saldó todo), solo para poder usar /borrar.
    con.execute(
        """CREATE TABLE IF NOT EXISTS respaldo (
               id INTEGER, nombre TEXT, tipo TEXT, monto REAL, fecha TEXT, descripcion TEXT
           )"""
    )
    con.commit()
    return con


def saldo(con, nombre, tipo):
    """Total que debe una persona en una cuenta (deudas menos abonos)."""
    fila = con.execute(
        "SELECT SUM(monto) FROM mov WHERE nombre=? AND tipo=?",
        (nombre, tipo),
    ).fetchone()
    total = fila[0]
    if total is None:  # todavía no tiene movimientos
        return 0
    return round(total, 2)


def anotar(con, nombre, tipo, monto, descripcion=""):
    """Guarda un movimiento nuevo (monto positivo = deuda, negativo = abono)."""
    ahora = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO mov (nombre, tipo, monto, fecha, descripcion) VALUES (?, ?, ?, ?, ?)",
        (nombre, tipo, monto, ahora, descripcion),
    )
    con.commit()  # sin commit no se guarda nada


def cerrar_si_saldado(con, nombre, tipo):
    """
    Si la cuenta quedó en $0, borra toda su lista y devuelve True.
    Antes guarda una copia en 'respaldo' (solo la del último cierre) por si hay que deshacer.
    """
    if saldo(con, nombre, tipo) > 0:
        return False

    con.execute("DELETE FROM respaldo")  # nos quedamos solo con el último cierre
    con.execute(
        "INSERT INTO respaldo (id, nombre, tipo, monto, fecha, descripcion) "
        "SELECT id, nombre, tipo, monto, fecha, descripcion FROM mov WHERE nombre=? AND tipo=?",
        (nombre, tipo),
    )
    con.execute("DELETE FROM mov WHERE nombre=? AND tipo=?", (nombre, tipo))
    con.commit()
    return True


def limpiar_cuentas_saldadas(con):
    """
    Se ejecuta al arrancar. Borra lo que ya estaba pagado por completo en datos viejos
    (las versiones anteriores no borraban nada). Si ya está todo limpio, no hace nada.
    """
    grupos = con.execute("SELECT DISTINCT nombre, tipo FROM mov").fetchall()
    for nombre, tipo in grupos:
        filas = con.execute(
            "SELECT id, monto FROM mov WHERE nombre=? AND tipo=? ORDER BY id",
            (nombre, tipo),
        ).fetchall()
        corriente = 0
        ultimo_en_cero = 0  # id del último movimiento en el que la cuenta quedó en $0
        for id_mov, monto in filas:
            corriente = round(corriente + monto, 2)
            if corriente <= 0:
                ultimo_en_cero = id_mov
        if ultimo_en_cero > 0:
            con.execute(
                "DELETE FROM mov WHERE nombre=? AND tipo=? AND id<=?",
                (nombre, tipo, ultimo_en_cero),
            )
    con.commit()


# ---------- utilidades ----------
def plata(x):
    """5000 -> $5.000   |   -2000 -> -$2.000"""
    texto = f"{abs(x):,.0f}".replace(",", ".")
    if x < 0:
        return "-$" + texto
    return "$" + texto


def leer_monto(texto):
    """'1.500' -> 1500.0   |   '1.500,50' -> 1500.5   |   '$2000' -> 2000.0"""
    texto = texto.replace("$", "").replace(".", "").replace(",", ".")
    return float(texto)  # si no es un número, falla con ValueError


def bloque_cuenta(con, nombre, tipo):
    """
    Arma las líneas de texto de UNA cuenta:
      • Juan (a cuenta)
          – pan y leche: $4.000
          – verdura: $2.000
          Abonó: -$3.000
          Total que adeuda: $3.000
    """
    filas = con.execute(
        "SELECT monto, descripcion FROM mov WHERE nombre=? AND tipo=? ORDER BY id",
        (nombre, tipo),
    ).fetchall()

    lineas = [f"• {nombre.title()} ({NOMBRE_TIPO[tipo]})"]
    abonado = 0
    for monto, descripcion in filas:
        if monto > 0:
            lineas.append(f"    – {descripcion or 'sin detalle'}: {plata(monto)}")
        else:
            abonado += -monto
    if abonado > 0:
        lineas.append(f"    Abonó: -{plata(round(abonado, 2))}")
    lineas.append(f"    {TEXTO_TOTAL[tipo]}: {plata(saldo(con, nombre, tipo))}")
    return lineas


def autorizado(update):
    return update.effective_user.id in ALLOWED


async def enviar(update, texto):
    """Telegram no deja mandar mensajes de más de 4096 caracteres: si es largo, lo parte."""
    parte = ""
    for linea in texto.split("\n"):
        if len(parte) + len(linea) + 1 > 4000:
            await update.message.reply_text(parte)
            parte = ""
        parte += linea + "\n"
    if parte.strip() != "":
        await update.message.reply_text(parte)


# ---------- handlers ----------
async def start(update, ctx):
    if not autorizado(update):
        await update.message.reply_text(
            f"Tu ID es {update.effective_user.id}. Agregalo a ALLOWED en el código."
        )
        return
    await update.message.reply_text(AYUDA)


async def mensaje(update, ctx):
    """Se ejecuta con cualquier texto que NO sea un comando."""
    if not autorizado(update):
        return

    # Ej: "Maria Lopez cuenta 1.500 pan y leche" -> ["Maria", "Lopez", "cuenta", "1.500", "pan", "y", "leche"]
    palabras = update.message.text.split()

    # 1) Buscar cuál de las palabras es la acción (cuenta, transf, pagó, transfirió)
    posicion = None
    for i, palabra in enumerate(palabras):
        if palabra.lower() in ACCIONES:
            posicion = i
            break

    # Si no hay acción, o no hay nombre antes de ella, no se entiende
    if posicion is None or posicion == 0:
        await update.message.reply_text(
            "No entendí. Ejemplos:\njuan cta 5000 pan y leche\njuan trans 3000\n"
            "juan pagó 2000\njuan transfirió 3000"
        )
        return

    # 2) Separar las partes
    nombre = " ".join(palabras[:posicion]).lower()      # lo que está antes de la acción
    tipo, signo = ACCIONES[palabras[posicion].lower()]  # ej: ("cuenta", -1)
    resto = palabras[posicion + 1:]                     # lo que viene después

    # 3) Leer el monto (puede no venir) y la descripción (todo lo que sigue al monto)
    monto = None
    descripcion = ""
    if len(resto) > 0:
        try:
            monto = leer_monto(resto[0])
        except ValueError:
            await update.message.reply_text(f"'{resto[0]}' no es un monto válido.")
            return
        if monto <= 0:
            await update.message.reply_text("El monto tiene que ser mayor a 0.")
            return
        descripcion = " ".join(resto[1:])  # puede quedar vacía, está bien

    con = abrir_db()
    bonito = nombre.title()  # "maria lopez" -> "Maria Lopez"

    # ----- Caso A: sumar deuda (cuenta / transf) -----
    if signo == +1:
        if monto is None:
            await update.message.reply_text("Falta el monto. Ej: juan cta 5000")
            return
        anotar(con, nombre, tipo, monto, descripcion)
        texto = f"✅ {bonito}: +{plata(monto)} {NOMBRE_TIPO[tipo]}"
        if descripcion != "":
            texto += f" ({descripcion})"
        texto += f"\n{TEXTO_TOTAL[tipo]}: {plata(saldo(con, nombre, tipo))}"
        await update.message.reply_text(texto)
        return

    # ----- Caso B: abonar (pagó / transfirió) -----
    actual = saldo(con, nombre, tipo)

    if actual <= 0:
        await update.message.reply_text(f"{bonito} no tiene saldo {NOMBRE_TIPO[tipo]}.")
        return

    if monto is None:   # "juan pagó" sin monto = abona todo
        monto = actual

    if monto > actual:
        await update.message.reply_text(
            f"⚠️ {bonito} solo debe {plata(actual)} {NOMBRE_TIPO[tipo]}, no {plata(monto)}."
        )
        return

    anotar(con, nombre, tipo, -monto, descripcion)   # se guarda en negativo
    verbo = "pagó" if tipo == "cuenta" else "transfirió"
    if cerrar_si_saldado(con, nombre, tipo):
        estado = "saldado 🎉 (se borró su lista)"
    else:
        estado = f"quedan {plata(saldo(con, nombre, tipo))}"
    await update.message.reply_text(f"💸 {bonito} {verbo} {plata(monto)}: {estado}")


def filas_pendientes(con, tipo=None):
    """Devuelve (nombre, tipo, total) de cada cuenta que todavía tiene deuda."""
    consulta = "SELECT nombre, tipo, ROUND(SUM(monto), 2) AS total FROM mov"
    parametros = ()
    if tipo is not None:
        consulta += " WHERE tipo=?"
        parametros = (tipo,)
    # GROUP BY: suma por persona y tipo. HAVING: descarta los que están en 0.
    consulta += " GROUP BY nombre, tipo HAVING total > 0 ORDER BY nombre"
    return con.execute(consulta, parametros).fetchall()


async def hoy(update, ctx):
    if not autorizado(update):
        return
    con = abrir_db()
    filas = filas_pendientes(con, "transf")
    if len(filas) == 0:
        await update.message.reply_text("No hay transferencias pendientes 👌")
        return

    lineas = []
    total = 0
    for nombre, tipo, monto in filas:
        lineas += bloque_cuenta(con, nombre, tipo)
        lineas.append("")   # línea en blanco entre personas
        total += monto

    texto = "\n".join(lineas)
    texto += f"\nTOTAL A TRANSFERIR: {plata(total)}"
    await enviar(update, texto)


async def resumen(update, ctx):
    if not autorizado(update):
        return
    con = abrir_db()
    filas = filas_pendientes(con)
    if len(filas) == 0:
        await update.message.reply_text("Todo saldado 👌")
        return

    lineas = []
    for nombre, tipo, monto in filas:
        lineas += bloque_cuenta(con, nombre, tipo)
        lineas.append("")   # línea en blanco entre cuentas

    await enviar(update, "\n".join(lineas))


async def cliente(update, ctx):
    """Solo los totales de una persona."""
    if not autorizado(update):
        return
    nombre = " ".join(ctx.args).lower()   # /cliente maria lopez -> "maria lopez"
    if nombre == "":
        await update.message.reply_text("Uso: /cliente juan")
        return

    con = abrir_db()
    a_cuenta = saldo(con, nombre, "cuenta")
    a_transferir = saldo(con, nombre, "transf")
    if a_cuenta <= 0 and a_transferir <= 0:
        await update.message.reply_text(f"{nombre.title()} no tiene nada pendiente.")
        return

    await update.message.reply_text(
        f"{nombre.title()}\n"
        f"{TEXTO_TOTAL['cuenta']}: {plata(a_cuenta)}\n"
        f"{TEXTO_TOTAL['transf']}: {plata(a_transferir)}"
    )


async def clienteresumen(update, ctx):
    """La lista completa de una persona, con lo abonado y el total."""
    if not autorizado(update):
        return
    nombre = " ".join(ctx.args).lower()
    if nombre == "":
        await update.message.reply_text("Uso: /clienteresumen juan")
        return

    con = abrir_db()
    lineas = []
    for tipo in ("cuenta", "transf"):
        if saldo(con, nombre, tipo) > 0:
            lineas += bloque_cuenta(con, nombre, tipo)
            lineas.append("")
    if len(lineas) == 0:
        await update.message.reply_text(f"{nombre.title()} no tiene nada pendiente.")
        return
    await enviar(update, "\n".join(lineas))


async def borrar(update, ctx):
    """
    Deshace lo último que pasó:
      - si fue un movimiento (deuda o abono), lo borra;
      - si fue un abono que saldó toda una cuenta, cancela ese abono y devuelve la lista.
    """
    if not autorizado(update):
        return
    con = abrir_db()

    ultimo_mov = con.execute("SELECT MAX(id) FROM mov").fetchone()[0]
    ultimo_cierre = con.execute("SELECT MAX(id) FROM respaldo").fetchone()[0]

    # ¿Lo último que pasó fue un cierre? (los ids nunca se repiten, así que mayor = más nuevo)
    if ultimo_cierre is not None and (ultimo_mov is None or ultimo_mov < ultimo_cierre):
        filas = con.execute(
            "SELECT id, nombre, tipo, monto, fecha, descripcion FROM respaldo ORDER BY id"
        ).fetchall()
        abono_final = filas[-1]   # la última fila es el abono que saldó todo: ese se cancela
        for fila in filas[:-1]:   # el resto de la lista se devuelve
            con.execute(
                "INSERT INTO mov (id, nombre, tipo, monto, fecha, descripcion) VALUES (?, ?, ?, ?, ?, ?)",
                fila,
            )
        con.execute("DELETE FROM respaldo")
        con.commit()

        nombre, tipo, monto = abono_final[1], abono_final[2], abono_final[3]
        await update.message.reply_text(
            f"↩️ Cancelé el abono de {plata(abs(monto))} de {nombre.title()} ({NOMBRE_TIPO[tipo]}). "
            f"Volvió su lista.\n{TEXTO_TOTAL[tipo]}: {plata(saldo(con, nombre, tipo))}"
        )
        return

    # Caso normal: borrar el último movimiento
    ultimo = con.execute(
        "SELECT id, nombre, tipo, monto, descripcion FROM mov ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if ultimo is None:
        await update.message.reply_text("No hay nada para borrar.")
        return

    id_mov, nombre, tipo, monto, descripcion = ultimo
    con.execute("DELETE FROM mov WHERE id=?", (id_mov,))
    con.commit()

    que_era = "una deuda" if monto > 0 else "un abono"
    texto = f"↩️ Borré {que_era} de {nombre.title()} ({NOMBRE_TIPO[tipo]}) por {plata(abs(monto))}"
    if descripcion:
        texto += f" ({descripcion})"
    texto += f".\n{TEXTO_TOTAL[tipo]}: {plata(saldo(con, nombre, tipo))}"
    await update.message.reply_text(texto)


def main():
    con = abrir_db()
    limpiar_cuentas_saldadas(con)   # limpia datos viejos ya pagados
    con.close()

    app = ApplicationBuilder().token(TOKEN).build()
    # El orden importa: se usa el primer handler que coincide.
    app.add_handler(CommandHandler(["start", "ayuda"], start))
    app.add_handler(CommandHandler("hoy", hoy))
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("cliente", cliente))
    app.add_handler(CommandHandler("clienteresumen", clienteresumen))
    app.add_handler(CommandHandler("borrar", borrar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje))
    app.run_polling()


if __name__ == "__main__":
    print("El bot de Telegram se está ejecutando correctamente...")
    main()