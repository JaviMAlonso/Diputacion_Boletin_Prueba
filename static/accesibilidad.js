// Menú de accesibilidad: botón flotante + panel lateral. Guarda los
// ajustes en localStorage (solo en este navegador) para que se
// mantengan aunque se recargue la página o se cambie de pestaña.
(() => {
  const CLAVE_STORAGE = 'a11y-ajustes-boletin';
  const html = document.documentElement;

  const boton = document.getElementById('a11y-toggle');
  const panel = document.getElementById('a11y-panel');
  const fondo = document.getElementById('a11y-fondo');
  const botonCerrar = document.getElementById('a11y-cerrar');
  const botonRestablecer = document.getElementById('a11y-restablecer');
  const guiaLinea = document.getElementById('a11y-guia-linea');

  // Si esta página no incluye el menú (por si algún día se reutiliza
  // este script en una página sin el HTML del widget), no hacemos nada.
  if (!boton || !panel) return;

  // --- Abrir / cerrar el panel ---
  function abrirPanel() {
    panel.classList.add('abierto');
    fondo.classList.add('visible');
    panel.setAttribute('aria-hidden', 'false');
  }
  function cerrarPanel() {
    panel.classList.remove('abierto');
    fondo.classList.remove('visible');
    panel.setAttribute('aria-hidden', 'true');
  }
  boton.addEventListener('click', () => {
    panel.classList.contains('abierto') ? cerrarPanel() : abrirPanel();
  });
  botonCerrar.addEventListener('click', cerrarPanel);
  fondo.addEventListener('click', cerrarPanel);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') cerrarPanel();
  });

  // --- Ajustes que se activan/desactivan de forma independiente ---
  const TOGGLES_INDEPENDIENTES = [
    'bold', 'line-height', 'letter-spacing', 'hide-images',
    'dislexia', 'resaltar-enlaces', 'resaltar-titulos',
    'cursor-grande', 'sin-animaciones', 'guia-lectura',
  ];

  // --- Ajustes de color: solo uno activo a la vez (como un grupo de radio) ---
  const MODOS_COLOR = [
    'contraste-oscuro', 'contraste-claro', 'invertir',
    'baja-saturacion', 'monocromo', 'alta-saturacion',
  ];

  // --- "Tamaño Fuente": 3 pasos que van rotando con cada clic ---
  const PASOS_FUENTE = ['', 'a11y-fuente-1', 'a11y-fuente-2'];

  function leerEstado() {
    try {
      return JSON.parse(localStorage.getItem(CLAVE_STORAGE)) || {};
    } catch {
      return {};
    }
  }

  function guardarEstado(estado) {
    try {
      localStorage.setItem(CLAVE_STORAGE, JSON.stringify(estado));
    } catch {
      // Si el navegador bloquea localStorage (modo privado, etc.), el
      // menú sigue funcionando, solo que no recordará la elección la
      // próxima vez.
    }
  }

  function actualizarBotonesActivos(estado) {
    document.querySelectorAll('.a11y-opcion').forEach((btn) => {
      const clave = btn.dataset.a11y;
      let activo = false;
      if (TOGGLES_INDEPENDIENTES.includes(clave)) {
        activo = !!estado[clave];
      } else if (MODOS_COLOR.includes(clave)) {
        activo = estado.colorModo === clave;
      } else if (clave === 'fuente') {
        activo = (estado.pasoFuente || 0) > 0;
      }
      btn.classList.toggle('activo', activo);
    });

    const pasoFuente = estado.pasoFuente || 0;
    document.querySelectorAll('#a11y-pasos-fuente i').forEach((punto, i) => {
      punto.classList.toggle('activo', i < pasoFuente);
    });
  }

  function aplicarEstado(estado) {
    TOGGLES_INDEPENDIENTES.forEach((nombre) => {
      html.classList.toggle('a11y-' + nombre, !!estado[nombre]);
    });

    MODOS_COLOR.forEach((nombre) => html.classList.remove('a11y-' + nombre));
    if (estado.colorModo) html.classList.add('a11y-' + estado.colorModo);

    PASOS_FUENTE.forEach((clase) => clase && html.classList.remove(clase));
    const paso = estado.pasoFuente || 0;
    if (PASOS_FUENTE[paso]) html.classList.add(PASOS_FUENTE[paso]);

    actualizarBotonesActivos(estado);
  }

  let estado = leerEstado();
  aplicarEstado(estado);

  document.querySelectorAll('.a11y-opcion').forEach((btn) => {
    btn.addEventListener('click', () => {
      const clave = btn.dataset.a11y;
      if (clave === 'fuente') {
        estado.pasoFuente = ((estado.pasoFuente || 0) + 1) % PASOS_FUENTE.length;
      } else if (MODOS_COLOR.includes(clave)) {
        estado.colorModo = (estado.colorModo === clave) ? null : clave;
      } else {
        estado[clave] = !estado[clave];
      }
      guardarEstado(estado);
      aplicarEstado(estado);
    });
  });

  if (botonRestablecer) {
    botonRestablecer.addEventListener('click', () => {
      estado = {};
      guardarEstado(estado);
      aplicarEstado(estado);
    });
  }

  // --- Guía de lectura: barra horizontal que sigue al ratón ---
  document.addEventListener('mousemove', (ev) => {
    if (html.classList.contains('a11y-guia-lectura') && guiaLinea) {
      guiaLinea.style.top = ev.clientY + 'px';
    }
  });
})();
