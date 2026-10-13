"""
Entra en un alumno del Cuaderno de seguimiento (icono de la columna "Tutoria"),
abre cada una de sus tutorías y revisa el contenido del formulario.

SOLO LEE: nunca pulsa Guardar, Eliminar, Imprimir ni similares, y cancela
cualquier pregunta de confirmación que haga la web mientras revisa.

Por defecto revisa SOLO la tutoría más próxima a partir de hoy (incluida la de hoy).

Uso:
  python revisar_tutorias.py                -> primer alumno, navegador a la vista
  python revisar_tutorias.py --todas        -> revisa todas sus tutorías
  python revisar_tutorias.py --fecha 10/10/2026  -> como si hoy fuera ese día (para probar)
  python revisar_tutorias.py --alumno 3     -> el tercer alumno de la lista
  python revisar_tutorias.py --oculto       -> sin ventana
  python revisar_tutorias.py "comprueba que todas tengan observaciones"
                                            -> instrucciones extra para la revisión de la IA

Al terminar envía un resumen por Telegram y/o WhatsApp (ver notificar.py), con
revisión de la IA si hay ANTHROPIC_API_KEY en .env. Las capturas y datos.json
quedan en la carpeta revision_alumnoN/.
"""
import os
import re
import sys
import json
from datetime import date, datetime
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import web
import informe
import notificar
from playwright.async_api import async_playwright, Page

logging.basicConfig(format="   · %(message)s", level=logging.INFO)
log = logging.getLogger("tutorias")

# --- Argumentos ---
args = sys.argv[1:]
VISIBLE = "--oculto" not in args
ALUMNO = 1
if "--alumno" in args:
    i = args.index("--alumno")
    ALUMNO = int(args[i + 1])
    del args[i:i + 2]
TODAS = "--todas" in args            # revisar todas, no solo la próxima
HOY = date.today()
if "--fecha" in args:                # para probar: --fecha 10/10/2026
    i = args.index("--fecha")
    HOY = datetime.strptime(args[i + 1], "%d/%m/%Y").date()
    del args[i:i + 2]
INSTRUCCIONES = " ".join(a for a in args if not a.startswith("--"))

FECHA_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})\b")


def row_date(cells: list[str]) -> date | None:
    """Primera fecha dd/mm/aaaa que aparezca en la fila."""
    for c in cells:
        m = FECHA_RE.search(c)
        if m:
            d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return date(y + 2000 if y < 100 else y, mth, d)
            except ValueError:
                continue
    return None


def next_tutoria(rows: list[list[str]], today: date):
    """Índice y fecha de la tutoría más próxima con fecha >= hoy."""
    fechas = [(i, row_date(r)) for i, r in enumerate(rows)]
    futuras = [(i, f) for i, f in fechas if f and f >= today]
    if not any(f for _, f in fechas):
        return None, "sin_fechas"
    if not futuras:
        return None, None
    return min(futuras, key=lambda t: t[1])

# --- Configuración (.env) ---
# Columna de la lista de alumnos cuyo icono hay que pulsar (texto de la cabecera).
COL_ALUMNO = os.getenv("WEB_COLUMNA_ALUMNO", "Tutoria").strip()
# Columna de la lista de tutorías cuyo icono abre cada una (vacío = se busca el icono de editar).
COL_TUTORIA = os.getenv("WEB_COLUMNA_TUTORIA", "").strip()
# Si tras el icono del alumno hay que pulsar además un enlace/pestaña (vacío = no hace falta).
TUTORIAS = os.getenv("WEB_TUTORIAS", "").strip()
# Selectores CSS opcionales del icono dentro de la fila, si la detección falla.
ICONO_ALUMNO = os.getenv("WEB_ICONO_EDITAR", "").strip()
ICONO_TUTORIA = os.getenv("WEB_ICONO_TUTORIA", "").strip()
OUT = Path(f"revision_alumno{ALUMNO}")


