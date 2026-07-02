from django.contrib import admin
from .models import DocumentoLegal, LogAceptacion

@admin.register(DocumentoLegal)
class DocumentoLegalAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'version_codigo', 'is_vigente', 'fecha_publicacion')
    list_filter = ('tipo', 'is_vigente')
    search_fields = ('version_codigo',)

@admin.register(LogAceptacion)
class LogAceptacionAdmin(admin.ModelAdmin):
    list_display = ('id', 'cliente', 'software', 'documento_legal', 'fecha_hora_registro', 'ip_direccion')
    list_filter = ('software', 'documento_legal__tipo', 'fecha_hora_registro')
    search_fields = ('ip_direccion', 'cliente__email_principal')
    
    # Convierte todos los campos a solo lectura en la vista de detalle
    readonly_fields = [f.name for f in LogAceptacion._meta.fields]

    # ==========================================
    # BLOQUEO DE SEGURIDAD PARA AUDITORÍA
    # ==========================================
    def has_add_permission(self, request):
        # Los logs solo nacen vía API Webhook, nunca manualmente desde el panel
        return False

    def has_change_permission(self, request, obj=None):
        # Un log guardado jamás se edita
        return False

    def has_delete_permission(self, request, obj=None):
        # Restricción Legal: Prohibido borrar evidencia
        return False