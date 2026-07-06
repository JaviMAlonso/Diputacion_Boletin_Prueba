const hoy = new Date();
document.getElementById('fecha-hoy').textContent = hoy.toLocaleDateString('es-ES', {
  day: 'numeric', month: 'long', year: 'numeric'
});

const checkboxIA = document.getElementById('usar_ia');
const camposIA = document.getElementById('ia-campos');
checkboxIA.addEventListener('change', () => {
  camposIA.classList.toggle('visible', checkboxIA.checked);
});

const OPOSICIONES_KEYWORDS = 'oposición, oposiciones, proceso selectivo, pruebas selectivas, concurso-oposición, bolsa de trabajo, bolsa de empleo, oferta de empleo público, personal funcionario, personal laboral, tribunal calificador';

const checkboxOposiciones = document.getElementById('solo_oposiciones');
const selectAreas = document.getElementById('areas');
const campoAreas = document.getElementById('campo-areas');
const notaOposiciones = document.getElementById('nota-oposiciones');

checkboxOposiciones.addEventListener('change', () => {
  const activo = checkboxOposiciones.checked;
  selectAreas.disabled = activo;
  campoAreas.classList.toggle('deshabilitado', activo);
  notaOposiciones.style.display = activo ? 'block' : 'none';
});

const checkboxHoy = document.getElementById('solo_hoy');
const inputDias = document.getElementById('dias');
const notaHoy = document.getElementById('nota-hoy');
const campoFechaElegida = document.getElementById('campo-fecha-elegida');
const selectFechaElegida = document.getElementById('fecha_elegida');
let diasPrevios = inputDias.value;

// Rellenamos el desplegable con los últimos 14 días (hoy incluido),
// por si el día de hoy todavía no tiene BOP/BOE publicado y hace
// falta elegir uno anterior para poder probar la búsqueda.
const DIAS_SEMANA = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'];
function pad2(n) { return String(n).padStart(2, '0'); }

for (let i = 0; i < 14; i++) {
  const d = new Date();
  d.setDate(d.getDate() - i);
  const valor = pad2(d.getDate()) + '/' + pad2(d.getMonth() + 1) + '/' + d.getFullYear();
  let etiqueta;
  if (i === 0) etiqueta = 'Hoy · ' + valor;
  else if (i === 1) etiqueta = 'Ayer · ' + valor;
  else etiqueta = DIAS_SEMANA[d.getDay()] + ' ' + valor;
  const opcion = document.createElement('option');
  opcion.value = valor;
  opcion.textContent = etiqueta;
  selectFechaElegida.appendChild(opcion);
}

checkboxHoy.addEventListener('change', () => {
  const activo = checkboxHoy.checked;
  notaHoy.style.display = activo ? 'block' : 'none';
  campoFechaElegida.classList.toggle('visible', activo);
  if (activo) {
    diasPrevios = inputDias.value;
    actualizarDiasSegunFecha();
    inputDias.disabled = true;
  } else {
    inputDias.disabled = false;
    inputDias.value = diasPrevios;
  }
});

// Cuando se elige un día distinto de hoy, "Días hacia atrás" debe ser
// lo bastante grande para que la búsqueda en el BOE llegue hasta esa
// fecha (si no, aunque exista el anuncio de ese día, no se detectaría).
function actualizarDiasSegunFecha() {
  const [d, m, y] = selectFechaElegida.value.split('/').map(Number);
  const elegida = new Date(y, m - 1, d);
  const hoy = new Date();
  hoy.setHours(0, 0, 0, 0);
  elegida.setHours(0, 0, 0, 0);
  const diferenciaDias = Math.round((hoy - elegida) / (1000 * 60 * 60 * 24));
  inputDias.value = Math.min(30, Math.max(1, diferenciaDias + 1));
}

selectFechaElegida.addEventListener('change', () => {
  if (checkboxHoy.checked) actualizarDiasSegunFecha();
});

