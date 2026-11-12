"""OpenAPI 3.0 para la API v1.

El spec se genera a partir de las rutas registradas en Flask y de la metadata
que cada vista declara con `@doc(...)`. No hay un catalogo escrito a mano que
se pueda desincronizar: si una ruta existe, aparece.
"""
import re

from apispec import APISpec
from flask import Response, jsonify

_PREFIJO = '/api/v1'
_RUTAS_INTERNAS = ('/api/v1/openapi.json', '/api/v1/docs')


def doc(summary, *, tags=None, auth=False, request=None, responses=None,
        description=None, success_status=200, response_model=None,
        parameters=None, request_multipart=None):
    """Adjunta metadata OpenAPI a una vista.

    `request` describe un cuerpo JSON; `request_multipart` describe un
    `multipart/form-data` con la forma
    `[{'name': 'imagenes', 'type': 'file', 'array': True, 'required': True}]`.
    """
    def decorador(fn):
        fn.__api_doc__ = {
            'summary': summary,
            'tags': tags or [],
            'auth': auth,
            'request': request,
            'request_multipart': request_multipart,
            'responses': responses or {200: 'OK'},
            'description': description,
            'success_status': success_status,
            'response_model': response_model,
            'parameters': parameters or [],
        }
        return fn
    return decorador


def _parametros_de_ruta(path):
    parametros = []
    for nombre in re.findall(r'{([^}]+)}', path):
        tipo = 'integer' if nombre in (
            'id', 'venta_id', 'producto_id', 'user_id', 'entrega_id',
            'ingreso_id', 'salida_id', 'incidencia_id', 'imagen_id',
            'proveedor_id', 'pedido_id') else 'string'
        parametros.append({
            'name': nombre,
            'in': 'path',
            'required': True,
            'schema': {'type': tipo},
        })
    return parametros


def _operacion(meta, nombre_vista):
    respuestas = {}
    for estado, descripcion in (meta.get('responses') or {200: 'OK'}).items():
        respuestas[str(estado)] = {'description': descripcion}

    modelo = meta.get('response_model')
    if modelo:
        estado = str(meta.get('success_status', 200))
        respuestas.setdefault(estado, {'description': 'OK'})
        # Todas las respuestas de la API comparten el sobre {ok, data, meta}.
        # `response_model` describe el contenido de `data` (objeto o array).
        respuestas[estado]['content'] = {
            'application/json': {
                'schema': {
                    'type': 'object',
                    'required': ['ok', 'data'],
                    'properties': {
                        'ok': {'type': 'boolean', 'example': True},
                        'data': {
                            '$ref': '#/components/schemas/%s' % modelo,
                        },
                        'meta': {
                            'type': 'object',
                            'nullable': True,
                            'additionalProperties': True,
                        },
                    },
                }
            }
        }

    operacion = {
        'summary': meta.get('summary') or nombre_vista,
        'tags': meta.get('tags') or [],
        'responses': respuestas,
    }
    if meta.get('description'):
        operacion['description'] = meta['description']
    if meta.get('auth'):
        operacion['security'] = [{'bearerAuth': []}]
    if meta.get('request'):
        operacion['requestBody'] = {
            'required': True,
            'content': {
                'application/json': {
                    'schema': {'$ref': '#/components/schemas/%s' % meta['request']}
                }
            },
        }
    if meta.get('request_multipart'):
        propiedades = {}
        requeridos = []
        for campo in meta['request_multipart']:
            nombre = campo['name']
            if campo.get('type') == 'file':
                esquema = {'type': 'string', 'format': 'binary'}
                if campo.get('array'):
                    esquema = {'type': 'array',
                               'items': {'type': 'string', 'format': 'binary'}}
            else:
                esquema = {'type': campo.get('type', 'string')}
            if campo.get('description'):
                esquema['description'] = campo['description']
            propiedades[nombre] = esquema
            if campo.get('required'):
                requeridos.append(nombre)
        esquema_cuerpo = {'type': 'object', 'properties': propiedades}
        if requeridos:
            esquema_cuerpo['required'] = requeridos
        operacion['requestBody'] = {
            'required': True,
            'content': {'multipart/form-data': {'schema': esquema_cuerpo}},
        }
    return operacion


