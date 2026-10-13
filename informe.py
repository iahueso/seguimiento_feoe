"""
Entra en la web con tu cuenta, lee los formularios y genera un informe HTML.

Uso:
  python informe.py
  python informe.py "comprueba que todas las solicitudes tengan DNI y fecha válida"

El informe se guarda en la carpeta informes/ y se abre en el navegador.
Si ANTHROPIC_API_KEY está en .env, el informe incluye la revisión de la IA.
"""
import os
import sys
import html
import asyncio
import webbrowser
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # antes de importar web.py, que lee su configuración del entorno

import markdown
import web

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
OUTPUT_DIR = Path(os.getenv("INFORME_DIR", "informes"))
OPEN_REPORT = os.getenv("INFORME_ABRIR", "true").lower() != "false"

SYSTEM_PROMPT = (
    "Eres un revisor de formularios meticuloso. Redacta en español un informe claro "
    "en Markdown con estas secciones: '## Resumen' (2-3 frases con el estado general), "
    "'## Problemas encontrados' (lista, de más a menos grave, indicando formulario y campo), "
    "'## Recomendaciones'. Usa solo los datos proporcionados; no inventes valores. "
    "Si no hay problemas, dilo claramente."
)


def ai_review(data: dict, instructions: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": web.to_prompt(data, instructions)}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def field_issues(f: dict) -> list[str]:
    issues = []
    if f["obligatorio"] and not f["valor"]:
        issues.append("Obligatorio vacío")
    elif f["invalido"]:
        issues.append("Valor no válido")
    return issues


def build_html(data: dict, review_md: str | None, instructions: str, when: datetime) -> str:
    e = html.escape
    forms = data["formularios"]
    all_fields = [c for f in forms for c in f["campos"]]
    empty = sum(1 for c in all_fields if not c["valor"])
    flagged = sum(1 for c in all_fields if field_issues(c))

    sections = []
    for form in forms:
        rows = []
        for c in form["campos"]:
            issues = field_issues(c)
            cls = ' class="bad"' if issues else (' class="empty"' if not c["valor"] else "")
            rows.append(
                f"<tr{cls}><td>{e(c['campo'] or '(sin nombre)')}</td>"
                f"<td>{e(c['valor']) or '<span class=muted>vacío</span>'}</td>"
                f"<td>{e(c['tipo'])}</td>"
                f"<td>{'Sí' if c['obligatorio'] else ''}</td>"
                f"<td>{e(', '.join(issues))}</td></tr>"
            )
        sections.append(
            f"<h3>{e(form['formulario'])}</h3>"
            "<div class=tablewrap><table><thead><tr><th>Campo</th><th>Valor</th><th>Tipo</th>"
            "<th>Oblig.</th><th>Aviso</th></tr></thead><tbody>"
            + ("".join(rows) or "<tr><td colspan=5 class=muted>Sin campos</td></tr>")
            + "</tbody></table></div>"
        )

    review_html = (
        markdown.markdown(review_md, extensions=["tables"])
        if review_md
        else "<p class=muted>Sin revisión de la IA (no hay ANTHROPIC_API_KEY en .env).</p>"
    )

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Informe de formularios – {when:%d/%m/%Y %H:%M}</title>
<style>
  :root {{ --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#77756f; --line:#e4e2dc;
          --bad:#fde8e6; --badtext:#9b2c20; --empty:#fdf6e3; --accent:#2f5d8a; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#18181a; --card:#222225; --text:#ececea; --muted:#9a9890; --line:#38383c;
            --bad:#4a2420; --badtext:#ffb4a8; --empty:#3d3620; --accent:#8fb5dc; }} }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width:960px; margin:0 auto; padding:32px 16px 64px; }}
  h1 {{ margin:0 0 4px; font-size:26px; }}
  h2 {{ margin-top:36px; font-size:19px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
  h3 {{ margin:24px 0 8px; font-size:16px; }}
  .meta, .muted {{ color:var(--muted); }}
  .stats {{ display:flex; flex-wrap:wrap; gap:12px; margin:20px 0; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px 16px; min-width:120px; }}
  .stat b {{ display:block; font-size:24px; }}
  .review {{ background:var(--card); border:1px solid var(--line); border-left:4px solid var(--accent);
            border-radius:10px; padding:4px 20px; }}
  .tablewrap {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
          border-radius:10px; overflow:hidden; font-size:14px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ font-weight:600; color:var(--muted); font-size:13px; }}
  tr.bad {{ background:var(--bad); }} tr.bad td:last-child {{ color:var(--badtext); font-weight:600; }}
  tr.empty {{ background:var(--empty); }}
  a {{ color:var(--accent); }}
</style></head><body><main>
<h1>Informe de formularios</h1>
<div class="meta">{when:%d/%m/%Y a las %H:%M} · <a href="{e(data['url'])}">{e(data['titulo'] or data['url'])}</a></div>
{f'<p class="meta">Instrucciones: {e(instructions)}</p>' if instructions else ''}
<div class="stats">
  <div class="stat"><b>{len(forms)}</b>formularios</div>
  <div class="stat"><b>{len(all_fields)}</b>campos</div>
  <div class="stat"><b>{empty}</b>vacíos</div>
  <div class="stat"><b>{flagged}</b>con avisos</div>
</div>
<h2>Revisión</h2>
<div class="review">{review_html}</div>
<h2>Datos leídos de la web</h2>
{''.join(sections) or '<p class=muted>No se ha encontrado ningún formulario en la página.</p>'}
</main></body></html>"""


def main() -> int:
    instructions = " ".join(sys.argv[1:])
    print("Entrando en la web y leyendo los formularios…")
    try:
        data = asyncio.run(web.fetch_forms())
    except web.WebError as e:
        print(f"ERROR: {e}")
        return 1

    n = sum(len(f["campos"]) for f in data["formularios"])
    print(f"Leídos {len(data['formularios'])} formulario(s) con {n} campo(s).")

    review = None
    if ANTHROPIC_API_KEY:
        print("La IA está revisando los formularios…")
        try:
            review = ai_review(data, instructions)
        except Exception as e:
            print(f"AVISO: la revisión de la IA ha fallado ({e}). El informe irá sin ella.")

    now = datetime.now()
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"informe_{now:%Y-%m-%d_%H%M%S}.html"
    out.write_text(build_html(data, review, instructions, now), encoding="utf-8")
    print(f"Informe guardado en: {out.resolve()}")

    if OPEN_REPORT:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
