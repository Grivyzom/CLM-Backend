import json
import logging
from django.conf import settings
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from clientes.models import Cliente
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from axes.decorators import axes_dispatch

logger = logging.getLogger(__name__)
from django_otp.plugins.otp_totp.models import TOTPDevice
from .models import SystemConfig
from .currency_config import get_currency_config, SUPPORTED_CURRENCIES
from tenants.permisos import permisos_efectivos
from tenants.plans import plan_payload


def _user_payload(user):
    """Identidad + contexto multi-tenant que consume el frontend:
    el plan (features/cuotas) decide qué módulos se muestran; `permisos`
    (membresía ∩ rol, ver tenants/permisos.py) decide qué acciones se
    habilitan. El backend revalida todo en cada petición."""
    payload = {
        'id': user.id,
        'username': user.username,
        'full_name': user.get_full_name() or '',
        'is_staff': user.is_staff,
        'is_superadmin': user.is_superadmin,
        'role': user.role if user.tenant_id else None,
        'platform_role': user.platform_role if user.tenant_id is None else None,
        'cliente_id': user.cliente_id,
        'permisos': sorted(permisos_efectivos(user)),
        'tenant': None,
        'plan': None,
    }
    if user.tenant_id:
        tenant = user.tenant
        payload['tenant'] = {
            'id': str(tenant.id),
            'razon_social': tenant.razon_social,
            'estado': tenant.estado,
        }
        payload['plan'] = plan_payload(tenant)
    return payload

@csrf_exempt
@require_POST
@axes_dispatch
def api_login(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    username = data.get('username')
    password = data.get('password')
    otp_token = data.get('otp_token')
    remember = bool(data.get('remember'))

    if not username or not password:
        return JsonResponse({'error': 'Faltan credenciales'}, status=400)

    # 1. Autenticación primaria (Dispara la verificación de fuerza bruta de django-axes)
    user = authenticate(request, username=username, password=password)

    if user is not None:
        if not user.is_active:
            return JsonResponse({'error': 'La cuenta está desactivada'}, status=403)

        # Bloqueo real de cliente: si el Cliente asociado está inactivo, el
        # usuario rol CLIENTE no puede iniciar sesión (el middleware
        # ClienteBloqueadoMiddleware cubre las sesiones ya abiertas).
        from tenants.models import RolTenant
        if user.tenant_id is not None and user.role == RolTenant.CLIENTE:
            bloqueado = (
                user.cliente_id is None
                or not Cliente.objects.filter(pk=user.cliente_id, is_active=True).exists()
            )
            if bloqueado:
                return JsonResponse({
                    'error': 'Tu cuenta de cliente está bloqueada. Contacta a soporte.',
                    'code': 'CLIENTE_BLOQUEADO',
                }, status=403)

        # 2. Verificación de Autenticación de Dos Factores (TOTP)
        devices = TOTPDevice.objects.filter(user=user, confirmed=True)
        if devices.exists():
            if not otp_token:
                # El usuario proporcionó contraseña correcta, pero necesita su token TOTP
                return JsonResponse({
                    'error': 'Se requiere código 2FA (TOTP)',
                    'require_2fa': True
                }, status=401)
            
            # Verificamos el token aportado
            valid_device = False
            for device in devices:
                # verify_token incluye protección nativa contra replay attacks y brute forcing del PIN
                if device.verify_token(otp_token):
                    valid_device = True
                    break
            
            if not valid_device:
                return JsonResponse({'error': 'Código 2FA inválido'}, status=401)
        
        # 3. Todo correcto: Crear la sesión segura en el servidor
        login(request, user)

        # "Recordarme": sesión persistente de 14 días en vez del expiry corto por defecto
        if remember:
            request.session.set_expiry(60 * 60 * 24 * 14)
        else:
            request.session.set_expiry(0)

        return JsonResponse({'success': 'Sesión iniciada con éxito', **_user_payload(user)})
    else:
        return JsonResponse({'error': 'Credenciales inválidas'}, status=401)

@require_POST
def api_logout(request):
    logout(request)
    return JsonResponse({'success': 'Sesión cerrada exitosamente'})


@require_http_methods(["GET"])
def api_me(request):
    """Devuelve el usuario de la sesión Django activa, o 401 si no hay sesión."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    return JsonResponse(_user_payload(request.user))


def enviar_correo_reset_password(user):
    """Genera el token de un solo uso (default_token_generator, el mismo que
    valida api_password_reset_confirm) y envía el correo con el enlace de
    /recuperar/confirmar/. Reutilizada por api_password_reset (autoservicio,
    por identifier) y por PlatformUserResetPasswordView (acción de un
    Administrador/Moderador sobre un usuario puntual, ver tenants/views.py)."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = f"{settings.FRONTEND_BASE_URL}/recuperar/confirmar/{uid}/{token}"
    send_mail(
        'Restablecer contraseña — Enfoque Platform',
        (
            f'Hola {user.username},\n\n'
            'Recibimos una solicitud para restablecer tu contraseña en Enfoque Platform (CLM).\n\n'
            f'Abre este enlace para definir una nueva contraseña:\n{url}\n\n'
            'El enlace es de un solo uso y expira pronto. Si no solicitaste esto, '
            'ignora este correo: tu contraseña no cambia.'
        ),
        None,  # DEFAULT_FROM_EMAIL
        [user.email],
    )


@csrf_exempt
@require_POST
def api_password_reset(request):
    """Solicita restablecer contraseña por correo.

    La respuesta es siempre la misma exista o no el correo (anti-enumeración
    de usuarios). El envío usa el token estándar de Django, de un solo uso:
    se invalida al cambiar la contraseña o al expirar PASSWORD_RESET_TIMEOUT.
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    identifier = (data.get('identifier') or data.get('email') or '').strip()
    if not identifier:
        return JsonResponse({'error': 'Falta el usuario o correo'}, status=400)

    User = get_user_model()
    candidates = User.objects.filter(
        Q(username__iexact=identifier) | Q(email__iexact=identifier),
        is_active=True,
    ).exclude(email='')
    for user in candidates:
        try:
            enviar_correo_reset_password(user)
        except Exception:
            # No filtrar el fallo al cliente: la respuesta sigue siendo genérica
            logger.exception('Fallo al enviar correo de reset para user id=%s', user.pk)

    return JsonResponse({
        'success': 'Si la cuenta existe, enviamos un enlace de restablecimiento al correo asociado.',
    })


@csrf_exempt
@require_POST
def api_password_reset_confirm(request):
    """Valida uid+token del correo y define la nueva contraseña."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    uid = data.get('uid')
    token = data.get('token')
    password = data.get('password')
    if not uid or not token or not password:
        return JsonResponse({'error': 'Faltan datos'}, status=400)

    User = get_user_model()
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)), is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return JsonResponse({'error': 'El enlace no es válido o ya expiró. Solicita uno nuevo.'}, status=400)

    try:
        validate_password(password, user=user)
    except DjangoValidationError as e:
        return JsonResponse({'error': ' '.join(e.messages)}, status=400)

    user.set_password(password)
    user.save(update_fields=['password'])
    return JsonResponse({'success': 'Contraseña actualizada. Ya puedes iniciar sesión.'})


