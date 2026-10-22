"""
Acceso a la web: inicia sesión con tus credenciales y extrae los formularios
de una página para que la IA los revise.

Todo se configura en .env (ver .env.example).
"""
import os
import re
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Page, Locator, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

LOGIN_URL = os.getenv("WEB_LOGIN_URL", "")
FORMS_URL = os.getenv("WEB_FORMS_URL", "")
WEB_USER = os.getenv("WEB_USER", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

# Selectores CSS opcionales. Si se dejan vacíos, se intentan detectar solos.
USER_SELECTOR = os.getenv("WEB_USER_SELECTOR", "")
PASS_SELECTOR = os.getenv("WEB_PASS_SELECTOR", "")
SUBMIT_SELECTOR = os.getenv("WEB_SUBMIT_SELECTOR", "")

# Botones a pulsar después de iniciar sesión, en orden, separados por comas
# (texto del botón, p. ej. "Continuar", o selector CSS, p. ej. "#btnContinuar").
CLICK_AFTER = [x.strip() for x in os.getenv("WEB_CLICK_DESPUES", "Continuar").split(",") if x.strip()]
# Botones a pulsar antes de ver el formulario de login (normalmente ninguno).
CLICK_BEFORE = [x.strip() for x in os.getenv("WEB_CLICK_ANTES", "").split(",") if x.strip()]
# Botones típicos para pasar del usuario a la contraseña en logins de dos pasos.
NEXT_BUTTONS = ["Continuar", "Siguiente", "Next", "Continue", "Acceder", "Entrar"]

# Opcional: centro y curso a elegir en la pantalla tras el login (texto de la opción,
# basta con una parte, p. ej. "SANTIAGO HERN" o "2026/27"). Vacío = el que venga elegido.
WEB_CENTRO = os.getenv("WEB_CENTRO", "").strip()
WEB_CURSO = os.getenv("WEB_CURSO", "").strip()

HEADLESS = os.getenv("WEB_HEADLESS", "true").lower() != "false"
MAX_PAGE_TEXT = int(os.getenv("WEB_MAX_TEXT", "30000"))

# Guarda las cookies para no tener que iniciar sesión cada vez.
SESSION_FILE = Path(os.getenv("WEB_SESSION_FILE", "session.json"))


class WebError(Exception):
    """Error comprensible para mostrar al usuario."""


def configured() -> bool:
    return all([LOGIN_URL, WEB_USER, WEB_PASSWORD])


def _forms_url_set() -> bool:
    return bool(FORMS_URL) and "ejemplo.com" not in FORMS_URL


async def _looks_logged_out(page: Page) -> bool:
    """Si la página tiene un campo de contraseña visible, seguimos sin sesión."""
    pw = page.locator("input[type=password]")
    return await pw.count() > 0 and await pw.first.is_visible()


def _is_css(what: str) -> bool:
    return what[:1] in "#.[" or what.startswith(("button", "input", "a[", "div", "span", "css="))


def _candidates(frame, what: str) -> list[Locator]:
    """Formas de encontrar el botón, de la más precisa a la más flexible."""
    if _is_css(what):
        return [frame.locator(what.removeprefix("css="))]
    exact = re.compile(rf"^\W*{re.escape(what)}\W*$", re.I)   # admite "Continuar →", "» Continuar"
    contains = re.compile(re.escape(what), re.I)
    q = what.replace("'", "\\'")
    return [
        frame.get_by_role("button", name=exact),
        frame.get_by_role("link", name=exact),
        frame.locator(f"input[type=submit][value*='{q}' i], input[type=button][value*='{q}' i]"),
        frame.get_by_role("button", name=contains),
        frame.get_by_role("link", name=contains),
        # Botones "falsos": div/span con onclick, clases tipo btn, role=button…
        frame.locator("[onclick], [role=button], [class*=btn], [class*=button], [tabindex]")
             .filter(has_text=contains),
        frame.get_by_text(exact),
    ]


def _parse_nth(what: str) -> tuple[str, int | None]:
    """'Cuaderno de seguimiento [2]' -> ('Cuaderno de seguimiento', 2)."""
    m = re.match(r"^(.*?)\s*\[(\d+)\]\s*$", what)
    return (m.group(1), int(m.group(2))) if m else (what, None)


# Posición en pantalla del último elemento pulsado con cada texto, para no
# volver a pulsar el mismo (p. ej. la cabecera de un menú) cuando el texto se repite.
_last_box: dict[str, dict] = {}


def _same_box(a: dict, b: dict) -> bool:
    return all(abs(a[k] - b[k]) < 3 for k in ("x", "y", "width", "height"))


def _overlaps(a: dict, b: dict) -> bool:
    """¿Se solapan mucho? (p. ej. el texto de un botón y el propio botón)."""
    w = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    if w <= 0 or h <= 0:
        return False
    smaller = min(a["width"] * a["height"], b["width"] * b["height"]) or 1
    return (w * h) / smaller > 0.5


def _contains(outer: dict, inner: dict) -> bool:
    return (outer["x"] <= inner["x"] + 1 and outer["y"] <= inner["y"] + 1 and
            outer["x"] + outer["width"] >= inner["x"] + inner["width"] - 1 and
            outer["y"] + outer["height"] >= inner["y"] + inner["height"] - 1 and
            not _same_box(outer, inner))


async def _all_visible_matches(page: Page, what: str) -> list[tuple[dict, Locator]]:
    """Todas las apariciones visibles del texto, ordenadas de arriba a abajo."""
    found: list[tuple[dict, Locator]] = []
    for frame in page.frames:
        for cand in _candidates(frame, what):
            try:
                n = await cand.count()
            except Exception:
                continue
            for i in range(min(n, 20)):
                el = cand.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                except Exception:
                    continue
                if box and not any(_same_box(box, b) for b, _ in found):
                    found.append((box, el))
    # Quita contenedores que engloban a otra aparición (p. ej. el bloque entero del menú)
    found = [(b, el) for b, el in found if not any(_contains(b, o) for o, _ in found)]
    return sorted(found, key=lambda t: (round(t[0]["y"]), t[0]["x"]))


async def _find_visible(page: Page, what: str, nth: int = 1, explicit: bool = False) -> Locator | None:
    """
    Busca el botón/enlace visible con ese texto.
    - nth=1: la forma más precisa que encuentre (botón > enlace > texto).
    - [n] explícito: la aparición nº n contando de arriba a abajo en la pantalla.
    - texto repetido en WEB_CLICK_DESPUES: la siguiente aparición por debajo,
      sin volver a pulsar la que se pulsó antes (p. ej. cabecera de menú -> opción del submenú).
    """
    if explicit or nth > 1:
        matches = await _all_visible_matches(page, what)
        if explicit:
            return matches[nth - 1][1] if len(matches) >= nth else None
        prev = _last_box.get(what.lower())
        others = [(b, el) for b, el in matches if not (prev and _overlaps(b, prev))]
        if prev:
            below = [(b, el) for b, el in others if b["y"] > prev["y"]]
            others = below or others
        return others[0][1] if others else None

    fallback = None
    for frame in page.frames:  # incluye iframes
        for cand in _candidates(frame, what):
            try:
                n = await cand.count()
            except Exception:
                continue
            visible = []
            for i in range(min(n, 20)):
                el = cand.nth(i)
                try:
                    if await el.is_visible():
                        visible.append(el)
                except Exception:
                    pass
            if len(visible) >= nth:
                return visible[nth - 1]
            if visible and fallback is None:
                fallback = visible[-1]
    return fallback


# "Huella" de la página: si cambia tras el clic, es que ha pasado algo
# (nueva página, menú desplegado, lista cargada…).
FINGERPRINT_JS = """() => {
  const vis = [...document.querySelectorAll('a, button, input, select, td, li')]
    .filter(e => e.offsetParent !== null).length;
  return location.href + '|' + vis + '|' + document.body.innerText.length;
}"""


async def _fingerprint(page: Page) -> str:
    parts = []
    for frame in page.frames:
        try:
            parts.append(await frame.evaluate(FINGERPRINT_JS))
        except Exception:
            parts.append("?")
    return page.url + "||" + "||".join(parts)


async def _page_changed(page: Page, before: str, ms: int) -> bool:
    """¿Ha cambiado algo tras el clic? (URL, contenido o elementos visibles)."""
    for _ in range(max(1, ms // 250)):
        await page.wait_for_timeout(250)
        try:
            if await _fingerprint(page) != before:
                return True
        except Exception:
            return True  # la página se está recargando: ha cambiado
    return False


async def _settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass


async def _click_if_visible(page: Page, what: str, timeout: int = 5000, nth: int = 1) -> str:
    """
    Espera hasta `timeout` ms a que aparezca el botón (su aparición nº `nth`) y lo pulsa.
    Devuelve "ok" si la web cambia, "sin_cambio" si se pulsó pero no pasó nada,
    o "" si no se encontró el botón.
    """
    what, explicit = _parse_nth(what)
    nth = explicit or nth
    target = None
    for _ in range(max(1, timeout // 500)):
        target = await _find_visible(page, what, nth, explicit=bool(explicit))
        if target:
            break
        await page.wait_for_timeout(500)
    if not target:
        return ""

    await _settle(page)  # que termine de cargar el JavaScript de la página antes de pulsar
    before = await _fingerprint(page)
    try:
        box = await target.bounding_box()
        if box:
            _last_box[what.lower()] = box
    except Exception:
        box = None
    where = f" (en x={box['x']:.0f}, y={box['y']:.0f})" if box else ""
    if explicit:
        log.info("Pulsando '%s' (aparición nº %d)%s", what, nth, where)
    elif nth > 1:
        log.info("Pulsando '%s' otra vez, en otro sitio%s", what, where)
    else:
        log.info("Pulsando '%s'%s", what, where)
    try:
        await target.scroll_into_view_if_needed(timeout=3000)
        await target.click(timeout=5000)
    except Exception:
        await target.click(force=True, timeout=5000)  # p. ej. si algo lo tapa

    if not await _page_changed(page, before, 6000):
        # Segundo intento: clic por JavaScript, que ejecuta el onclick directamente.
        log.info("La página no ha cambiado; reintento el clic por JavaScript")
        try:
            await target.evaluate("e => e.click()")
        except Exception:
            pass
        if not await _page_changed(page, before, 6000):
            await _settle(page)
            return "sin_cambio"
    await _settle(page)
    return "ok"


async def _select_option(page: Page, label: str, option: str, index: int) -> bool:
    """Elige `option` en el desplegable con etiqueta `label` (o el nº `index` de la página)."""
    for frame in page.frames:
        cands = [
            frame.get_by_label(re.compile(label, re.I)),
            frame.locator(f"select[name*='{label.split()[0].lower()}' i], select[id*='{label.split()[0].lower()}' i]"),
        ]
        for c in cands:
            try:
                if await c.count() and await c.first.is_visible():
                    sel = c.first
                    break
            except Exception:
                pass
        else:
            continue
        break
    else:
        selects = page.locator("select:visible")
        if await selects.count() <= index:
            return False
        sel = selects.nth(index)

    # Busca la opción por su texto (sin importar mayúsculas ni coincidencia exacta).
    opts = await sel.locator("option").all_inner_texts()
    match = next((o for o in opts if o.strip().lower() == option.lower()), None) or \
            next((o for o in opts if option.lower() in o.lower()), None)
    if not match:
        raise WebError(f"No encuentro '{option}' en el desplegable '{label}'. Opciones: {', '.join(o.strip() for o in opts)}")
    await sel.select_option(label=match)
    log.info("Elegido '%s' en '%s'", match.strip(), label)
    await _settle(page)  # a veces al cambiar el centro se recargan los cursos
    return True


async def list_clickables(page: Page) -> list[str]:
    """Textos de los botones/enlaces visibles, para diagnosticar qué hay en pantalla."""
    js = """() => [...document.querySelectorAll(
        'button, a, input[type=submit], input[type=button], [role=button], [onclick], [class*=btn]')]
      .filter(e => e.offsetParent !== null)
      .map(e => {
        const t = (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\\s+/g,' ').trim();
        const id = e.id ? '#' + e.id : '';
        return t ? `${t}  <${e.tagName.toLowerCase()}${id}>` : '';
      }).filter(Boolean)"""
    out = []
    for frame in page.frames:
        try:
            out += await frame.evaluate(js)
        except Exception:
            pass
    return list(dict.fromkeys(out))[:40]


def _user_field(page: Page) -> Locator:
    if USER_SELECTOR:
        return page.locator(USER_SELECTOR).first
    # El campo de usuario suele ser el primer input de texto/email visible.
    return page.locator(
        "input[type=email]:visible, input[type=text]:visible, "
        "input:not([type]):visible, input[type=tel]:visible"
    ).first


async def _accept_dialog(dialog) -> None:
    log.info("Aviso de la web aceptado: %s", dialog.message)
    try:
        await dialog.accept()
    except Exception:
        pass


async def _login(page: Page) -> dict[str, str]:
    """
    Inicia sesión. Devuelve, para cada botón de WEB_CLICK_DESPUES, qué pasó:
    "ok" (pulsado y la web avanzó), "sin_cambio" (pulsado sin efecto) o "" (no encontrado).
    """
    # Acepta los avisos emergentes (alert/confirm) en vez de cancelarlos.
    if not getattr(page, "_dialogs_ok", False):
        page.on("dialog", _accept_dialog)
        page._dialogs_ok = True
    _last_box.clear()
    log.info("Iniciando sesión en %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    pass_sel = PASS_SELECTOR or "input[type=password]"
    password = page.locator(pass_sel).first

    # 1) Botones que hay que pulsar antes de ver el login (p. ej. "Continuar", cookies…)
    for what in CLICK_BEFORE:
        if not await password.is_visible():
            await _click_if_visible(page, what)

    # 2) Login en dos pasos: primero el usuario, "Continuar", y luego la contraseña.
    user_filled = False
    if not await password.is_visible():
        user_field = _user_field(page)
        try:
            await user_field.wait_for(state="visible", timeout=5000)
            await user_field.fill(WEB_USER)
            user_filled = True
            for w in NEXT_BUTTONS:
                if await _find_visible(page, w):
                    await _click_if_visible(page, w, 1000)
                    break
            else:
                await user_field.press("Enter")
        except PWTimeout:
            pass

    try:
        await password.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        raise WebError(
            "No encuentro el campo de contraseña en la página de login. "
            "Puede que haya que pulsar otro botón antes: ponlo en WEB_CLICK_ANTES, "
            "o define WEB_PASS_SELECTOR en .env."
        )

    # 3) Usuario (si no se rellenó ya en el paso anterior) y contraseña.
    if not user_filled:
        await _user_field(page).fill(WEB_USER)
    else:
        # Algunas webs vuelven a mostrar el usuario en la 2ª pantalla, vacío.
        uf = _user_field(page)
        if await uf.count() and await uf.is_visible() and not await uf.input_value():
            await uf.fill(WEB_USER)
    await password.fill(WEB_PASSWORD)

    if SUBMIT_SELECTOR:
        await page.locator(SUBMIT_SELECTOR).first.click()
    else:
        await page.locator(pass_sel).first.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass

    if await _looks_logged_out(page):
        raise WebError(
            "No he podido iniciar sesión. Comprueba el usuario y la contraseña, "
            "o si la web pide captcha o un código de verificación."
        )

    # 4) Elegir centro y curso, si se ha indicado en .env.
    if WEB_CENTRO:
        await _select_option(page, "Centro", WEB_CENTRO, 0)
    if WEB_CURSO:
        await _select_option(page, "Curso", WEB_CURSO, 1)

    # 5) Pantallas intermedias tras el login (p. ej. un botón "Continuar").
    # Si un mismo texto se repite (p. ej. "Cuaderno, Cuaderno"), la 2ª vez se pulsa
    # su 2ª aparición en pantalla (típico de menú + submenú).
    results = {}
    seen: dict[str, int] = {}
    for what in CLICK_AFTER:
        base = _parse_nth(what)[0].lower()
        seen[base] = seen.get(base, 0) + 1
        key = what if seen[base] == 1 else f"{what} ({seen[base]}ª vez)"
        results[key] = await _click_if_visible(page, what, timeout=8000, nth=seen[base])
        if not results[key]:
            log.info("No ha aparecido el botón '%s' tras el login; sigo.", what)
    return results


# JavaScript que recorre la página y saca cada formulario con sus campos.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const parent = el.closest("label");
    if (parent) return clean(parent.innerText);
    const aria = el.getAttribute("aria-label") || el.title;
    if (aria) return clean(aria);
    // Formularios maquetados con tablas: <td>Fecha</td><td><input></td>
    const td = el.closest("td");
    if (td) {
      let prev = td.previousElementSibling;
      while (prev && !clean(prev.innerText)) prev = prev.previousElementSibling;
      if (prev) return clean(prev.innerText).replace(/\s*[:*]+\s*$/, "");
    }
    return clean(el.placeholder || el.name || el.id);
  };
  // Radios (p. ej. notas 1-2-3-4 en columnas): un solo campo por grupo, con la opción marcada.
  const radiosDone = new Set();
  const radioInfo = el => {
    const group = el.name || el.id;
    if (radiosDone.has(group)) return null;
    const all = el.name ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
    radiosDone.add(group);
    const chk = all.find(r => r.checked);
    // Ítem: primera celda con texto de la fila; si no, la etiqueta del grupo
    const tr = el.closest("tr");
    let item = "";
    if (tr) {
      const c = [...tr.cells].find(c => !c.querySelector("input[type=radio]") && clean(c.innerText));
      if (c) item = clean(c.innerText);
    }
    if (!item) item = clean(el.closest("fieldset")?.querySelector("legend")?.innerText) || labelFor(el);
    let value = "";
    if (chk) {
      const own = chk.id && document.querySelector(`label[for="${CSS.escape(chk.id)}"]`);
      const td = chk.closest("td"), table = td?.closest("table");
      const head = table && (table.querySelector("thead tr") || table.rows[0]);
      const colHead = td && head && head !== tr ? clean(head.cells[td.cellIndex]?.innerText) : "";
      value = clean(own?.innerText) || clean(chk.closest("label")?.innerText) || colHead || chk.value;
    }
    return { campo: item, tipo: "radio", valor: value, obligatorio: all.some(r => r.required),
             invalido: false, desactivado: el.disabled };
  };
  const fieldInfo = el => {
    const type = (el.type || el.tagName).toLowerCase();
    if (["hidden", "submit", "button", "reset", "image"].includes(type)) return null;
    if (type === "radio") return radioInfo(el);
    let value;
    if (type === "checkbox") value = el.checked ? "marcado" : "no marcado";
    else if (el.tagName === "SELECT") value = [...el.selectedOptions].map(o => clean(o.text)).join(", ");
    else value = el.value;
    return {
      campo: labelFor(el),
      tipo: type,
      // En textos largos se respetan los saltos de línea
      valor: type === "textarea"
        ? (value || "").replace(/[ \t]+/g, " ").replace(/ *\n\s*/g, "\n").trim()
        : clean(value),
      obligatorio: el.required || el.getAttribute("aria-required") === "true",
      invalido: el.validity ? !el.validity.valid : false,
      desactivado: el.disabled,
    };
  };
  const forms = [...document.querySelectorAll("form")].map((f, i) => ({
    formulario: clean(f.getAttribute("name") || f.id || f.querySelector("legend,h1,h2,h3")?.innerText) || `Formulario ${i + 1}`,
    campos: [...f.querySelectorAll("input, select, textarea")].map(fieldInfo).filter(Boolean),
  }));
  // Campos sueltos fuera de un <form> (muchas webs modernas no usan <form>)
  const sueltos = [...document.querySelectorAll("input, select, textarea")]
    .filter(el => !el.closest("form")).map(fieldInfo).filter(Boolean);
  if (sueltos.length) forms.push({ formulario: "Campos fuera de formulario", campos: sueltos });
  return forms;
}
"""


# JavaScript que saca las tablas visibles de la página (cabeceras + filas).
TABLES_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const cellText = c => {
    // Casillas marcadas, desplegables y campos de texto dentro de la celda
    const inp = c.querySelector("input:not([type=hidden]), select, textarea");
    let t = clean(c.innerText);
    if (!t && inp) {
      if (inp.type === "checkbox" || inp.type === "radio") t = inp.checked ? "✔" : "";
      else if (inp.tagName === "SELECT") t = clean(inp.selectedOptions[0]?.text);
      else t = clean(inp.value);
    }
    return t;
  };
  return [...document.querySelectorAll("table")]
    .filter(t => t.offsetParent !== null && !t.querySelector("table"))   // visibles y sin tablas dentro
    .map(t => {
      const rows = [...t.rows].map(r => [...r.cells].map(cellText));
      let headers = [];
      const th = t.querySelector("thead tr") || [...t.rows].find(r => r.querySelector("th"));
      if (th) headers = [...th.cells].map(c => clean(c.innerText));
      const body = rows.filter((r, i) => !(th && t.rows[i] === th) && r.some(x => x));
      return { titulo: clean(t.caption?.innerText || t.getAttribute("summary") || ""), cabeceras: headers, filas: body };
    })
    .filter(t => t.filas.length);
}
"""


async def extract_tables(page: Page) -> list[dict]:
    tables = []
    for frame in page.frames:  # incluye iframes
        try:
            tables += await frame.evaluate(TABLES_JS)
        except Exception:
            pass
    return tables


async def fetch_forms() -> dict:
    """Inicia sesión si hace falta y devuelve los formularios y el texto de la página."""
    if not configured():
        raise WebError(
            "Falta configurar la web. Rellena WEB_LOGIN_URL, WEB_USER y "
            "WEB_PASSWORD en el archivo .env."
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        use_url = _forms_url_set()
        context = await browser.new_context(
            storage_state=str(SESSION_FILE) if use_url and SESSION_FILE.exists() else None,
            locale="es-ES",
        )
        page = await context.new_page()
        try:
            if not use_url:
                # Sin WEB_FORMS_URL: inicia sesión, pulsa los botones/enlaces de
                # WEB_CLICK_DESPUES en orden y lee la página a la que se llega.
                clicks = await _login(page)
                for what, result in clicks.items():
                    if not result:
                        raise WebError(f"No he encontrado '{what}' tras el login. "
                                       "Ejecuta probar_login.py para ver qué botones hay en pantalla.")
                    if result == "sin_cambio":
                        raise WebError(f"He pulsado '{what}' pero la web no ha avanzado.")
            else:
                await page.goto(FORMS_URL, wait_until="domcontentloaded")
                if await _looks_logged_out(page) or LOGIN_URL.rstrip("/") == page.url.rstrip("/"):
                    await _login(page)
                    await page.goto(FORMS_URL, wait_until="domcontentloaded")
                    await context.storage_state(path=str(SESSION_FILE))

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            if await _looks_logged_out(page):
                raise WebError("La web me ha vuelto a pedir iniciar sesión.")

            forms = await page.evaluate(EXTRACT_JS)
            text = (await page.inner_text("body"))[:MAX_PAGE_TEXT]
            return {
                "url": page.url,
                "titulo": await page.title(),
                "formularios": forms,
                "tablas": await extract_tables(page),
                "texto_pagina": text,
            }
        except PWTimeout:
            raise WebError("La web ha tardado demasiado en responder.")
        finally:
            await browser.close()


def forget_session() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def to_prompt(data: dict, instructions: str) -> str:
    return (
        "Revisa los formularios de esta página web.\n"
        f"Instrucciones del usuario: {instructions or 'revisa que estén completos y correctos, señala campos vacíos, obligatorios sin rellenar, datos incoherentes o errores.'}\n\n"
        f"URL: {data['url']}\nTítulo: {data['titulo']}\n\n"
        "FORMULARIOS (JSON):\n"
        f"{json.dumps(data['formularios'], ensure_ascii=False, indent=1)}\n\n"
        "TABLAS (JSON):\n"
        f"{json.dumps(data.get('tablas', []), ensure_ascii=False, indent=1)}\n\n"
        "TEXTO VISIBLE DE LA PÁGINA:\n"
        f"{data['texto_pagina']}"
    )
