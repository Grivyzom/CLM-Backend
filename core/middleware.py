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