@require_http_methods(["GET", "POST"])
def api_currency_config(request):
    """
    GET: Obtener configuración de moneda actual (requiere sesión activa)
    POST: Actualizar configuración de moneda (requiere staff)
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Autenticación requerida'}, status=401)

    if request.method == "POST" and not request.user.is_staff:
        return JsonResponse({'error': 'Requiere permisos de administrador'}, status=403)

    try:
        if request.method == "GET":
            # Obtener configuración actual
            config = SystemConfig.get_config()
            currency_info = get_currency_config(config.default_currency)

            return JsonResponse({
                "success": True,
                "data": {
                    "default_currency": config.default_currency,
                    "currency_info": currency_info,
                    "supported_currencies": SUPPORTED_CURRENCIES,
                },
            })

        elif request.method == "POST":
            # Actualizar configuración de moneda
            data = json.loads(request.body)
            new_currency = data.get("currency")

            if not new_currency:
                return JsonResponse(
                    {"error": "Campo 'currency' requerido"},
                    status=400,
                )

            if new_currency not in SUPPORTED_CURRENCIES:
                return JsonResponse(
                    {
                        "error": f"Moneda no soportada: {new_currency}. Soportadas: {SUPPORTED_CURRENCIES}"
                    },
                    status=400,
                )

            # Actualizar configuración
            config = SystemConfig.set_default_currency(new_currency)
            currency_info = get_currency_config(config.default_currency)

            return JsonResponse({
                "success": True,
                "message": f"Moneda actualizada a {new_currency}",
                "data": {
                    "default_currency": config.default_currency,
                    "currency_info": currency_info,
                },
            })

    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON inválido"}, status=400)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse(
            {"error": f"Error al procesar la solicitud: {str(e)}"},
            status=500,
        )

@csrf_exempt
@require_POST
def api_register_cliente(request):
    """Completa el registro del portal de cliente en un solo paso.

    La cuenta de portal (User inactivo, sin password usable) ya fue creada
    al momento de dar de alta al Cliente (ver enviar_correo_bienvenida /
    ClienteListView.post), junto con el enlace de un solo uso (uid+token,
    mismo default_token_generator que password-reset) que llegó por correo.
    Esta vista valida ese enlace y, en el mismo paso, fija la contraseña y
    activa la cuenta — sin un segundo correo de confirmación. Sin la
    verificación de uid+token, conocer el email_principal de un Cliente
    (dato a menudo público, ej. un correo de contacto comercial) bastaría
    para tomar el registro de otra persona.
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    uid = data.get('uid')
    token = data.get('token')
    password = data.get('password')

    if not uid or not token or not password:
        return JsonResponse({'error': 'Faltan datos para completar el registro'}, status=400)

    try:
        validate_password(password)
    except DjangoValidationError as e:
        return JsonResponse({'error': ' '.join(e.messages)}, status=400)

    User = get_user_model()
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)), is_active=False)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return JsonResponse({'error': 'El enlace de activación es inválido o expiró.'}, status=400)

    cliente = Cliente.objects.filter(pk=user.cliente_id).first()
    if cliente and not cliente.is_active:
        return JsonResponse({
            'error': 'Este cliente está bloqueado y no puede activar una cuenta. Contacta a soporte.',
            'code': 'CLIENTE_BLOQUEADO',
        }, status=403)

    user.set_password(password)
    user.is_active = True
    user.save(update_fields=['password', 'is_active'])
    return JsonResponse({'success': 'Cuenta activada. Ya puedes iniciar sesión.'})
