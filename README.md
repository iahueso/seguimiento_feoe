# Revisión automática del Cuaderno de seguimiento (FCT)

Scripts en Python que entran en la aplicación de FCT con tu cuenta, llegan al
**Cuaderno de seguimiento**, abren la tutoría más próxima de un alumno y te envían
por **Telegram** (o WhatsApp) los ítems con nota y las observaciones del alumno y
de los tutores.

Los scripts **solo leen**: nunca pulsan Guardar, Eliminar ni Imprimir.

## Scripts

| Script | Qué hace |
|---|---|
| `probar_login.py` | Comprueba que entra en la web y pulsa los botones de `WEB_CLICK_DESPUES` |
| `ver_alumnos.py` | Muestra la lista de alumnos del Cuaderno de seguimiento (y la guarda en `alumnos.csv`) |
| `revisar_tutorias.py` | Revisa a todos los alumnos (próxima tutoría + desarrollos asociados) y envía un mensaje de Telegram por alumno |
| `notificar.py` | Envía mensajes; `python notificar.py --chat-id` te da tu `TELEGRAM_CHAT_ID` |
| `informe.py` | Genera un informe HTML de los formularios de una página |
| `web.py` | Funciones comunes (login, clics, lectura de formularios y tablas) |

## Instalación (Ubuntu)

```bash
sudo apt install -y python3 python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env
nano .env
```

En `.env` rellena al menos `WEB_LOGIN_URL`, `WEB_USER`, `WEB_PASSWORD`,
`WEB_CLICK_DESPUES`, `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.

> Cada vez que abras una terminal nueva: `source venv/bin/activate`

## Uso

```bash
python3 probar_login.py                         # comprobar el acceso
python3 revisar_tutorias.py                     # todos los alumnos: un mensaje de Telegram por alumno
python3 revisar_tutorias.py --alumno 3          # solo un alumno
python3 revisar_tutorias.py --sin-enviar        # solo mostrarlo en pantalla, sin enviar
python3 revisar_tutorias.py --fecha 10/10/2026  # como si hoy fuera ese día
python3 revisar_tutorias.py --todas             # todas sus tutorías
python3 revisar_tutorias.py --oculto            # sin abrir la ventana del navegador
```

## Telegram

1. En Telegram habla con **@BotFather** → `/newbot` → copia el token en `TELEGRAM_TOKEN`.
2. Abre tu bot, pulsa **Iniciar** y escríbele algo.
3. `python3 notificar.py --chat-id` → copia la línea `TELEGRAM_CHAT_ID=…` en `.env`.
4. `python3 notificar.py` → te llega un mensaje de prueba.

## Privacidad

`.env` (credenciales), `session.json`, las capturas, los CSV y las carpetas
`revision/` y `revision_alumno*` contienen datos sensibles o de alumnos y están excluidos en
`.gitignore`. No los subas nunca al repositorio.
