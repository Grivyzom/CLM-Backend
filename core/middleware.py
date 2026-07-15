import threading
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from catalogo.models import Software

_thread_locals = threading.local()

class ThreadLocalContext:
    @staticmethod
    def get_current_software_id():
        return getattr(_thread_locals, 'software_id', None)

    @staticmethod
    def set_current_software_id(software_id):
        _thread_locals.software_id = software_id

    @staticmethod
    def clear():
        if hasattr(_thread_locals, 'software_id'):
            del _thread_locals.software_id


class ClienteBloqueadoMiddleware:
    """Corta el acceso API de usuarios rol CLIENTE cuyo Cliente fue bloqueado
    (is_active=False). Solo consulta la DB para ese rol; staff y usuarios de
    tenant no-CLIENTE pasan sin costo. Las sesiones vivas quedan inertes sin
    necesidad de invalidarlas."""

    EXEMPT_PATHS = ('/api/auth/logout/',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (
            request.path.startswith('/api/')
            and request.path not in self.EXEMPT_PATHS
            and user is not None
            and user.is_authenticated
            and user.tenant_id is not None
        ):
            from tenants.models import RolTenant
            if getattr(user, 'role', None) == RolTenant.CLIENTE:
                from clientes.models import Cliente
                bloqueado = (
                    user.cliente_id is None
                    or not Cliente.objects.filter(pk=user.cliente_id, is_active=True).exists()
                )
                if bloqueado:
                    return JsonResponse({
                        'error': 'Tu cuenta de cliente está bloqueada. Contacta a soporte.',
                        'code': 'CLIENTE_BLOQUEADO',
                    }, status=403)
        return self.get_response(request)


class SoftwareIsolationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        api_key = request.headers.get('api-key')
        
        # Omitimos validación en el admin o rutas que no sean API si fuera necesario, 
        # pero como el requerimiento es aislar la API, validamos aquí:
        if api_key:
            try:
                software = Software.objects.get(api_key=api_key)
                ThreadLocalContext.set_current_software_id(software.id)
            except (Software.DoesNotExist, ValueError):
                return JsonResponse({'error': 'Invalid API Key'}, status=401)
        
        response = self.get_response(request)
        
        # Limpiar el contexto para la siguiente request en el mismo thread
        ThreadLocalContext.clear()
        
        return response
