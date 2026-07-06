import django.utils.timezone
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0003_contrato_etapa_historialetapacontrato'),
    ]

    operations = [
        migrations.AddField(
            model_name='contrato',
            name='fecha_creacion',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='contrato',
            name='frecuencia_facturacion',
            field=models.CharField(
                blank=True, null=True, max_length=10,
                choices=[('MENSUAL', 'Mensual'), ('ANUAL', 'Anual')],
            ),
        ),
        migrations.AddConstraint(
            model_name='contrato',
            constraint=models.CheckConstraint(
                check=models.Q(('frecuencia_facturacion__isnull', True), ('tipo_contrato', 'RECURRENTE'), _connector='OR'),
                name='chk_frecuencia_solo_recurrente',
            ),
        ),
    ]
