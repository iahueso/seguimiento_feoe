"""
Revisa las prácticas de TODOS los alumnos del Cuaderno de seguimiento y envía
un mensaje de Telegram por alumno con:
  - la próxima tutoría (a partir de hoy): ítems con nota y observaciones
    (o "No hay datos" si está vacía),
  - el texto de su botón "Desarrollos asociados" (o "No hay texto").

SOLO LEE: nunca pulsa Guardar, Eliminar, Imprimir ni similares, y cancela
cualquier pregunta de confirmación que haga la web mientras revisa.

Uso:
  python revisar_tutorias.py                -> todos los alumnos
  python revisar_tutorias.py --alumno 3     -> solo el tercer alumno de la lista
  python revisar_tutorias.py --nombre gabor -> solo el alumno cuyo nombre contenga "gabor"
  python revisar_tutorias.py --solo-desarrollos  -> solo "Desarrollos asociados", con detalle
  python revisar_tutorias.py --todas        -> todas las tutorías de cada alumno, no solo la próxima
  python revisar_tutorias.py --fecha 10/10/2026  -> como si hoy fuera ese día (para probar)
  python revisar_tutorias.py --sin-enviar   -> no envía nada, solo lo muestra en pantalla
  python revisar_tutorias.py --oculto       -> sin ventana

Las capturas y datos.json de cada alumno quedan en la carpeta revision/NN/.
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
import notificar
from playwright.async_api import async_playwright, Page

logging.basicConfig(format="   · %(message)s", level=logging.INFO)
log = logging.getLogger("tutorias")

# --- Argumentos ---
args = sys.argv[1:]
VISIBLE = "--oculto" not in args
ALUMNO = None                        # None = todos
if "--alumno" in args:
    i = args.index("--alumno")
    ALUMNO = int(args[i + 1])
    del args[i:i + 2]
TODAS = "--todas" in args            # revisar todas, no solo la próxima
SIN_ENVIAR = "--sin-enviar" in args
SOLO_DES = "--solo-desarrollos" in args   # solo mirar "Desarrollos asociados" (con detalle)
NOMBRE = None                             # --nombre GABOR: elegir alumno por su nombre
if "--nombre" in args:
    i = args.index("--nombre")
    NOMBRE = args[i + 1]
    del args[i:i + 2]
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
# Texto del botón de cada alumno con sus desarrollos asociados.
DESARROLLOS = os.getenv("WEB_DESARROLLOS", "Desarrollos asociados").strip()
OUT = Path("revision")


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


# Enlace del menú que lleva a la lista de alumnos (se pulsa para volver a ella).
MENU_ALUMNOS = os.getenv("WEB_MENU_ALUMNOS", "Cuaderno de seguimiento").strip()


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def _names(rows: list) -> list[str]:
    return [next((c for c in r if c), "") for r in rows]


class Sesion:
    """Una sola sesión en la web para todos los alumnos."""

    def __init__(self, browser):
        self.browser = browser
        self.ctx = None
        self.page = None
        self.alumnos = None      # nombres de la lista de alumnos (para reconocerla)
        self.logins = 0

    async def login(self) -> None:
        if self.ctx:
            await self.ctx.close()
        self.ctx = await self.browser.new_context(locale="es-ES")
        self.page = await self.ctx.new_page()
        self.logins += 1
        clicks = await web._login(self.page)
        bad = [b for b, r in clicks.items() if r != "ok"]
        if bad:
            raise web.WebError(f"No he podido pulsar: {', '.join(bad)}. Prueba antes probar_login.py.")
        # A partir de aquí no se confirma nada: solo lectura (también en ventanas nuevas).
        self.page.remove_listener("dialog", web._accept_dialog)
        self.page.on("dialog", safe_dialog)
        self.ctx.on("page", lambda p: p.on("dialog", safe_dialog))

    async def _clickable_matches(self, text: str):
        out = []
        for box, el in await web._all_visible_matches(self.page, text):
            try:
                if await el.evaluate("e => !!e.closest('a, button, [onclick], input[type=button], input[type=submit]')"):
                    out.append((box, el))
            except Exception:
                pass
        return out

    async def _menu_to_students(self) -> bool:
        """Pulsa "Cuaderno de seguimiento" en el menú (desplegándolo si hace falta)."""
        m = await self._clickable_matches(MENU_ALUMNOS)
        if not m:
            return False
        if len(m) == 1:
            # Solo se ve la cabecera del menú: se despliega y se pulsa la opción de debajo
            head = m[0][0]
            await click_follow(self.page, m[0][1])
            m = [x for x in await self._clickable_matches(MENU_ALUMNOS) if not web._overlaps(x[0], head)]
            if not m:
                return True
        log.info("Vuelvo a la lista de alumnos por el menú")
        await click_follow(self.page, m[-1][1])     # la opción de más abajo (el submenú)
        return True

    async def _is_students_list(self):
        fr, headers, rows = await mark_list(self.page)
        ok = bool(rows) and (self.alumnos is None or _names(rows) == self.alumnos)
        return ok, fr, headers, rows

    async def ir_a_lista(self):
        """Deja la página en la lista de alumnos. Devuelve (frame, cabeceras, filas)."""
        if self.page is None:
            await self.login()
        else:
            for p in list(self.ctx.pages):            # cierra ventanas emergentes que queden
                if p is not self.page:
                    await p.close()
        ok, fr, headers, rows = await self._is_students_list()
        if not ok:
            if await self._menu_to_students():
                ok, fr, headers, rows = await self._is_students_list()
        if not ok:
            log.info("No consigo volver a la lista por el menú; inicio sesión de nuevo")
            await self.login()
            ok, fr, headers, rows = await self._is_students_list()
        if not ok:
            await self.page.screenshot(path=str(OUT / "error_lista_alumnos.png"), full_page=True)
            await show_buttons(self.page, "No encuentro la lista de alumnos (mira revision/error_lista_alumnos.png).")
            raise web.WebError("Sin lista de alumnos")
        if self.alumnos is None:
            self.alumnos = _names(rows)
        return fr, headers, rows

    async def close(self):
        if self.ctx:
            await self.ctx.close()


async def open_tutorias(ses: Sesion, idx: int, outdir: Path, first: bool = False):
    """Lista de alumnos → icono "Tutoria" del alumno nº idx (0 = primero) → su lista de tutorías.
    Devuelve (page, nombre, frame, cabeceras, filas_tutorias, nº_alumnos)."""
    fr, headers, rows = await ses.ir_a_lista()
    if first:
        print(f"\n👥 {len(rows)} alumno(s) en la lista. Columnas: {' | '.join(h for h in headers if h)}")
        for k, r in enumerate(rows, 1):
            print(f"     {k}. {next((c for c in r if c), '')}")
    if idx >= len(rows):
        raise web.WebError(f"Has pedido el alumno {idx + 1} pero solo hay {len(rows)}.")
    nombre = next((c for c in rows[idx] if c), f"Alumno {idx + 1}")
    res = await fr.evaluate(ICON_JS, [idx, ICONO_ALUMNO, COL_ALUMNO])
    if res.get("error"):
        for i in res.get("info", []):
            print(f"     - {i}")
        raise web.WebError(f"{res['error']}. Revisa WEB_COLUMNA_ALUMNO en .env.")
    if SOLO_DES:
        print(f"   🔎 Elementos pulsables en la fila de {nombre}:")
        for x in res.get("info", []):
            print(f"        - {x}")
    log.info("Pulsando el icono del alumno: %s", res["picked"])
    page = await click_follow(ses.page, fr.locator("[data-iw-click]").first)

    # Enlace/pestaña extra de tutorías, solo si se ha configurado
    if TUTORIAS:
        r = await web._click_if_visible(page, TUTORIAS, timeout=8000)
        if not r:
            await page.screenshot(path=str(outdir / "error_tutorias.png"), full_page=True)
            await show_buttons(page, f"No encuentro '{TUTORIAS}'.")
            raise web.WebError("Revisa WEB_TUTORIAS en .env.")
    await page.screenshot(path=str(outdir / "1_tutorias.png"), full_page=True)

    fr, theaders, trows = await mark_list(page)
    return page, nombre, fr, theaders, trows, len(rows)


def print_tutoria(n: int, label: str, forms: list, text: str) -> None:
    print(f"\n🗂  Tutoría {n}: {label}")
    campos = [c for f in forms for c in f["campos"]]
    if not campos:
        print("   (No hay campos de formulario; texto de la página:)")
        for l in page_lines(text)[:30]:
            print(f"     {l[:100]}")
        return
    for c in campos[:50]:
        aviso = "  (vacío)" if not c["valor"] else ""
        valor = c["valor"][:90] + ("…" if len(c["valor"]) > 90 else "")
        print(f"     {c['campo'] or '(sin nombre)'}: {valor}{aviso}")


def page_lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


async def find_button(page: Page, text: str, ms: int = 2500):
    for _ in range(max(1, ms // 500)):
        el = await web._find_visible(page, text)
        if el:
            return el
        await page.wait_for_timeout(500)
    return None


async def read_desarrollos(page: Page, outdir: Path):
    """Pulsa "Desarrollos asociados" si está en esta página y devuelve (texto, página_resultado).
    texto = None si el botón no está aquí; "" si está pero no hay texto."""
    el = await find_button(page, DESARROLLOS)
    if not el:
        if SOLO_DES:
            print(f"   🔎 No veo '{DESARROLLOS}' en: {await page.title()}  ({page.url})")
            botones = await web.list_clickables(page)
            if botones:
                print("      Botones/enlaces que hay en esa pantalla:")
                for b in botones:
                    print(f"        - {b}")
        return None, page
    forms_before, _, before = await read_page(page)
    before_vals = {(c["valor"] or "").strip() for f in forms_before for c in f["campos"]}
    log.info("Pulsando '%s'", DESARROLLOS)
    target = await click_follow(page, el)
    await target.screenshot(path=str(outdir / "desarrollos.png"), full_page=True)
    forms, tables, after = await read_page(target)
    if SOLO_DES:
        print(f"   🔎 Tras pulsar, estoy en: {await target.title()}  ({target.url})")
        print(f"      Captura: {(outdir / 'desarrollos.png').resolve()}")
        nuevos = [l for l in page_lines(after) if l not in set(page_lines(before))]
        print(f"      Texto nuevo en la pantalla ({len(nuevos)} líneas):")
        for l in nuevos[:40]:
            print(f"        | {l[:110]}")
        campos_txt = [c for f in forms for c in f["campos"] if c["tipo"] in ("textarea", "text")]
        if campos_txt:
            print("      Campos de texto:")
            for c in campos_txt:
                print(f"        - {c['campo'] or '(sin nombre)'}: {(c['valor'] or '(vacío)')[:110]}")

    # Solo lo que es NUEVO respecto a la página anterior (así no se cuelan menús ni cabeceras).
    # 1º) Campos de texto: si la página tiene un formulario, manda lo que haya en él.
    campos = [c for f in forms for c in f["campos"]
              if c["tipo"] in ("textarea", "text")
              and not NO_NOTA_VALOR_RE.search(c["valor"] or "")
              and not ((c["valor"] or "").strip() and ((c["valor"] or "").strip() in before_vals
                                                      or (c["valor"] or "").strip() in before))]
    if campos:
        llenos = [c for c in campos if (c["valor"] or "").strip() and not SIN_NOTA_RE.match(c["valor"].strip())]
        if len(llenos) == 1:
            return llenos[0]["valor"].strip(), target
        return "\n".join(f"{c['campo']}: {c['valor'].strip()}" if c["campo"] else c["valor"].strip()
                         for c in llenos), target

    # 2º) Tablas (sin las celdas que son solo etiquetas tipo "Descripción:")
    parts = []
    for t in tables:
        for row in t["filas"]:
            cells = [x for x in row if x and not x.endswith(":")]
            if cells and not all(x in before for x in cells):
                parts.append(" | ".join(cells))
    # 3º) Si no, el texto nuevo de la página (sin etiquetas ni botones de navegación)
    if not parts:
        prev = set(page_lines(before))
        ruido = re.compile(r"^(volver|guardar|aceptar|cancelar|cerrar|imprimir|salir)$", re.I)
        parts = [l for l in page_lines(after)
                 if l not in prev and DESARROLLOS.lower() not in l.lower()
                 and len(l) > 2 and not l.endswith(":") and not ruido.match(l)]
    return "\n".join(dict.fromkeys(parts)), target


async def process_student(ses: Sesion, idx: int, first: bool) -> dict:
    """Revisa un alumno. Devuelve un dict con todo lo encontrado."""
    outdir = OUT / f"{idx + 1:02d}"
    outdir.mkdir(parents=True, exist_ok=True)
    page, nombre, fr, theaders, trows, n_alumnos = await open_tutorias(ses, idx, outdir, first)
    res = {"n": idx + 1, "alumno": nombre, "n_alumnos": n_alumnos, "tutorias": [],
           "estado": "ok", "desarrollos": None}
    print(f"\n━━━━━━━━ 👤 {idx + 1}/{n_alumnos}: {nombre} ━━━━━━━━")
    try:
        async def back_to_list():
            """Vuelve a la lista de tutorías (atrás o, si no se puede, entrando de nuevo)."""
            nonlocal page, fr
            fr, _, rows_now = await mark_list(page)
            if rows_now == trows:
                return
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
                await web._settle(page)
            except Exception:
                pass
            fr, _, rows_now = await mark_list(page)
            if rows_now != trows:
                log.info("No puedo volver atrás; vuelvo a entrar en el alumno desde la lista")
                page, _, fr, _, _, _ = await open_tutorias(ses, idx, outdir)

        # 1) "Desarrollos asociados" en la página de tutorías del alumno
        texto, target = await read_desarrollos(page, outdir)
        if texto is not None:
            res["desarrollos"] = texto
            if target is not page:
                await target.close()
            if trows:
                await back_to_list()

        # 2) Tutoría(s) a revisar
        if SOLO_DES and res["desarrollos"] is not None:
            pass        # ya está: en este modo no hace falta mirar las tutorías
        elif not trows:
            res["estado"] = "sin_tutorias"
            print("   No hay tutorías (o no encuentro la lista).")
        else:
            print(f"📚 {len(trows)} tutoría(s):")
            for i, r in enumerate(trows, 1):
                print(f"     {i}. {' | '.join(c for c in r if c)}")
            if TODAS:
                indices = list(range(len(trows)))
            else:
                i_next, fecha = next_tutoria(trows, HOY)
                if i_next is None and SOLO_DES:
                    indices = [len(trows) - 1]   # solo para buscar el botón dentro de una tutoría
                elif i_next is None:
                    res["estado"] = "sin_fechas" if fecha == "sin_fechas" else "sin_proxima"
                    indices = []
                else:
                    print(f"📅 Reviso la más próxima: nº {i_next + 1} ({fecha:%d/%m/%Y})")
                    indices = [i_next]

            for k, i in enumerate(indices):
                if k > 0:
                    await back_to_list()
                label = " | ".join(c for c in trows[i] if c)[:100]
                ic = await fr.evaluate(ICON_JS, [i, ICONO_TUTORIA, COL_TUTORIA])
                if ic.get("error"):
                    print(f"⚠️  Tutoría {i + 1}: {ic['error']}")
                    for x in ic.get("info", []):
                        print(f"     - {x}")
                    res["tutorias"].append({"tutoria": i + 1, "fila": label, "error": ic["error"]})
                    continue
                log.info("Abriendo tutoría %d: %s", i + 1, ic["picked"])
                target = await click_follow(page, fr.locator("[data-iw-click]").first)
                await target.screenshot(path=str(outdir / f"tutoria_{i + 1}.png"), full_page=True)
                forms, tables, text = await read_page(target)
                if not SOLO_DES:
                    print_tutoria(i + 1, label, forms, text)
                res["tutorias"].append({"tutoria": i + 1, "fila": label, "url": target.url,
                                        "formularios": forms, "tablas": tables, "texto": text})
                # "Desarrollos asociados" en la propia tutoría, si no estaba en la lista
                if res["desarrollos"] is None:
                    texto, t2 = await read_desarrollos(target, outdir)
                    if texto is not None:
                        res["desarrollos"] = texto
                        if t2 is not target and t2 is not page:
                            await t2.close()
                if target is not page:
                    await target.close()
    finally:
        pass

    (outdir / "datos.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# Qué campos son observaciones y cuáles no son "ítems con nota" (datos generales).
OBS_RE = re.compile(os.getenv("WEB_CAMPOS_OBSERVACIONES", r"observ|coment|valoraci[oó]n general"), re.I)
NO_NOTA_RE = re.compile(os.getenv(
    "WEB_CAMPOS_EXCLUIR",
    r"fecha|hora|tipo|centro|curso|ciclo|nombre|apellido|documento|dni|nif|buscar|periodo|empresa|"
    r"^tutor$|lugar|modalidad"), re.I)
SIN_NOTA_RE = re.compile(r"^(seleccione.*|elija.*|-+|ninguna?|no marcado|sin calificar|0?)$", re.I)
# Valores que son datos de cabecera (curso, centro…) y no notas
NO_NOTA_VALOR_RE = re.compile(r"^\d{4}/\d{2,4}$|^(IES|CPIFP|CIFP|CPI|CPEPA|COLEGIO|CENTRO)\b", re.I)


def split_fields(campos: list) -> tuple[list, list]:
    """Separa los campos en (ítems con nota, observaciones)."""
    notas, obs, vistos = [], [], set()
    for c in campos:
        nombre_campo, valor = c["campo"] or "", (c["valor"] or "").strip()
        if (nombre_campo, valor) in vistos:
            continue
        vistos.add((nombre_campo, valor))
        if OBS_RE.search(nombre_campo):
            obs.append((nombre_campo, valor))
        elif valor and not SIN_NOTA_RE.match(valor) and not NO_NOTA_RE.search(nombre_campo) \
                and not NO_NOTA_VALOR_RE.search(valor) \
                and c["tipo"] not in ("textarea", "date"):
            notas.append((nombre_campo or "(sin nombre)", "✔" if valor == "marcado" else valor))
    return notas, obs


def build_message(r: dict) -> str:
    """Mensaje de Telegram de UN alumno."""
    lines = [f"👤 {r['alumno']}"]
    if r.get("error"):
        lines.append(f"❓ No he podido revisarlo: {r['error']}")
        return "\n".join(lines)
    if SOLO_DES:
        lines += ["", "📎 DESARROLLOS",
                  "(no he encontrado el botón)" if r["desarrollos"] is None else (r["desarrollos"] or "No hay texto.")]
        return "\n".join(lines)

    estados = {
        "sin_tutorias": "📭 No hay tutorías.",
        "sin_proxima": f"📭 No hay ninguna tutoría a partir del {HOY:%d/%m/%Y}.",
        "sin_fechas": "📭 No he encontrado fechas en sus tutorías.",
    }
    if r["estado"] in estados:
        lines.append(estados[r["estado"]])

    for t in r["tutorias"]:
        lines += ["", f"📅 {'Tutoría' if TODAS else 'Próxima tutoría'}: {t['fila'][:60]}"]
        if t.get("error"):
            lines.append(f"❓ No he podido abrirla: {t['error']}")
            continue
        notas, obs = split_fields([c for f in t.get("formularios", []) for c in f["campos"]])
        if not notas and not any(v for _, v in obs):
            lines.append("📭 No hay datos.")
            continue
        if notas:
            lines.append("📝 Notas:")
            lines += [f"• {k}: {v}" for k, v in notas]
        lines += ["", "💬 OBSERVACIONES"]
        if not obs:
            lines.append("(no he encontrado campos de observaciones)")
        for k, v in obs:
            if v:
                lines += [f"▸ {k}:", v, ""]
        sin_obs = [k for k, v in obs if not v]
        if sin_obs:
            lines.append("Sin rellenar: " + ", ".join(sin_obs))
        while lines and lines[-1] == "":
            lines.pop()

    lines += ["", "📎 DESARROLLOS"]
    if r["desarrollos"] is None:
        lines.append("(no he encontrado el botón)")
    else:
        lines.append(r["desarrollos"] or "No hay texto.")
    return "\n".join(lines)


def alumno_nombre(ses: "Sesion", idx: int) -> str:
    if ses.alumnos and idx < len(ses.alumnos):
        return ses.alumnos[idx]
    return f"Alumno {idx + 1}"


async def main() -> int:
    if not web.configured():
        print("Falta rellenar en .env: WEB_LOGIN_URL, WEB_USER y WEB_PASSWORD")
        return 1
    OUT.mkdir(exist_ok=True)
    if not SIN_ENVIAR and not notificar.channels():
        print("⚠️  No hay Telegram configurado en .env: solo lo mostraré en pantalla.")

    resumen = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not VISIBLE, slow_mo=100 if VISIBLE else 0)
        ses = Sesion(browser)
        alumno = ALUMNO
        if NOMBRE:
            try:
                await ses.ir_a_lista()
            except web.WebError as e:
                print(f"\n❌ {e}")
                await browser.close()
                return 1
            buscados = [k for k, n in enumerate(ses.alumnos) if _norm(NOMBRE) in _norm(n)]
            if len(buscados) != 1:
                print(f"\n❌ '{NOMBRE}' coincide con {len(buscados)} alumnos. Lista:")
                for k, n in enumerate(ses.alumnos, 1):
                    print(f"     {k}. {n}")
                await browser.close()
                return 1
            alumno = buscados[0] + 1
            print(f"\n🎯 Alumno elegido: {ses.alumnos[buscados[0]]} (nº {alumno})")
        idx = (alumno - 1) if alumno else 0
        total = None
        first = True
        while total is None or idx < total:
            try:
                r = await process_student(ses, idx, first)
                total = idx + 1 if alumno else r["n_alumnos"]
            except web.WebError as e:
                if first:          # si falla el primero, es un problema general: parar
                    print(f"\n❌ {e}")
                    await browser.close()
                    return 1
                r = {"n": idx + 1, "alumno": alumno_nombre(ses, idx), "error": str(e)}
            except Exception as e:
                log.exception("Error inesperado")
                if total is None:
                    await browser.close()
                    return 1
                r = {"n": idx + 1, "alumno": alumno_nombre(ses, idx), "error": str(e)}
            first = False

            msg = build_message(r)
            print("\n----- Mensaje -----\n" + msg + "\n-------------------")
            if not SIN_ENVIAR:
                for line in notificar.send(msg):
                    print(line)
                await asyncio.sleep(1)   # no saturar Telegram
            resumen.append(r)
            idx += 1
        log.info("Inicios de sesión en la web: %d", ses.logins)
        await ses.close()
        await browser.close()

    print(f"\n✅ Revisados {len(resumen)} alumno(s). Capturas y datos en: {OUT.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
