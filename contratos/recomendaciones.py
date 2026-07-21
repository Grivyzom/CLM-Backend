"""Motor de recomendaciones del panorama de inicio.

Prioriza señales de negocio ya calculadas en otras vistas (Dashboard,
Analytics, Auditoría) mediante un score explícito y auditable:

    score = severidad_base(tipo) * factor_urgencia * factor_volumen

- severidad_base: constante por tipo de señal.
- factor_urgencia: crece cuanto más cerca está una fecha límite (1.0 si no aplica).
- factor_volumen: crece logarítmicamente con la cantidad de casos, para que un
  volumen alto pese más sin dominar desproporcionadamente frente a la severidad.
"""
import math
from datetime import timedelta

from django.db.models import Count, Q, Sum

from clientes.models import Cliente
from contratos.models import Contrato, EstadoContrato, EtapaContrato, RegistroPerdonazo
from tenants.scoping import scoped

SEVERIDAD_BASE = {
    'MORA': 90,
    'RIESGO_CARTERA': 75,
    'VENCE_PRONTO': 70,
    'COMPLIANCE_BAJO': 65,
    'REINCIDENCIA_PERDONAZO': 60,
    'GRACIA': 55,
    'SIN_DOCUMENTO': 40,
    'AUDITORIA_PENDIENTE': 35,
}


def _factor_volumen(count):
    return 1 + math.log10(1 + max(count, 0))


def _factor_urgencia(dias_restantes):
    if dias_restantes is None:
        return 1.0
    return 1 + max(0, 7 - dias_restantes) * 0.1


def _severidad_de(score):
    if score >= 70:
        return 'alta'
    if score >= 40:
        return 'media'
    return 'baja'


def _reco(tipo, *, count, monto=None, dias_restantes=None, titulo, mensaje, cta_label, cta_link, meta=None):
    score = round(
        SEVERIDAD_BASE[tipo] * _factor_urgencia(dias_restantes) * _factor_volumen(count),
        1,
    )
    return {
        'id': f'{tipo.lower()}-{count}',
        'tipo': tipo,
        'severidad': _severidad_de(score),
        'titulo': titulo,
        'mensaje': mensaje,
        'score': score,
        'cta_label': cta_label,
        'cta_link': cta_link,
        'meta': meta or {'count': count, 'monto': monto},
    }


