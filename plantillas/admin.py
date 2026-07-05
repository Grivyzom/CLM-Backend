from django.contrib import admin

from .models import PlantillaDocumento, DocumentoGenerado


@admin.register(PlantillaDocumento)
class PlantillaDocumentoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo_contrato', 'software', 'version_codigo', 'activa', 'fecha_creacion')
    list_filter = ('tipo_contrato', 'software', 'activa')
    search_fields = ('nombre', 'version_codigo')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.subida_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(DocumentoGenerado)
class DocumentoGeneradoAdmin(admin.ModelAdmin):
    list_display = ('id', 'contrato', 'plantilla', 'fecha_generacion', 'generado_por')
    list_filter = ('plantilla__tipo_contrato', 'fecha_generacion')
    search_fields = ('contrato__id', 'hash_sha256')

    # Convierte todos los campos a solo lectura en la vista de detalle
    readonly_fields = [f.name for f in DocumentoGenerado._meta.fields]

    # ==========================================
    # BLOQUEO DE SEGURIDAD: documento inmutable
    # ==========================================
    def has_add_permission(self, request):
        # Un documento generado solo nace vía el motor de plantillas, nunca manualmente
        return False

    def has_change_permission(self, request, obj=None):
        # Un documento generado jamás se edita
        return False

    def has_delete_permission(self, request, obj=None):
        # Restricción legal: prohibido borrar el registro de un documento emitido
        return False
