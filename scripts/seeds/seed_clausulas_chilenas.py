import os
import django

# Setup Django environment
if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    django.setup()

from plantillas.models import Clausula, VersionClausula
from tenants.models import Tenant

CLAUSULAS = [
    { "cat": "Generales", "name": "Domicilio Especial", "risk": "Bajo", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "Para todos los efectos legales derivados del presente contrato, las partes fijan su domicilio especial en la ciudad y comuna de Santiago, y se someten desde ya a la jurisdicción de sus Tribunales Ordinarios de Justicia." }
    ]},
    { "cat": "Resolución de Disputas", "name": "Resolución de Conflictos (Arbitraje CAM)", "risk": "Medio", "versions": [
        { "label": "Arbitraje CAM Santiago", "tag": "Alternativa", "text": "Cualquier dificultad o controversia que se produzca entre las partes respecto de la aplicación, interpretación, duración, validez o ejecución de este contrato, será sometida a arbitraje, conforme al Reglamento Arbitral del Centro de Arbitraje y Mediación de Santiago, en vigor al momento de solicitarlo." }
    ]},
    { "cat": "Generales", "name": "Comunicaciones y Notificaciones", "risk": "Bajo", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "Todas las notificaciones, avisos o comunicaciones que las partes deban dirigirse en virtud de este contrato, se realizarán por escrito y se enviarán por correo electrónico a las direcciones indicadas en la comparecencia. Se entenderá practicada la notificación el mismo día de su envío, siempre que no se reciba un mensaje de error en la entrega." }
    ]},
    { "cat": "Responsabilidad", "name": "Fuerza Mayor o Caso Fortuito", "risk": "Medio", "versions": [
        { "label": "Estándar (Art. 45 CC)", "tag": "Estándar", "text": "Ninguna de las partes será responsable del retraso o incumplimiento de sus obligaciones bajo este contrato si dicho retraso o incumplimiento se debe a un evento de caso fortuito o fuerza mayor, entendiéndose por tal cualquier imprevisto al que no es posible resistir, debiendo la parte afectada notificar a la otra en un plazo no superior a 5 días corridos desde su ocurrencia." }
    ]},
    { "cat": "Generales", "name": "Integridad del Contrato", "risk": "Bajo", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "El presente contrato constituye el acuerdo completo, total y definitivo entre las partes respecto de la materia objeto del mismo, y deja sin efecto y reemplaza a cualquier otro acuerdo, negociación, tratativa o comunicación verbal o escrita celebrada con anterioridad entre ellas." }
    ]},
    { "cat": "Generales", "name": "Divisibilidad (Salvaguarda)", "risk": "Bajo", "versions": [
        { "label": "Estándar", "tag": "Estándar", "text": "Si cualquier disposición de este contrato fuere declarada nula, inválida o ineficaz por un tribunal competente, dicha declaración no afectará la validez, eficacia o exigibilidad de las restantes disposiciones, las cuales continuarán en pleno vigor y efecto." }
    ]},
    { "cat": "Cesión", "name": "Prohibición de Cesión", "risk": "Medio", "versions": [
        { "label": "Prohibición Absoluta", "tag": "Estándar", "text": "Queda expresamente prohibido a las partes ceder o transferir, total o parcialmente, los derechos y obligaciones derivados del presente contrato a terceros, sin contar con la autorización previa, expresa y por escrito de la otra parte." }
    ]},
    { "cat": "Confidencialidad", "name": "Confidencialidad General", "risk": "Alto", "versions": [
        { "label": "Estricta (3 años)", "tag": "Estándar", "text": "Las partes se obligan a mantener en la más estricta reserva y confidencialidad toda la información, datos, documentos o antecedentes a los que tengan acceso con ocasión de este contrato. Esta obligación de confidencialidad se mantendrá vigente durante toda la vigencia de este contrato y por un plazo de 3 años contados desde su terminación." }
    ]}
]

def seed():
    tenant = Tenant.objects.first()
    if not tenant:
        print("No tenant found. Creating a default tenant.")
        tenant = Tenant.objects.create(name="Default Tenant")

    for c_data in CLAUSULAS:
        clausula, created = Clausula.objects.get_or_create(
            nombre=c_data['name'],
            tenant=tenant,
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
