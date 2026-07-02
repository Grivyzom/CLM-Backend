from django.contrib import admin
from .models import Cliente, PersonaNatural, PersonaJuridica, ContactoRepresentante

class ContactoInline(admin.TabularInline):
    model = ContactoRepresentante
    extra = 1  # Muestra una fila vacía por defecto para agregar un nuevo contacto

@admin.register(PersonaJuridica)
class PersonaJuridicaAdmin(admin.ModelAdmin):
    list_display = ('rut', 'razon_social', 'email_principal', 'is_active')
    search_fields = ('rut', 'razon_social', 'email_principal')
    list_filter = ('is_active',)
    inlines = [ContactoInline]

@admin.register(PersonaNatural)
class PersonaNaturalAdmin(admin.ModelAdmin):
    list_display = ('run', 'nombre_completo', 'email_principal', 'is_active')
    search_fields = ('run', 'nombre_completo', 'email_principal')
    list_filter = ('is_active',)