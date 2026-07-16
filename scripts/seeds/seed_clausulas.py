import os
import django

# Setup Django environment
if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    django.setup()

from plantillas.models import Clausula, VersionClausula

CLAUSULAS = [
    { "cat": "Responsabilidad", "name": "Limitación de Responsabilidad", "risk": "Alto", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "La responsabilidad total acumulada de cada parte frente a la otra, surgida de o relacionada con este Acuerdo, ya sea en contrato, agravio o de otra índole, no excederá el monto total pagado por el Cliente durante los doce (12) meses inmediatamente anteriores al evento que origina la reclamación. En ningún caso ninguna de las partes será responsable por daños indirectos, incidentales, especiales, ejemplares, consecuentes o punitivos." },
        { "label": "Flexible (negociación)", "tag": "Alternativa", "text": "La responsabilidad total de cada parte se limitará a dos veces (2x) el monto total pagado durante los doce (12) meses anteriores. Esta limitación no aplicará en casos de dolo, negligencia grave, violación de confidencialidad o infracciones de propiedad intelectual." }
    ]},
    { "cat": "Confidencialidad", "name": "Obligación de Confidencialidad", "risk": "Alto", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "Cada parte acuerda mantener en estricta confidencialidad toda la Información Confidencial recibida de la otra parte y no revelarla a ningún tercero sin el consentimiento previo por escrito de la parte divulgadora. Esta obligación permanecerá vigente durante cinco (5) años posteriores a la terminación del Acuerdo." },
        { "label": "Ampliada (datos sensibles)", "tag": "Alternativa", "text": "Las obligaciones de confidencialidad sobre Datos Personales, secretos comerciales o propiedad intelectual no tendrán límite temporal y permanecerán vigentes indefinidamente. Cualquier incumplimiento dará derecho a la parte afectada a solicitar medidas cautelares sin necesidad de acreditar perjuicio económico." }
    ]},
    { "cat": "Pagos", "name": "Condiciones de Pago y Mora", "risk": "Medio", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "El Cliente abonará las facturas emitidas dentro de los treinta (30) días calendario contados desde la fecha de emisión. Los montos vencidos devengarán un interés de mora equivalente a la tasa de referencia del Banco Central más 2 puntos porcentuales, calculado de forma diaria." },
        { "label": "Acelerada (enterprise)", "tag": "Alternativa", "text": "El pago se realizará a quince (15) días netos. Facturas impagas a los 45 días facultan al proveedor a suspender servicios y declarar vencimiento anticipado de todas las obligaciones pendientes sin necesidad de requerimiento previo." }
    ]},
    { "cat": "Resolución de Disputas", "name": "Mediación y Arbitraje", "risk": "Medio", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "Las partes se comprometen a resolver cualquier controversia mediante mediación previa ante el Centro de Arbitraje y Mediación de Santiago. De no alcanzarse acuerdo en 30 días, la disputa será resuelta por un árbitro arbitrador designado de común acuerdo o por el referido Centro." },
        { "label": "Internacional", "tag": "Alternativa", "text": "Toda disputa se resolverá mediante arbitraje bajo las Reglas de la ICC, con sede en Miami, Florida. El idioma del procedimiento será el español. El laudo será definitivo y vinculante y podrá ejecutarse en cualquier jurisdicción competente." }
    ]},
    { "cat": "Vigencia y Terminación", "name": "Causales de Terminación Anticipada", "risk": "Alto", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "Cualquiera de las partes podrá dar por terminado este Acuerdo con treinta (30) días de aviso previo por escrito en caso de incumplimiento material no subsanado dentro de los quince (15) días siguientes a la notificación de dicho incumplimiento." },
        { "label": "Protección al proveedor", "tag": "Alternativa", "text": "El proveedor podrá terminar de inmediato, sin previo aviso, ante insolvencia declarada, cesión no autorizada o violación de cláusulas de confidencialidad. El Cliente deberá indemnizar los ingresos proyectados del período remanente del contrato." }
    ]}
]

def seed():
    for c_data in CLAUSULAS:
        clausula, created = Clausula.objects.get_or_create(
            nombre=c_data['name'],
            defaults={
                'categoria': c_data['cat'],
                'riesgo': c_data['risk'],
                'activa': True
            }
        )
        if created:
            print(f"Cláusula creada: {clausula.nombre}")
        else:
            print(f"Cláusula existente: {clausula.nombre}")

        for v_data in c_data['versions']:
            version, v_created = VersionClausula.objects.get_or_create(
                clausula=clausula,
                etiqueta=v_data['label'],
                defaults={
                    'tipo': v_data['tag'],
                    'texto': v_data['text']
                }
            )
            if v_created:
                print(f"  - Versión creada: {version.etiqueta}")

if __name__ == '__main__':
    seed()
