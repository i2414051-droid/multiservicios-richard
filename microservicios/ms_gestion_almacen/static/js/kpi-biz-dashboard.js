/*
  Panel de KPIs de Ventas y Almacen: cambio de periodo sin recargar, refresco.

  Decision de diseno: al cambiar de periodo NO se reconstruye el panel en
  JavaScript. Se vuelve a pedir ESTE MISMO FRAGMENTO al servidor y se
  sustituye el HTML. El servidor sigue siendo el unico que pinta los datos.

  El fragmento se pide a su propio endpoint, jamas a la pagina que lo aloja.
*/
(function () {
  'use strict';

  var panel = document.getElementById('kpiBizPanel');
  if (!panel) return;

  var urlFragmento = panel.dataset.kpiUrl || '/api/kpi-biz/panel';

  var form = document.getElementById('kpiBizFiltros');
  var preset = document.getElementById('kpiBizPreset');
  var desde = document.getElementById('kpiBizDesde');
  var hasta = document.getElementById('kpiBizHasta');
  var botonActualizar = document.getElementById('kpiBizActualizar');
  var aviso = document.getElementById('kpiBizError');

  var cargando = false;
  var temporizador = null;
  var REFRESH_MS = 60000;

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
    if (preset && preset.value === 'personalizado') {
      if (desde && desde.value.trim()) p.set('desde', desde.value.trim());
      if (hasta && hasta.value.trim()) p.set('hasta', hasta.value.trim());
    }
    return p;
  }

  function aplicarParametrosEnUrl(p) {
    var params = new URLSearchParams(window.location.search);

    if (p.get('preset') !== 'personalizado') {
      params.delete('desde');
      params.delete('hasta');
    }
    p.forEach(function (valor, clave) { params.set(clave, valor); });

    var qs = params.toString();
    window.history.replaceState(
      { kpiBiz: true },
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
        var nuevo = doc.getElementById('kpiBizPanel');
        if (!nuevo) throw new Error('El servidor no devolvio el panel');

        panel.innerHTML = nuevo.innerHTML;

        form = document.getElementById('kpiBizFiltros');
        preset = document.getElementById('kpiBizPreset');
        desde = document.getElementById('kpiBizDesde');
        hasta = document.getElementById('kpiBizHasta');
        botonActualizar = document.getElementById('kpiBizActualizar');
        aviso = document.getElementById('kpiBizError');

        volverAEnlazar();
        aplicarParametrosEnUrl(p);
      })
      .catch(function (err) {
        mostrarAviso('No se pudo actualizar el panel: ' + err.message +
                     '. Los datos mostrados son los de la ultima carga correcta.');
      })
      .then(function () {
        marcarCarga(false);
      });
  }

  function alternarRefresco(activo) {
    if (temporizador) {
      clearInterval(temporizador);
      temporizador = null;
    }
    if (activo) {
      temporizador = setInterval(cargar, REFRESH_MS);
    }
  }

  function volverAEnlazar() {
    if (preset) {
      preset.addEventListener('change', function () {
        if (preset.value !== 'personalizado' && desde && hasta) {
          desde.value = '';
          hasta.value = '';
        }
        cargar();
      });
    }

    if (form) {
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        cargar();
      });
    }

    if (botonActualizar) {
      botonActualizar.addEventListener('click', function () {
        botonActualizar.classList.add('kpi-biz-actualizando');
        cargar();
        setTimeout(function () {
          var b = document.getElementById('kpiBizActualizar');
          if (b) b.classList.remove('kpi-biz-actualizando');
        }, 900);
      });

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

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) cargar();
  });
})();