"""Verificación end-to-end del workspace de cliente sobre una DB efímera.

Usa el test runner de Django (DB de test aislada) y ejercita:
scoping CLIENTE, bloqueo real (middleware + login), workspace, timeline,
correos (backend locmem) y notificaciones.
"""
import datetime

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from catalogo.models import Producto
from clientes.models import PersonaJuridica, PersonaNatural
from contratos.models import SLA, Contrato, RegistroPerdonazo
from tenants.models import RolTenant, Tenant, User


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    AXES_ENABLED=False,
)
class WorkspaceE2E(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(razon_social='ACME SpA', categoria='PLATINO')
        cls.otro_tenant = Tenant.objects.create(razon_social='Otro Ltda', categoria='COBRE')

        cls.cliente_a = PersonaJuridica.objects.create(
            tenant=cls.tenant, email_principal='a@acme.cl', rut='76.111.111-1',
            razon_social='Cliente A SpA', giro='Software')
        cls.cliente_b = PersonaNatural.objects.create(
            tenant=cls.tenant, email_principal='b@acme.cl', run='11.111.111-1',
            nombre_completo='Benito Bravo')

        cls.admin = User.objects.create_user(
            username='admin_x', password='pw12345', tenant=cls.tenant, role=RolTenant.TENANT_ADMIN)
        cls.user_cliente = User.objects.create_user(
            username='ucliente', password='pw12345', tenant=cls.tenant,
            role=RolTenant.CLIENTE, cliente=cls.cliente_a)
        cls.superadmin = User.objects.create_superuser(
            username='root_x', password='pw12345')

        cls.producto = Producto.objects.create(
            tenant=cls.tenant, sku='SW-1', nombre='KYO Suite', precio=100)

        sla = SLA.objects.create(tenant=cls.tenant, nombre='SLA base',
                                 uptime_garantizado=99.9, tiempo_respuesta_horas=4)
        hoy = timezone.localdate()
        cls.contrato = Contrato.objects.create(
            tenant=cls.tenant, cliente=cls.cliente_a, software=cls.producto, sla=sla,
            tipo_contrato='RECURRENTE', status='MORA', monto=1200,
            frecuencia_facturacion='MENSUAL',
            fecha_inicio=hoy - datetime.timedelta(days=95),
            fecha_vencimiento=hoy + datetime.timedelta(days=270),
        )
        RegistroPerdonazo.objects.create(
            contrato=cls.contrato, dias_extendidos=10, motivo='Cliente antiguo',
            fecha_vencimiento_anterior=hoy - datetime.timedelta(days=5))

    def login(self, username):
        ok = self.client.login(username=username, password='pw12345')
        self.assertTrue(ok, f'login {username} falló')

    # ── Workspace y timeline (staff) ────────────────────────────────────────
    def test_workspace_staff(self):
        self.login('admin_x')
        r = self.client.get(f'/api/clientes/{self.cliente_a.id}/workspace/')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data['perfil']['razon_social'], 'Cliente A SpA')
        self.assertEqual(data['tipo'], 'juridica')
        self.assertEqual(len(data['contratos']), 1)
        self.assertEqual(data['membresia']['categoria'], 'PLATINO')
        self.assertIn('usuarios_cuenta', data)
        self.assertEqual(len(data['usuarios_cuenta']), 1)
        self.assertTrue(any(a['tipo'] == 'REGISTRO' for a in data['actividad']))

    def test_timeline_pagos(self):
        self.login('admin_x')
        r = self.client.get(f'/api/clientes/{self.cliente_a.id}/timeline-pagos/')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        tipos = {e['tipo'] for e in data['eventos']}
        self.assertIn('INICIO_CONTRATO', tipos)
        self.assertIn('VENCIMIENTO_CUOTA', tipos)  # ~3 cuotas de 95 días
        self.assertIn('PERDONAZO', tipos)
        self.assertIn('ESTADO_COBRANZA', tipos)    # está en MORA
        self.assertEqual(data['resumen']['total_contratos'], 1)
        self.assertEqual(data['resumen']['en_mora'], 1)
        cuotas = [e for e in data['eventos'] if e['tipo'] == 'VENCIMIENTO_CUOTA']
        self.assertEqual(len(cuotas), 3)

    # ── Correos ─────────────────────────────────────────────────────────────
    def test_enviar_correo_y_historial(self):
        self.login('admin_x')
        r = self.client.post(
            f'/api/clientes/{self.cliente_a.id}/enviar-correo/',
            {'asunto': 'Hola', 'cuerpo': 'Mensaje de prueba'},
            content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['a@acme.cl'])

        r = self.client.get(f'/api/clientes/{self.cliente_a.id}/correos/')
        self.assertEqual(r.status_code, 200)
        results = r.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['estado'], 'ENVIADO')

    def test_cliente_no_puede_enviar_correo(self):
        self.login('ucliente')
        r = self.client.post(
            f'/api/clientes/{self.cliente_a.id}/enviar-correo/',
            {'asunto': 'x', 'cuerpo': 'y'}, content_type='application/json')
        self.assertEqual(r.status_code, 403)

    # ── Notificaciones ──────────────────────────────────────────────────────
    def test_notificaciones_flujo(self):
        self.login('admin_x')
        r = self.client.post(
            '/api/notificaciones/',
            {'cliente_id': self.cliente_a.id, 'titulo': 'Aviso', 'cuerpo': 'Pago pendiente', 'tipo': 'AVISO'},
            content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content)
        notif_id = r.json()['id']

        # El usuario-cliente la ve y su unread-count es 1
        self.client.logout()
        self.login('ucliente')
        r = self.client.get('/api/notificaciones/unread-count/')
        self.assertEqual(r.json()['count'], 1)
        r = self.client.get('/api/notificaciones/')
        self.assertEqual(len(r.json()['results']), 1)

        # No puede crear
        r = self.client.post(
            '/api/notificaciones/',
            {'cliente_id': self.cliente_a.id, 'titulo': 'x', 'cuerpo': 'y'},
            content_type='application/json')
        self.assertEqual(r.status_code, 403)

        # Marca leída → count 0
        r = self.client.post(f'/api/notificaciones/{notif_id}/leer/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['leida'])
        r = self.client.get('/api/notificaciones/unread-count/')
        self.assertEqual(r.json()['count'], 0)

    # ── Scoping CLIENTE ─────────────────────────────────────────────────────
    def test_scoping_cliente(self):
        self.login('ucliente')
        # Ve su propio workspace
        r = self.client.get(f'/api/clientes/{self.cliente_a.id}/workspace/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn('usuarios_cuenta', r.json())  # oculto para rol CLIENTE
        # No ve el workspace de otro cliente del mismo tenant
        r = self.client.get(f'/api/clientes/{self.cliente_b.id}/workspace/')
        self.assertEqual(r.status_code, 404)
        # Contratos: solo los suyos
        r = self.client.get('/api/contratos/')
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        contratos = payload.get('results', payload)
        ids = {c['id'] for c in contratos} if isinstance(contratos, list) else set()
        self.assertEqual(ids, {self.contrato.id})

    # ── Bloqueo real ────────────────────────────────────────────────────────
    def test_bloqueo_real(self):
        # Sesión CLIENTE viva queda inerte al bloquear
        self.login('ucliente')
        r = self.client.get('/api/notificaciones/unread-count/')
        self.assertEqual(r.status_code, 200)

        self.cliente_a.is_active = False
        self.cliente_a.save()

        r = self.client.get('/api/notificaciones/unread-count/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'CLIENTE_BLOQUEADO')

        # Login rechazado
        self.client.logout()
        r = self.client.post('/api/auth/login/',
                             {'username': 'ucliente', 'password': 'pw12345'},
                             content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'CLIENTE_BLOQUEADO')

        # Staff sigue operativo
        self.login('admin_x')
        r = self.client.get(f'/api/clientes/{self.cliente_a.id}/workspace/')
        self.assertEqual(r.status_code, 200)

        # Desbloquear restaura acceso
        self.cliente_a.is_active = True
        self.cliente_a.save()
        self.client.logout()
        self.login('ucliente')
        r = self.client.get('/api/notificaciones/unread-count/')
        self.assertEqual(r.status_code, 200)
