import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()
from axes.decorators import axes_dispatch
print("axes_dispatch exists")
