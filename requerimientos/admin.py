from django.contrib import admin

from .models import PlantillaRequerimiento, Requerimiento, RequerimientoGenerado


@admin.register(PlantillaRequerimiento)
class PlantillaRequerimientoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'categoria_producto', 'tenant', 'activa', 'fecha_modificacion')
    list_filter = ('categoria_producto', 'activa')
    search_fields = ('nombre',)


@admin.register(Requerimiento)
class RequerimientoAdmin(admin.ModelAdmin):
    list_display = ('id', 'cliente', 'contrato', 'categoria_producto', 'estado', 'fecha_creacion')
    list_filter = ('categoria_producto', 'estado')
    search_fields = ('cliente__id', 'contrato__id')

    # Las respuestas se capturan vía el formulario del panel, no manualmente.
    readonly_fields = [f.name for f in Requerimiento._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RequerimientoGenerado)
class RequerimientoGeneradoAdmin(admin.ModelAdmin):
    list_display = ('id', 'requerimiento', 'fecha_generacion', 'generado_por')
    list_filter = ('fecha_generacion',)
    search_fields = ('requerimiento__id', 'hash_sha256')

    readonly_fields = [f.name for f in RequerimientoGenerado._meta.fields]

    # ==========================================
    # BLOQUEO DE SEGURIDAD: documento inmutable
    # ==========================================
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
