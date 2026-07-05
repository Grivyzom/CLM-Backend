from django.contrib import admin
from .models import Software, SoftwareVersion, Producto

class VersionInline(admin.TabularInline):
    model = SoftwareVersion
    extra = 0

@admin.register(Software)
class SoftwareAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'slug', 'fecha_creacion', 'api_key')
    prepopulated_fields = {'slug': ('nombre',)}
    readonly_fields = ('api_key', 'fecha_creacion')
    inlines = [VersionInline]

@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ('sku', 'nombre', 'categoria', 'precio', 'moneda', 'estado')
    list_filter = ('categoria', 'estado')
    search_fields = ('sku', 'nombre')