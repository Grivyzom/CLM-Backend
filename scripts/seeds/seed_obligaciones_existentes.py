import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from contratos.models import Contrato, ObligacionSLA

def run():
    print("Iniciando migración de datos para obligaciones SLA existentes con bypass...")
    contratos = Contrato.objects.all()
    count = 0
    for c in contratos:
        if not c.obligaciones.exists() and c.sla_id:
            # Creamos las dos obligaciones por defecto de su SLA usando el bypass
            ob1 = ObligacionSLA(
                contrato=c,
                tipo_obligacion="Disponibilidad de plataforma",
                descripcion=f"Garantizar un {c.sla.uptime_garantizado}% de tiempo en línea mensual",
                penalizacion="Descuento del 10% en la siguiente factura si no se cumple"
            )
            ob1.save(bypass_etapa_check=True)
            
            ob2 = ObligacionSLA(
                contrato=c,
                tipo_obligacion="Tiempo de respuesta soporte",
                descripcion=f"Tiempo de respuesta máximo de {c.sla.tiempo_respuesta_horas} horas para incidentes",
                penalizacion="Compensación según acuerdo comercial"
            )
            ob2.save(bypass_etapa_check=True)
            
            count += 1
            print(f"Creadas obligaciones por defecto para Contrato ID {c.id}")
            
    print(f"Completado. Se actualizaron {count} contratos.")

if __name__ == '__main__':
    run()
