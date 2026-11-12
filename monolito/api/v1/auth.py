"""Endpoints de autenticacion y cuenta de la API v1."""
import os
import secrets
import threading
from datetime import datetime, timedelta

from flask import current_app, g, request
from flask_jwt_extended import (
    decode_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)
from flask_mail import Message
from pydantic import BaseModel, Field

from services import auth_service
from services.seguridad import (
    PRIMER_BLOQUEO_FALLO,
    correo_valido,
    dominio_permitido,
    minutos_bloqueo,
    nombre_valido,
    password_valida,
    recaptcha_configurado,
    validar_recaptcha,
)

from ..deps import cuerpo_json, get_bcrypt, get_mysql
from ..errors import ApiError, ok, validar_modelo
from ..openapi import doc
from ..security import asegurar_esquema, emitir_tokens, requiere_cliente
from ..serializers import estado_cliente_a_json, tokens_a_json, usuario_a_json
from . import bp


# ─────────────────────────────────────────────
# MODELOS DE ENTRADA
# ─────────────────────────────────────────────
class LoginIn(BaseModel):
    correo: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=72)
    device_id: str | None = Field(default=None, max_length=120)


class RegistroIn(BaseModel):
    correo: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1)
    nombres: str = Field(min_length=1, max_length=100)
    apellidos: str = Field(min_length=1, max_length=50)
    documento_tipo: str = Field(default='dni', max_length=5)
    documento_numero: str = Field(default='', max_length=15)
    telefono: str = Field(default='', max_length=9)
    recaptcha_token: str | None = None
    device_id: str | None = Field(default=None, max_length=120)


class PerfilIn(BaseModel):
    nombre: str | None = Field(default=None, max_length=100)
    apellido: str | None = Field(default=None, max_length=50)
    telefono: str | None = Field(default=None, max_length=9)
    documento_tipo: str | None = Field(default=None, max_length=5)
    documento_numero: str | None = Field(default=None, max_length=11)


class PasswordIn(BaseModel):
    actual: str = Field(min_length=1, max_length=72)
    nueva: str = Field(min_length=1)
    confirmar: str = Field(min_length=1)


class ForgotIn(BaseModel):
    correo: str = Field(min_length=3, max_length=120)


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1)
    confirmar: str = Field(min_length=1)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _abrir_cursor():
    mysql = get_mysql()
    try:
        return mysql, mysql.connection.cursor()
    except Exception as exc:
        raise ApiError('BD_NO_DISPONIBLE',
                       'No se pudo conectar con la base de datos.', 503) from exc


def _error_bd(exc, mensaje='No se pudo completar la operacion en la base de datos.'):
    try:
        current_app.logger.error('API v1 auth: %s', exc, exc_info=True)
    except Exception:
        pass
    return ApiError('BD_NO_DISPONIBLE', mensaje, 503)


def _normalizar_correo(valor):
    return (valor or '').strip().lower()


