from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .membresias import sincronizar_categoria
from .models import ClienteGrant, Membresia, Tenant, User


class MembresiaInline(admin.TabularInline):
    model = Membresia
    fk_name = 'tenant'
    extra = 0
    fields = ('categoria', 'estado', 'fecha_inicio', 'fecha_expiracion', 'otorgada_por', 'notas')
    readonly_fields = ('otorgada_por',)
    ordering = ('-fecha_inicio',)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('razon_social', 'categoria', 'estado', 'fecha_creacion')
    list_filter = ('categoria', 'estado')
    search_fields = ('razon_social',)
    # categoria es denormalizada: la escribe tenants/membresias.py a partir de
    # la Membresia ACTIVA (inline de abajo), no se edita a mano.
    readonly_fields = ('id', 'categoria', 'fecha_creacion', 'fecha_modificacion')
    inlines = (MembresiaInline,)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        sincronizar_categoria(form.instance)


@admin.register(Membresia)
class MembresiaAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'categoria', 'estado', 'fecha_inicio', 'fecha_expiracion', 'otorgada_por')
    list_filter = ('categoria', 'estado')
    search_fields = ('tenant__razon_social',)
    autocomplete_fields = ('tenant',)
    readonly_fields = ('otorgada_por', 'fecha_creacion', 'fecha_modificacion')

    def save_model(self, request, obj, form, change):
        if not obj.otorgada_por_id:
            obj.otorgada_por = request.user
        super().save_model(request, obj, form, change)
        sincronizar_categoria(obj.tenant)

    def delete_model(self, request, obj):
        tenant = obj.tenant
        super().delete_model(request, obj)
        sincronizar_categoria(tenant)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ('username', 'email', 'tenant', 'role', 'platform_role', 'is_staff', 'is_active')
    list_filter = DjangoUserAdmin.list_filter + ('tenant', 'role', 'platform_role')
    autocomplete_fields = ('cliente',)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('Tenant', {'fields': ('tenant', 'role', 'cliente')}),
        ('Staff de plataforma', {
            'fields': ('platform_role',),
            'description': (
                'Solo aplica cuando Tenant está vacío: SuperAdmin (o marcar '
                '"Superusuario" abajo), Moderador o Trabajador. Un Trabajador '
                'no ve nada hasta que se le concedan Clientes en la sección '
                '"Concesiones de acceso a clientes".'
            ),
        }),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('Tenant', {'fields': ('tenant', 'role', 'cliente')}),
        ('Staff de plataforma', {'fields': ('platform_role',)}),
    )


@admin.register(ClienteGrant)
class ClienteGrantAdmin(admin.ModelAdmin):
    """Acá el SuperAdmin concede a un Trabajador acceso de solo lectura a un
    Cliente puntual (y sus contratos, resuelto automáticamente por cliente_id)."""
    list_display = ('trabajador', 'cliente', 'otorgado_por', 'fecha_creacion')
    autocomplete_fields = ('trabajador', 'cliente')
    readonly_fields = ('fecha_creacion',)
    search_fields = ('trabajador__username', 'cliente__email_principal')

    def save_model(self, request, obj, form, change):
        if not obj.otorgado_por_id:
            obj.otorgado_por = request.user
        super().save_model(request, obj, form, change)