def construir_spec(app):
    spec = APISpec(
        title='Multiservicios Richard API',
        version='1.0.0',
        openapi_version='3.0.3',
        info={
            'description': (
                'API JSON del monolito Multiservicios Richard. Autenticacion '
                'con JWT: envia `Authorization: Bearer <access_token>`. '
                'La web sigue usando cookie de sesion y CSRF; esta API no.'
            ),
        },
        servers=[{'url': '/'}],
    )
    spec.components.security_scheme(
        'bearerAuth',
        {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'JWT'},
    )

    spec.components.schema('Error', {
        'type': 'object',
        'properties': {
            'ok': {'type': 'boolean', 'example': False},
            'error': {
                'type': 'object',
                'properties': {
                    'code': {'type': 'string', 'example': 'VALIDACION'},
                    'message': {'type': 'string'},
                    'details': {'type': 'object', 'nullable': True},
                },
            },
        },
    })
    spec.components.schema('Usuario', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'correo': {'type': 'string', 'format': 'email'},
            'rol': {'type': 'string', 'enum': ['cliente', 'admin', 'administrador', 'superadmin']},
            'nombre': {'type': 'string'},
            'apellido': {'type': 'string'},
            'telefono': {'type': 'string'},
            'documento_tipo': {'type': 'string'},
            'documento_numero': {'type': 'string'},
            'estado': {'type': 'string'},
            'created_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
        },
    })
    spec.components.schema('TokenPair', {
        'type': 'object',
        'properties': {
            'access_token': {'type': 'string'},
            'refresh_token': {'type': 'string'},
            'token_type': {'type': 'string', 'example': 'Bearer'},
            'expires_in': {'type': 'integer', 'example': 1800},
            'usuario': {'$ref': '#/components/schemas/Usuario'},
        },
    })
    spec.components.schema('LoginIn', {
        'type': 'object',
        'required': ['correo', 'password'],
        'properties': {
            'correo': {'type': 'string', 'format': 'email'},
            'password': {'type': 'string', 'format': 'password'},
            'device_id': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('RegistroIn', {
        'type': 'object',
        'required': ['correo', 'password', 'nombres', 'apellidos'],
        'properties': {
            'correo': {'type': 'string', 'format': 'email'},
            'password': {'type': 'string', 'format': 'password'},
            'nombres': {'type': 'string'},
            'apellidos': {'type': 'string'},
            'documento_tipo': {'type': 'string', 'enum': ['dni', 'ruc'], 'default': 'dni'},
            'documento_numero': {'type': 'string'},
            'telefono': {'type': 'string'},
            'recaptcha_token': {'type': 'string', 'nullable': True},
            'device_id': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('ActualizarPerfilIn', {
        'type': 'object',
        'properties': {
            'nombre': {'type': 'string', 'nullable': True},
            'apellido': {'type': 'string', 'nullable': True},
            'telefono': {'type': 'string', 'nullable': True},
            'documento_tipo': {'type': 'string', 'nullable': True},
            'documento_numero': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('CambiarPasswordIn', {
        'type': 'object',
        'required': ['actual', 'nueva', 'confirmar'],
        'properties': {
            'actual': {'type': 'string', 'format': 'password'},
            'nueva': {'type': 'string', 'format': 'password'},
            'confirmar': {'type': 'string', 'format': 'password'},
        },
    })

    # ── Catalogo ──
    spec.components.schema('Categoria', {
        'type': 'object',
        'properties': {
            'nombre': {'type': 'string', 'example': 'Herramientas'},
            'total_productos': {'type': 'integer', 'example': 12},
            'disponibles': {'type': 'integer', 'example': 10},
        },
    })
    spec.components.schema('CategoriaLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/Categoria'},
    })
    spec.components.schema('Producto', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer', 'example': 1},
            'nombre': {'type': 'string'},
            'descripcion': {'type': 'string'},
            'precio': {'type': 'string', 'example': '12.50'},
            'precio_centimos': {'type': 'integer', 'example': 1250},
            'stock': {'type': 'integer', 'example': 8},
            'categoria': {'type': 'string'},
            'disponible': {'type': 'boolean'},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagenes': {
                'type': 'array',
                'items': {'type': 'string', 'format': 'uri'},
            },
            'imagenes_thumb': {
                'type': 'array',
                'items': {'type': 'string', 'format': 'uri'},
            },
            'imagenes_extra': {
                'type': 'array',
                'items': {'type': 'string', 'format': 'uri'},
            },
            'created_at': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
        },
    })
    spec.components.schema('ProductoLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/Producto'},
    })
    spec.components.schema('ImagenesProducto', {
        'type': 'object',
        'properties': {
            'producto_id': {'type': 'integer'},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri',
                                'nullable': True},
            'imagenes': {
                'type': 'array',
                'items': {'type': 'string', 'format': 'uri'},
            },
            'imagenes_thumb': {
                'type': 'array',
                'items': {'type': 'string', 'format': 'uri'},
            },
        },
    })

    # ── Carrito ──
    spec.components.schema('ItemCarrito', {
        'type': 'object',
        'properties': {
            'item_id': {'type': 'integer'},
            'producto_id': {'type': 'integer'},
            'nombre': {'type': 'string'},
            'categoria': {'type': 'string'},
            'precio': {'type': 'string', 'example': '12.50'},
            'precio_centimos': {'type': 'integer'},
            'cantidad': {'type': 'integer'},
            'subtotal': {'type': 'string', 'example': '25.00'},
            'subtotal_centimos': {'type': 'integer'},
            'stock': {'type': 'integer'},
            'maximo': {'type': 'integer'},
            'disponible': {'type': 'boolean'},
            'stock_suficiente': {'type': 'boolean'},
            'puede_comprar': {'type': 'boolean'},
            'motivo_no_disponible': {'type': 'string', 'nullable': True,
                                     'enum': ['inactivo', 'sin_stock',
                                              'stock_insuficiente']},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri',
                                'nullable': True},
            'agregado_en': {'type': 'string', 'format': 'date-time',
                            'nullable': True},
        },
    })
    spec.components.schema('EnvioCarrito', {
        'type': 'object',
        'properties': {
            'costo': {'type': 'string', 'example': '15.00'},
            'costo_centimos': {'type': 'integer', 'example': 1500},
            'gratis_desde': {'type': 'string', 'example': '300.00'},
            'gratis_desde_centimos': {'type': 'integer', 'example': 30000},
            'gratis': {'type': 'boolean'},
            'minimo_delivery': {'type': 'string', 'example': '50.00'},
            'minimo_delivery_centimos': {'type': 'integer', 'example': 5000},
            'elegible_delivery': {'type': 'boolean'},
            'costo_estimado': {'type': 'string', 'example': '15.00'},
            'costo_estimado_centimos': {'type': 'integer', 'example': 1500},
            'distritos': {
                'type': 'array',
                'items': {'type': 'string'},
            },
        },
    })
    spec.components.schema('Carrito', {
        'type': 'object',
        'properties': {
            'items': {
                'type': 'array',
                'items': {'$ref': '#/components/schemas/ItemCarrito'},
            },
            'cantidad_items': {'type': 'integer'},
            'lineas': {'type': 'integer'},
            'total': {'type': 'string', 'example': '25.00'},
            'total_centimos': {'type': 'integer'},
            'total_disponible': {'type': 'string', 'example': '25.00'},
            'total_disponible_centimos': {'type': 'integer'},
            'hay_no_disponibles': {'type': 'boolean'},
            'envio': {'$ref': '#/components/schemas/EnvioCarrito'},
            'total_con_envio': {'type': 'string', 'example': '40.00'},
            'total_con_envio_centimos': {'type': 'integer'},
            'moneda': {'type': 'string', 'example': 'PEN'},
        },
    })
    spec.components.schema('ResumenCarrito', {
        'type': 'object',
        'properties': {
            'cantidad_items': {'type': 'integer'},
            'lineas': {'type': 'integer'},
            'total': {'type': 'string', 'example': '25.00'},
            'total_centimos': {'type': 'integer'},
            'total_disponible': {'type': 'string', 'example': '25.00'},
            'total_disponible_centimos': {'type': 'integer'},
            'hay_no_disponibles': {'type': 'boolean'},
            'envio': {'$ref': '#/components/schemas/EnvioCarrito'},
            'total_con_envio': {'type': 'string', 'example': '40.00'},
            'total_con_envio_centimos': {'type': 'integer'},
            'moneda': {'type': 'string', 'example': 'PEN'},
        },
    })
    spec.components.schema('CarritoMutation', {
        'type': 'object',
        'properties': {
            'eliminado': {'type': 'boolean'},
            'item': {
                'nullable': True,
                'allOf': [{'$ref': '#/components/schemas/ItemCarrito'}],
            },
            'carrito': {'$ref': '#/components/schemas/Carrito'},
        },
    })
    spec.components.schema('CarritoVaciado', {
        'type': 'object',
        'properties': {
            'eliminados': {'type': 'integer'},
            'carrito': {'$ref': '#/components/schemas/Carrito'},
        },
    })
    spec.components.schema('AgregarItemIn', {
        'type': 'object',
        'required': ['producto_id'],
        'properties': {
            'producto_id': {'type': 'integer', 'minimum': 1},
            'cantidad': {'type': 'integer', 'minimum': 1, 'maximum': 500,
                         'default': 1},
        },
    })
    spec.components.schema('ActualizarItemIn', {
        'type': 'object',
        'required': ['cantidad'],
        'properties': {
            'cantidad': {'type': 'integer', 'minimum': 0, 'maximum': 500},
        },
    })

    # ── Configuracion de la tienda ──
    spec.components.schema('EnvioConfig', {
        'type': 'object',
        'properties': {
            'costo': {'type': 'string', 'example': '15.00'},
            'costo_centimos': {'type': 'integer', 'example': 1500},
            'gratis_desde': {'type': 'string', 'example': '300.00'},
            'gratis_desde_centimos': {'type': 'integer', 'example': 30000},
            'minimo_delivery': {'type': 'string', 'example': '50.00'},
            'minimo_delivery_centimos': {'type': 'integer', 'example': 5000},
            'distritos': {
                'type': 'array',
                'items': {'type': 'string'},
            },
        },
    })
    spec.components.schema('ConfigTienda', {
        'type': 'object',
        'properties': {
            'moneda': {'type': 'string', 'example': 'PEN'},
            'empresa': {
                'type': 'object',
                'properties': {
                    'direccion': {'type': 'string'},
                    'lat': {'type': 'number', 'format': 'float'},
                    'lon': {'type': 'number', 'format': 'float'},
                },
            },
            'envio': {'$ref': '#/components/schemas/EnvioConfig'},
            'categorias': {
                'type': 'array',
                'items': {'type': 'string'},
            },
        },
    })

    # ── Pedidos (Fase 2 sin cobro real) ──
    spec.components.schema('PedidoIn', {
        'type': 'object',
        'required': ['doc_numero', 'telefono'],
        'properties': {
            'doc_tipo': {'type': 'string', 'enum': ['dni', 'ruc'],
                         'default': 'dni'},
            'doc_numero': {'type': 'string'},
            'nombres': {'type': 'string', 'nullable': True},
            'apellido_paterno': {'type': 'string', 'nullable': True},
            'apellido_materno': {'type': 'string', 'nullable': True},
            'razon_social': {'type': 'string', 'nullable': True},
            'telefono': {'type': 'string'},
            'correo': {'type': 'string', 'nullable': True},
            'metodo_pago': {'type': 'string',
                            'enum': ['transferencia', 'contra_entrega'],
                            'default': 'contra_entrega'},
            'tipo_entrega': {'type': 'string', 'enum': ['recojo', 'delivery'],
                             'default': 'recojo'},
            'direccion': {'type': 'string', 'nullable': True},
            'distrito': {'type': 'string', 'nullable': True},
            'lat': {'type': 'number', 'format': 'float', 'nullable': True},
            'lon': {'type': 'number', 'format': 'float', 'nullable': True},
            'notas': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('CancelarIn', {
        'type': 'object',
        'required': ['motivo'],
        'properties': {
            'motivo': {'type': 'string',
                       'enum': ['cambio_opinion', 'mejor_precio',
                                'demora_entrega', 'error_pedido',
                                'producto_equivocado', 'otro']},
            'detalle': {'type': 'string', 'nullable': True, 'maxLength': 300},
        },
    })
    spec.components.schema('BoletaIn', {
        'type': 'object',
        'required': ['documento', 'nombre'],
        'properties': {
            'documento': {'type': 'string', 'example': '12345678'},
            'nombre': {'type': 'string', 'maxLength': 200},
        },
    })
    spec.components.schema('ItemPedido', {
        'type': 'object',
        'properties': {
            'producto_id': {'type': 'integer'},
            'nombre': {'type': 'string'},
            'descripcion': {'type': 'string'},
            'cantidad': {'type': 'integer'},
            'precio': {'type': 'string', 'example': '12.50'},
            'precio_centimos': {'type': 'integer'},
            'subtotal': {'type': 'string', 'example': '25.00'},
            'subtotal_centimos': {'type': 'integer'},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri',
                                'nullable': True},
        },
    })
    spec.components.schema('SeguimientoPedido', {
        'type': 'object',
        'properties': {
            'estado': {'type': 'string'},
            'direccion': {'type': 'string'},
            'distrito': {'type': 'string'},
            'tipo_entrega': {'type': 'string'},
            'fecha_estimada': {'type': 'string', 'format': 'date',
                               'nullable': True},
            'fecha_entrega': {'type': 'string', 'format': 'date-time',
                              'nullable': True},
            'notas': {'type': 'string'},
            'creado_en': {'type': 'string', 'format': 'date-time',
                          'nullable': True},
            'lat': {'type': 'number', 'format': 'float', 'nullable': True},
            'lon': {'type': 'number', 'format': 'float', 'nullable': True},
            'costo_envio': {'type': 'string', 'example': '15.00'},
            'costo_envio_centimos': {'type': 'integer'},
        },
    })
    spec.components.schema('Pedido', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'fecha': {'type': 'string', 'format': 'date-time', 'nullable': True},
            'estado': {'type': 'string', 'example': 'en espera'},
            'estado_pago': {'type': 'string', 'example': 'pendiente'},
            'metodo_pago': {'type': 'string', 'example': 'transferencia'},
            'moneda': {'type': 'string', 'example': 'PEN'},
            'subtotal': {'type': 'string', 'example': '100.00'},
            'subtotal_centimos': {'type': 'integer'},
            'costo_envio': {'type': 'string', 'example': '15.00'},
            'costo_envio_centimos': {'type': 'integer'},
            'total': {'type': 'string', 'example': '115.00'},
            'total_centimos': {'type': 'integer'},
            'cliente': {'type': 'object', 'additionalProperties': True},
            'documento': {'type': 'object', 'additionalProperties': True},
            'entrega': {'type': 'object', 'additionalProperties': True},
            'items': {
                'type': 'array',
                'items': {'$ref': '#/components/schemas/ItemPedido'},
            },
            'seguimiento': {
                'nullable': True,
                'allOf': [{'$ref': '#/components/schemas/SeguimientoPedido'}],
            },
            'cancelacion': {'type': 'object', 'nullable': True,
                            'additionalProperties': True},
        },
    })
    spec.components.schema('PedidoLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/Pedido'},
    })
    spec.components.schema('CheckoutValidacion', {
        'type': 'object',
        'properties': {
            'valido': {'type': 'boolean', 'example': True},
            'moneda': {'type': 'string', 'example': 'PEN'},
            'items': {
                'type': 'array',
                'items': {'$ref': '#/components/schemas/ItemCarrito'},
            },
            'subtotal': {'type': 'string', 'example': '100.00'},
            'subtotal_centimos': {'type': 'integer'},
            'costo_envio': {'type': 'string', 'example': '15.00'},
            'costo_envio_centimos': {'type': 'integer'},
            'total': {'type': 'string', 'example': '115.00'},
            'total_centimos': {'type': 'integer'},
            'metodo_pago': {'type': 'string'},
            'tipo_entrega': {'type': 'string'},
            'distrito': {'type': 'string'},
        },
    })
    spec.components.schema('CheckoutOpciones', {
        'type': 'object',
        'properties': {
            'moneda': {'type': 'string', 'example': 'PEN'},
            'carrito': {'$ref': '#/components/schemas/Carrito'},
            'usuario': {'type': 'object', 'additionalProperties': True},
            'direcciones': {
                'type': 'array',
                'items': {'type': 'object', 'additionalProperties': True},
            },
            'metodos_pago': {
                'type': 'array',
                'items': {'type': 'object', 'additionalProperties': True},
            },
            'entrega': {'type': 'object', 'additionalProperties': True},
        },
    })

    # ── Cuenta: direcciones (Fase 3) ──
    spec.components.schema('DireccionIn', {
        'type': 'object',
        'required': ['direccion'],
        'properties': {
            'direccion': {'type': 'string', 'maxLength': 300},
            'distrito': {'type': 'string', 'nullable': True, 'maxLength': 150},
            'referencia': {'type': 'string', 'nullable': True, 'maxLength': 300},
            'predeterminada': {'type': 'boolean', 'default': False},
        },
    })
    spec.components.schema('DireccionPatchIn', {
        'type': 'object',
        'properties': {
            'direccion': {'type': 'string', 'nullable': True, 'maxLength': 300},
            'distrito': {'type': 'string', 'nullable': True, 'maxLength': 150},
            'referencia': {'type': 'string', 'nullable': True, 'maxLength': 300},
            'predeterminada': {'type': 'boolean', 'nullable': True},
        },
    })
    spec.components.schema('Direccion', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'direccion': {'type': 'string'},
            'distrito': {'type': 'string'},
            'referencia': {'type': 'string'},
            'predeterminada': {'type': 'boolean'},
            'created_at': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
        },
    })
    spec.components.schema('DireccionLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/Direccion'},
    })
    spec.components.schema('DireccionEliminada', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'eliminado': {'type': 'boolean'},
        },
    })
    # ── Cuenta: reclamos (Fase 3) ──
    spec.components.schema('ReclamoIn', {
        'type': 'object',
        'required': ['consumidor_nombre'],
        'properties': {
            'tipo': {'type': 'string', 'enum': ['reclamo', 'queja'],
                     'default': 'reclamo'},
            'consumidor_nombre': {'type': 'string', 'maxLength': 200},
            'consumidor_documento_tipo': {'type': 'string',
                                          'enum': ['DNI', 'CE', 'PASAPORTE'],
                                          'default': 'DNI'},
            'consumidor_documento': {'type': 'string', 'maxLength': 20},
            'consumidor_domicilio': {'type': 'string', 'maxLength': 300},
            'consumidor_telefono': {'type': 'string', 'nullable': True,
                                    'maxLength': 20},
            'consumidor_correo': {'type': 'string', 'maxLength': 200},
            'bien_descripcion': {'type': 'string', 'maxLength': 500},
            'bien_monto': {'type': 'string', 'nullable': True,
                           'description': 'Monto en soles; acepta coma o punto.'},
            'detalle': {'type': 'string', 'maxLength': 2000},
            'pedido': {'type': 'string', 'maxLength': 1000},
        },
    })
    spec.components.schema('Reclamo', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'codigo': {'type': 'string', 'example': 'LR-2026-000001'},
            'token': {'type': 'string'},
            'tipo': {'type': 'string'},
            'estado': {'type': 'string', 'example': 'pendiente'},
            'fecha': {'type': 'string', 'format': 'date-time', 'nullable': True},
            'fecha_limite': {'type': 'string', 'format': 'date-time',
                             'nullable': True},
            'dias_restantes': {'type': 'integer', 'nullable': True},
            'vencido': {'type': 'boolean'},
            'consumidor': {'type': 'object', 'additionalProperties': True},
            'bien': {'type': 'object', 'additionalProperties': True},
            'detalle': {'type': 'string'},
            'pedido': {'type': 'string'},
            'respuesta': {'type': 'object', 'nullable': True,
                          'additionalProperties': True},
            'created_at': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
        },
    })
    spec.components.schema('ReclamoLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/Reclamo'},
    })

    # ── Admin: productos (Fase 4) ──
    spec.components.schema('ImagenProducto', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'url': {'type': 'string', 'format': 'uri', 'nullable': True},
            'url_thumb': {'type': 'string', 'format': 'uri', 'nullable': True},
            'orden': {'type': 'integer'},
        },
    })
    spec.components.schema('ProductoAdmin', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'nombre': {'type': 'string'},
            'descripcion': {'type': 'string'},
            'precio': {'type': 'string', 'example': '12.50'},
            'precio_centimos': {'type': 'integer'},
            'stock': {'type': 'integer'},
            'categoria': {'type': 'string'},
            'estado': {'type': 'string', 'enum': ['activo', 'inactivo']},
            'disponible': {'type': 'boolean'},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri',
                                'nullable': True},
            'imagenes': {'type': 'array',
                         'items': {'type': 'string', 'format': 'uri'}},
            'imagenes_thumb': {'type': 'array',
                               'items': {'type': 'string', 'format': 'uri'}},
            'imagenes_extra': {'type': 'array',
                               'items': {'type': 'string', 'format': 'uri'}},
            'imagenes_detalle': {'type': 'array',
                                 'items': {'$ref': '#/components/schemas/ImagenProducto'}},
            'created_at': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
        },
    })
    spec.components.schema('ProductoAdminLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/ProductoAdmin'},
    })
    spec.components.schema('ProductoAdminIn', {
        'type': 'object',
        'required': ['nombre', 'precio', 'stock'],
        'properties': {
            'nombre': {'type': 'string', 'maxLength': 200},
            'descripcion': {'type': 'string', 'nullable': True,
                            'maxLength': 2000},
            'precio': {'type': 'string', 'example': '12.50',
                       'description': 'Precio en soles (acepta numero o texto).'},
            'stock': {'type': 'integer', 'minimum': 0},
            'categoria': {'type': 'string', 'nullable': True, 'maxLength': 100,
                          'default': 'Otros'},
            'estado': {'type': 'string', 'enum': ['activo', 'inactivo'],
                       'default': 'activo'},
        },
    })
    spec.components.schema('ProductoAdminPatchIn', {
        'type': 'object',
        'properties': {
            'nombre': {'type': 'string', 'nullable': True, 'maxLength': 200},
            'descripcion': {'type': 'string', 'nullable': True,
                            'maxLength': 2000},
            'precio': {'type': 'string', 'nullable': True},
            'stock': {'type': 'integer', 'nullable': True, 'minimum': 0},
            'categoria': {'type': 'string', 'nullable': True, 'maxLength': 100},
            'estado': {'type': 'string', 'nullable': True,
                       'enum': ['activo', 'inactivo']},
        },
    })
    spec.components.schema('ProductoImagenSubida', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri',
                                'nullable': True},
            'imagenes': {'type': 'array',
                         'items': {'type': 'string', 'format': 'uri'}},
            'imagenes_thumb': {'type': 'array',
                               'items': {'type': 'string', 'format': 'uri'}},
            'imagenes_detalle': {
                'type': 'array',
                'items': {'$ref': '#/components/schemas/ImagenProducto'},
            },
        },
    })
    spec.components.schema('ProductoEliminado', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'estado': {'type': 'string', 'nullable': True},
            'eliminado': {'type': 'boolean'},
            'definitivo': {'type': 'boolean'},
        },
    })

    # ── Admin: ventas (Fase 4) ──
    spec.components.schema('VentaAdmin', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'fecha': {'type': 'string', 'format': 'date-time',
                      'nullable': True},
            'estado': {'type': 'string'},
            'estado_pago': {'type': 'string'},
            'metodo_pago': {'type': 'string'},
            'oculta': {'type': 'boolean'},
            'moneda': {'type': 'string', 'example': 'PEN'},
            'subtotal': {'type': 'string'},
            'subtotal_centimos': {'type': 'integer'},
            'costo_envio': {'type': 'string'},
            'costo_envio_centimos': {'type': 'integer'},
            'total': {'type': 'string'},
            'total_centimos': {'type': 'integer'},
            'cliente': {'type': 'object', 'additionalProperties': True},
            'documento': {'type': 'object', 'additionalProperties': True},
            'entrega': {'type': 'object', 'additionalProperties': True},
            'items': {'type': 'array',
                      'items': {'$ref': '#/components/schemas/ItemPedido'}},
            'cancelacion': {'type': 'object', 'nullable': True,
                            'additionalProperties': True},
        },
    })
    spec.components.schema('VentaAdminLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/VentaAdmin'},
    })
    spec.components.schema('VentaEstadoIn', {
        'type': 'object',
        'required': ['estado'],
        'properties': {
            'estado': {'type': 'string',
                       'enum': ['en espera', 'entregado', 'cancelado']},
        },
    })
    spec.components.schema('VentaVisibilidadIn', {
        'type': 'object',
        'required': ['oculta'],
        'properties': {'oculta': {'type': 'boolean'}},
    })
    spec.components.schema('VentasLimpiadas', {
        'type': 'object',
        'properties': {'eliminadas': {'type': 'integer'}},
    })

    # ── Admin: clientes y permisos (Fase 4) ──
    spec.components.schema('ClienteAdmin', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'correo': {'type': 'string', 'format': 'email'},
            'nombre': {'type': 'string'},
            'apellido': {'type': 'string'},
            'telefono': {'type': 'string'},
            'rol': {'type': 'string'},
            'documento_tipo': {'type': 'string'},
            'documento_numero': {'type': 'string'},
            'estado': {'type': 'string'},
            'estado_detalle': {'$ref': '#/components/schemas/EstadoCliente'},
            'num_compras': {'type': 'integer'},
            'total_compras': {'type': 'string'},
            'total_compras_centimos': {'type': 'integer'},
            'created_at': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
        },
    })
    spec.components.schema('ClienteAdminLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/ClienteAdmin'},
    })
    spec.components.schema('EstadoCliente', {
        'type': 'object',
        'properties': {
            'estado': {'type': 'string'},
            'nombre': {'type': 'string'},
            'color': {'type': 'string'},
        },
    })
    spec.components.schema('ClienteEstadoIn', {
        'type': 'object',
        'required': ['estado'],
        'properties': {
            'estado': {'type': 'string',
                       'enum': ['activo', 'inactivo', 'bloqueado',
                                'pendiente_verificacion', 'moroso']},
            'motivo': {'type': 'string', 'nullable': True, 'maxLength': 300},
        },
    })
    spec.components.schema('ClienteEstadoCambiado', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'correo': {'type': 'string'},
            'cambiado': {'type': 'boolean'},
            'anterior': {'type': 'string'},
            'estado': {'type': 'string'},
        },
    })
    spec.components.schema('HistorialEstado', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'anterior': {'type': 'string', 'nullable': True},
            'nuevo': {'type': 'string', 'nullable': True},
            'motivo': {'type': 'string'},
            'admin': {'type': 'string', 'nullable': True},
            'fecha': {'type': 'string', 'format': 'date-time',
                      'nullable': True},
        },
    })
    spec.components.schema('HistorialEstadoLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/HistorialEstado'},
    })
    spec.components.schema('UsuarioPermiso', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'correo': {'type': 'string', 'format': 'email'},
            'rol': {'type': 'string'},
            'estado': {'type': 'string'},
            'estado_detalle': {'$ref': '#/components/schemas/EstadoCliente'},
        },
    })
    spec.components.schema('UsuarioPermisoLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/UsuarioPermiso'},
    })
    spec.components.schema('RolIn', {
        'type': 'object',
        'required': ['rol'],
        'properties': {'rol': {'type': 'string', 'enum': ['admin', 'cliente']}},
    })
    spec.components.schema('RolActualizado', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'rol': {'type': 'string'},
        },
    })

    # ── Admin: proveedores y pedidos a proveedor (Fase 5) ──
    spec.components.schema('Proveedor', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'nombre': {'type': 'string'},
            'tipo_documento': {'type': 'string', 'enum': ['DNI', 'RUC']},
            'numero_documento': {'type': 'string'},
            'nombres': {'type': 'string'},
            'apellido_paterno': {'type': 'string'},
            'apellido_materno': {'type': 'string'},
            'celular': {'type': 'string'},
            'direccion': {'type': 'string'},
            'categoria': {'type': 'string'},
            'notas': {'type': 'string'},
            'created_at': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
        },
    })
    spec.components.schema('ProveedorLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/Proveedor'},
    })
    spec.components.schema('ProveedorIn', {
        'type': 'object',
        'properties': {
            'tipo_documento': {'type': 'string', 'enum': ['dni', 'ruc'],
                               'default': 'dni'},
            'numero_documento': {'type': 'string', 'nullable': True},
            'nombres': {'type': 'string', 'nullable': True},
            'apellido_paterno': {'type': 'string', 'nullable': True},
            'apellido_materno': {'type': 'string', 'nullable': True},
            'razon_social': {'type': 'string', 'nullable': True,
                             'description': 'Solo para RUC.'},
            'celular': {'type': 'string', 'nullable': True},
            'direccion': {'type': 'string', 'nullable': True},
            'categoria': {'type': 'string', 'nullable': True},
            'notas': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('ProveedorEliminado', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'eliminado': {'type': 'boolean'},
        },
    })
    spec.components.schema('PedidoCompra', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'producto_id': {'type': 'integer'},
            'producto_nombre': {'type': 'string'},
            'stock_actual': {'type': 'integer'},
            'categoria': {'type': 'string'},
            'cantidad_pedido': {'type': 'integer'},
            'fecha': {'type': 'string', 'format': 'date-time',
                      'nullable': True},
            'estado': {'type': 'string'},
            'proveedor': {'type': 'object', 'nullable': True,
                          'additionalProperties': True},
            'proveedores': {'type': 'array',
                            'items': {'type': 'object',
                                      'additionalProperties': True}},
            'sin_proveedor': {'type': 'boolean'},
            'colgado': {'type': 'boolean'},
        },
    })
    spec.components.schema('PedidoCompraLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/PedidoCompra'},
    })
    spec.components.schema('PedidoCompraCantidadIn', {
        'type': 'object',
        'required': ['cantidad'],
        'properties': {'cantidad': {'type': 'integer', 'minimum': 1}},
    })
    spec.components.schema('PedidoCompraProveedorIn', {
        'type': 'object',
        'properties': {'proveedor_id': {'type': 'integer', 'nullable': True}},
    })
    spec.components.schema('PedidoCompraEnviado', {
        'type': 'object',
        'properties': {
            'proveedor_id': {'type': 'integer'},
            'proveedor_nombre': {'type': 'string'},
            'enviados': {'type': 'integer'},
        },
    })

    # ── Admin: ingresos (Fase 5) ──
    spec.components.schema('IngresoItemAdmin', {
        'type': 'object',
        'properties': {
            'producto_id': {'type': 'integer'},
            'producto_nombre': {'type': 'string'},
            'cantidad': {'type': 'integer'},
            'precio_compra': {'type': 'string'},
            'precio_compra_centimos': {'type': 'integer'},
            'precio_venta': {'type': 'string'},
            'precio_venta_centimos': {'type': 'integer'},
            'subtotal_compra': {'type': 'string'},
            'subtotal_compra_centimos': {'type': 'integer'},
            'stock_actual': {'type': 'integer', 'nullable': True},
        },
    })
    spec.components.schema('IngresoAdmin', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'fecha': {'type': 'string', 'format': 'date-time',
                      'nullable': True},
            'estado': {'type': 'string', 'enum': ['vigente', 'anulado']},
            'anulado': {'type': 'boolean'},
            'motivo_anulacion': {'type': 'string', 'nullable': True},
            'anulado_en': {'type': 'string', 'format': 'date-time',
                           'nullable': True},
            'anulado_por': {'type': 'string', 'nullable': True},
            'proveedor': {'type': 'object', 'additionalProperties': True},
            'notas': {'type': 'string'},
            'productos_distintos': {'type': 'integer'},
            'total_cantidad': {'type': 'integer'},
            'total_compra': {'type': 'string'},
            'total_compra_centimos': {'type': 'integer'},
            'total_venta': {'type': 'string'},
            'total_venta_centimos': {'type': 'integer'},
            'moneda': {'type': 'string', 'example': 'PEN'},
            'items': {'type': 'array',
                      'items': {'$ref': '#/components/schemas/IngresoItemAdmin'}},
        },
    })
    spec.components.schema('IngresoAdminLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/IngresoAdmin'},
    })
    spec.components.schema('IngresoItemIn', {
        'type': 'object',
        'required': ['producto_id', 'cantidad', 'precio_compra', 'precio_venta'],
        'properties': {
            'producto_id': {'type': 'integer', 'minimum': 1},
            'cantidad': {'type': 'integer', 'minimum': 1},
            'precio_compra': {'type': 'string', 'example': '8.00'},
            'precio_venta': {'type': 'string', 'example': '12.50'},
        },
    })
    spec.components.schema('IngresoIn', {
        'type': 'object',
        'required': ['proveedor_id', 'items'],
        'properties': {
            'proveedor_id': {'type': 'integer', 'minimum': 1},
            'notas': {'type': 'string', 'nullable': True},
            'items': {'type': 'array', 'minItems': 1,
                      'items': {'$ref': '#/components/schemas/IngresoItemIn'}},
        },
    })
    spec.components.schema('IngresoAnularIn', {
        'type': 'object',
        'required': ['motivo'],
        'properties': {
            'motivo': {'type': 'string',
                       'enum': ['duplicado', 'datos', 'devolucion', 'carga',
                                'otro']},
            'motivo_detalle': {'type': 'string', 'nullable': True,
                               'description': 'Obligatorio si motivo=otro.'},
        },
    })
    spec.components.schema('IngresoAnulado', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'estado': {'type': 'string', 'example': 'anulado'},
            'motivo_anulacion': {'type': 'string'},
            'revertidos': {'type': 'integer'},
            'faltaron': {'type': 'integer'},
        },
    })

    # ── Admin: salidas (Fase 5) ──
    spec.components.schema('SalidaAdmin', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'fecha': {'type': 'string', 'format': 'date-time',
                      'nullable': True},
            'notas': {'type': 'string'},
            'venta_id': {'type': 'integer', 'nullable': True},
            'cliente': {'type': 'object', 'additionalProperties': True},
            'venta_total': {'type': 'string', 'nullable': True},
            'venta_total_centimos': {'type': 'integer', 'nullable': True},
            'total_items': {'type': 'integer'},
            'valor_estimado': {'type': 'string', 'nullable': True},
            'valor_estimado_centimos': {'type': 'integer', 'nullable': True},
            'items': {'type': 'array',
                      'items': {'type': 'object',
                                'additionalProperties': True}},
        },
    })
    spec.components.schema('SalidaAdminLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/SalidaAdmin'},
    })
    spec.components.schema('SalidaItemIn', {
        'type': 'object',
        'required': ['producto_id', 'cantidad'],
        'properties': {
            'producto_id': {'type': 'integer', 'minimum': 1},
            'cantidad': {'type': 'integer', 'minimum': 1},
        },
    })
    spec.components.schema('SalidaIn', {
        'type': 'object',
        'required': ['items'],
        'properties': {
            'venta_id': {'type': 'integer', 'nullable': True},
            'notas': {'type': 'string', 'nullable': True},
            'items': {'type': 'array', 'minItems': 1,
                      'items': {'$ref': '#/components/schemas/SalidaItemIn'}},
        },
    })
    spec.components.schema('SalidaEliminada', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'eliminada': {'type': 'boolean'},
        },
    })

    # ── Admin: entregas (Fase 5) ──
    spec.components.schema('EntregaAdmin', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'venta_id': {'type': 'integer'},
            'estado': {'type': 'string',
                       'enum': ['pendiente', 'en camino', 'entregado',
                                'cancelado']},
            'direccion_envio': {'type': 'string'},
            'fecha_estimada': {'type': 'string', 'format': 'date',
                               'nullable': True},
            'fecha_entrega': {'type': 'string', 'format': 'date-time',
                              'nullable': True},
            'notas': {'type': 'string'},
            'creado_en': {'type': 'string', 'format': 'date-time',
                          'nullable': True},
            'cliente': {'type': 'object', 'additionalProperties': True},
            'venta_total': {'type': 'string'},
            'venta_total_centimos': {'type': 'integer'},
            'moneda': {'type': 'string', 'example': 'PEN'},
            'cancelacion': {'type': 'object', 'nullable': True,
                            'additionalProperties': True},
            'productos_venta': {'type': 'array',
                                'items': {'$ref': '#/components/schemas/ItemPedido'}},
            'productos_entrega': {'type': 'array',
                                  'items': {'type': 'object',
                                            'additionalProperties': True}},
        },
    })
    spec.components.schema('EntregaAdminLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/EntregaAdmin'},
    })
    spec.components.schema('ProductoCantidadIn', {
        'type': 'object',
        'required': ['producto_id'],
        'properties': {
            'producto_id': {'type': 'integer', 'minimum': 1},
            'cantidad': {'type': 'integer', 'minimum': 1, 'default': 1},
        },
    })
    spec.components.schema('EntregaIn', {
        'type': 'object',
        'required': ['venta_id'],
        'properties': {
            'venta_id': {'type': 'integer', 'minimum': 1},
            'direccion_envio': {'type': 'string', 'nullable': True},
            'fecha_estimada': {'type': 'string', 'format': 'date',
                               'nullable': True},
            'notas': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('EntregaPatchIn', {
        'type': 'object',
        'required': ['estado'],
        'properties': {
            'estado': {'type': 'string',
                       'enum': ['pendiente', 'en camino', 'entregado',
                                'cancelado']},
            'notas': {'type': 'string', 'nullable': True},
        },
    })
    spec.components.schema('EntregaProductosIn', {
        'type': 'object',
        'properties': {
            'items': {'type': 'array',
                      'items': {'$ref': '#/components/schemas/ProductoCantidadIn'}},
        },
    })
    spec.components.schema('AsignarEntregaIn', {
        'type': 'object',
        'properties': {
            'direccion_envio': {'type': 'string', 'nullable': True},
            'fecha_estimada': {'type': 'string', 'format': 'date',
                               'nullable': True},
            'notas': {'type': 'string', 'nullable': True},
            'items': {'type': 'array',
                      'items': {'$ref': '#/components/schemas/ProductoCantidadIn'}},
        },
    })
    spec.components.schema('EntregaEliminada', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'eliminada': {'type': 'boolean'},
        },
    })
    spec.components.schema('EntregaAsignada', {
        'type': 'object',
        'properties': {
            'venta_id': {'type': 'integer'},
            'entrega_id': {'type': 'integer'},
            'accion': {'type': 'string', 'enum': ['creada', 'actualizada']},
        },
    })

    # ── Admin: inventario (Fase 5) ──
    spec.components.schema('InventarioProducto', {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'nombre': {'type': 'string'},
            'stock': {'type': 'integer'},
            'precio': {'type': 'string'},
            'precio_centimos': {'type': 'integer'},
            'categoria': {'type': 'string'},
            'estado': {'type': 'string', 'enum': ['activo', 'inactivo']},
            'nivel': {'type': 'string',
                      'enum': ['sin_stock', 'critico', 'bajo', 'normal',
                               'inactivo']},
            'imagen': {'type': 'string', 'format': 'uri', 'nullable': True},
            'imagen_original': {'type': 'string', 'format': 'uri',
                                'nullable': True},
        },
    })
    spec.components.schema('InventarioProductoLista', {
        'type': 'array',
        'items': {'$ref': '#/components/schemas/InventarioProducto'},
    })

    for regla in app.url_map.iter_rules():
        ruta = str(regla)
        if not ruta.startswith(_PREFIJO) or ruta in _RUTAS_INTERNAS:
            continue
        vista = app.view_functions.get(regla.endpoint)
        if vista is None:
            continue
        meta = getattr(vista, '__api_doc__', {})
        path = re.sub(r'<(?:[^:>]+:)?([^>]+)>', r'{\1}', ruta)
        operaciones = {}
        for metodo in sorted(regla.methods - {'HEAD', 'OPTIONS'}):
            operaciones[metodo.lower()] = _operacion(meta, vista.__name__)
        parametros = list(meta.get('parameters') or []) + _parametros_de_ruta(path)
        if parametros:
            for operacion in operaciones.values():
                operacion.setdefault('parameters', []).extend(parametros)
        spec.path(path=path, operations=operaciones)

    return spec.to_dict()


_DOCS_HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>API v1 - Multiservicios Richard</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: '/api/v1/openapi.json',
      dom_id: '#swagger-ui',
      persistAuthorization: true,
      displayRequestDuration: true
    });
  </script>
</body>
</html>
"""


def init_openapi(app):
    @app.get('/api/v1/openapi.json')
    def api_openapi():
        return jsonify(construir_spec(app))

    @app.get('/api/v1/docs')
    def api_docs():
        return Response(_DOCS_HTML, mimetype='text/html')
