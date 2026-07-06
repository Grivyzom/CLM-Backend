from rest_framework import serializers
from .models import PersonaJuridica, PersonaNatural, ContactoRepresentante, Cliente
from contratos.models import Contrato


class ContactoRepresentanteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactoRepresentante
        fields = ['id', 'nombre', 'cargo', 'email', 'telefono']


class PersonaJuridicaSerializer(serializers.ModelSerializer):
    """Serializa una PersonaJuridica como un registro unificado de cliente."""
    tipo = serializers.SerializerMethodField()
    estado = serializers.SerializerMethodField()
    id_fiscal = serializers.CharField(source='rut')
    nombre_comercial = serializers.CharField(source='razon_social')
    razon_social = serializers.CharField()
    sector = serializers.CharField(source='giro')
    email = serializers.CharField(source='email_principal')
    telefono = serializers.CharField(source='telefono_contacto', default='')
    fecha_registro = serializers.DateTimeField()
    fecha_modificacion = serializers.DateTimeField()
    contratos_count = serializers.SerializerMethodField()
    contacto_principal = serializers.SerializerMethodField()
    contacto_tel = serializers.SerializerMethodField()

    class Meta:
        model = PersonaJuridica
        fields = [
            'id', 'tipo', 'razon_social', 'nombre_comercial',
            'id_fiscal', 'sector', 'estado',
            'email', 'telefono', 'fecha_registro', 'fecha_modificacion',
            'contratos_count', 'contacto_principal', 'contacto_tel',
        ]

    def get_tipo(self, obj):
        return 'juridica'

    def get_estado(self, obj):
        if not obj.is_active:
            return 'Inactivo'
        # Revisar si tiene contratos activos
        has_active = Contrato.objects.filter(
            cliente_id=obj.id, status='ACTIVO'
        ).exists()
        if has_active:
            return 'Activo'
        has_mora = Contrato.objects.filter(
            cliente_id=obj.id, status__in=['MORA', 'GRACIA']
        ).exists()
        if has_mora:
            return 'En revisión'
        return 'Activo'

    def get_contratos_count(self, obj):
        return self._get_from_context(obj, 'contratos_count', 0)

    def get_contacto_principal(self, obj):
        contacto = self._get_from_context(obj, 'contacto', None)
        if contacto:
            return contacto.get('nombre', '')
        return ''

    def get_contacto_tel(self, obj):
        contacto = self._get_from_context(obj, 'contacto', None)
        if contacto:
            return contacto.get('telefono', '') or ''
        return obj.telefono_contacto or ''

    def _get_from_context(self, obj, key, default):
        context = self.context.get('extra', {})
        return context.get(obj.id, {}).get(key, default)


class PersonaNaturalSerializer(serializers.ModelSerializer):
    """Serializa una PersonaNatural como un registro unificado de cliente."""
    tipo = serializers.SerializerMethodField()
    estado = serializers.SerializerMethodField()
    id_fiscal = serializers.CharField(source='run')
    nombre_comercial = serializers.CharField(source='nombre_completo')
    razon_social = serializers.CharField(source='nombre_completo')
    sector = serializers.SerializerMethodField()
    email = serializers.CharField(source='email_principal')
    telefono = serializers.CharField(source='telefono_contacto', default='')
    fecha_registro = serializers.DateTimeField()
    fecha_modificacion = serializers.DateTimeField()
    contratos_count = serializers.SerializerMethodField()
    contacto_principal = serializers.SerializerMethodField()
    contacto_tel = serializers.SerializerMethodField()

    class Meta:
        model = PersonaNatural
        fields = [
            'id', 'tipo', 'razon_social', 'nombre_comercial',
            'id_fiscal', 'sector', 'estado',
            'email', 'telefono', 'fecha_registro', 'fecha_modificacion',
            'contratos_count', 'contacto_principal', 'contacto_tel',
        ]

    def get_tipo(self, obj):
        return 'natural'

    def get_sector(self, obj):
        return 'Persona Natural'

    def get_estado(self, obj):
        if not obj.is_active:
            return 'Inactivo'
        has_active = Contrato.objects.filter(
            cliente_id=obj.id, status='ACTIVO'
        ).exists()
        if has_active:
            return 'Activo'
        has_mora = Contrato.objects.filter(
            cliente_id=obj.id, status__in=['MORA', 'GRACIA']
        ).exists()
        if has_mora:
            return 'En revisión'
        return 'Activo'

    def get_contratos_count(self, obj):
        return self._get_from_context(obj, 'contratos_count', 0)

    def get_contacto_principal(self, obj):
        return obj.nombre_completo

    def get_contacto_tel(self, obj):
        return obj.telefono_contacto or ''

    def _get_from_context(self, obj, key, default):
        context = self.context.get('extra', {})
        return context.get(obj.id, {}).get(key, default)


class ClienteStatsSerializer(serializers.Serializer):
    """Estadísticas globales de clientes."""
    total = serializers.IntegerField()
    activos = serializers.IntegerField()
    en_revision = serializers.IntegerField()
    inactivos = serializers.IntegerField()
