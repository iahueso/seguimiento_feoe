"""
Entra en la web, llega al Cuaderno de seguimiento (pulsando WEB_CLICK_DESPUES)
y muestra los alumnos que aparecen en pantalla.

Uso:
  python ver_alumnos.py            -> con el navegador a la vista
  python ver_alumnos.py --oculto   -> sin ventana

También guarda la lista en alumnos.csv (se abre con LibreOffice o Excel)
y una captura de la página en alumnos.png.
"""
import sys
import csv
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

import web
from playwright.async_api import async_playwright

logging.basicConfig(format="   · %(message)s", level=logging.INFO)
VISIBLE = "--oculto" not in sys.argv


def print_table(t: dict, n: int) -> None:
    rows = t["filas"]
    headers = t["cabeceras"]
    ncols = max([len(headers)] + [len(r) for r in rows])
    # Ancho de cada columna (limitado a 40 caracteres para que quepa en la terminal)
    widths = [min(40, max([len(headers[i]) if i < len(headers) else 0] +
                          [len(r[i]) if i < len(r) else 0 for r in rows])) for i in range(ncols)]
    fmt = lambda cells: "  ".join((cells[i] if i < len(cells) else "")[:widths[i]].ljust(widths[i])
                                  for i in range(ncols)).rstrip()
    titulo = f" – {t['titulo']}" if t["titulo"] else ""
    print(f"\n📋 Tabla {n}{titulo}  ({len(rows)} fila(s))")
    if headers:
        print("   " + fmt(headers))
        print("   " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("   " + fmt(r))


async def main() -> int:
    if not web.configured():
        print("Falta rellenar en .env: WEB_LOGIN_URL, WEB_USER y WEB_PASSWORD")
        return 1

    print(f"Abriendo {web.LOGIN_URL} …")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not VISIBLE, slow_mo=200 if VISIBLE else 0)
        page = await (await browser.new_context(locale="es-ES")).new_page()
        try:
            clicks = await web._login(page)
        except web.WebError as e:
            print(f"\n❌ {e}")
            await browser.close()
            return 1

        problemas = [f"{b} ({'no encontrado' if not r else 'no avanza'})" for b, r in clicks.items() if r != "ok"]
        if problemas:
            print(f"\n⚠️  Algo no ha ido bien con: {', '.join(problemas)}. Ejecuta probar_login.py para verlo.")

        await page.screenshot(path="alumnos.png", full_page=True)
        print(f"\nPágina actual: {await page.title()}  ({page.url})")

        tables = await web.extract_tables(page)
        if tables:
            for i, t in enumerate(tables, 1):
                print_table(t, i)

            # La tabla con más filas suele ser la de alumnos: se guarda en CSV.
            main_t = max(tables, key=lambda t: len(t["filas"]))
            with open("alumnos.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                if main_t["cabeceras"]:
                    w.writerow(main_t["cabeceras"])
                w.writerows(main_t["filas"])
            print(f"\n💾 La tabla más grande ({len(main_t['filas'])} filas) está guardada en alumnos.csv")
        else:
            print("\nNo veo ninguna tabla en esta página. Este es su texto:\n")
            text = await page.inner_text("body")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for l in lines[:80]:
                print("   " + l)
            if len(lines) > 80:
                print(f"   … ({len(lines) - 80} líneas más)")
        print("   Captura de la página: alumnos.png")

        if VISIBLE:
            input("\nPulsa Enter para cerrar el navegador…")
        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