# Marca la tabla "principal" de la página: la que tiene más filas de datos con al menos
# 3 celdas y algún elemento pulsable. Devuelve sus cabeceras y el texto de las filas.
MARK_JS = r"""
(minCells) => {
  for (const a of ['data-iw-main', 'data-iw-row', 'data-iw-head', 'data-iw-click'])
    document.querySelectorAll(`[${a}]`).forEach(e => e.removeAttribute(a));
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const clickable = 'a, button, img, input[type=image], input[type=button], [onclick]';
  let best = null, bestRows = [], bestHead = null;
  for (const t of document.querySelectorAll('table')) {
    if (!vis(t) || t.querySelector('table')) continue;
    const all = [...t.rows].filter(vis);
    // Cabecera: fila con <th>, o la primera fila si no tiene números (las de datos sí: fechas…)
    let head = all.find(r => r.querySelector('th')) || null;
    if (!head && all.length > 1 && !/\d/.test(all[0].innerText)) head = all[0];
    const rows = all.filter(r => r !== head && !r.querySelector('th')
                              && r.cells.length >= minCells && r.querySelector(clickable));
    if (rows.length > bestRows.length) { best = t; bestRows = rows; bestHead = head; }
  }
  if (!best) return null;
  best.setAttribute('data-iw-main', '1');
  if (bestHead) bestHead.setAttribute('data-iw-head', '1');
  bestRows.forEach((r, i) => r.setAttribute('data-iw-row', String(i)));
  return {
    headers: bestHead ? [...bestHead.cells].map(c => clean(c.innerText)) : [],
    rows: bestRows.map(r => [...r.cells].map(c => clean(c.innerText))),
  };
}
"""

# Elige el icono a pulsar en una fila: el de la columna indicada, o el de "editar".
# Nunca elige borrar/eliminar/imprimir/PDF ni casillas.
ICON_JS = r"""
([idx, sel, col]) => {
  document.querySelectorAll('[data-iw-click]').forEach(e => e.removeAttribute('data-iw-click'));
  const row = document.querySelector(`[data-iw-main] tr[data-iw-row="${idx}"]`);
  if (!row) return { error: 'No encuentro la fila' };
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const norm = s => (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').trim();
  const desc = e => [e, ...e.querySelectorAll('img, input[type=image]')].map(x =>
      [x.title, x.alt, x.getAttribute('src'), x.getAttribute('onclick'), x.getAttribute('href'),
       x.getAttribute('aria-label'), (x.tagName === 'A' || x.tagName === 'BUTTON') ? x.innerText : '']
      .filter(Boolean).join(' ')).join(' ').replace(/\s+/g, ' ').trim();
  const DANGER = /borrar|eliminar|baja|delete|remove|anular|papelera|trash|supr|cancelar|imprimir|print|pdf/i;
  const HINT = /editar|modificar|edit|l[aá]piz|pencil|consultar|detalle|seguim|tutor|\bver\b/i;

  // Restringe la búsqueda a la celda de la columna indicada
  let scope = row, colInfo = '';
  if (col) {
    const head = document.querySelector('[data-iw-main] tr[data-iw-head]');
    const headers = head ? [...head.cells].map(c => c.innerText.replace(/\s+/g, ' ').trim()) : [];
    const ci = headers.findIndex(h => norm(h).includes(norm(col)));
    if (ci < 0 || !row.cells[ci])
      return { error: `No encuentro la columna '${col}'`, info: headers.map(h => `columna: ${h}`) };
    scope = row.cells[ci];
    colInfo = ` (columna '${headers[ci]}')`;
  }

  let cands = [...scope.querySelectorAll(sel ||
      'a, button, input[type=image], input[type=button], img[onclick], [onclick]')]
      .filter(c => vis(c) && c.type !== 'checkbox' && c.type !== 'radio');
  if (!cands.length && !sel) cands = [...scope.querySelectorAll('img')].filter(vis);  // la celda tiene el onclick
  if (!sel) cands = cands.filter(c => !cands.some(o => o !== c && c.contains(o)));   // el más interior
  const items = cands.map(c => [c, desc(c)]);
  let pick;
  if (sel) pick = items[0];
  else {
    const safe = items.filter(([c, d]) => !DANGER.test(d));
    pick = safe.find(([c, d]) => HINT.test(d))
        || safe.find(([c]) => c.tagName === 'IMG' || c.type === 'image' || c.querySelector('img'))
        || safe[0];
  }
  const info = items.map(([c, d]) => `<${c.tagName.toLowerCase()}> ${d.slice(0, 120)}`);
  if (!pick) return { error: `No encuentro ningún icono seguro en la fila${colInfo}`, info };
  pick[0].setAttribute('data-iw-click', '1');
  return { picked: (pick[1] || '(icono)').slice(0, 120) + colInfo, info };
}
"""