def _validar_registro(datos):
    correo = _normalizar_correo(datos.correo)
    nombres = (datos.nombres or '').strip()
    apellidos = (datos.apellidos or '').strip()
    documento_tipo = (datos.documento_tipo or 'dni').strip().lower()
    documento_numero = (datos.documento_numero or '').strip()
    telefono = (datos.telefono or '').strip()

    if not correo_valido(correo):
        raise ApiError('VALIDACION', 'Ingresa un correo electronico valido.', 422,
                       {'campo': 'correo'})
    if not dominio_permitido(correo):
        raise ApiError('VALIDACION',
                       'Solo se permiten correos @gmail.com o @hotmail.com.', 422,
                       {'campo': 'correo'})
    if len(nombres) < 4:
        raise ApiError('VALIDACION',
                       'El nombre debe tener al menos 4 caracteres.', 422,
                       {'campo': 'nombres'})
    if len(apellidos) < 4:
        raise ApiError('VALIDACION',
                       'Los apellidos deben tener al menos 4 caracteres.', 422,
                       {'campo': 'apellidos'})
    if not nombre_valido(nombres):
        raise ApiError('VALIDACION',
                       'Los nombres solo pueden contener letras.', 422,
                       {'campo': 'nombres'})
    if not nombre_valido(apellidos):
        raise ApiError('VALIDACION',
                       'Los apellidos solo pueden contener letras.', 422,
                       {'campo': 'apellidos'})
    if documento_tipo not in ('dni', 'ruc'):
        raise ApiError('VALIDACION',
                       'El tipo de documento debe ser dni o ruc.', 422,
                       {'campo': 'documento_tipo'})
    if documento_numero and not documento_numero.isdigit():
        raise ApiError('VALIDACION',
                       'El numero de documento solo puede contener digitos.', 422,
                       {'campo': 'documento_numero'})
    if documento_tipo == 'dni' and len(documento_numero) > 8:
        raise ApiError('VALIDACION',
                       'El DNI no puede tener mas de 8 digitos.', 422,
                       {'campo': 'documento_numero'})
    if documento_tipo == 'ruc' and len(documento_numero) > 11:
        raise ApiError('VALIDACION',
                       'El RUC no puede tener mas de 11 digitos.', 422,
                       {'campo': 'documento_numero'})
    if telefono and not telefono.isdigit():
        raise ApiError('VALIDACION',
                       'El telefono solo puede contener numeros.', 422,
                       {'campo': 'telefono'})

    ok_pwd, mensaje = password_valida(datos.password)
    if not ok_pwd:
        raise ApiError('VALIDACION', mensaje, 422, {'campo': 'password'})

    return correo, nombres, apellidos, documento_tipo, documento_numero, telefono


def _validar_perfil(datos):
    if 'nombre' in datos.model_fields_set:
        nombre = (datos.nombre or '').strip()
        if nombre and not nombre_valido(nombre):
            raise ApiError('VALIDACION',
                           'Los nombres solo pueden contener letras.', 422,
                           {'campo': 'nombre'})
    if 'apellido' in datos.model_fields_set:
        apellido = (datos.apellido or '').strip()
        if apellido and not nombre_valido(apellido):
            raise ApiError('VALIDACION',
                           'Los apellidos solo pueden contener letras.', 422,
                           {'campo': 'apellido'})
    if 'telefono' in datos.model_fields_set:
        telefono = (datos.telefono or '').strip()
        if telefono and not telefono.isdigit():
            raise ApiError('VALIDACION',
                           'El telefono solo puede contener numeros.', 422,
                           {'campo': 'telefono'})
    if 'documento_tipo' in datos.model_fields_set:
        tipo = (datos.documento_tipo or '').strip().lower()
        if tipo and tipo not in ('dni', 'ruc'):
            raise ApiError('VALIDACION',
                           'El tipo de documento debe ser dni o ruc.', 422,
                           {'campo': 'documento_tipo'})
    if 'documento_numero' in datos.model_fields_set:
        numero = (datos.documento_numero or '').strip()
        if numero and not numero.isdigit():
            raise ApiError('VALIDACION',
                           'El numero de documento solo puede contener digitos.',
                           422, {'campo': 'documento_numero'})
        tipo = (datos.documento_tipo or '').strip().lower()
        if tipo == 'dni' and len(numero) > 8:
            raise ApiError('VALIDACION',
                           'El DNI no puede tener mas de 8 digitos.', 422,
                           {'campo': 'documento_numero'})
        if tipo == 'ruc' and len(numero) > 11:
            raise ApiError('VALIDACION',
                           'El RUC no puede tener mas de 11 digitos.', 422,
                           {'campo': 'documento_numero'})


