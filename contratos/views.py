import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from catalogo.models import Software
from clientes.models import Cliente
from .models import Contrato, EstadoContrato, TipoContrato

MESES_ES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']
DIAS_SEMANA_ES = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']


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


class DashboardView(APIView):
    """
    GET /api/dashboard/
    Agrega KPIs, series históricas y contratos por vencer para la vista general.
    Todas las métricas se derivan de datos reales; si no hay contratos/clientes
    cargados, los valores caen a cero/listas vacías (no hay datos de ejemplo).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()

        return Response({
            'kpis': self._build_kpis(today),
            'chart_area': self._build_series(today, mode='contratos'),
            'chart_bar': self._build_series(today, mode='ingresos'),
            'urgent_contracts': self._build_urgent_contracts(today),
        })

    # ── KPIs ─────────────────────────────────────────────────────────────────
    def _build_kpis(self, today):
        month_start, month_end = _month_bounds(today)
        prev_month_anchor = _shift_months(today, -1)
        prev_start, prev_end = _month_bounds(prev_month_anchor)

        contratos_activos = Contrato.objects.filter(status=EstadoContrato.ACTIVO).count()
        nuevos_contratos_mes = Contrato.objects.filter(
            fecha_inicio__gte=month_start, fecha_inicio__lte=month_end
        ).count()

        clientes_activos_ids = set(
            Contrato.objects.filter(status=EstadoContrato.ACTIVO)
            .values_list('cliente_id', flat=True).distinct()
        )
        clientes_nuevos_mes = Cliente.objects.filter(
            fecha_registro__date__gte=month_start, fecha_registro__date__lte=month_end
        ).count()

        # Ingresos del mes: MRR aproximado = suma de monto de contratos RECURRENTE activos.
        ingresos_mes = self._ingresos_recurrentes_en(month_start, month_end)
        ingresos_mes_anterior = self._ingresos_recurrentes_en(prev_start, prev_end)
        if ingresos_mes_anterior > 0:
            variacion_ingresos = round(
                float((ingresos_mes - ingresos_mes_anterior) / ingresos_mes_anterior) * 100, 1
            )
        else:
            variacion_ingresos = 100.0 if ingresos_mes > 0 else 0.0

        # Retención: de los clientes con contrato activo o vencido en los últimos 90 días,
        # % que sigue con contrato activo hoy.
        ventana_retencion = today - timedelta(days=90)
        clientes_relevantes = set(
            Contrato.objects.filter(
                Q(status=EstadoContrato.ACTIVO) |
                Q(fecha_vencimiento__gte=ventana_retencion, fecha_vencimiento__lte=today)
            ).values_list('cliente_id', flat=True).distinct()
        )
        if clientes_relevantes:
            tasa_retencion = round(len(clientes_relevantes & clientes_activos_ids) / len(clientes_relevantes) * 100, 1)
        else:
            tasa_retencion = 0.0

        por_vencer_7dias = Contrato.objects.filter(
            status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA],
            fecha_vencimiento__gte=today,
            fecha_vencimiento__lte=today + timedelta(days=7),
        ).count()

        total_software = Software.objects.count()
        software_con_contrato_activo = Contrato.objects.filter(
            status=EstadoContrato.ACTIVO
        ).values_list('software_id', flat=True).distinct().count()

        return {
            'contratos_activos': {'value': contratos_activos, 'nuevos_mes': nuevos_contratos_mes},
            'clientes_activos': {'value': len(clientes_activos_ids), 'nuevos_mes': clientes_nuevos_mes},
            'ingresos_mes': {'value': float(ingresos_mes), 'variacion_pct': variacion_ingresos},
            'tasa_retencion': {'value': tasa_retencion},
            'por_vencer_7dias': {'value': por_vencer_7dias},
            'servicios': {'activos': software_con_contrato_activo, 'total': total_software},
        }

    def _ingresos_recurrentes_en(self, start, end):
        """Suma monto de contratos RECURRENTE cuyo rango de vigencia solapa [start, end]."""
        total = Contrato.objects.filter(
            tipo_contrato=TipoContrato.RECURRENTE,
            fecha_inicio__lte=end,
        ).filter(
            Q(fecha_vencimiento__gte=start) | Q(fecha_vencimiento__isnull=True)
        ).aggregate(total=Sum('monto'))['total']
        return total or Decimal('0')

    # ── Series históricas (6 meses) ──────────────────────────────────────────
    def _build_series(self, today, mode):
        softwares = list(Software.objects.order_by('nombre'))
        meses = [_shift_months(today, -i) for i in range(5, -1, -1)]

        data = []
        for mes_anchor in meses:
            m_start, m_end = _month_bounds(mes_anchor)
            punto = {'date': MESES_ES[mes_anchor.month - 1]}
            for sw in softwares:
                vigentes = Contrato.objects.filter(
                    software=sw, fecha_inicio__lte=m_end
                ).filter(Q(fecha_vencimiento__gte=m_start) | Q(fecha_vencimiento__isnull=True))
                if mode == 'contratos':
                    punto[sw.nombre] = vigentes.count()
                else:
                    ingresos = vigentes.filter(tipo_contrato=TipoContrato.RECURRENTE).aggregate(
                        total=Sum('monto')
                    )['total'] or Decimal('0')
                    punto[sw.nombre] = round(float(ingresos) / 1000, 2)  # en miles ($k)
            data.append(punto)

        return {
            'softwares': [sw.nombre for sw in softwares],
            'data': data,
        }

    # ── Tabla de contratos por vencer ────────────────────────────────────────
    def _build_urgent_contracts(self, today, limit=50):
        qs = Contrato.objects.filter(
            status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA, EstadoContrato.MORA]
        ).filter(
            Q(fecha_vencimiento__lte=today + timedelta(days=7)) | Q(status=EstadoContrato.GRACIA)
        ).select_related('cliente', 'software', 'sla').order_by('fecha_vencimiento')[:limit]

        results = []
        for c in qs:
            if c.fecha_vencimiento is None:
                date_value = 9999
                date_label = 's/f'
            else:
                date_value = (c.fecha_vencimiento - today).days
                if date_value == 0:
                    date_label = 'Hoy'
                elif date_value < 0:
                    date_label = f'Hace {abs(date_value)}d'
                else:
                    date_label = f'{c.fecha_vencimiento.day} {MESES_ES[c.fecha_vencimiento.month - 1].lower()}'

            if c.status == EstadoContrato.GRACIA:
                estado_label, status_class = '● En gracia', 'db-status-warning'
            elif date_value <= 0:
                estado_label, status_class = '● Vence hoy' if date_value == 0 else '● Vencido', 'db-status-danger'
            else:
                estado_label, status_class = '● Por vencer', 'db-status-warning'

            results.append({
                'id': c.id,
                'client': str(c.cliente),
                'app': c.software.nombre if c.software_id else '',
                'date': date_label,
                'date_value': date_value,
                'plan': c.sla.nombre if c.sla_id else '',
                'status': estado_label,
                'status_class': status_class,
                'date_class': 'db-date-urgent' if date_value <= 0 else ('db-date-warn' if date_value <= 1 else 'db-td-date'),
            })

        return results