async def safe_dialog(dialog) -> None:
    """Mientras se revisa: acepta avisos informativos, cancela cualquier pregunta."""
    if dialog.type in ("alert", "beforeunload"):
        log.info("Aviso de la web: %s", dialog.message)
        await dialog.accept()
    else:
        log.info("Pregunta de la web CANCELADA por seguridad: %s", dialog.message)
        await dialog.dismiss()


async def mark_list(page: Page):
    """Devuelve (frame, cabeceras, filas) de la tabla principal de la página."""
    best = (None, [], [])
    for fr in page.frames:
        try:
            res = await fr.evaluate(MARK_JS, 3)
        except Exception:
            continue
        if res and len(res["rows"]) > len(best[2]):
            best = (fr, res["headers"], res["rows"])
    return best


async def click_follow(page: Page, target) -> Page:
    """Pulsa y devuelve la página donde aparece el resultado (la misma o una ventana nueva)."""
    ctx = page.context
    n_pages = len(ctx.pages)
    before = await web._fingerprint(page)
    try:
        await target.scroll_into_view_if_needed(timeout=3000)
        await target.click(timeout=5000)
    except Exception:
        await target.click(force=True, timeout=5000)
    for _ in range(40):
        await page.wait_for_timeout(250)
        if len(ctx.pages) > n_pages:
            new = ctx.pages[-1]
            log.info("Se ha abierto en una ventana nueva")
            try:
                await new.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await web._settle(new)
            return new
        try:
            if await web._fingerprint(page) != before:
                break
        except Exception:
            break
    await web._settle(page)
    return page


async def read_page(p: Page) -> tuple[list, list, str]:
    forms, text = [], ""
    for fr in p.frames:
        try:
            forms += await fr.evaluate(web.EXTRACT_JS)
            text += await fr.inner_text("body") + "\n"
        except Exception:
            pass
    return forms, await web.extract_tables(p), text


async def show_buttons(p: Page, why: str) -> None:
    print(f"\n❌ {why}")
    botones = await web.list_clickables(p)
    if botones:
        print("   Botones/enlaces que veo en la pantalla:")
        for b in botones:
            print(f"     - {b}")


async def open_tutorias(browser, first: bool):
    """Login → Cuaderno → icono "Tutoria" del alumno → lista de tutorías."""
    ctx = await browser.new_context(locale="es-ES")
    page = await ctx.new_page()

    clicks = await web._login(page)
    bad = [b for b, r in clicks.items() if r != "ok"]
    if bad:
        raise web.WebError(f"No he podido pulsar: {', '.join(bad)}. Prueba antes probar_login.py.")
    # A partir de aquí no se confirma nada: solo lectura (también en ventanas nuevas).
    page.remove_listener("dialog", web._accept_dialog)
    page.on("dialog", safe_dialog)
    ctx.on("page", lambda p: p.on("dialog", safe_dialog))

    # 1) Lista de alumnos → icono de la columna "Tutoria" del alumno elegido
    fr, headers, rows = await mark_list(page)
    if not rows:
        await page.screenshot(path=str(OUT / "error_lista_alumnos.png"), full_page=True)
        await show_buttons(page, "No encuentro la lista de alumnos (mira error_lista_alumnos.png).")
        raise web.WebError("Sin lista de alumnos")
    if ALUMNO > len(rows):
        raise web.WebError(f"Has pedido el alumno {ALUMNO} pero solo hay {len(rows)}.")
    nombre = next((c for c in rows[ALUMNO - 1] if c), f"Alumno {ALUMNO}")
    res = await fr.evaluate(ICON_JS, [ALUMNO - 1, ICONO_ALUMNO, COL_ALUMNO])
    if first:
        print(f"\n👥 {len(rows)} alumno(s) en la lista. Columnas: {' | '.join(h for h in headers if h)}")
        print(f"👤 Alumno {ALUMNO}: {' | '.join(c for c in rows[ALUMNO - 1] if c)}")
    if res.get("error"):
        for i in res.get("info", []):
            print(f"     - {i}")
        raise web.WebError(f"{res['error']}. Revisa WEB_COLUMNA_ALUMNO en .env.")
    log.info("Pulsando el icono del alumno: %s", res["picked"])
    page = await click_follow(page, fr.locator("[data-iw-click]").first)

    # 2) Enlace/pestaña extra de tutorías, solo si se ha configurado
    if TUTORIAS:
        r = await web._click_if_visible(page, TUTORIAS, timeout=8000)
        if not r:
            await page.screenshot(path=str(OUT / "error_tutorias.png"), full_page=True)
            await show_buttons(page, f"No encuentro '{TUTORIAS}' (mira error_tutorias.png).")
            raise web.WebError("Revisa WEB_TUTORIAS en .env.")
    if first:
        await page.screenshot(path=str(OUT / "1_tutorias.png"), full_page=True)

    # 3) Lista de tutorías
    fr, theaders, trows = await mark_list(page)
    return ctx, page, nombre, fr, theaders, trows


