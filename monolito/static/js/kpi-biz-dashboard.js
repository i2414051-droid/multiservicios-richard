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
  var ultimaCarga = 0;
  // Refresco automatico cada 2 minutos. El servidor guarda el snapshot en
  // cache (KPI_CACHE_TTL), asi que casi todos los refrescos son instantaneos
  // y el recalculo real (~4-5 s) ocurre cada ~4 min, en segundo plano.
  var REFRESH_MS = 120000;

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

  function cargar(forzar) {
    if (cargando) return Promise.resolve();
    marcarCarga(true);
    ocultarAviso();

    var p = parametros();
    // `forzar` pide al servidor que ignore la cache. Se anade solo a la
    // peticion: la URL visible no debe quedar con ese parametro.
    var url = urlFragmento + '?' + p.toString() + (forzar ? '&forzar=1' : '');

    return fetch(url, {
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
        ultimaCarga = Date.now();

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
      temporizador = setInterval(function () {
        // Con la pestana en segundo plano no se pide nada: puede haber
        // muchas pestanas abiertas y no hay que gastar cuota de la BD.
        if (document.hidden) return;
        cargar(false);
      }, REFRESH_MS);
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
        // Pulsacion manual: se piden datos frescos y se salta la cache.
        cargar(true);
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
          mostrarAviso('Se actualizara cada ' + Math.round(REFRESH_MS / 1000) +
                       ' s. Doble clic para detenerlo.');
          setTimeout(ocultarAviso, 4000);
        }
      });
    }

    enlazarIncidencias();
  }

  /* ───────────────────────────────────────────
     REGISTRO DE INCIDENCIAS

     La API existed (/api/kpi-biz/incidencia) pero no habia ninguna pantalla
     que la llamara, asi que la tabla kpi_incidencias se quedaba vacia para
     siempre y los indicadores de devoluciones, mermas y motivos de abandono
     nunca tenian nada que medir.
     ─────────────────────────────────────────── */
  var ETIQUETAS_MOTIVO = {
    'incompatibilidad': 'Incompatibilidad con el equipo / repuesto',
    'producto_defectuoso': 'Producto defectuoso',
    'envio_equivocado': 'Se envio otro producto',
    'producto_equivocado': 'Producto equivocado',
    'cantidad_equivocada': 'Cantidad equivocada',
    'producto_danado': 'Producto danado en el traslado',
    'direccion_incorrecta': 'Direccion incorrecta',
    'paquete_perdido': 'Paquete perdido',
    'retraso_paqueteria': 'Retraso de la paqueteria',
    'dano_interno': 'Dano interno',
    'ruptura': 'Ruptura',
    'vencimiento': 'Vencimiento',
    'robo': 'Robo',
    'incertidumbre': 'Incertidumbre / dudas tecnicas',
    'precio': 'Precio',
    'envio': 'Costo o tiempo de envio',
    'proceso_largo': 'Proceso de pago largo',
    'plazo_entrega': 'Plazo de entrega',
    'proveedor_elegido': 'Proveedor elegido',
    'cambio_opinion': 'Cambio de opinion',
    'mejor_precio': 'Encontro mejor precio',
    'demora_entrega': 'Demora en la entrega',
    'error_pedido': 'Error en el pedido',
    'otro': 'Otro'
  };

  function enlazarIncidencias() {
    var formInc = document.getElementById('kpiBizIncidenciaForm');
    if (formInc) {
      var selTipo = document.getElementById('kpiIncTipo');
      var selMotivo = document.getElementById('kpiIncMotivo');
      var estado = document.getElementById('kpiIncEstado');
      var boton = document.getElementById('kpiIncGuardar');

      if (selTipo && selMotivo) {
        selTipo.addEventListener('change', function () {
          var opcion = selTipo.options[selTipo.selectedIndex];
          var motivos = [];
          if (opcion && opcion.dataset.motivos) {
            try { motivos = JSON.parse(opcion.dataset.motivos); } catch (e) { motivos = []; }
          }
          selMotivo.innerHTML = '';
          if (!motivos.length) {
            var vacia = document.createElement('option');
            vacia.value = 'otro';
            vacia.textContent = 'Otro';
            selMotivo.appendChild(vacia);
          } else {
            motivos.forEach(function (m) {
              var o = document.createElement('option');
              o.value = m;
              o.textContent = ETIQUETAS_MOTIVO[m] || m;
              selMotivo.appendChild(o);
            });
          }
          selMotivo.disabled = false;
        });
      }

      formInc.addEventListener('submit', function (ev) {
        ev.preventDefault();
        if (!selTipo || !selTipo.value) return;

        var cuerpo = {
          tipo: selTipo.value,
          motivo: (document.getElementById('kpiIncMotivo') || {}).value || 'otro',
          cantidad: parseInt((document.getElementById('kpiIncCantidad') || {}).value || '1', 10),
          venta_id: parseInt((document.getElementById('kpiIncVenta') || {}).value || '0', 10) || null,
          producto_id: parseInt((document.getElementById('kpiIncProducto') || {}).value || '0', 10) || null,
          referencia: (document.getElementById('kpiIncReferencia') || {}).value || '',
          nota: (document.getElementById('kpiIncNota') || {}).value || ''
        };

        if (boton) boton.disabled = true;
        if (estado) estado.textContent = 'Guardando...';

        fetch('/api/kpi-biz/incidencia', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          credentials: 'same-origin',
          body: JSON.stringify(cuerpo)
        })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
          .then(function (res) {
            if (!res.ok || !res.d.ok) throw new Error(res.d.error || 'error desconocido');
            formInc.reset();
            var selM = document.getElementById('kpiIncMotivo');
            if (selM) { selM.disabled = true; selM.innerHTML = '<option value="">Primero elige el tipo</option>'; }
            // El panel se recarga entero, asi que el mensaje de confirmacion
            // se pone DESPUES: si se pone antes, el refresco lo borra.
            return cargar();
          })
          .then(function () {
            var avisoInc = document.getElementById('kpiIncEstado');
            if (avisoInc) avisoInc.textContent = 'Incidencia registrada';
          })
          .catch(function (err) {
            var avisoErr = document.getElementById('kpiIncEstado');
            if (avisoErr) avisoErr.textContent = 'No se pudo registrar: ' + err.message;
          })
          .then(function () {
            var b = document.getElementById('kpiIncGuardar');
            if (b) b.disabled = false;
          });
      });
    }

    var botones = document.querySelectorAll('.kpi-biz-borrar');
    Array.prototype.forEach.call(botones, function (btn) {
      btn.addEventListener('click', function () {
        var id = btn.getAttribute('data-incidencia');
        if (!id) return;
        if (!window.confirm('Eliminar la incidencia #' + id + '?')) return;
        btn.disabled = true;
        fetch('/api/kpi-biz/incidencia/' + id, {
          method: 'DELETE',
          headers: { 'X-Requested-With': 'fetch' },
          credentials: 'same-origin'
        })
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (!res.ok) throw new Error(res.error || 'no se pudo eliminar');
            cargar();
          })
          .catch(function (err) {
            mostrarAviso('No se pudo eliminar la incidencia: ' + err.message);
            btn.disabled = false;
          });
      });
    });
  }

  volverAEnlazar();

  // El refresco automatico se arranca al cargar. Antes solo arrancaba con un
  // doble clic en "Actualizar", asi que el panel se quedaba congelado con los
  // datos del momento en que se abrio: los indicadores no se veian en vivo.
  alternarRefresco(true);

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    // Al volver a la pestana solo se recarga si los datos ya tienen un rato:
    // cambiar de ventana no debe disparar una peticion cada vez.
    if (Date.now() - ultimaCarga > 60000) cargar(false);
  });
})();