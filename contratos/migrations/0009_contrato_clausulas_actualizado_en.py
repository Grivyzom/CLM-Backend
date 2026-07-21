# Generated migration for clausulas_actualizado_en field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0008_contrato_clausulas_estructuradas'),
    ]

    operations = [
        migrations.AddField(
            model_name='contrato',
            name='clausulas_actualizado_en',
            field=models.DateTimeField(blank=True, editable=False, help_text='Fecha de última edición de cláusulas estructuradas o texto adicional', null=True),
        ),
    ]
