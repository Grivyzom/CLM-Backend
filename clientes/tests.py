from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()
from rest_framework.test import APIClient
from rest_framework import status
from clientes.models import PersonaJuridica, PersonaNatural, ContactoRepresentante

class ClientesAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser('admin', 'admin@example.com', 'admin')
        self.client.force_authenticate(user=self.user)

    def test_crear_cliente_natural_exitoso(self):
        data = {
            'tipo': 'natural',
            'email_principal': 'juan@natural.com',
            'telefono_contacto': '+56912345678',
            'run': '12345678-9',
            'nombre_completo': 'Juan Perez'
        }
        response = self.client.post('/api/clientes/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PersonaNatural.objects.count(), 1)

    def test_crear_cliente_juridico_exitoso(self):
        data = {
            'tipo': 'juridica',
            'email_principal': 'contacto@empresa.com',
            'telefono_contacto': '+56912345678',
            'rut': '76123456-7',
            'razon_social': 'Empresa SA',
            'giro': 'Tecnologia'
        }
        response = self.client.post('/api/clientes/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PersonaJuridica.objects.count(), 1)

    def test_crear_cliente_juridico_con_representante(self):
        data = {
            'tipo': 'juridica',
            'email_principal': 'contacto@empresa.com',
            'telefono_contacto': '+56912345678',
            'rut': '76123456-7',
            'razon_social': 'Empresa SA',
            'giro': 'Tecnologia',
            'contacto_representante': {
                'nombre': 'Carlos Representante',
                'cargo': 'Gerente',
                'email': 'carlos@empresa.com',
                'telefono': '+56987654321'
            }
        }
        response = self.client.post('/api/clientes/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PersonaJuridica.objects.count(), 1)
        self.assertEqual(ContactoRepresentante.objects.count(), 1)
