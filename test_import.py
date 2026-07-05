import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from documentos.services.exportar import clientes_a_excel
from documentos.services.importar import excel_a_clientes
from clientes.models import Cliente

print(f"Total clients before: {Cliente.objects.count()}")

# Export to memory
buf = clientes_a_excel(Cliente.objects.all())

# Import from memory
res = excel_a_clientes(buf)

print(f"Creados: {len(res['creados'])}")
print(f"Actualizados: {len(res['actualizados'])}")
print(f"Errores: {len(res['errores'])}")
if res['errores']:
    for e in res['errores'][:10]:
        print(e)
