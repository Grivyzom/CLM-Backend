from django.db import transaction
from datetime import timedelta
from .models import Contrato, RegistroPerdonazo

def registrar_extension_contrato(contrato_id, dias_a_extender, motivo_texto):
    """
    Registra una extensión de contrato usando bloqueo pesimista
    (SELECT FOR UPDATE) para evitar race conditions.
    """
    with transaction.atomic():
        contrato = Contrato.objects.select_for_update().get(id=contrato_id)
        
        vencimiento_anterior = contrato.fecha_vencimiento
        
        contrato.dias_gracia_autorizados += dias_a_extender
        if contrato.fecha_vencimiento:
            contrato.fecha_vencimiento += timedelta(days=dias_a_extender)
        if contrato.fin_periodo_gracia:
            contrato.fin_periodo_gracia += timedelta(days=dias_a_extender)
            
        contrato.save()
        
        RegistroPerdonazo.objects.create(
            contrato=contrato,
            dias_extendidos=dias_a_extender,
            motivo=motivo_texto,
            fecha_vencimiento_anterior=vencimiento_anterior
        )
