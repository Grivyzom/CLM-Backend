import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from contratos.models import Contrato
from plantillas.services.renderizado import generar_documento

c = Contrato.objects.get(id=50017)
print("Contract:", c)
try:
    doc = generar_documento(c)
    print("Generated doc:", doc)
except Exception as e:
    import traceback
    traceback.print_exc()