def _registrar_fallo(cur, correo, fila_intento, fila_ip, ahora, ip):
    """Actualiza intentos_usuario y bloqueos_ip. Devuelve el total de fallos."""
    if fila_intento:
        intentos = int(fila_intento.get('intentos') or 0) + 1
    else:
        intentos = 1

    castigo = minutos_bloqueo(intentos) if intentos >= PRIMER_BLOQUEO_FALLO else 0
    bloqueado_hasta = ahora + timedelta(minutes=castigo) if castigo else None
    pico = max(intentos, int((fila_intento or {}).get('max_intentos') or 0))

    if fila_intento:
        cur.execute(
            "UPDATE intentos_usuario SET intentos=%s, bloqueado_hasta=%s, "
            "ultimo_fallo=%s, max_intentos=%s WHERE correo=%s",
            (intentos, bloqueado_hasta, ahora, pico, correo),
        )
    else:
        cur.execute(
            "INSERT INTO intentos_usuario (correo, intentos, ultimo_fallo, max_intentos) "
            "VALUES (%s,%s,%s,%s)",
            (correo, intentos, ahora, pico),
        )

    if castigo:
        if fila_ip:
            distintos = int(fila_ip.get('usuarios_diferentes') or 0) + 1
            hasta = ahora + timedelta(minutes=10) if distintos >= 2 else fila_ip.get('bloqueado_hasta')
            cur.execute(
                "UPDATE bloqueos_ip SET usuarios_diferentes=%s, bloqueado_hasta=%s "
                "WHERE ip=%s",
                (distintos, hasta, ip),
            )
        else:
            cur.execute(
                "INSERT INTO bloqueos_ip (ip, usuarios_diferentes, bloqueado_hasta) "
                "VALUES (%s,1,NULL)",
                (ip,),
            )
    return intentos


def _segundos_access():
    valor = current_app.config['JWT_ACCESS_TOKEN_EXPIRES']
    if hasattr(valor, 'total_seconds'):
        return int(valor.total_seconds())
    return int(valor)


def _respuesta_sesion(usuario, device_id=None):
    access, refresh, _ = emitir_tokens(usuario, device_id)
    return ok(tokens_a_json(access, refresh, _segundos_access(), usuario))


def _enviar_correo_reset(correo, token):
    """Envia el enlace web y el codigo que puede usar la app movil."""
    try:
        app = current_app._get_current_object()
        if 'mail' not in app.extensions:
            return
        base = os.environ.get('PUBLIC_BASE_URL') or request.host_url.rstrip('/')
        enlace = '%s/restablecer-password/%s' % (base, token)
        mensaje = Message(
            subject='Restablece tu contrasena - Multiservicios Richard',
            recipients=[correo],
            body=(
                'Recibimos una solicitud para restablecer tu contrasena.\n\n'
                'Enlace web: %s\n\n'
                'Codigo para la app: %s\n\n'
                'Si no fuiste tu, ignora este correo. El codigo vence en 2 horas.'
            ) % (enlace, token),
        )

        def _enviar():
            with app.app_context():
                try:
                    app.extensions['mail'].send(mensaje)
                except Exception as exc:
                    app.logger.warning(
                        'No se pudo enviar el correo de restablecimiento: %s', exc)

        threading.Thread(target=_enviar, daemon=True).start()
    except Exception as exc:
        try:
            current_app.logger.warning('Reset de contrasena: %s', exc)
        except Exception:
            pass


