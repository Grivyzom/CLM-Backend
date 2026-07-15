from django.contrib import admin

from .models import (
    AdjuntoIncidencia,
    ComentarioIncidencia,
    HistorialEstadoIncidencia,
    Incidencia,
)


class ComentarioInline(admin.TabularInline):
    model = ComentarioIncidencia
    extra = 0
    readonly_fields = ('fecha_creacion',)


class AdjuntoInline(admin.TabularInline):
    model = AdjuntoIncidencia
    extra = 0
    readonly_fields = ('fecha_subida',)


class HistorialEstadoInline(admin.TabularInline):
    model = HistorialEstadoIncidencia
    extra = 0
    readonly_fields = ('estado_anterior', 'estado_nuevo', 'usuario', 'fecha_cambio')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Incidencia)
class IncidenciaAdmin(admin.ModelAdmin):
    list_display = ('id', 'titulo', 'cliente', 'tenant', 'severidad', 'estado', 'asignado_a', 'fecha_creacion')
    list_filter = ('estado', 'severidad', 'tenant')
    search_fields = ('titulo', 'cliente__email_principal')
    autocomplete_fields = ('cliente', 'contrato', 'software', 'reportado_por', 'asignado_a')
    inlines = [ComentarioInline, AdjuntoInline, HistorialEstadoInline]

    def save_model(self, request, obj, form, change):
        if change:
            old_obj = Incidencia.objects.get(pk=obj.pk)
            nuevo_estado = obj.estado
            obj.estado = old_obj.estado
            super().save_model(request, obj, form, change)
            if old_obj.estado != nuevo_estado:
                obj.transicionar_estado(nuevo_estado, usuario=request.user)
        else:
            super().save_model(request, obj, form, change)
