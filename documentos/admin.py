from django.contrib import admin
from .models import TipoDocumento, RequisitoDocumental

@admin.register(TipoDocumento)
class TipoDocumentoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tenant')
    search_fields = ('nombre',)
    list_filter = ('tenant',)

@admin.register(RequisitoDocumental)
class RequisitoDocumentalAdmin(admin.ModelAdmin):
    list_display = ('tipo_documento', 'tipo_cliente', 'categoria_producto', 'producto_especifico', 'es_obligatorio', 'tenant')
    list_filter = ('tipo_cliente', 'es_obligatorio', 'categoria_producto', 'tenant')
    search_fields = ('tipo_documento__nombre',)
