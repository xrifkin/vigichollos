#!/usr/bin/env python3
"""
Vigilante de errores de precio (Chollometro -> ntfy).

Qué hace cada vez que se ejecuta:
  1. Lee los feeds RSS de Chollometro que se indican en config.json.
  2. Descarta las ofertas que ya había visto (memoria en state.json).
  3. Aplica las reglas de config.json (palabras de "error de precio",
     descuento alto, precio casi cero).
  4. Si una oferta pasa las reglas, envía una notificación al móvil con ntfy.

Modos de uso:
  python monitor.py --mode run        funcionamiento normal (el que usa GitHub)
  python monitor.py --mode diagnose   muestra qué datos trae el feed, sin avisar
  python monitor.py --mode test       envía una notificación de prueba

Solo usa la librería estándar de Python: no hay que instalar nada.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"

USER_AGENT = "chollos-alertas/1.0 (uso personal)"
NTFY_URL = "https://ntfy.sh/"
MAX_AVISOS_POR_EJECUCION = 10

# --------------------------------------------------------------------------
# Utilidades de texto y precios
# --------------------------------------------------------------------------

# Un precio: "12,99€", "1.299,99 €", "12.99€", "€12,99"
PRECIO_RE = re.compile(r"(\d[\d.,]*)\s*(?:€|eur\b|euros?\b)|€\s*(\d[\d.,]*)", re.I)

# Precio "anterior" mencionado de forma explícita: "antes 199€", "PVP: 249,99 €"...
PRECIO_ANTERIOR_RE = re.compile(
    r"(?:antes|pvp|rrp|precio\s+(?:anterior|habitual|original|normal|medio|recomendado)"
    r"|normalmente|val[ií]a)\D{0,25}?(\d[\d.,]*)\s*(?:€|eur\b|euros?\b)",
    re.I,
)

# Porcentajes explícitos: "-80%" o "80% de descuento"
PORCENTAJE_RES = [
    re.compile(r"[-−–]\s?(\d{2,3})\s?%"),
    re.compile(r"(\d{2,3})\s?%\s*(?:de\s+)?(?:dto\.?|descuento|off)\b", re.I),
]


def a_numero(texto):
    """Convierte '1.299,99' o '12.99' o '12,99' en un float. None si no se puede."""
    s = texto.strip().strip(".,")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif "." in s:
        partes = s.split(".")
        if len(partes) > 1 and len(partes[-1]) == 3:  # 1.299 -> 1299
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def limpiar_html(texto):
    """Quita etiquetas HTML y entidades (&amp; etc.) de la descripción."""
    if not texto:
        return ""
    sin_tags = re.sub(r"<[^>]+>", " ", html.unescape(texto))
    return re.sub(r"\s+", " ", sin_tags).strip()


def precios_en(texto):
    encontrados = []
    for m in PRECIO_RE.finditer(texto):
        valor = a_numero(m.group(1) or m.group(2) or "")
        if valor is not None:
            encontrados.append(valor)
    return encontrados


# --------------------------------------------------------------------------
# Lectura del feed
# --------------------------------------------------------------------------

def nombre_local(etiqueta):
    """'{namespace}merchant' -> 'merchant'."""
    return etiqueta.rsplit("}", 1)[-1]


def descargar(url):
    peticion = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(peticion, timeout=20) as respuesta:
        return respuesta.read()


def leer_feed(contenido):
    """Convierte el XML (RSS o Atom) en una lista de ofertas (diccionarios)."""
    raiz = ET.fromstring(contenido)
    nodos = [e for e in raiz.iter() if nombre_local(e.tag) in ("item", "entry")]
    ofertas = []
    for nodo in nodos:
        campos = {}
        comercio = None
        precio_comercio = None
        for hijo in nodo:
            nombre = nombre_local(hijo.tag)
            if nombre == "merchant":  # <pepper:merchant name="..." price="..."/>
                comercio = hijo.attrib.get("name")
                precio_comercio = hijo.attrib.get("price")
            elif nombre == "link" and not (hijo.text or "").strip():
                campos.setdefault("link", hijo.attrib.get("href", ""))
            else:
                campos.setdefault(nombre, (hijo.text or "").strip())
        enlace = campos.get("link", "")
        identificador = campos.get("guid") or campos.get("id") or enlace
        if not identificador:
            continue
        ofertas.append(
            {
                "id": identificador,
                "titulo": limpiar_html(campos.get("title", "")),
                "enlace": enlace,
                "descripcion": limpiar_html(
                    campos.get("description") or campos.get("summary") or campos.get("content") or ""
                ),
                "comercio": comercio,
                "precio_comercio": precio_comercio,
            }
        )
    return ofertas


# --------------------------------------------------------------------------
# Reglas
# --------------------------------------------------------------------------

def analizar(oferta, cfg):
    """
    Devuelve None si la oferta no interesa, o un diccionario con los motivos
    y la prioridad del aviso.
    """
    titulo = oferta["titulo"]
    texto = f"{titulo} {oferta['descripcion']}"
    texto_min = texto.lower()
    titulo_min = titulo.lower()

    if any(p.lower() in texto_min for p in cfg["palabras_ignorar"]):
        return None

    # Precio de la oferta: el que declara el feed o, si no, el primero del título.
    precio = None
    if oferta.get("precio_comercio"):
        encontrados = precios_en(oferta["precio_comercio"])
        precio = encontrados[0] if encontrados else a_numero(oferta["precio_comercio"])
    if precio is None:
        en_titulo = precios_en(titulo)
        precio = en_titulo[0] if en_titulo else None

    # Descuento: porcentaje escrito o calculado con un "precio anterior" explícito.
    descuento = None
    for patron in PORCENTAJE_RES:
        for m in patron.finditer(texto):
            valor = int(m.group(1))
            if 0 < valor <= 100:
                descuento = max(descuento or 0, valor)
    if precio is not None:
        for m in PRECIO_ANTERIOR_RE.finditer(texto):
            anterior = a_numero(m.group(1))
            if anterior and anterior > precio:
                calculado = round((1 - precio / anterior) * 100)
                descuento = max(descuento or 0, calculado)

    motivos = []
    prioridad = 3
    etiquetas = []

    frases = [p for p in cfg["palabras_error"] if p.lower() in texto_min]
    if frases:
        motivos.append(f"Menciona «{frases[0]}»")
        prioridad = 4
        etiquetas.append("rotating_light")

    if descuento is not None and descuento >= cfg["descuento_minimo_porcentaje"]:
        motivos.append(f"Descuento de {descuento} %")
        etiquetas.append("chart_with_downwards_trend")

    if (
        cfg.get("avisar_precio_casi_cero")
        and precio is not None
        and 0 <= precio <= cfg["precio_casi_cero_max_eur"]
        and not any(p.lower() in titulo_min for p in cfg["palabras_que_anulan_precio_cero"])
    ):
        motivos.append(f"Precio casi cero ({precio:g} €)")
        etiquetas.append("money_with_wings")

    if not motivos:
        return None
    return {"motivos": motivos, "prioridad": prioridad, "etiquetas": etiquetas,
            "precio": precio, "descuento": descuento}


# --------------------------------------------------------------------------
# Avisos (ntfy)
# --------------------------------------------------------------------------

def enviar_ntfy(topic, titulo, mensaje, enlace=None, prioridad=3, etiquetas=None, simulacro=False):
    """Envía una notificación. Devuelve True si salió bien."""
    carga = {
        "topic": topic,
        "title": titulo[:200],
        "message": mensaje[:1000],
        "priority": prioridad,
        "tags": etiquetas or [],
    }
    if enlace:
        carga["click"] = enlace
        carga["actions"] = [{"action": "view", "label": "Abrir oferta", "url": enlace}]

    if simulacro:
        print(f"  [SIMULACRO] aviso -> {json.dumps(carga, ensure_ascii=False)}")
        return True

    peticion = urllib.request.Request(
        NTFY_URL,
        data=json.dumps(carga).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=20) as respuesta:
            return 200 <= respuesta.status < 300
    except Exception as error:  # noqa: BLE001 - queremos que nunca rompa el bot
        print(f"  ! No se pudo enviar el aviso a ntfy: {error}")
        return False


def construir_aviso(oferta, resultado):
    comercio = f" · {oferta['comercio']}" if oferta.get("comercio") else ""
    lineas = [" · ".join(resultado["motivos"]) + comercio]
    if resultado["precio"] is not None:
        lineas.append(f"Precio detectado: {resultado['precio']:g} €")
    return oferta["titulo"] or "Oferta sin título", "\n".join(lineas)


# --------------------------------------------------------------------------
# Estado (memoria entre ejecuciones)
# --------------------------------------------------------------------------

def cargar_estado(ruta):
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("! state.json estaba dañado; se empieza de cero.")
    return {"inicializado": False, "vistas": [], "fallos_seguidos": 0, "fallo_avisado": False}


def guardar_estado(ruta, estado):
    ruta.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------

def obtener_ofertas(fuentes, desde_archivo):
    """Lee todas las fuentes. Devuelve (ofertas sin duplicados, feeds_ok, errores)."""
    ofertas, vistos, feeds_ok, errores = [], set(), 0, []
    for fuente in fuentes:
        try:
            contenido = Path(fuente).read_bytes() if desde_archivo else descargar(fuente)
            leidas = leer_feed(contenido)
            feeds_ok += 1
            print(f"✓ {fuente}: {len(leidas)} ofertas leídas")
            for o in leidas:
                if o["id"] not in vistos:
                    vistos.add(o["id"])
                    ofertas.append(o)
        except urllib.error.HTTPError as e:
            errores.append(f"{fuente} -> HTTP {e.code}")
            print(f"✗ {fuente}: HTTP {e.code}")
        except ET.ParseError:
            errores.append(f"{fuente} -> no es un feed XML válido (¿página de bloqueo?)")
            print(f"✗ {fuente}: la respuesta no es XML válido (¿página de bloqueo?)")
        except Exception as e:  # noqa: BLE001
            errores.append(f"{fuente} -> {type(e).__name__}: {e}")
            print(f"✗ {fuente}: {type(e).__name__}: {e}")
    return ofertas, feeds_ok, errores


def modo_diagnostico(ofertas, cfg):
    print("\n=== DIAGNÓSTICO: primeras ofertas del feed ===")
    for o in ofertas[:8]:
        resultado = analizar(o, cfg)
        print(f"\n- Título:      {o['titulo']}")
        print(f"  Enlace:      {o['enlace']}")
        print(f"  Comercio:    {o['comercio']}  |  Precio (feed): {o['precio_comercio']}")
        print(f"  Descripción: {o['descripcion'][:200]}")
        print(f"  Resultado:   {'AVISARÍA -> ' + '; '.join(resultado['motivos']) if resultado else 'no avisaría'}")
    total = sum(1 for o in ofertas if analizar(o, cfg))
    print(f"\nDe {len(ofertas)} ofertas leídas, {total} dispararían un aviso con las reglas actuales.")


def main():
    parser = argparse.ArgumentParser(description="Vigilante de errores de precio")
    parser.add_argument("--mode", choices=["run", "diagnose", "test"], default="run")
    parser.add_argument("--file", help="Leer un feed desde un archivo local (para pruebas)")
    parser.add_argument("--state", default=str(STATE_PATH), help="Ruta del archivo de estado")
    parser.add_argument("--dry-run", action="store_true", help="No enviar avisos reales: solo mostrarlos")
    args = parser.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    simulacro = args.dry_run

    if args.mode != "diagnose" and not topic and not simulacro:
        print("ERROR: falta el secreto NTFY_TOPIC (el nombre de tu tema de ntfy).")
        return 2

    # ---- Prueba de notificación
    if args.mode == "test":
        ok = enviar_ntfy(topic or "prueba", "✅ Prueba del vigilante de chollos",
                         "Si ves esto en el móvil, los avisos funcionan.",
                         prioridad=3, etiquetas=["white_check_mark"], simulacro=simulacro)
        print("Aviso de prueba enviado." if ok else "No se pudo enviar el aviso de prueba.")
        return 0 if ok else 1

    # ---- Lectura del feed
    fuentes = [args.file] if args.file else cfg["feeds"]
    ofertas, feeds_ok, errores = obtener_ofertas(fuentes, bool(args.file))

    if args.mode == "diagnose":
        if feeds_ok == 0:
            print("\nNingún feed respondió. Errores:\n  " + "\n  ".join(errores))
            return 0
        modo_diagnostico(ofertas, cfg)
        return 0

    # ---- Modo normal
    ruta_estado = Path(args.state)
    estado = cargar_estado(ruta_estado)

    if feeds_ok == 0:
        estado["fallos_seguidos"] = estado.get("fallos_seguidos", 0) + 1
        print(f"! Ningún feed respondió ({estado['fallos_seguidos']} ejecuciones seguidas).")
        if (estado["fallos_seguidos"] >= cfg["fallos_seguidos_antes_de_avisar"]
                and not estado.get("fallo_avisado")):
            enviado = enviar_ntfy(
                topic or "prueba",
                "⚠️ El vigilante no puede leer Chollometro",
                "Lleva varias ejecuciones seguidas sin leer el feed. Revisa la pestaña Actions de GitHub.\n"
                + "\n".join(errores[:3]),
                prioridad=4, etiquetas=["warning"], simulacro=simulacro)
            estado["fallo_avisado"] = bool(enviado)
        guardar_estado(ruta_estado, estado)
        return 0

    estado["fallos_seguidos"] = 0
    estado["fallo_avisado"] = False
    vistas = list(estado.get("vistas", []))
    conjunto_vistas = set(vistas)

    # Primera ejecución: se memoriza todo lo actual sin avisar (evita una avalancha).
    if not estado.get("inicializado"):
        for o in ofertas:
            if o["id"] not in conjunto_vistas:
                vistas.append(o["id"])
        estado["vistas"] = vistas[-cfg["max_ofertas_recordadas"]:]
        estado["inicializado"] = True
        enviar_ntfy(topic or "prueba", "✅ Vigilante de chollos activo",
                    f"He leído {len(ofertas)} ofertas y las doy por vistas. "
                    "Desde ahora te avisaré solo de las nuevas que cumplan tus reglas.",
                    prioridad=3, etiquetas=["white_check_mark"], simulacro=simulacro)
        guardar_estado(ruta_estado, estado)
        print(f"Primera ejecución: {len(ofertas)} ofertas memorizadas, sin avisos.")
        return 0

    nuevas = [o for o in ofertas if o["id"] not in conjunto_vistas]
    print(f"Ofertas nuevas desde la última ejecución: {len(nuevas)}")

    avisos = 0
    for oferta in reversed(nuevas):  # de la más antigua a la más reciente
        resultado = analizar(oferta, cfg)
        if resultado:
            if avisos >= MAX_AVISOS_POR_EJECUCION:
                print(f"  (límite de {MAX_AVISOS_POR_EJECUCION} avisos por ejecución alcanzado)")
                vistas.append(oferta["id"])
                continue
            titulo, mensaje = construir_aviso(oferta, resultado)
            enviado = enviar_ntfy(topic or "prueba", titulo, mensaje, enlace=oferta["enlace"],
                                  prioridad=resultado["prioridad"], etiquetas=resultado["etiquetas"],
                                  simulacro=simulacro)
            if not enviado:
                continue  # no se marca como vista: se reintentará en la próxima ejecución
            avisos += 1
            print(f"  → AVISO: {titulo} ({'; '.join(resultado['motivos'])})")
        vistas.append(oferta["id"])

    estado["vistas"] = vistas[-cfg["max_ofertas_recordadas"]:]
    guardar_estado(ruta_estado, estado)
    print(f"Avisos enviados: {avisos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
