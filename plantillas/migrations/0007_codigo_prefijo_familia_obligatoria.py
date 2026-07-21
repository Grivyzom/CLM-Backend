import re

from django.db import migrations, models

STOPWORDS = {'de', 'del', 'la', 'el', 'los', 'las', 'y', 'o', 'e',
             'para', 'con', 'por', 'a', 'en', 'un', 'una'}


def derivar_prefijo(nombre):
    """Deriva un prefijo de familia a partir del nombre, para poblar codigo_prefijo
    en filas creadas antes de que el campo fuera obligatorio para todos los modos.
    Recorta puntuación de borde por palabra (p.ej. "(NDA)" -> "NDA") para no dejar
    un signo suelto como inicial."""
    crudas = re.split(r'\s+', (nombre or '').strip())
    palabras = []
    for w in crudas:
        limpio = re.sub(r'^[^A-Za-z0-9]+|[^A-Za-z0-9]+$', '', w)
        if limpio and limpio.lower() not in STOPWORDS:
            palabras.append(limpio)
    iniciales = ''.join(w[0].upper() for w in palabras[:3])
    if iniciales:
        return iniciales[:20]
    solo_alnum = re.sub(r'[^A-Za-z0-9]', '', nombre or '')
    return solo_alnum[:20].upper() or 'DOC'


def backfill_codigo_prefijo(apps, schema_editor):
    PlantillaDocumento = apps.get_model('plantillas', 'PlantillaDocumento')
    qs = PlantillaDocumento.objects.filter(
        models.Q(codigo_prefijo__isnull=True) | models.Q(codigo_prefijo='')
    )
    for p in qs:
        p.codigo_prefijo = derivar_prefijo(p.nombre)
        p.save(update_fields=['codigo_prefijo'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("plantillas", "0006_plantilladocumento_requiere_sla_facturacion"),
    ]

    operations = [
        migrations.RunPython(backfill_codigo_prefijo, noop),
        migrations.AlterField(
            model_name="plantilladocumento",
            name="codigo_prefijo",
            field=models.CharField(
                max_length=20,
                help_text="Identificador de familia de documento (ej: NDA, MSA, TOS, REQ), compartido "
                          "por todas las versiones del mismo documento sin importar el modo de "
                          "generación. Agrupa las versiones en el catálogo y, para plantillas HTML, "
                          "arma el correlativo de 'Referencia' como PREFIJO-AÑO-NNN al generar el "
                          "documento.",
            ),
        ),
    ]
