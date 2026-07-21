import os
import sys
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from catalogo.models import Software, Producto
from tenants.models import Tenant

tenant = Tenant.objects.first()
if not tenant:
    print("No tenant found!")
    sys.exit(1)

# Add Software
sw, created = Software.objects.get_or_create(
    tenant=tenant,
    nombre="kyoworkspace",
    slug="kyoworkspace",
    defaults={"descripcion": "Kyo Workspace Project - includes kyo-backend and kyo-frontend."}
)
print(f"Software {'created' if created else 'found'}: {sw.nombre}")

# Add Producto
prod, created = Producto.objects.get_or_create(
    tenant=tenant,
    sku="KYO-001",
    defaults={
        "nombre": "kyoworkspace",
        "descripcion": "Kyo Workspace App",
        "categoria": "Software",
        "tipo_licencia": "Comercial",
        "precio": 0.00,
        "moneda": "USD",
        "estado": "Activo"
    }
)
print(f"Producto {'created' if created else 'found'}: {prod.nombre}")

