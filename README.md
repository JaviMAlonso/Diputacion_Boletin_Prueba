# Boletín Municipal de Convocatorias — versión web

Interfaz web local para monitorizar BOE, DOCM y BOP sin usar la terminal
en cada búsqueda. Usa exactamente la misma lógica que el script de línea
de comandos (`monitor_convocatorias.py`); esta carpeta solo añade un
formulario web encima.

## Puesta en marcha

La carpeta debería verse tal que así:
### monitor_web/
### ├── app.py
### ├── monitor_convocatorias.py
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

1. Rellena el municipio, la provincia (para el BOP) y cuántos días hacia
   atrás quieres revisar.
2. (Opcional) Escribe áreas de interés separadas por comas — por ejemplo
   `cultura, deporte, infraestructuras` — para filtrar los resultados.
   Déjalo vacío para ver todo.
3. (Opcional) Activa "Generar resumen con IA" y pega tu clave gratuita de
   Gemini (consíguela en https://aistudio.google.com/apikey). La clave solo
   viaja a tu propio servidor local (127.0.0.1) y nunca se guarda en disco.
4. Pulsa "Buscar convocatorias".
5. Descarga el informe en Markdown con el botón de descarga si quieres
   archivarlo o compartirlo.

## Notas

- **DOCM y BOP** no tienen API pública estable; si sus portales bloquean
  peticiones automatizadas (403), lo verás reflejado en el estado de
  fuentes ("sin acceso") sin que la app se detenga. El BOE sí tiene API
  oficial y debería funcionar siempre.
- Esta app está pensada para uso **local**, en tu propio ordenador. No
  está preparada para desplegarse en un servidor público sin añadir
  autenticación, ya que cualquiera que acceda podría usar tu clave de IA.
