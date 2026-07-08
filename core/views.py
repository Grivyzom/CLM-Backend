import json
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from django_otp.plugins.otp_totp.models import TOTPDevice
from .models import SystemConfig
from .currency_config import get_currency_config, SUPPORTED_CURRENCIES

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

        return JsonResponse({'success': 'Sesión iniciada con éxito', 'username': user.username})
    else:
        return JsonResponse({'error': 'Credenciales inválidas'}, status=401)

@csrf_exempt
@require_POST
def api_logout(request):
    logout(request)
    return JsonResponse({'success': 'Sesión cerrada exitosamente'})


@require_http_methods(["GET"])
def api_me(request):
    """Devuelve el usuario de la sesión Django activa, o 401 si no hay sesión."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    return JsonResponse({
        'username': request.user.username,
        'is_staff': request.user.is_staff,
    })

def login_page(request):
    return render(request, 'login.html')


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