const NUMERALES = ['I', 'II', 'III', 'IV', 'V', 'VI'];

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}

document.getElementById('form-busqueda').addEventListener('submit', async (ev) => {
  ev.preventDefault();

  const boton = document.getElementById('btn-buscar');
  const resultados = document.getElementById('resultados');
  boton.disabled = true;
  boton.textContent = 'Consultando boletines…';
  resultados.innerHTML = '<div class="cargando">Consultando BOE, DOCM y BOP…</div>';

  const payload = {
    provincia: document.getElementById('provincia').value,
    dias: document.getElementById('dias').value,
    areas: checkboxOposiciones.checked ? OPOSICIONES_KEYWORDS : document.getElementById('areas').value,
    plazo_alerta: document.getElementById('plazo_alerta').value,
    solo_hoy: checkboxHoy.checked,
    fecha_elegida: selectFechaElegida.value,
    usar_ia: checkboxIA.checked,
    gemini_key: document.getElementById('gemini_key').value,
    gemini_modelo: document.getElementById('gemini_modelo').value,
    max_ia: document.getElementById('max_ia').value,
  };

  try {
    const resp = await fetch('/api/buscar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error('El servidor respondió con un error (' + resp.status + ')');
    const data = await resp.json();
    renderResultados(data);
  } catch (err) {
    resultados.innerHTML = '<div class="error-carga">No se pudo completar la búsqueda: ' + escapeHtml(err.message) + '</div>';
  } finally {
    boton.disabled = false;
    boton.textContent = 'Buscar convocatorias';
  }
});

function renderResultados(data) {
  const resultados = document.getElementById('resultados');
  const partes = [];

  // Estado de fuentes
  partes.push('<div class="estado-fuentes">');
  for (const [nombre, estado] of Object.entries(data.fuentes || {})) {
    const clase = estado.ok ? 'ok' : 'fail';
    const texto = estado.ok
      ? nombre + ': ' + estado.detectadas + ' detectadas'
      : nombre + ': sin acceso';
    partes.push('<span class="pastilla ' + clase + '">' + escapeHtml(texto) + '</span>');
  }
  if (data.ia_estado === 'ok') {
    partes.push('<span class="pastilla ok sello">✓ resúmenes IA generados</span>');
  } else if (data.ia_estado === 'sin_clave') {
    partes.push('<span class="pastilla fail">IA: falta la clave de Gemini</span>');
  } else if (data.ia_estado && data.ia_estado.startsWith('error')) {
    partes.push('<span class="pastilla fail">IA: ' + escapeHtml(data.ia_estado) + '</span>');
  }
  partes.push('</div>');

  // Cabecera de resumen + descarga
  partes.push('<div class="resumen-cabecera">');
  partes.push('<div class="cifra">Detectadas <b>' + data.total_detectadas + '</b> · Relevantes <b>' + data.total_relevantes + '</b></div>');
  partes.push('<a class="descarga" href="/api/descargar-informe" download>↓ Descargar informe (.md)</a>');
  partes.push('</div>');

  // Listado bruto del BOP de Toledo publicado hoy (solo título + PDF,
  // sin clasificar ni filtrar), cuando el modo "solo hoy" está activo.
  if (Array.isArray(data.bop_hoy)) {
    partes.push('<div class="seccion-categoria bop-hoy">');
    partes.push('<div class="titulo-seccion"><span class="numeral">§</span><h3>BOP Toledo — ' + escapeHtml(data.fecha_bop ? 'publicado el ' + data.fecha_bop : 'publicado hoy') + '</h3><span class="cuenta">(' + data.bop_hoy.length + ')</span></div>');
    if (data.bop_hoy.length === 0) {
      partes.push('<div class="vacio">No se ha detectado ningún anuncio del BOP de Toledo para la fecha de hoy (o el portal no ha podido consultarse).</div>');
    } else {
      for (const item of data.bop_hoy) {
        partes.push('<div class="entrada entrada-bop-hoy">');
        partes.push('<h4>' + escapeHtml(item.titulo) + '</h4>');
        partes.push('<a class="enlace" href="' + escapeHtml(item.url_pdf) + '" target="_blank" rel="noopener">↓ Descargar PDF →</a>');
        partes.push('</div>');
      }
    }
    partes.push('</div>');
  }

  const convocatorias = data.convocatorias || [];
  const alertaIds = new Set(data.alertas_ids || []);

  // Alertas de plazo
  const urgentes = convocatorias.filter(c => alertaIds.has(c.id_unico));
  if (urgentes.length > 0) {
    partes.push('<div class="alertas visible"><h3>⚠ Plazos próximos a vencer</h3><ul>');
    for (const c of urgentes) {
      partes.push('<li><b>' + (c.dias_restantes ?? '?') + ' días</b> — ' + escapeHtml(c.titulo) + '</li>');
    }
    partes.push('</ul></div>');
  }

  if (convocatorias.length === 0) {
    partes.push('<div class="vacio">No se han encontrado convocatorias relevantes con estos criterios. Prueba a ampliar el rango de días o quitar las áreas de interés.</div>');
    resultados.innerHTML = partes.join('');
    return;
  }

  // Agrupar por categoría
  const porCategoria = {};
  for (const c of convocatorias) {
    if (!porCategoria[c.categoria]) porCategoria[c.categoria] = [];
    porCategoria[c.categoria].push(c);
  }

  // La categoría de empleo público / oposiciones siempre va al final,
  // para que quede claramente diferenciada del resto de convocatorias
  // (subvenciones, licitaciones, etc.) y el modo "solo empleo" tenga
  // sentido visual incluso cuando se combina con otras áreas.
  const CATEGORIA_EMPLEO = 'Empleo público / Oposición';
  const entradasCategorias = Object.entries(porCategoria).sort(([a], [b]) => {
    if (a === CATEGORIA_EMPLEO) return 1;
    if (b === CATEGORIA_EMPLEO) return -1;
    return 0;
  });

  let i = 0;
  for (const [categoria, items] of entradasCategorias) {
    const numeral = NUMERALES[i % NUMERALES.length];
    i++;
    partes.push('<div class="seccion-categoria' + (categoria === CATEGORIA_EMPLEO ? ' seccion-empleo' : '') + '">');
    partes.push('<div class="titulo-seccion"><span class="numeral">' + numeral + '.</span><h3>' + escapeHtml(categoria) + '</h3><span class="cuenta">(' + items.length + ')</span></div>');

    for (const c of items) {
      const urgente = alertaIds.has(c.id_unico);
      partes.push('<div class="entrada">');
      partes.push('<div class="fuente-linea">' + escapeHtml(c.fuente) + (c.organismo ? ' · ' + escapeHtml(c.organismo) : '') + '</div>');
      partes.push('<h4>' + escapeHtml(c.titulo) + '</h4>');
      partes.push('<div class="datos">');
      partes.push('<span>Publicado: <b>' + escapeHtml(c.fecha_publicacion) + '</b></span>');
      partes.push('<span class="' + (urgente ? 'urgente' : '') + '">Plazo: <b>' + escapeHtml(c.plazo || 'No detectado') + '</b></span>');
      if (c.importe) {
        partes.push('<span class="importe">Cuantía: <b>' + escapeHtml(c.importe) + '</b></span>');
      }
      partes.push('</div>');
      if (c.resumen) {
        partes.push('<div class="resumen-texto">' + escapeHtml(c.resumen) + '</div>');
      }
      if (c.url) {
        partes.push('<a class="enlace" href="' + escapeHtml(c.url) + '" target="_blank" rel="noopener">Ver documento oficial →</a>');
      }
      partes.push('</div>');
    }
    partes.push('</div>');
  }

  resultados.innerHTML = partes.join('');
}
