#!/bin/bash
# Lanza la revisión de alumnos desde crontab (sin ventana) y guarda lo que pasa en cron.log.
#
#   chmod +x ejecutar_cron.sh
#   crontab -e   y añade, por ejemplo (lunes a viernes a las 8:00):
#   0 8 * * 1-5 /home/hueso/curro/feoe/ejecutar_cron.sh

# Carpeta donde está este script (así funciona aunque cron arranque en otro sitio)
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

export PYTHONIOENCODING=utf-8      # para los emojis de los mensajes
export LANG=C.UTF-8

{
  echo
  echo "===== $(date '+%d/%m/%Y %H:%M') ====="
  "$DIR/venv/bin/python" revisar_tutorias.py --oculto "$@"
  echo "(terminado con código $?)"
} >> "$DIR/cron.log" 2>&1
