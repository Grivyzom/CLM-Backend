from django.contrib import admin
from django.utils.html import format_html
from .models import SLA, Contrato, RegistroPerdonazo, HistorialEtapaContrato


@admin.register(SLA)
class SLAAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'uptime_garantizado', 'tiempo_respuesta_horas')

class PerdonazoInline(admin.TabularInline):
    model = RegistroPerdonazo
    extra = 0
    readonly_fields = ('fecha_concesion',)

class HistorialEtapaInline(admin.TabularInline):
    model = HistorialEtapaContrato
    extra = 0
    readonly_fields = ('etapa_anterior', 'etapa_nueva', 'fecha_cambio', 'usuario', 'notas')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

@admin.register(Contrato)
class ContratoAdmin(admin.ModelAdmin):
    list_display = ('id', 'cliente', 'software', 'tipo_contrato', 'etapa', 'semaforo_estado', 'dias_restantes_display', 'monto')
    list_filter = ('etapa', 'status', 'tipo_contrato', 'software')
    search_fields = ('cliente__email_principal',)
    inlines = [HistorialEtapaInline, PerdonazoInline]
    
    # Define qué campos agrupar visualmente al editar un contrato
    fieldsets = (
        ('Entidades', {
            'fields': ('cliente', 'software', 'sla')
        }),
        ('Condiciones Comerciales', {
            'fields': ('tipo_contrato', 'etapa', 'status', 'monto')
        }),
        ('Tiempos y Gracia', {
            'fields': ('fecha_inicio', 'fecha_vencimiento', 'dias_gracia_autorizados', 'fin_periodo_gracia')
        }),
    )

    def save_model(self, request, obj, form, change):
        if change:
            old_obj = Contrato.objects.get(pk=obj.pk)
            nueva_etapa = obj.etapa
            super().save_model(request, obj, form, change)
            
            if old_obj.etapa != nueva_etapa:
                HistorialEtapaContrato.objects.create(
                    contrato=obj,
                    etapa_anterior=old_obj.etapa,
                    etapa_nueva=nueva_etapa,
                    usuario=request.user,
                    notas="Cambio desde panel de administración"
                )
        else:
            super().save_model(request, obj, form, change)
            HistorialEtapaContrato.objects.create(
                contrato=obj,
                etapa_anterior=None,
                etapa_nueva=obj.etapa,
                usuario=request.user,
                notas="Creación inicial desde panel de administración"
            )


    def semaforo_estado(self, obj):
        colores = {
            'ACTIVO': 'green',
            'MORA': 'red',
            'GRACIA': 'orange',
            'SUSPENDIDO': 'black',
            'VENCIDO': 'gray'
        }
        color = colores.get(obj.status, 'black')
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, obj.get_status_display())
    semaforo_estado.short_description = 'Estado Operativo'

    def dias_restantes_display(self, obj):
        dias = obj.tiempo_restante
        if isinstance(dias, int) and dias <= 5:
            # Alerta visual si quedan 5 días o menos
            return format_html('<span style="color: red; font-weight: bold;">{} días</span>', dias)
        return dias
    dias_restantes_display.short_description = 'Tiempo Restante'