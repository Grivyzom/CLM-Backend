import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from clientes.models import PersonaJuridica, PersonaNatural
from django.db.models import ProtectedError

print("Testing delete on all PersonaJuridica...")
for pj in PersonaJuridica.objects.all():
    print(f"Trying to delete PJ: {pj.id}")
    try:
        pj.delete()
        print("Deleted!")
    except Exception as e:
        print(f"Error: {type(e).__name__} - {e}")

print("Testing delete on all PersonaNatural...")
for pn in PersonaNatural.objects.all():
    print(f"Trying to delete PN: {pn.id}")
    try:
        pn.delete()
        print("Deleted!")
    except Exception as e:
        print(f"Error: {type(e).__name__} - {e}")
