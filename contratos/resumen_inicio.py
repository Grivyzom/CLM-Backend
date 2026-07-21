"""Panorama de inicio: actividad del usuario logueado, recomendaciones
priorizadas y comparativo semanal — todo en una sola llamada, separada del
polling de 30s de /api/dashboard/ porque estos datos dependen de request.user
y cambian a ritmo de horas/días, no de segundos.
"""
from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from clientes.models import Cliente
from tenants.permissions import IsTenantMember, RequiresFeature
from tenants.scoping import scoped

from .models import Contrato, EstadoContrato, EtapaContrato, HistorialEtapaContrato, ObligacionSLAAuditLog
from .recomendaciones import construir_recomendaciones
from .views import _cliente_qparam


def _week_bounds(anchor):
    """Devuelve (lunes, domingo) de la semana de `anchor`."""
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)


def _shift_weeks(anchor, delta):
    return anchor + timedelta(weeks=delta)


def _delta_pct(actual, anterior):
    if not anterior:
        return None
    return round((actual - anterior) / anterior * 100, 1)


class ResumenInicioView(APIView):
    """GET /api/dashboard/resumen-inicio/
    Se pide una sola vez al entrar al sistema (sin polling)."""
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def _contratos(self):
        qs = scoped(Contrato.objects.all(), self.request)
        cliente_id = _cliente_qparam(self.request)
        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)
        return qs

    def _clientes(self):
        qs = scoped(Cliente.objects.all(), self.request)
        cliente_id = _cliente_qparam(self.request)
        if cliente_id:
            qs = qs.filter(pk=cliente_id)
        return qs

    def get(self, request):
        today = timezone.localdate()
        return Response({
            'mi_actividad': self._build_mi_actividad(),
            'recomendaciones': construir_recomendaciones(request, today),
            'resumen_semanal': self._build_resumen_semanal(today),
        })

    # ── Mi actividad: solo eventos del usuario logueado ──────────────────────
    def _build_mi_actividad(self, limit=10):
        usuario = self.request.user
        etapa_labels = dict(EtapaContrato.choices)
        items = []

        etapa_qs = (
            scoped(HistorialEtapaContrato.objects.all(), self.request, 'contrato__tenant')
            .filter(usuario=usuario)
            .select_related('contrato__cliente', 'contrato__software')
            .order_by('-fecha_cambio')[:limit]
        )
        for h in etapa_qs:
            items.append({
                'id': f'etapa-{h.id}',
                'origen': 'ETAPA',
                'contrato_id': h.contrato_id,
                'cliente': str(h.contrato.cliente),
                'software': h.contrato.software.nombre if h.contrato.software_id else '',
                'descripcion': f"Movió a {etapa_labels.get(h.etapa_nueva, h.etapa_nueva)}",
                'fecha': h.fecha_cambio.isoformat(),
            })

        sla_qs = (
            scoped(ObligacionSLAAuditLog.objects.all(), self.request, 'contrato__tenant')
            .filter(usuario=usuario)
            .select_related('contrato__cliente')
            .order_by('-fecha_cambio')[:limit]
        )
        for sl in sla_qs:
            items.append({
                'id': f'sla-{sl.id}',
                'origen': 'SLA',
                'contrato_id': sl.contrato_id,
                'cliente': str(sl.contrato.cliente),
                'software': '',
                'descripcion': f"{sl.accion.lower().capitalize()} obligación SLA ({sl.tipo_obligacion})",
                'fecha': sl.fecha_cambio.isoformat(),
            })

        items.sort(key=lambda x: x['fecha'], reverse=True)
        return items[:limit]

    # ── Resumen semanal: semana actual vs. semana anterior ───────────────────
    def _build_resumen_semanal(self, today):
        actual_inicio, actual_fin = _week_bounds(today)
        anterior_inicio, anterior_fin = _week_bounds(_shift_weeks(today, -1))

        def contratos_nuevos_en(inicio, fin):
            return self._contratos().filter(fecha_inicio__gte=inicio, fecha_inicio__lte=fin).count()

        def contratos_cerrados_en(inicio, fin):
            qs = scoped(HistorialEtapaContrato.objects.all(), self.request, 'contrato__tenant')
            cliente_id = _cliente_qparam(self.request)
            if cliente_id:
                qs = qs.filter(contrato__cliente_id=cliente_id)
            return qs.filter(
                etapa_nueva=EtapaContrato.TERMINADO,
                fecha_cambio__date__gte=inicio,
                fecha_cambio__date__lte=fin,
            ).count()

        def valor_negociado_en(inicio, fin):
            total = self._contratos().filter(
                fecha_inicio__gte=inicio, fecha_inicio__lte=fin
            ).aggregate(total=Sum('monto'))['total']
            return float(total or 0)

        def clientes_nuevos_en(inicio, fin):
            return self._clientes().filter(
                fecha_registro__date__gte=inicio, fecha_registro__date__lte=fin
            ).count()

        cn_actual, cn_anterior = contratos_nuevos_en(actual_inicio, actual_fin), contratos_nuevos_en(anterior_inicio, anterior_fin)
        cc_actual, cc_anterior = contratos_cerrados_en(actual_inicio, actual_fin), contratos_cerrados_en(anterior_inicio, anterior_fin)
        vn_actual, vn_anterior = valor_negociado_en(actual_inicio, actual_fin), valor_negociado_en(anterior_inicio, anterior_fin)
        cli_actual, cli_anterior = clientes_nuevos_en(actual_inicio, actual_fin), clientes_nuevos_en(anterior_inicio, anterior_fin)

        ESTADOS_CARTERA = [EstadoContrato.ACTIVO, EstadoContrato.MORA, EstadoContrato.GRACIA, EstadoContrato.SUSPENDIDO]
        rows = self._contratos().filter(status__in=ESTADOS_CARTERA).values('status').annotate(monto=Sum('monto'))
        monto_total = sum(float(r['monto'] or 0) for r in rows)
        monto_riesgo = sum(float(r['monto'] or 0) for r in rows if r['status'] != EstadoContrato.ACTIVO)
        cartera_en_riesgo_pct = round(monto_riesgo / monto_total * 100, 1) if monto_total else 0.0

        return {
            'semana_actual': {'inicio': actual_inicio.isoformat(), 'fin': actual_fin.isoformat()},
            'semana_anterior': {'inicio': anterior_inicio.isoformat(), 'fin': anterior_fin.isoformat()},
            'contratos_nuevos': {'actual': cn_actual, 'anterior': cn_anterior, 'delta_pct': _delta_pct(cn_actual, cn_anterior)},
            'contratos_cerrados': {'actual': cc_actual, 'anterior': cc_anterior, 'delta_pct': _delta_pct(cc_actual, cc_anterior)},
            'valor_negociado': {'actual': vn_actual, 'anterior': vn_anterior, 'delta_pct': _delta_pct(vn_actual, vn_anterior)},
            'clientes_nuevos': {'actual': cli_actual, 'anterior': cli_anterior, 'delta_pct': _delta_pct(cli_actual, cli_anterior)},
            'cartera_en_riesgo_pct': cartera_en_riesgo_pct,
        }