# ─────────────────────────────────────────────
# REGISTRO
# ─────────────────────────────────────────────
@bp.post('/auth/register')
@doc(
    summary='Crear cuenta de cliente',
    tags=['Autenticacion'],
    request='RegistroIn',
    response_model='TokenPair',
    success_status=201,
    responses={201: 'Cuenta creada y sesion iniciada',
               409: 'El correo ya esta registrado',
               422: 'Datos invalidos'},
)
def register():
    datos = validar_modelo(RegistroIn, cuerpo_json())
    correo, nombres, apellidos, documento_tipo, documento_numero, telefono = \
        _validar_registro(datos)

    if recaptcha_configurado():
        captcha_ok, mensaje = validar_recaptcha(
            datos.recaptcha_token, request.remote_addr)
        if not captcha_ok:
            raise ApiError('CAPTCHA_INVALIDO', mensaje or 'Captcha invalido.', 400)

    asegurar_esquema()
    mysql, cur = _abrir_cursor()
    try:
        cur.execute("SELECT id FROM usuarios WHERE correo=%s", (correo,))
        if cur.fetchone():
            raise ApiError('CORREO_EN_USO',
                           'Ya existe una cuenta con ese correo.', 409)

        nuevo_id = auth_service.crear_usuario(
            cur,
            get_bcrypt(),
            correo=correo,
            password=datos.password,
            nombres=nombres,
            apellidos=apellidos,
            documento_tipo=documento_tipo,
            documento_numero=documento_numero,
            telefono=telefono,
        )
        mysql.connection.commit()

        usuario = auth_service.buscar_usuario_por_id(cur, nuevo_id)
        access, refresh, _ = emitir_tokens(usuario, datos.device_id)
        return ok(tokens_a_json(access, refresh, _segundos_access(), usuario),
                  status=201)
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────
@bp.post('/auth/login')
@doc(
    summary='Iniciar sesion',
    tags=['Autenticacion'],
    request='LoginIn',
    response_model='TokenPair',
    responses={200: 'Sesion iniciada',
               401: 'Credenciales invalidas',
               429: 'Cuenta o IP bloqueada temporalmente'},
)
def login():
    datos = validar_modelo(LoginIn, cuerpo_json())
    correo = _normalizar_correo(datos.correo)
    password = datos.password or ''
    if not correo or not password:
        raise ApiError('CAMPOS_OBLIGATORIOS',
                       'Correo y contrasena son obligatorios.', 400)

    asegurar_esquema()
    mysql, cur = _abrir_cursor()
    ip = request.remote_addr or 'desconocida'
    ahora = datetime.now()
    try:
        # Bloqueo por IP.
        cur.execute("SELECT * FROM bloqueos_ip WHERE ip=%s", (ip,))
        fila_ip = cur.fetchone()
        if (fila_ip and fila_ip.get('bloqueado_hasta')
                and ahora < fila_ip['bloqueado_hasta']):
            restante = fila_ip['bloqueado_hasta'] - ahora
            raise ApiError(
                'IP_BLOQUEADA',
                'Demasiados intentos desde esta IP. Intenta en %d minutos.'
                % max(1, int(restante.total_seconds() // 60) + 1),
                429,
            )

        # Bloqueo por cuenta.
        cur.execute("SELECT * FROM intentos_usuario WHERE correo=%s", (correo,))
        fila_intento = cur.fetchone()
        if fila_intento and fila_intento.get('bloqueado_hasta'):
            if ahora < fila_intento['bloqueado_hasta']:
                restante = fila_intento['bloqueado_hasta'] - ahora
                raise ApiError(
                    'CUENTA_BLOQUEADA',
                    'La cuenta esta bloqueada temporalmente. Intenta en %d minutos.'
                    % max(1, int(restante.total_seconds() // 60) + 1),
                    429,
                )
            cur.execute(
                "UPDATE intentos_usuario SET bloqueado_hasta=NULL WHERE correo=%s",
                (correo,),
            )
            fila_intento = dict(fila_intento, bloqueado_hasta=None)

        cur.execute("SELECT * FROM usuarios WHERE correo=%s", (correo,))
        usuario = cur.fetchone()

        if usuario:
            estado = (usuario.get('estado') or 'activo').lower()
            if estado in ('bloqueado', 'inactivo'):
                raise ApiError(
                    'CUENTA_NO_ACTIVA',
                    'Tu cuenta esta %s. Contacta al administrador.' % estado,
                    403,
                )

        clave_ok = False
        if usuario:
            if len(password.encode('utf-8')) <= 72:
                try:
                    clave_ok = get_bcrypt().check_password_hash(
                        usuario['password'], password)
                except (ValueError, TypeError):
                    clave_ok = False

        if usuario and clave_ok:
            cur.execute(
                "UPDATE intentos_usuario SET intentos=0, bloqueado_hasta=NULL "
                "WHERE correo=%s",
                (correo,),
            )
            mysql.connection.commit()
            usuario = auth_service.buscar_usuario_por_id(cur, usuario['id'])
            return _respuesta_sesion(usuario, datos.device_id)

        intentos = _registrar_fallo(
            cur, correo, fila_intento, fila_ip, ahora, ip)
        mysql.connection.commit()
        restantes = max(0, PRIMER_BLOQUEO_FALLO - intentos)
        raise ApiError(
            'CREDENCIALES_INVALIDAS',
            'Correo o contrasena incorrectos.',
            401,
            {'intentos_restantes': restantes},
        )
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


# ─────────────────────────────────────────────
# REFRESH Y LOGOUT
# ─────────────────────────────────────────────
@bp.post('/auth/refresh')
@doc(
    summary='Renovar el access token',
    tags=['Autenticacion'],
    auth=True,
    description='Envia el refresh token en Authorization: Bearer. Rota el refresh.',
    response_model='TokenPair',
    responses={200: 'Tokens renovados', 401: 'Refresh token invalido o expirado'},
)
@jwt_required(refresh=True)
def refresh():
    asegurar_esquema()
    claims = get_jwt()
    jti = claims.get('jti')
    identidad = get_jwt_identity()
    if not jti or identidad is None:
        raise ApiError('REFRESH_INVALIDO', 'Refresh token invalido.', 401)

    mysql, cur = _abrir_cursor()
    try:
        fila = auth_service.buscar_refresh_por_jti(cur, jti)
        if not auth_service.refresh_vigente(fila):
            raise ApiError('REFRESH_INVALIDO',
                           'El refresh token expiro o fue revocado. Inicia sesion de nuevo.',
                           401)

        usuario = auth_service.buscar_usuario_por_id(cur, identidad)
        if not usuario:
            raise ApiError('USUARIO_NO_ENCONTRADO', 'La cuenta no existe.', 401)

        estado = (usuario.get('estado') or 'activo').lower()
        if estado in ('bloqueado', 'inactivo'):
            raise ApiError('CUENTA_NO_ACTIVA',
                           'Tu cuenta esta %s.' % estado, 403)
        if int(claims.get('ver', 0)) != int(usuario.get('token_version') or 0):
            raise ApiError('TOKEN_REVOCADO',
                           'La sesion fue revocada. Inicia sesion de nuevo.', 401)

        auth_service.revocar_refresh(cur, jti)
        access, nuevo_refresh, _ = emitir_tokens(
            usuario, fila.get('device_id') if fila else None)
        return ok(tokens_a_json(access, nuevo_refresh, _segundos_access(), usuario))
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


@bp.post('/auth/logout')
@doc(
    summary='Cerrar sesion',
    tags=['Autenticacion'],
    auth=True,
    responses={200: 'Sesion cerrada', 401: 'Token invalido'},
)
@requiere_cliente
def logout():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        datos = {}

    asegurar_esquema()
    mysql, cur = _abrir_cursor()
    try:
        if datos.get('todas'):
            auth_service.revocar_todos_los_refresh(cur, g.usuario['id'])
            # Invalida tambien los access tokens ya emitidos.
            cur.execute(
                "UPDATE usuarios SET token_version=COALESCE(token_version,0)+1 "
                "WHERE id=%s",
                (g.usuario['id'],),
            )
        else:
            refresh_token = datos.get('refresh_token')
            if refresh_token:
                try:
                    payload = decode_token(refresh_token)
                except Exception as exc:
                    raise ApiError('REFRESH_INVALIDO',
                                   'Refresh token invalido.', 400) from exc
                if (payload.get('type') != 'refresh'
                        or str(payload.get('sub')) != str(g.usuario['id'])):
                    raise ApiError('REFRESH_INVALIDO',
                                   'Refresh token invalido.', 400)
                auth_service.revocar_refresh(cur, payload.get('jti'))
        mysql.connection.commit()
        return ok({'mensaje': 'Sesion cerrada.'})
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


# ─────────────────────────────────────────────
# CUENTA
# ─────────────────────────────────────────────
@bp.get('/auth/me')
@doc(
    summary='Datos de la cuenta autenticada',
    tags=['Cuenta'],
    auth=True,
    response_model='Usuario',
    responses={200: 'Datos del usuario', 401: 'Token invalido'},
)
@requiere_cliente
def me():
    return ok(usuario_a_json(g.usuario))


@bp.patch('/auth/me')
@doc(
    summary='Actualizar datos personales',
    tags=['Cuenta'],
    auth=True,
    request='ActualizarPerfilIn',
    response_model='Usuario',
    responses={200: 'Datos actualizados', 422: 'Datos invalidos'},
)
@requiere_cliente
def actualizar_me():
    datos = validar_modelo(PerfilIn, cuerpo_json())
    _validar_perfil(datos)

    campos = []
    valores = []
    for campo in ('nombre', 'apellido', 'telefono',
                  'documento_tipo', 'documento_numero'):
        if campo in datos.model_fields_set:
            campos.append('%s=%%s' % campo)
            valor = getattr(datos, campo)
            valores.append((valor or '').strip() or None)

    if not campos:
        raise ApiError('SIN_CAMBIOS', 'No enviaste ningun campo para actualizar.', 400)

    mysql, cur = _abrir_cursor()
    try:
        valores.append(g.usuario['id'])
        cur.execute(
            "UPDATE usuarios SET %s WHERE id=%%s" % ', '.join(campos),
            tuple(valores),
        )
        mysql.connection.commit()
        usuario = auth_service.buscar_usuario_por_id(cur, g.usuario['id'])
        return ok(usuario_a_json(usuario))
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


@bp.put('/auth/me/password')
@doc(
    summary='Cambiar la contrasena',
    tags=['Cuenta'],
    auth=True,
    request='CambiarPasswordIn',
    responses={200: 'Contrasena actualizada; hay que volver a iniciar sesion',
               401: 'La contrasena actual no es correcta',
               422: 'La nueva contrasena no cumple la politica'},
)
@requiere_cliente
def cambiar_password():
    datos = validar_modelo(PasswordIn, cuerpo_json())
    ok_pwd, mensaje = password_valida(datos.nueva)
    if not ok_pwd:
        raise ApiError('VALIDACION', mensaje, 422, {'campo': 'nueva'})
    if datos.nueva != datos.confirmar:
        raise ApiError('VALIDACION', 'Las contrasenas no coinciden.', 422,
                       {'campo': 'confirmar'})

    mysql, cur = _abrir_cursor()
    try:
        cur.execute("SELECT password FROM usuarios WHERE id=%s", (g.usuario['id'],))
        fila = cur.fetchone()
        clave_ok = False
        if fila and len(datos.actual.encode('utf-8')) <= 72:
            try:
                clave_ok = get_bcrypt().check_password_hash(
                    fila['password'], datos.actual)
            except (ValueError, TypeError):
                clave_ok = False
        if not clave_ok:
            raise ApiError('PASSWORD_ACTUAL_INCORRECTA',
                           'La contrasena actual no es correcta.', 401)

        auth_service.actualizar_password(
            cur, get_bcrypt(), g.usuario['id'], datos.nueva)
        auth_service.revocar_todos_los_refresh(cur, g.usuario['id'])
        mysql.connection.commit()
        return ok({
            'mensaje': 'Contrasena actualizada. Inicia sesion de nuevo.',
            'relogin': True,
        })
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


@bp.get('/auth/me/estado')
@doc(
    summary='Estado de la cuenta',
    tags=['Cuenta'],
    auth=True,
    responses={200: 'Estado actual del cliente'},
)
@requiere_cliente
def mi_estado():
    return ok(estado_cliente_a_json(g.usuario.get('estado')))


@bp.post('/auth/me/revision')
@doc(
    summary='Solicitar revision del estado de la cuenta',
    tags=['Cuenta'],
    auth=True,
    responses={201: 'Solicitud registrada'},
)
@requiere_cliente
def solicitar_revision():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        datos = {}
    motivo = (datos.get('motivo') or 'Solicitud de revision de estado').strip()[:300]

    mysql, cur = _abrir_cursor()
    try:
        actual = (g.usuario.get('estado') or 'activo')
        cur.execute(
            "INSERT INTO historial_estado_cliente "
            "(usuario_id, estado_anterior, estado_nuevo, motivo, admin_id) "
            "VALUES (%s,%s,%s,%s,NULL)",
            (g.usuario['id'], actual, actual, motivo),
        )
        mysql.connection.commit()
        return ok({'mensaje': 'Solicitud enviada. Un administrador la revisara.'},
                  status=201)
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


# ─────────────────────────────────────────────
# RECUPERACION DE CONTRASENA
# ─────────────────────────────────────────────
@bp.post('/auth/password/forgot')
@doc(
    summary='Solicitar restablecimiento de contrasena',
    tags=['Autenticacion'],
    request='ForgotIn',
    responses={200: 'Solicitud aceptada (no revela si el correo existe)'},
)
def forgot_password():
    datos = validar_modelo(ForgotIn, cuerpo_json())
    correo = _normalizar_correo(datos.correo)

    asegurar_esquema()
    mysql, cur = _abrir_cursor()
    try:
        cur.execute("SELECT id FROM usuarios WHERE correo=%s", (correo,))
        usuario = cur.fetchone()
        if usuario:
            token = secrets.token_urlsafe(48)
            expira = datetime.now() + timedelta(hours=2)
            cur.execute(
                "UPDATE usuarios SET recuperacion_token=%s, recuperacion_expira=%s "
                "WHERE id=%s",
                (token, expira, usuario['id']),
            )
            mysql.connection.commit()
            _enviar_correo_reset(correo, token)
        return ok({'mensaje': 'Si el correo existe, recibiras instrucciones.'})
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()


@bp.post('/auth/password/reset')
@doc(
    summary='Restablecer la contrasena con el token recibido',
    tags=['Autenticacion'],
    request='ResetIn',
    responses={200: 'Contrasena restablecida',
               400: 'Token invalido o expirado',
               422: 'La nueva contrasena no cumple la politica'},
)
def reset_password():
    datos = validar_modelo(ResetIn, cuerpo_json())
    ok_pwd, mensaje = password_valida(datos.password)
    if not ok_pwd:
        raise ApiError('VALIDACION', mensaje, 422, {'campo': 'password'})
    if datos.password != datos.confirmar:
        raise ApiError('VALIDACION', 'Las contrasenas no coinciden.', 422,
                       {'campo': 'confirmar'})

    asegurar_esquema()
    mysql, cur = _abrir_cursor()
    try:
        cur.execute(
            "SELECT id FROM usuarios WHERE recuperacion_token=%s "
            "AND recuperacion_expira>%s",
            (datos.token, datetime.now()),
        )
        usuario = cur.fetchone()
        if not usuario:
            raise ApiError('TOKEN_INVALIDO',
                           'El enlace es invalido o ha expirado.', 400)

        auth_service.actualizar_password(
            cur, get_bcrypt(), usuario['id'], datos.password)
        auth_service.revocar_todos_los_refresh(cur, usuario['id'])
        mysql.connection.commit()
        return ok({'mensaje': 'Contrasena restablecida. Inicia sesion.'})
    except ApiError:
        raise
    except Exception as exc:
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        raise _error_bd(exc) from exc
    finally:
        cur.close()
