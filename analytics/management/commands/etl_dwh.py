import logging
from django.core.management.base import BaseCommand
from django.db import transaction

# Modelos del Data Warehouse
from analytics.models import DimCliente, DimSoftware, FactContrato

# Modelos transaccionales
from clientes.models import Cliente, PersonaNatural, PersonaJuridica
from catalogo.models import Software
from contratos.models import Contrato

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Ejecuta el proceso ETL para popular el Data Warehouse (DWH).'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Iniciando proceso ETL para DWH...'))
        
        try:
            with transaction.atomic():
                self.cargar_dimension_clientes()
                self.cargar_dimension_software()
                self.cargar_hechos_contratos()
            
            self.stdout.write(self.style.SUCCESS('ETL finalizado con éxito.'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error en ETL: {str(e)}'))
            logger.error('ETL falló', exc_info=True)

    def cargar_dimension_clientes(self):
        self.stdout.write('Procesando DimCliente...')
        clientes = Cliente.objects.all()
        for c in clientes:
            nombre = "Desconocido"
            rut = ""
            industria = None
            tipo = "NATURAL"
            
            try:
                if hasattr(c, 'personanatural'):
                    pn = c.personanatural
                    nombre = f"{pn.nombres} {pn.apellidos}"
                    rut = pn.rut
                elif hasattr(c, 'personajuridica'):
                    pj = c.personajuridica
                    nombre = pj.razon_social
                    rut = pj.rut
                    industria = getattr(pj, 'industria', None) # Asumiendo que puede existir
                    tipo = "JURIDICA"
            except Exception:
                pass
                
            DimCliente.objects.update_or_create(
                cliente_id_origen=c.id,
                defaults={
                    'tipo_cliente': tipo,
                    'nombre_completo': nombre,
                    'rut_identificador': rut,
                    'pais': getattr(c, 'pais', None),
                    'industria': industria,
                    'fecha_registro': getattr(c, 'created_at', None) # Asumiendo BaseModel
                }
            )

    def cargar_dimension_software(self):
        self.stdout.write('Procesando DimSoftware...')
        softwares = Software.objects.all()
        for s in softwares:
            DimSoftware.objects.update_or_create(
                software_id_origen=s.id,
                defaults={
                    'nombre': s.nombre,
                    'categoria': getattr(s, 'categoria', None),
                    'estado': 'ACTIVO' # Por defecto o mapear si existe
                }
            )

    def cargar_hechos_contratos(self):
        self.stdout.write('Procesando FactContrato...')
        contratos = Contrato.objects.all()
        for c in contratos:
            dim_cliente = DimCliente.objects.filter(cliente_id_origen=c.cliente_id).first()
            dim_software = None
            if c.software_id:
                dim_software = DimSoftware.objects.filter(software_id_origen=c.software_id).first()
            
            if not dim_cliente:
                continue # No procesar si no hay dimensión (integridad referencial rota o no sincronizada)
                
            monto = getattr(c, 'monto', 0)
            
            # Cálculo simple de MRR (ejemplo)
            mrr = 0
            if getattr(c, 'frecuencia_facturacion', '') == 'MENSUAL':
                mrr = monto
            elif getattr(c, 'frecuencia_facturacion', '') == 'ANUAL':
                mrr = monto / 12

            FactContrato.objects.update_or_create(
                contrato_id_origen=c.id,
                defaults={
                    'dim_cliente': dim_cliente,
                    'dim_software': dim_software,
                    'tipo_contrato': getattr(c, 'tipo', None),
                    'estado': getattr(c, 'estado', None),
                    'etapa': getattr(c, 'etapa', None),
                    'fecha_inicio': getattr(c, 'fecha_inicio', None),
                    'fecha_termino': getattr(c, 'fecha_termino', None),
                    'monto_total': monto,
                    'ingreso_mensual_recurrente': mrr,
                    'sla_cumplimiento_porcentaje': getattr(c, 'sla_general', None),
                }
            )