def print_tutoria(n: int, label: str, forms: list, text: str) -> None:
    print(f"\n🗂  Tutoría {n}: {label}")
    campos = [c for f in forms for c in f["campos"]]
    if not campos:
        print("   (No hay campos de formulario; texto de la página:)")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for l in lines[:30]:
            print(f"     {l[:100]}")
        return
    for c in campos[:50]:
        aviso = ""
        if c["obligatorio"] and not c["valor"]:
            aviso = "  ⚠ obligatorio vacío"
        elif not c["valor"]:
            aviso = "  (vacío)"
        valor = c["valor"][:90] + ("…" if len(c["valor"]) > 90 else "")
        print(f"     {c['campo'] or '(sin nombre)'}: {valor}{aviso}")


async def main() -> int:
    if not web.configured():
        print("Falta rellenar en .env: WEB_LOGIN_URL, WEB_USER y WEB_PASSWORD")
        return 1
    OUT.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not VISIBLE, slow_mo=150 if VISIBLE else 0)
        try:
            ctx, page, nombre, fr, theaders, trows = await open_tutorias(browser, first=True)
        except web.WebError as e:
            print(f"\n❌ {e}")
            await browser.close()
            return 1

        if not trows:
            await page.screenshot(path=str(OUT / "error_lista_tutorias.png"), full_page=True)
            print(f"\n❌ He entrado en {nombre}, pero no encuentro la lista de tutorías con iconos.")
            print("   Mira 1_tutorias.png y pásamela para ajustarlo. Texto de la página:")
            _, _, text = await read_page(page)
            for l in [l.strip() for l in text.splitlines() if l.strip()][:40]:
                print(f"     {l[:100]}")
            if VISIBLE:
                input("\nPulsa Enter para cerrar el navegador…")
            await browser.close()
            return 1

        n = len(trows)
        print(f"\n📚 {n} tutoría(s) de {nombre}:   [{' | '.join(h for h in theaders if h)}]")
        for i, r in enumerate(trows, 1):
            print(f"     {i}. {' | '.join(c for c in r if c)}")

        # ¿Cuáles revisar? Por defecto, solo la más próxima a partir de hoy.
        if TODAS:
            indices = list(range(n))
        else:
            idx, fecha = next_tutoria(trows, HOY)
            if idx is None:
                await browser.close()
                msg = (f"📋 {nombre}\nNo hay ninguna tutoría a partir del {HOY:%d/%m/%Y}."
                       if fecha != "sin_fechas" else
                       f"📋 {nombre}\nNo he encontrado fechas en la lista de tutorías.")
                print("\n" + msg)
                for line in notificar.send(msg):
                    print(line)
                return 0
            print(f"\n📅 Hoy es {HOY:%d/%m/%Y}: reviso solo la más próxima, la nº {idx + 1} ({fecha:%d/%m/%Y}).")
            indices = [idx]

        resultados = []
        for k, i in enumerate(indices):
            if k > 0:
                # Volver a la lista de tutorías
                fr, _, rows_now = await mark_list(page)
                if rows_now != trows:
                    try:
                        await page.go_back(wait_until="domcontentloaded", timeout=15000)
                        await web._settle(page)
                    except Exception:
                        pass
                    fr, _, rows_now = await mark_list(page)
                if rows_now != trows:
                    log.info("No puedo volver atrás; vuelvo a entrar desde el principio")
                    await ctx.close()
                    ctx, page, _, fr, _, rows_now = await open_tutorias(browser, first=False)

            label = " | ".join(c for c in trows[i] if c)[:100]
            res = await fr.evaluate(ICON_JS, [i, ICONO_TUTORIA, COL_TUTORIA])
            if res.get("error"):
                print(f"\n⚠️  Tutoría {i + 1}: {res['error']}. Elementos de la fila:")
                for x in res.get("info", []):
                    print(f"     - {x}")
                resultados.append({"tutoria": i + 1, "fila": label, "error": res["error"]})
                continue
            log.info("Abriendo tutoría %d: %s", i + 1, res["picked"])
            target = await click_follow(page, fr.locator("[data-iw-click]").first)
            await target.screenshot(path=str(OUT / f"tutoria_{i + 1}.png"), full_page=True)
            forms, tables, text = await read_page(target)
            print_tutoria(i + 1, label, forms, text)
            resultados.append({"tutoria": i + 1, "fila": label, "url": target.url,
                               "formularios": forms, "tablas": tables, "texto": text})
            if target is not page:
                await target.close()

        if VISIBLE:
            input("\nPulsa Enter para cerrar el navegador y enviar el mensaje…")
        await browser.close()

    # --- Guardar datos y enviar el mensaje ---
    (OUT / "datos.json").write_text(json.dumps(
        {"alumno": nombre, "tutorias": resultados}, ensure_ascii=False, indent=1), encoding="utf-8")

    mensaje = build_message(nombre, resultados)
    review = ai_summary(nombre, resultados)
    if review:
        mensaje += "\n\n🤖 Revisión de la IA:\n" + review

    print(f"\n✅ Revisadas {len(resultados)} tutoría(s) de {nombre}.")
    print("\n----- Mensaje -----\n" + mensaje + "\n-------------------")
    for line in notificar.send(mensaje):
        print(line)
    return 0


