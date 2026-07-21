import os
import sys
import django
import json

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from catalogo.models import Software, Producto
from tenants.models import Tenant

tenant = Tenant.objects.first()
if not tenant:
    print("No tenant found!")
    sys.exit(1)

info = """
Kyo Workspace is a full-stack application composed of:
1. kyo-backend: A Go-based backend (Go 1.23+) handling data storage and business logic.
2. kyo-frontend: A modern React application using Vite, TypeScript, and Tailwind CSS.
Features include:
- Interactive diagrams and whiteboarding using Mermaid and react-force-graph-2d.
- Complex animations powered by GSAP (@gsap/react).
- Math rendering using KaTeX and mathjs.
- Video/Audio processing capabilities using @ffmpeg/ffmpeg.
- State management via Zustand and immer.
- Google OAuth integration (@react-oauth/google).
- Custom modern fonts (Caveat, JetBrains Mono, Kalam).
"""

sw, created = Software.objects.get_or_create(
    tenant=tenant,
    nombre="kyoworkspace",
    slug="kyoworkspace",
)
sw.descripcion = info
sw.save()
print(f"Software updated: {sw.nombre}")

prod, created = Producto.objects.get_or_create(
    tenant=tenant,
    sku="KYO-001",
    defaults={
        "nombre": "kyoworkspace",
        "categoria": "Software",
        "tipo_licencia": "Comercial",
        "precio": 0.00,
        "moneda": "USD",
        "estado": "Activo"
    }
)
prod.descripcion = info
prod.save()
print(f"Producto updated: {prod.nombre}")