def construir_recomendaciones(request, today, limit=6):
    """Devuelve hasta `limit` recomendaciones priorizadas por score, para el
    tenant/vista activa del request (respeta scoped())."""
    contratos = scoped(Contrato.objects.all(), request)

    recomendaciones = []

    mora = contratos.filter(status=EstadoContrato.MORA).aggregate(n=Count('id'), monto=Sum('monto'))
    if mora['n']:
        recomendaciones.append(_reco(
            'MORA', count=mora['n'], monto=float(mora['monto'] or 0),
            titulo=f"{mora['n']} contrato(s) en mora requieren seguimiento",
            mensaje=f"Hay {mora['n']} contrato(s) en estado de mora por un total de ${float(mora['monto'] or 0):,.0f}.",
            cta_label='Ver contratos en mora', cta_link='/contratos?status=MORA',
        ))

    gracia = contratos.filter(status=EstadoContrato.GRACIA).aggregate(n=Count('id'), monto=Sum('monto'))
    if gracia['n']:
        recomendaciones.append(_reco(
            'GRACIA', count=gracia['n'], monto=float(gracia['monto'] or 0),
            titulo=f"{gracia['n']} contrato(s) en periodo de gracia",
            mensaje=f"Hay {gracia['n']} contrato(s) en gracia por un total de ${float(gracia['monto'] or 0):,.0f}. Revisa antes de que pasen a mora.",
            cta_label='Ver contratos en gracia', cta_link='/contratos?status=GRACIA',
        ))

    vence = contratos.filter(
        status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA],
        fecha_vencimiento__gte=today,
        fecha_vencimiento__lte=today + timedelta(days=7),
    ).aggregate(n=Count('id'), monto=Sum('monto'))
    if vence['n']:
        recomendaciones.append(_reco(
            'VENCE_PRONTO', count=vence['n'], monto=float(vence['monto'] or 0), dias_restantes=3,
            titulo=f"{vence['n']} contrato(s) vencen en los próximos 7 días",
            mensaje=f"Gestiona la renovación de {vence['n']} contrato(s) por ${float(vence['monto'] or 0):,.0f} antes de que expiren.",
            cta_label='Ver vencimientos próximos', cta_link='/contratos?vence=7',
        ))

    sin_doc = contratos.filter(
        status=EstadoContrato.ACTIVO, documentos_generados__isnull=True
    ).distinct().count()
    if sin_doc:
        recomendaciones.append(_reco(
            'SIN_DOCUMENTO', count=sin_doc,
            titulo=f"{sin_doc} contrato(s) activo(s) sin documento",
            mensaje=f"Hay {sin_doc} contrato(s) activos sin documento generado. Genera el documento para evitar riesgo legal.",
            cta_label='Ver contratos sin documento', cta_link='/contratos?sin_documento=1',
        ))

    ESTADOS_CARTERA = [EstadoContrato.ACTIVO, EstadoContrato.MORA, EstadoContrato.GRACIA, EstadoContrato.SUSPENDIDO]
    rows = contratos.filter(status__in=ESTADOS_CARTERA).values('status').annotate(count=Count('id'), monto=Sum('monto'))
    monto_total = sum(float(r['monto'] or 0) for r in rows)
    monto_riesgo = sum(float(r['monto'] or 0) for r in rows if r['status'] != EstadoContrato.ACTIVO)
    pct_riesgo = round(monto_riesgo / monto_total * 100, 1) if monto_total else 0.0
    if pct_riesgo >= 15:
        recomendaciones.append(_reco(
            'RIESGO_CARTERA', count=int(pct_riesgo),
            titulo=f"{pct_riesgo}% de la cartera está en riesgo",
            mensaje=f"El {pct_riesgo}% del valor de tu cartera (${monto_riesgo:,.0f}) corresponde a contratos en mora, gracia o suspendidos.",
            cta_label='Ver salud de cartera', cta_link='/analytics',
            meta={'pct_riesgo': pct_riesgo, 'monto_riesgo': monto_riesgo},
        ))

    ventana_inicio = today - timedelta(days=365)
    perdonazo_rows = list(
        scoped(RegistroPerdonazo.objects.all(), request, 'contrato__tenant')
        .filter(fecha_concesion__date__gte=ventana_inicio)
        .values('contrato__cliente_id')
        .annotate(count=Count('id'))
        .filter(count__gte=2)
    )
    if perdonazo_rows:
        nombres = {
            c.pk: str(c)
            for c in Cliente.objects.filter(pk__in=[r['contrato__cliente_id'] for r in perdonazo_rows])
        }
        top = max(perdonazo_rows, key=lambda r: r['count'])
        recomendaciones.append(_reco(
            'REINCIDENCIA_PERDONAZO', count=len(perdonazo_rows),
            titulo=f"{len(perdonazo_rows)} cliente(s) con perdonazos recurrentes",
            mensaje=f"{nombres.get(top['contrato__cliente_id'], 'Un cliente')} y {len(perdonazo_rows) - 1} más han recibido 2+ perdonazos en 12 meses — señal temprana de riesgo de churn.",
            cta_label='Ver reincidencia de perdonazos', cta_link='/analytics',
            meta={'clientes_afectados': len(perdonazo_rows)},
        ))

    pending_audits = contratos.filter(etapa__in=[EtapaContrato.REVISION, EtapaContrato.APROBADO]).count()
    if pending_audits:
        recomendaciones.append(_reco(
            'AUDITORIA_PENDIENTE', count=pending_audits,
            titulo=f"{pending_audits} contrato(s) pendientes de revisión/aprobación",
            mensaje=f"Hay {pending_audits} contrato(s) detenidos en revisión o aprobación interna.",
            cta_label='Ver auditoría legal', cta_link='/auditoria',
        ))

    mora_suspendido = contratos.filter(status__in=[EstadoContrato.MORA, EstadoContrato.SUSPENDIDO]).count()
    expired_active = contratos.filter(status=EstadoContrato.ACTIVO, fecha_vencimiento__lt=today).count()
    total_contratos = contratos.count()
    if total_contratos:
        compliance_score = max(50, 100 - (mora_suspendido * 15) - (expired_active * 5))
    else:
        compliance_score = 100
    if compliance_score < 80:
        recomendaciones.append(_reco(
            'COMPLIANCE_BAJO', count=100 - compliance_score,
            titulo=f"Compliance score en {compliance_score}/100",
            mensaje="El score de cumplimiento bajó por contratos en mora/suspendidos o vencidos aún activos. Revisa la auditoría legal.",
            cta_label='Ver auditoría legal', cta_link='/auditoria',
            meta={'compliance_score': compliance_score},
        ))

    recomendaciones.sort(key=lambda r: r['score'], reverse=True)
    return recomendaciones[:limit]
