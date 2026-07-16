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
    """Auto-registro del portal de cliente.

    La cuenta se crea inactiva y solo se habilita al confirmar el enlace
    enviado a `email_principal` del Cliente (api_register_cliente_confirm,
    mismo mecanismo de token de un solo uso que password-reset). Sin esto,
    conocer el email_principal de un Cliente (dato a menudo público, ej. un
    correo de contacto comercial) bastaba para crear una cuenta con rol
    CLIENTE y acceder a su workspace suplantándolo.
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return JsonResponse({'error': 'Email y contraseña son requeridos'}, status=400)

    try:
        validate_password(password)
    except DjangoValidationError as e:
        return JsonResponse({'error': ' '.join(e.messages)}, status=400)

    # Buscar al cliente
    cliente = Cliente.objects.filter(email_principal=email).first()
    if not cliente:
        return JsonResponse({'error': 'No existe un cliente registrado con este correo'}, status=404)

    if not cliente.is_active:
        return JsonResponse({
            'error': 'Este cliente está bloqueado y no puede registrar una cuenta. Contacta a soporte.',
            'code': 'CLIENTE_BLOQUEADO',
        }, status=403)

    User = get_user_model()
    if User.objects.filter(username=email).exists():
        return JsonResponse({'error': 'Ya existe una cuenta con este correo'}, status=400)

    # Crear usuario inactivo: no puede autenticarse hasta confirmar el correo
    try:
        from tenants.models import RolTenant
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            tenant_id=cliente.tenant_id,
            role=RolTenant.CLIENTE,
            cliente=cliente,
            is_active=False,
        )
    except Exception as e:
        return JsonResponse({'error': f'Error al crear la cuenta: {str(e)}'}, status=500)

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = f"{settings.FRONTEND_BASE_URL}/portal/confirmar/{uid}/{token}"
    try:
        send_mail(
            'Confirma tu cuenta — Enfoque Platform',
            (
                f'Hola,\n\n'
                'Creaste una cuenta en el portal de clientes de Enfoque Platform (CLM) '
                f'asociada al correo {email}.\n\n'
                f'Confirma tu cuenta abriendo este enlace:\n{url}\n\n'
                'El enlace es de un solo uso y expira pronto. Si no solicitaste esto, '
                'ignora este correo: la cuenta no queda activa.'
            ),
            None,  # DEFAULT_FROM_EMAIL
            [email],
        )
    except Exception:
        logger.exception('Fallo al enviar correo de confirmación de registro para user id=%s', user.pk)
        user.delete()
        return JsonResponse({'error': 'No se pudo enviar el correo de confirmación. Intenta nuevamente.'}, status=500)

    return JsonResponse({
        'success': 'Cuenta creada. Revisa tu correo para confirmarla antes de iniciar sesión.',
    })


@csrf_exempt
@require_POST
def api_register_cliente_confirm(request):
    """Valida uid+token del correo de registro y activa la cuenta."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    uid = data.get('uid')
    token = data.get('token')
    if not uid or not token:
        return JsonResponse({'error': 'Falta uid o token'}, status=400)

    User = get_user_model()
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)), is_active=False)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return JsonResponse({'error': 'El enlace de confirmación es inválido o expiró.'}, status=400)

    user.is_active = True
    user.save(update_fields=['is_active'])
    return JsonResponse({'success': 'Cuenta confirmada. Ya puedes iniciar sesión.'})
