from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0007_tokenfirmacontrato'),
    ]

    operations = [
        migrations.AddField(
            model_name='contrato',
            name='clausulas_estructuradas',
            field=models.JSONField(
                blank=True, null=True,
                help_text='Bloques del documento generado por cláusulas: lista de '
                          '{titulo, texto, clausula_id?, version_id?, origen, modificada}. '
                          'Tiene prioridad sobre texto_adicional_clausulas al renderizar.'
            ),
        ),
    ]
