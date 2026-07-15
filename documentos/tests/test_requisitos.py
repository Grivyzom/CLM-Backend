from django.test import TestCase
from tenants.models import Tenant
from clientes.models import PersonaNatural, PersonaJuridica
from catalogo.models import Producto
from documentos.models import TipoDocumento, RequisitoDocumental
from documentos.services.requisitos import obtener_documentos_necesarios

class RequisitosDocumentalesTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(nombre="Test Tenant", schema_name="test_tenant")
        
        # Crear Clientes
        self.cliente_natural = PersonaNatural.objects.create(
            tenant=self.tenant, 
            email_principal="nat@test.com", 
            run="12345678-9", 
            nombre_completo="Juan Perez"
        )
        self.cliente_juridico = PersonaJuridica.objects.create(
            tenant=self.tenant, 
            email_principal="jur@test.com", 
            rut="98765432-1", 
            razon_social="Empresa SPA",
            giro="Tecnologia"
        )
        
        # Crear Productos
        self.producto_bot = Producto.objects.create(
            tenant=self.tenant,
            sku="BOT-01",
            nombre="Bot Test",
            categoria="Bot",
            precio=10.0
        )
        
        self.producto_software = Producto.objects.create(
            tenant=self.tenant,
            sku="SOFT-01",
            nombre="Software Test",
            categoria="Software",
            precio=100.0
        )
        
        # Crear Tipos de Documento
        self.rut_empresa = TipoDocumento.objects.create(tenant=self.tenant, nombre="RUT de Empresa")
        self.cedula = TipoDocumento.objects.create(tenant=self.tenant, nombre="Cedula de Identidad")
        self.contrato_bot = TipoDocumento.objects.create(tenant=self.tenant, nombre="Acuerdo Especial para Bot")

    def test_regla_solo_persona_juridica(self):
        # Exigir RUT de empresa SOLO a personas jurídicas, sin importar el producto
        RequisitoDocumental.objects.create(
            tenant=self.tenant,
            tipo_documento=self.rut_empresa,
            tipo_cliente='JURIDICA',
            es_obligatorio=True
        )
        
        # Para natural debería ser vacío
        docs_natural = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_natural)
        self.assertEqual(len(docs_natural), 0)
        
        # Para juridica debería retornar 1 documento
        docs_juridica = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_juridico)
        self.assertEqual(len(docs_juridica), 1)
        self.assertEqual(docs_juridica[0]['tipo_documento'].nombre, "RUT de Empresa")

    def test_regla_por_categoria_producto(self):
        # Exigir Acuerdo Especial para cualquier producto tipo Bot, para todos los clientes
        RequisitoDocumental.objects.create(
            tenant=self.tenant,
            tipo_documento=self.contrato_bot,
            tipo_cliente='TODOS',
            categoria_producto='Bot',
            es_obligatorio=True
        )
        
        # Si compran el software (no bot)
        docs_soft = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_natural, producto=self.producto_software)
        self.assertEqual(len(docs_soft), 0)
        
        # Si compran el bot
        docs_bot = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_juridico, producto=self.producto_bot)
        self.assertEqual(len(docs_bot), 1)
        self.assertEqual(docs_bot[0]['tipo_documento'].nombre, "Acuerdo Especial para Bot")

    def test_superposicion_reglas(self):
        # Regla 1: Cedula requerida para todos
        RequisitoDocumental.objects.create(
            tenant=self.tenant,
            tipo_documento=self.cedula,
            tipo_cliente='TODOS',
            es_obligatorio=True
        )
        
        # Regla 2: RUT requerido para juridica que compre Software
        RequisitoDocumental.objects.create(
            tenant=self.tenant,
            tipo_documento=self.rut_empresa,
            tipo_cliente='JURIDICA',
            categoria_producto='Software',
            es_obligatorio=True
        )
        
        # Natural + Software -> solo Cedula
        docs = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_natural, producto=self.producto_software)
        self.assertEqual(len(docs), 1)
        
        # Juridica + Bot -> solo Cedula (porque RUT es para Software)
        docs2 = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_juridico, producto=self.producto_bot)
        self.assertEqual(len(docs2), 1)
        
        # Juridica + Software -> Cedula y RUT
        docs3 = obtener_documentos_necesarios(self.tenant.id, cliente=self.cliente_juridico, producto=self.producto_software)
        self.assertEqual(len(docs3), 2)
