import calendar
from decimal import Decimal

from django.db.models import Q, Sum, Count, Case, When, F, DecimalField
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from clientes.models import Cliente
from contratos.models import (
    Contrato, EstadoContrato, TipoContrato, FrecuenciaFacturacion,
    EtapaContrato, HistorialEtapaContrato, RegistroPerdonazo,
)

MESES_ES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']

# MRR normalizado: contratos con facturación ANUAL aportan monto/12 por mes.
MRR_EXPR = Case(
    When(frecuencia_facturacion=FrecuenciaFacturacion.ANUAL, then=F('monto') / 12),
    default=F('monto'),
    output_field=DecimalField(max_digits=15, decimal_places=4),
)


def _month_bounds(anchor):
    """Devuelve (primer_dia, ultimo_dia) del mes de `anchor`."""
    first = anchor.replace(day=1)
    last_day = calendar.monthrange(anchor.year, anchor.month)[1]
    return first, anchor.replace(day=last_day)


def _shift_months(anchor, delta):
    """Retrocede/avanza `delta` meses respecto a `anchor` (delta negativo = pasado)."""
    month_index = anchor.month - 1 + delta
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def _etiqueta_mes(anchor, today):
    """'Ene' dentro del año actual; 'Ene 25' cuando la ventana cruza de año."""
    base = MESES_ES[anchor.month - 1]
    return base if anchor.year == today.year else f"{base} {anchor.year % 100}"


