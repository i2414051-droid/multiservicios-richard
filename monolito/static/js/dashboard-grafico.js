/* Reporte circular del dashboard (tiempo real).
 *
 * Solo pinta lo que entrega /api/dashboard/grafico: el servidor hace el
 * GROUP BY y aqui no se calcula ningun total sobre ventas.
 */
(function () {
  'use strict';

  var raiz = document.getElementById('graficoVentas');
  if (!raiz || typeof Chart === 'undefined') return;

  var URL_DATOS = raiz.dataset.url;
  var REFRESCO_MS = 30000;

  var elCanvas = document.getElementById('lienzoGraficoVentas');
  var elVacio = document.getElementById('grafVacio');
  var elLeyenda = document.getElementById('grafLeyenda');
  var elError = document.getElementById('grafError');
  var elActualizado = document.getElementById('grafActualizado');
  var elRefrescar = document.getElementById('grafRefrescar');
  var elPor = document.getElementById('grafPor');
  var elPagos = document.getElementById('grafPagos');
  var elPagoTopCaja = document.getElementById('grafPagoTopCaja');
  var elPagoTop = document.getElementById('grafPagoTop');
  var elPagoTopDetalle = document.getElementById('grafPagoTopDetalle');
  var elPagoBajoCaja = document.getElementById('grafPagoBajoCaja');
  var elPagoBajo = document.getElementById('grafPagoBajo');
  var elPagoBajoDetalle = document.getElementById('grafPagoBajoDetalle');
  var elReclamosTotal = document.getElementById('grafReclamosTotal');
  var elReclamosPendientes = document.getElementById('grafReclamosPendientes');
  var elReclamosVencidas = document.getElementById('grafReclamosVencidas');
  var elReclamosRespondidas = document.getElementById('grafReclamosRespondidas');
  var botonesRango = raiz.querySelectorAll('[data-graf-rango]');
  var botonesMedida = raiz.querySelectorAll('[data-graf-medida]');

  var estado = {
    rango: '30d',
    por: 'estado',
    medida: 'pedidos',
    datos: null,
    grafico: null
  };
  var ultimaCarga = 0;
  var cargando = false;
  var refresco = null;

  function cssVar(nombre) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(nombre).trim() || '#888';
  }

  function soles(valor) {
    return 'S/ ' + Number(valor || 0).toLocaleString('es-PE', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function valorDe(item) {
    return estado.medida === 'soles' ? soles(item.monto) : item.cantidad;
  }

  /* Texto del centro del doughnut: refleja lo que suma el grafico. */
  var pluginCentro = {
    id: 'centroGrafico',
    afterDraw: function (chart) {
      var datos = estado.datos;
      var area = chart.chartArea;
      if (!datos || !area) return;
      var total = datos.valores.reduce(function (a, b) { return a + b; }, 0);
      var cx = (area.left + area.right) / 2;
      var cy = (area.top + area.bottom) / 2;
      var ctx = chart.ctx;
      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = cssVar('--text-primary');
      ctx.font = '700 24px Montserrat, sans-serif';
      var texto = estado.medida === 'soles'
        ? soles(total)
        : String(total);
      ctx.fillText(texto, cx, cy - 8);
      ctx.fillStyle = cssVar('--text-muted');
      ctx.font = '600 11px Montserrat, sans-serif';
      ctx.fillText(estado.medida === 'soles' ? 'en ventas' : 'pedidos', cx, cy + 16);
      ctx.restore();
    }
  };

  function pintarLeyenda(datos) {
    elLeyenda.textContent = '';
    datos.detalle.forEach(function (item) {
      var boton = document.createElement('button');
      boton.type = 'button';
      boton.className = 'graf-item';

      var punto = document.createElement('span');
      punto.className = 'graf-punto';
      punto.style.background = item.color;

      var nombre = document.createElement('span');
      nombre.className = 'nombre';
      nombre.textContent = item.etiqueta;

      var valor = document.createElement('span');
      valor.className = 'valor';
      valor.textContent = valorDe(item);

      var pct = document.createElement('span');
      pct.className = 'pct';
      pct.textContent = item.porcentaje + '%';

      boton.appendChild(punto);
      boton.appendChild(nombre);
      boton.appendChild(valor);
      boton.appendChild(pct);

      if (item.url) {
        boton.title = 'Ver ' + item.etiqueta + ' en el detalle';
        boton.addEventListener('click', function () {
          window.location.href = item.url;
        });
      } else {
        boton.disabled = true;
        boton.style.cursor = 'default';
      }
      elLeyenda.appendChild(boton);
    });
  }

  function pintarKpis(kpis) {
    document.getElementById('grafKpiTotal').textContent = kpis.total;
    document.getElementById('grafKpiMonto').textContent = soles(kpis.monto);
    document.getElementById('grafKpiTicket').textContent = soles(kpis.ticket);
    document.getElementById('grafKpiCancel').textContent = kpis.pct_cancel + '%';
  }

  /* Ranking de metodos de pago: extremos y barras comparativas. */
  function pintarPagos(pagos) {
    var lista = (pagos && pagos.lista) || [];
    var top = pagos && pagos.top;
    var bajo = pagos && pagos.bajo;

    function extremo(caja, nombreEl, detalleEl, item, titulo) {
      if (!item) {
        nombreEl.textContent = '—';
        detalleEl.textContent = 'Sin ventas en el periodo';
        caja.disabled = true;
        caja.onclick = null;
        caja.removeAttribute('title');
        return;
      }
      nombreEl.textContent = item.etiqueta;
      detalleEl.textContent = item.cantidad +
        (item.cantidad === 1 ? ' compra' : ' compras') + ' · ' + soles(item.monto);
      caja.disabled = false;
      caja.onclick = function () {
        if (item.url) window.location.href = item.url;
      };
      caja.title = titulo + ': ' + item.etiqueta +
        ' (' + item.cantidad + ' compras)';
    }

    extremo(elPagoTopCaja, elPagoTop, elPagoTopDetalle, top, 'Método más usado');
    extremo(elPagoBajoCaja, elPagoBajo, elPagoBajoDetalle, bajo, 'Método menos usado');

    elPagos.textContent = '';
    if (!lista.length) {
      var vacio = document.createElement('p');
      vacio.className = 'graf-actualizado';
      vacio.textContent = 'Sin ventas en el periodo.';
      elPagos.appendChild(vacio);
      return;
    }

    var mayor = lista[0].cantidad || 1;
    lista.forEach(function (item) {
      var fila = document.createElement('div');
      fila.className = 'graf-pago-fila';

      var nombre = document.createElement('span');
      nombre.textContent = item.etiqueta;

      var barra = document.createElement('div');
      barra.className = 'graf-pago-barra';
      var relleno = document.createElement('span');
      relleno.style.width = Math.max(4, Math.round(item.cantidad * 100 / mayor)) + '%';
      relleno.style.background = item.color || '#f39c12';
      barra.appendChild(relleno);

      var cantidad = document.createElement('b');
      cantidad.textContent = item.cantidad;

      fila.appendChild(nombre);
      fila.appendChild(barra);
      fila.appendChild(cantidad);
      fila.title = item.etiqueta + ': ' + item.cantidad +
        ' compras (' + soles(item.monto) + ')';
      elPagos.appendChild(fila);
    });
  }

  function pintarReclamaciones(reclamos) {
    if (!reclamos) return;
    elReclamosTotal.textContent = reclamos.total;
    elReclamosPendientes.textContent = reclamos.pendientes;
    elReclamosVencidas.textContent = reclamos.vencidas;
    elReclamosRespondidas.textContent = reclamos.respondidas;
  }

  function pintarGrafico(datos) {
    if (estado.grafico) {
      estado.grafico.destroy();
      estado.grafico = null;
    }

    var sinDatos = !datos.valores.length || datos.valores.every(function (v) { return !v; });
    elVacio.hidden = !sinDatos;
    if (sinDatos) return;

    var borde = cssVar('--bg-card');
    var texto = cssVar('--text-primary');

    estado.grafico = new Chart(elCanvas, {
      type: 'doughnut',
      data: {
        labels: datos.etiquetas,
        datasets: [{
          data: datos.valores,
          backgroundColor: datos.colores,
          borderColor: borde,
          borderWidth: 3,
          hoverOffset: 8
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '68%',
        layout: { padding: 6 },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(20,20,20,.92)',
            titleColor: '#fff',
            bodyColor: '#eee',
            padding: 10,
            callbacks: {
              label: function (ctx) {
                var item = datos.detalle[ctx.dataIndex];
                if (!item) return '';
                return ' ' + item.etiqueta + ': ' + valorDe(item) +
                  ' (' + item.porcentaje + '%)';
              }
            }
          }
        },
        onClick: function (evt, elementos) {
          if (!elementos.length) return;
          var item = datos.detalle[elementos[0].index];
          if (item && item.url) window.location.href = item.url;
        },
        onHover: function (evt, elementos) {
          if (evt.native && evt.native.target) {
            evt.native.target.style.cursor = elementos.length ? 'pointer' : 'default';
          }
        }
      },
      plugins: [pluginCentro]
    });

    // El color de texto cambia con el tema claro/oscuro.
    if (estado.grafico.options.plugins.tooltip) {
      estado.grafico.options.plugins.tooltip.titleColor = texto;
    }
  }

  function textoActualizado() {
    if (!ultimaCarga) return;
    var segundos = Math.round((Date.now() - ultimaCarga) / 1000);
    elActualizado.textContent = segundos < 2
      ? 'Actualizado ahora'
      : 'Actualizado hace ' + segundos + ' s';
  }

  function cargar() {
    if (cargando) return;
    cargando = true;
    elRefrescar.disabled = true;

    var params = new URLSearchParams({
      rango: estado.rango,
      por: estado.por,
      medida: estado.medida
    });

    fetch(URL_DATOS + '?' + params.toString(), {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Accept': 'application/json' }
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      })
      .then(function (datos) {
        if (!datos || !datos.ok) throw new Error('respuesta invalida');
        estado.datos = datos;
        pintarKpis(datos.kpis);
        pintarPagos(datos.pagos);
        pintarReclamaciones(datos.reclamaciones);
        pintarLeyenda(datos);
        pintarGrafico(datos);
        elError.hidden = true;
        ultimaCarga = Date.now();
        textoActualizado();
      })
      .catch(function () {
        // Se conserva el ultimo grafico bueno y se avisa del fallo.
        elError.hidden = false;
      })
      .then(function () {
        cargando = false;
        elRefrescar.disabled = false;
      });
  }

  function marcarActivo(lista, boton, atributo) {
    Array.prototype.forEach.call(lista, function (b) {
      b.classList.toggle('activo', b === boton);
      b.setAttribute('aria-pressed', b === boton ? 'true' : 'false');
    });
    estado[atributo] = boton.dataset[atributo === 'rango' ? 'grafRango' : 'grafMedida'];
    cargar();
  }

  Array.prototype.forEach.call(botonesRango, function (boton) {
    boton.setAttribute('aria-pressed', boton.classList.contains('activo') ? 'true' : 'false');
    boton.addEventListener('click', function () {
      marcarActivo(botonesRango, boton, 'rango');
    });
  });

  Array.prototype.forEach.call(botonesMedida, function (boton) {
    boton.setAttribute('aria-pressed', boton.classList.contains('activo') ? 'true' : 'false');
    boton.addEventListener('click', function () {
      marcarActivo(botonesMedida, boton, 'medida');
    });
  });

  elPor.addEventListener('change', function () {
    estado.por = elPor.value;
    cargar();
  });

  elRefrescar.addEventListener('click', cargar);

  // Las pestañas en segundo plano no consultan la base: se reanuda al volver.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && Date.now() - ultimaCarga > REFRESCO_MS) cargar();
  });

  refresco = setInterval(function () {
    if (!document.hidden) cargar();
  }, REFRESCO_MS);

  setInterval(textoActualizado, 1000);

  // Re-render al cambiar el tema para refrescar los colores del lienzo.
  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (estado.datos) {
        pintarLeyenda(estado.datos);
        pintarGrafico(estado.datos);
      }
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme', 'class']
    });
  }

  cargar();
})();
