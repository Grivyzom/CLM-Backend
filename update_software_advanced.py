import os
import sys
import django
import json
from datetime import date

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from catalogo.models import Software, Producto, SoftwareVersion
from tenants.models import Tenant

tenant = Tenant.objects.first()
if not tenant:
    print("No tenant found!")
    sys.exit(1)

# Structured JSON data
datos_adicionales = {
    "repository_paths": {
        "frontend": "/grivyzom/proyectos/VrindaWorkspace/kyo-frontend",
        "backend": "/grivyzom/proyectos/VrindaWorkspace/kyo-backend",
        "context": "/grivyzom/proyectos/VrindaWorkspace/contexto"
    },
    "tech_stack": {
        "frontend": {
            "framework": "React (Vite)",
            "language": "TypeScript",
            "styling": "Tailwind CSS",
            "state_management": ["Zustand", "Immer"],
            "key_libraries": [
                "@gsap/react (GSAP for animations)",
                "react-force-graph-2d",
                "mermaid",
                "@ffmpeg/ffmpeg (Video/Audio processing)",
                "katex",
                "mathjs"
            ],
            "fonts": ["Caveat", "JetBrains Mono", "Kalam"]
        },
        "backend": {
            "language": "Go",
            "version": "1.23+"
        }
    },
    "environment": "Development",
    "supported_features": [
        "Whiteboarding",
        "Complex Animations",
        "Interactive Diagrams",
        "Media Processing",
        "Google OAuth Authentication",
        "Math rendering"
    ]
}

# Update Producto with datos_adicionales
prod = Producto.objects.filter(tenant=tenant, sku="KYO-001").first()
if prod:
    prod.datos_adicionales = datos_adicionales
    prod.unidad = "Licencia / Usuario"
    prod.save()
    print(f"Producto {prod.nombre} updated with datos_adicionales.")

# Create or Update SoftwareVersion
sw = Software.objects.filter(tenant=tenant, slug="kyoworkspace").first()
if sw:
    version, created = SoftwareVersion.objects.get_or_create(
        software=sw,
        version_semver="0.0.0",
        defaults={
            "changelog": "Initial development version. Core stack setup with Go backend and React frontend.",
            "fecha_liberacion": date.today()
        }
    )
    print(f"SoftwareVersion {version.version_semver} {'created' if created else 'found'} for {sw.nombre}.")