def build_message(nombre: str, resultados: list) -> str:
    """Resumen corto para el móvil: qué tutorías tienen campos vacíos."""
    titulo = f"Tutorías de {nombre} ({len(resultados)})" if TODAS else f"Próxima tutoría de {nombre}"
    lines = [f"📋 {titulo}", ""]
    con_avisos = 0
    for r in resultados:
        cab = f"Tutoría {r['tutoria']} · {r['fila'][:50]}"
        if r.get("error"):
            lines.append(f"❓ {cab}\n   No he podido abrirla: {r['error']}")
            con_avisos += 1
            continue
        campos = [c for f in r.get("formularios", []) for c in f["campos"]]
        oblig = [c["campo"] or "(sin nombre)" for c in campos if c["obligatorio"] and not c["valor"]]
        vacios = [c["campo"] or "(sin nombre)" for c in campos if not c["obligatorio"] and not c["valor"]]
        if not campos:
            lines.append(f"❓ {cab}\n   No he encontrado campos de formulario")
            con_avisos += 1
        elif oblig or vacios:
            con_avisos += 1
            lines.append(f"⚠️ {cab}")
            if oblig:
                lines.append(f"   Obligatorios vacíos: {', '.join(oblig)}")
            if vacios:
                lines.append(f"   Vacíos: {', '.join(vacios)}")
        else:
            lines.append(f"✅ {cab}")
    lines.insert(1, "Todo completo 👍" if not con_avisos else f"{con_avisos} con avisos")
    return "\n".join(lines)


def ai_summary(nombre: str, resultados: list) -> str | None:
    """Revisión breve de la IA (solo si hay ANTHROPIC_API_KEY en .env)."""
    if not informe.ANTHROPIC_API_KEY:
        return None
    print("\nLa IA está revisando las tutorías…")
    datos = [{"tutoria": r["tutoria"], "fila": r["fila"],
              "campos": [{k: c[k] for k in ("campo", "valor", "obligatorio")}
                         for f in r.get("formularios", []) for c in f["campos"]]}
             for r in resultados]
    try:
        from anthropic import Anthropic
        resp = Anthropic(api_key=informe.ANTHROPIC_API_KEY).messages.create(
            model=informe.MODEL, max_tokens=800,
            system=("Revisas tutorías del cuaderno de seguimiento de FCT. Escribe en español un "
                    "resumen MUY breve para un mensaje de móvil: texto plano, sin Markdown, sin "
                    "tablas, máximo 8 líneas. Señala solo problemas concretos (campos vacíos, "
                    "fechas u horas incoherentes, observaciones ausentes o pobres) indicando el "
                    "nº de tutoría. Si todo está bien, dilo en una línea. No inventes datos."),
            messages=[{"role": "user", "content":
                       f"Alumno: {nombre}\n{INSTRUCCIONES}\n\n" + json.dumps(datos, ensure_ascii=False)}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        print(f"AVISO: la revisión de la IA ha fallado ({e}).")
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
