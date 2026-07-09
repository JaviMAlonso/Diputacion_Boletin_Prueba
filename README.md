# Boletín Municipal de Convocatorias — versión web

Interfaz web local para monitorizar BOE, DOCM y BOP sin usar la terminal
en cada búsqueda. Usa exactamente la misma lógica que el script de línea
de comandos (`monitor_convocatorias.py`); esta carpeta solo añade un
formulario web encima, más avisos automáticos por Telegram cuando un
plazo está a punto de vencer.

## Puesta en marcha

La carpeta debería verse tal que así:
### monitor_web/
### ├── app.py
### ├── monitor_convocatorias.py
### ├── config.env······(opcional, para los avisos por Telegram)
### ├── requirements.txt
### ├── templates/
### │······└── index.html
### └── static/
### ········├── style.css
### ········└── script.js

```bash
cd monitor_web
pip install -r requirements.txt
python app.py
```

Abre en el navegador: **http://127.0.0.1:5000**

## Uso

1. Rellena la provincia (para el BOP) y cuántos días hacia atrás quieres
   revisar, o marca "Ver solo un día concreto" para consultar una fecha
   fija.
2. (Opcional) Marca "Ver solo oposiciones y empleo público" para filtrar
   solo procesos selectivos, bolsas de trabajo y ofertas de empleo
   público. También puedes elegir un área de interés distinta (cultura,
   deporte, infraestructuras...) en el desplegable, o dejarlo en "Sin
   filtro" para ver todo.
3. (Opcional) Activa "Generar resumen con IA" y pega tu clave gratuita de
   Gemini (consíguela en https://aistudio.google.com/apikey). La clave solo
   viaja a tu propio servidor local (127.0.0.1) y nunca se guarda en disco.
4. (Opcional) Activa "Avisar por Telegram cuando un plazo esté a punto de
   vencer" — ver más abajo cómo configurarlo la primera vez.
5. Pulsa "Buscar convocatorias".
6. Descarga el informe en Markdown con el botón de descarga si quieres
   archivarlo o compartirlo.

## Avisos por Telegram (opcional)

Para recibir un aviso automático cuando una convocatoria entre en la
ventana crítica de vencimiento (por defecto, entre 3 y 5 días antes de
que caduque el plazo):

1. Abre Telegram, busca a **@BotFather** y escríbele `/newbot`. Sigue los
   pasos (nombre y username del bot) y copia el token que te da — algo
   como `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`.
2. En la carpeta `monitor_web`, crea un archivo de texto llamado
   **`config.env`** (puedes copiar `config.env.ejemplo` y renombrarlo) con
   esta línea:
   ```
   TELEGRAM_BOT_TOKEN=tu_token_de_botfather
   ```
   No hace falta usar la terminal ni `export`: basta con ese archivo.
3. Arranca `python app.py`. En la web aparecerá un botón
   **"📩 Conectar mi Telegram"**. Pulsa el botón y dale a **Iniciar** en el
   chat que se abre — con eso quedas suscrito, sin tener que buscar tu
   `chat_id` a mano.
4. Marca la casilla de avisos en el formulario y ajusta si quieres la
   ventana de días ("Avisar desde / hasta"). Nunca se avisa el mismo día
   en que vence el plazo (0 días restantes), porque a esas alturas ya no
   da tiempo útil de reacción.

`config.env` es solo tuyo: no lo compartas ni lo subas a ningún
repositorio público, ya que quien tenga el token puede controlar tu bot.

## Notas

- **DOCM y BOP** no tienen API pública estable; si sus portales bloquean
  peticiones automatizadas (403), lo verás reflejado en el estado de
  fuentes ("sin acceso") sin que la app se detenga. El BOE sí tiene API
  oficial y debería funcionar siempre.
- Cuando el título de un anuncio no trae plazo ni importe, la app
  descarga el documento completo y vuelve a intentarlo sobre el texto
  íntegro (limitado a 25 documentos por búsqueda para no saturar los
  servidores de origen ni ralentizar demasiado).
- Esta app está pensada para uso **local**, en tu propio ordenador. No
  está preparada para desplegarse en un servidor público sin añadir
  autenticación, ya que cualquiera que acceda podría usar tu clave de IA
  o tu bot de Telegram.
