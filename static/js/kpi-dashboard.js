/*
  Panel de KPIs: cambio de periodo sin recargar, grafico y refresco.

  Decision de diseno importante: al cambiar de periodo NO se reconstruye el
  panel en JavaScript. Se vuelve a pedir ESTE MISMO FRAGMENTO al servidor y se
  sustituye el HTML. El servidor sigue siendo el unico que pinta los datos,
  asi que el resultado con y sin este script es identico y no hay dos
  implementaciones del mismo render que se puedan desincronizar.

  El fragmento se pide a su propio endpoint, jamas a la pagina que lo Aloja.
  El panel vive dentro de /admin, y recargar esa pagina entera para cambiar
  un periodo repetiria todas las consultas de productos.

  El unico dato que se lee en crudo es la serie del grafico, que viaja en un
  <script type="application/json"> porque Chart.js necesita un array y no un
  HTML.
*/
(function () {
  'use strict';

  var panel = document.getElementById('kpiPanel');
  if (!panel) return;

  // La URL la declara la plantilla con url_for, de modo que este script no
  // necesita saber en que pagina esta incrustado.
  var urlFragmento = panel.dataset.kpiUrl || window.location.pathname;

  var form = document.getElementById('kpiFiltros');
  var preset = document.getElementById('kpiPreset');
  var desde = document.getElementById('kpiDesde');
  var hasta = document.getElementById('kpiHasta');
  var botonActualizar = document.getElementById('kpiActualizar');
  var aviso = document.getElementById('kpiError');

  var NARANJA = '#f39c12';
  var VERDE = '#22c55e';
  var ROJO = '#ef4444';
  var REFRESH_MS = 60000;
  var cargando = false;
  var grafico = null;
  var temporizador = null;

  // ─────────────────────────────────────────────
  // Utilidades
  // ─────────────────────────────────────────────
  function mostrarAviso(texto) {
    if (!aviso) return;
    aviso.textContent = texto;
    aviso.classList.add('visible');
  }

  function ocultarAviso() {
    if (!aviso) return;
    aviso.textContent = '';
    aviso.classList.remove('visible');
  }

  function marcarCarga(activo) {
    cargando = activo;
    panel.classList.toggle('cargando', activo);
  }

  function parametros() {
    var p = new URLSearchParams();
    p.set('preset', preset ? preset.value : '30d');
    // Las fechas solo aplican al periodo personalizado: mandarlas siempre
    // haria que 'hoy' ignorase el preset.
    if (preset && preset.value === 'personalizado') {
      if (desde && desde.value.trim()) p.set('desde', desde.value.trim());
      if (hasta && hasta.value.trim()) p.set('hasta', hasta.value.trim());
    }
    return p;
  }

  // ─────────────────────────────────────────────
  // Grafico
  // ─────────────────────────────────────────────
  function leerSerie() {
    var nodo = document.getElementById('kpiTendencia');
    if (!nodo) return [];
    try {
      var datos = JSON.parse(nodo.textContent || '[]');
      return Array.isArray(datos) ? datos : [];
    } catch (e) {
      return [];
    }
  }

  function coloresDelTema() {
    // Los tokens viven en CSS, asi que se leen del propio documento para
    // que el grafico acompanhe al tema claro/oscuro.
    var estilos = getComputedStyle(document.documentElement);
    return {
      texto: estilos.getPropertyValue('--text-muted').trim() || '#888',
      borde: estilos.getPropertyValue('--border-subtle').trim() || 'rgba(255,255,255,.05)'
    };
  }

  function pintarGrafico() {
    var lienzo = document.getElementById('kpiGrafico');
    if (!lienzo || typeof Chart === 'undefined') return;

    var serie = leerSerie();
    if (!serie.length) return;

    var tema = coloresDelTema();

    // Chart.js reutiliza el canvas: hay que destruir la instancia previa o
    // se apilan las capas al cambiar de periodo.
    if (grafico) {
      grafico.destroy();
      grafico = null;
    }

    grafico = new Chart(lienzo, {
      type: 'line',
      data: {
        labels: serie.map(function (d) { return d.dia; }),
        datasets: [
          {
            label: 'Peticiones',
            data: serie.map(function (d) { return d.peticiones; }),
            borderColor: NARANJA,
            backgroundColor: 'rgba(243,156,18,.15)',
            borderWidth: 2,
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointBackgroundColor: NARANJA
          },
          {
            label: 'Errores 5xx',
            data: serie.map(function (d) { return d.errores; }),
            borderColor: ROJO,
            backgroundColor: 'rgba(239,68,68,.12)',
            borderWidth: 2,
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointBackgroundColor: ROJO,
            // Los errores son una magnitud mucho menor que el trafico: sin
            // este eje propio quedarian pegados a cero y no se verian.
            yAxisID: 'y1'
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            labels: { color: tema.texto, boxWidth: 12, font: { size: 11 } }
          },
          tooltip: {
            backgroundColor: 'rgba(0,0,0,.85)',
            titleColor: '#fff',
            bodyColor: '#ddd',
            borderColor: NARANJA,
            borderWidth: 1
          }
        },
        scales: {
          x: {
            grid: { color: tema.borde },
            ticks: { color: tema.texto, font: { size: 10 }, maxRotation: 0,
                     autoSkipPadding: 12 }
          },
          y: {
            position: 'left',
            beginAtZero: true,
            grid: { color: tema.borde },
            ticks: { color: tema.texto, font: { size: 10 }, precision: 0 }
          },
          y1: {
            position: 'right',
            beginAtZero: true,
            grid: { drawOnChartArea: false },
            ticks: { color: ROJO, font: { size: 10 }, precision: 0 }
          }
        }
      }
    });
  }

  // ─────────────────────────────────────────────
  // Cambio de periodo
  // ─────────────────────────────────────────────
  function aplicarParametrosEnUrl(p) {
    // Se parte de los parametros que ya tiene la pagina y se sobrescriben
    // solo los del panel. Reemplazar la query entera perderia, en /admin, la
    // busqueda de productos y la pagina de la tabla.
    var params = new URLSearchParams(window.location.search);

    // Los tres forman un grupo: si el periodo deja de ser personalizado, sus
    // fechas dejan de aplicar y no deben quedar colgando en la URL.
    if (p.get('preset') !== 'personalizado') {
      params.delete('desde');
      params.delete('hasta');
    }
    p.forEach(function (valor, clave) { params.set(clave, valor); });

    var qs = params.toString();
    window.history.replaceState(
      { kpi: true },
      '',
      window.location.pathname + (qs ? '?' + qs : '')
    );
  }

  function cargar() {
    if (cargando) return;
    marcarCarga(true);
    ocultarAviso();

    var p = parametros();

    fetch(urlFragmento + '?' + p.toString(), {
      headers: { 'X-Requested-With': 'fetch' },
      credentials: 'same-origin'
    })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var nuevo = doc.getElementById('kpiPanel');
        if (!nuevo) throw new Error('El servidor no devolvio el panel');

        // Se sustituye el contenido y se conserva el div exterior, que es
        // el que lleva la URL del endpoint en data-kpi-url.
        panel.innerHTML = nuevo.innerHTML;

        // Los nodos de los filtros acaban de ser sustituidos, asi que los
        // listeners de antes apuntan a elementos que ya no existen: hay que
        // volver a buscarlos y a enlazarlos.
        form = document.getElementById('kpiFiltros');
        preset = document.getElementById('kpiPreset');
        desde = document.getElementById('kpiDesde');
        hasta = document.getElementById('kpiHasta');
        botonActualizar = document.getElementById('kpiActualizar');
        aviso = document.getElementById('kpiError');

        volverAEnlazar();
        pintarGrafico();
        aplicarParametrosEnUrl(p);
      })
      .catch(function (err) {
        mostrarAviso('No se pudo actualizar el panel: ' + err.message +
                     '. Los datos mostrados son los del ultima carga correcta.');
      })
      .then(function () {
        marcarCarga(false);
      });
  }

  // ─────────────────────────────────────────────
  // Refresco automatico
  // ─────────────────────────────────────────────
  function alternarRefresco(activo) {
    if (temporizador) {
      clearInterval(temporizador);
      temporizador = null;
    }
    if (activo) {
      temporizador = setInterval(cargar, REFRESH_MS);
    }
  }

  // ─────────────────────────────────────────────
  // Eventos
  // ─────────────────────────────────────────────
  function volverAEnlazar() {
    if (preset) {
      preset.addEventListener('change', function () {
        // Al salir de 'personalizado' se limpian las fechas: si no, el
        // preset recien elegido seguiria arrastrando el rango anterior.
        if (preset.value !== 'personalizado' && desde && hasta) {
          desde.value = '';
          hasta.value = '';
        }
        cargar();
      });
    }

    if (form) {
      form.addEventListener('submit', function (ev) {
        // Sin JavaScript el formulario funciona igual: solo se intercepta
        // cuando el script esta activo.
        ev.preventDefault();
        cargar();
      });
    }

    if (botonActualizar) {
      botonActualizar.addEventListener('click', function () {
        botonActualizar.classList.toggle('kpi-refrescando');
        cargar();
        setTimeout(function () {
          var b = document.getElementById('kpiActualizar');
          if (b) b.classList.remove('kpi-refrescando');
        }, 900);
      });

      // Doble clic en el boton: alterna el refresco automatico.
      botonActualizar.addEventListener('dblclick', function () {
        if (temporizador) {
          alternarRefresco(false);
          mostrarAviso('Actualizacion automatica desactivada.');
          setTimeout(ocultarAviso, 2500);
        } else {
          alternarRefresco(true);
          mostrarAviso('Se actualizara cada 60 s. Doble clic para detenerlo.');
          setTimeout(ocultarAviso, 4000);
        }
      });
    }
  }

  volverAEnlazar();
  pintarGrafico();

  // Al volver de otra pestana los datos pueden estar viejos: se refresca.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) cargar();
  });
})();