class AnalyticsView(APIView):
    """
    GET /api/analytics/
    Métricas históricas y de composición de la cartera. Complementa al
    dashboard (foco operativo) con una mirada analítica: flujo de contratos,
    calendario de vencimientos, concentración por software/cliente y mezcla
    por tipo y SLA. Todo se deriva de datos reales; sin datos cae a cero.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()
        return Response({
            'kpis': self._build_kpis(today),
            'salud_cartera': self._build_salud_cartera(today),
            'reincidencia_perdonazos': self._build_reincidencia_perdonazos(today),
            'flujo_contratos': self._build_flujo(today),
            'vencimientos': self._build_vencimientos(today),
            'por_software': self._build_por_software(),
            'top_clientes': self._build_top_clientes(),
            'por_tipo': self._build_por_tipo(),
            'por_sla': self._build_por_sla(),
        })

    # ── KPIs ─────────────────────────────────────────────────────────────────
    def _build_kpis(self, today):
        activos = Contrato.objects.filter(status=EstadoContrato.ACTIVO)

        agg = activos.aggregate(total=Sum('monto'), n=Count('id'))
        valor_cartera = agg['total'] or Decimal('0')
        n_activos = agg['n'] or 0
        ticket_promedio = (valor_cartera / n_activos) if n_activos else Decimal('0')

        mrr = Contrato.objects.filter(
            tipo_contrato=TipoContrato.RECURRENTE,
            fecha_inicio__lte=today,
        ).filter(
            Q(fecha_vencimiento__gte=today) | Q(fecha_vencimiento__isnull=True)
        ).annotate(mrr=MRR_EXPR).aggregate(total=Sum('mrr'))['total'] or Decimal('0')

        # Duración contratada media, solo contratos con vencimiento definido.
        duraciones = [
            (c['fecha_vencimiento'] - c['fecha_inicio']).days
            for c in Contrato.objects.filter(fecha_vencimiento__isnull=False)
            .values('fecha_inicio', 'fecha_vencimiento')
            if c['fecha_vencimiento'] >= c['fecha_inicio']
        ]
        duracion_meses = round(sum(duraciones) / len(duraciones) / 30.44, 1) if duraciones else 0

        monto_recurrente = activos.filter(
            tipo_contrato=TipoContrato.RECURRENTE
        ).aggregate(total=Sum('monto'))['total'] or Decimal('0')
        pct_recurrente = round(float(monto_recurrente / valor_cartera) * 100, 1) if valor_cartera else 0.0

        return {
            'valor_cartera': {'value': float(valor_cartera), 'contratos': n_activos},
            'arr': {'value': float(mrr * 12)},
            'ticket_promedio': {'value': float(ticket_promedio)},
            'duracion_media': {'value': duracion_meses},
            'mix_recurrente': {'value': pct_recurrente},
        }

    # ── Salud de cartera: cobranza (ACTIVO/MORA/GRACIA/SUSPENDIDO) ───────────
    def _build_salud_cartera(self, today):
        ESTADOS = [EstadoContrato.ACTIVO, EstadoContrato.MORA, EstadoContrato.GRACIA, EstadoContrato.SUSPENDIDO]
        ESTADOS_RIESGO = [EstadoContrato.MORA, EstadoContrato.GRACIA, EstadoContrato.SUSPENDIDO]
        labels = dict(EstadoContrato.choices)

        rows = (
            Contrato.objects.filter(status__in=ESTADOS)
            .values('status').annotate(count=Count('id'), monto=Sum('monto'))
        )
        by_status = {r['status']: r for r in rows}
        por_estado = [
            {
                'estado': s,
                'label': labels[s],
                'count': by_status.get(s, {}).get('count', 0),
                'monto': float(by_status.get(s, {}).get('monto') or 0),
            }
            for s in ESTADOS
        ]

        monto_total = sum(e['monto'] for e in por_estado)
        monto_riesgo = sum(e['monto'] for e in por_estado if e['estado'] != EstadoContrato.ACTIVO)
        pct_riesgo = round(monto_riesgo / monto_total * 100, 1) if monto_total else 0.0

        contratos_riesgo = []
        for c in Contrato.objects.filter(status__in=ESTADOS_RIESGO).select_related('cliente', 'software'):
            dias_vencido = (today - c.fecha_vencimiento).days if c.fecha_vencimiento and today > c.fecha_vencimiento else 0
            contratos_riesgo.append({
                'id': c.id,
                'cliente': str(c.cliente),
                'software': c.software.nombre if c.software else 's/software',
                'estado': c.status,
                'estado_label': labels[c.status],
                'monto': float(c.monto),
                'dias_vencido': dias_vencido,
            })
        contratos_riesgo.sort(key=lambda x: x['dias_vencido'], reverse=True)

        return {
            'por_estado': por_estado,
            'pct_riesgo': pct_riesgo,
            'monto_riesgo': monto_riesgo,
            'contratos_riesgo': contratos_riesgo[:10],
        }

    # ── Reincidencia de perdonazos: señal temprana de riesgo de churn ────────
    def _build_reincidencia_perdonazos(self, today, meses=12):
        ventana_inicio = _shift_months(today, -meses)

        rows = list(
            RegistroPerdonazo.objects.filter(fecha_concesion__date__gte=ventana_inicio)
            .values('contrato__cliente_id')
            .annotate(
                count=Count('id'),
                dias_totales=Sum('dias_extendidos'),
                contratos=Count('contrato_id', distinct=True),
            )
            .order_by('-count', '-dias_totales')
        )

        nombres = {
            c.pk: str(c)
            for c in Cliente.objects.filter(pk__in=[r['contrato__cliente_id'] for r in rows])
        }

        top_reincidentes = [
            {
                'cliente_id': r['contrato__cliente_id'],
                'cliente': nombres.get(r['contrato__cliente_id'], f"Cliente #{r['contrato__cliente_id']}"),
                'count': r['count'],
                'dias_totales': r['dias_totales'] or 0,
                'contratos': r['contratos'],
                'reincidente': r['count'] >= 2,
            }
            for r in rows[:10]
        ]

        return {
            'ventana_meses': meses,
            'total_perdonazos': sum(r['count'] for r in rows),
            'clientes_afectados': len(rows),
            'top_reincidentes': top_reincidentes,
        }

    # ── Flujo: contratos iniciados vs terminados por mes (últimos 12) ────────
    def _build_flujo(self, today):
        data = []
        for i in range(11, -1, -1):
            anchor = _shift_months(today, -i)
            m_start, m_end = _month_bounds(anchor)
            iniciados = Contrato.objects.filter(
                fecha_inicio__gte=m_start, fecha_inicio__lte=m_end
            ).count()
            terminados = HistorialEtapaContrato.objects.filter(
                etapa_nueva=EtapaContrato.TERMINADO,
                fecha_cambio__date__gte=m_start,
                fecha_cambio__date__lte=m_end,
            ).values('contrato_id').distinct().count()
            data.append({
                'mes': _etiqueta_mes(anchor, today),
                'iniciados': iniciados,
                'terminados': terminados,
            })
        return data

    # ── Calendario de vencimientos (próximos 12 meses) ───────────────────────
    def _build_vencimientos(self, today):
        data = []
        for i in range(12):
            anchor = _shift_months(today, i)
            m_start, m_end = _month_bounds(anchor)
            if i == 0:
                m_start = today  # el mes en curso cuenta desde hoy
            agg = Contrato.objects.filter(
                status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA],
                fecha_vencimiento__gte=m_start,
                fecha_vencimiento__lte=m_end,
            ).aggregate(n=Count('id'), monto=Sum('monto'))
            data.append({
                'mes': _etiqueta_mes(anchor, today),
                'count': agg['n'] or 0,
                'monto': float(agg['monto'] or 0),
            })
        return data

    # ── Concentración de cartera ─────────────────────────────────────────────
    def _build_por_software(self, limit=8):
        rows = (
            Contrato.objects.filter(status=EstadoContrato.ACTIVO)
            .values(nombre_sw=F('software__nombre'))
            .annotate(monto=Sum('monto'), count=Count('id'))
            .order_by('-monto')[:limit]
        )
        return [
            {'nombre': r['nombre_sw'] or 's/software', 'monto': float(r['monto'] or 0), 'count': r['count']}
            for r in rows
        ]

    def _build_top_clientes(self, limit=8):
        rows = list(
            Contrato.objects.filter(status=EstadoContrato.ACTIVO)
            .values('cliente_id')
            .annotate(monto=Sum('monto'), count=Count('id'))
            .order_by('-monto')[:limit]
        )
        # str(Cliente) resuelve el nombre según sea persona natural o jurídica.
        nombres = {
            c.pk: str(c)
            for c in Cliente.objects.filter(pk__in=[r['cliente_id'] for r in rows])
        }
        return [
            {
                'nombre': nombres.get(r['cliente_id'], f"Cliente #{r['cliente_id']}"),
                'monto': float(r['monto'] or 0),
                'count': r['count'],
            }
            for r in rows
        ]

    # ── Mezcla de cartera ────────────────────────────────────────────────────
    def _build_por_tipo(self):
        labels = dict(TipoContrato.choices)
        rows = (
            Contrato.objects.filter(status=EstadoContrato.ACTIVO)
            .values('tipo_contrato')
            .annotate(count=Count('id'), monto=Sum('monto'))
            .order_by('-monto')
        )
        return [
            {
                'tipo': r['tipo_contrato'],
                'label': labels.get(r['tipo_contrato'], r['tipo_contrato']),
                'count': r['count'],
                'monto': float(r['monto'] or 0),
            }
            for r in rows
        ]

    def _build_por_sla(self):
        rows = (
            Contrato.objects.filter(status=EstadoContrato.ACTIVO)
            .values(nombre_sla=F('sla__nombre'))
            .annotate(count=Count('id'), monto=Sum('monto'))
            .order_by('-count')
        )
        return [
            {'nombre': r['nombre_sla'] or 'Sin SLA', 'count': r['count'], 'monto': float(r['monto'] or 0)}
            for r in rows
        ]
