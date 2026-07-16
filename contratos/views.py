import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Q, Sum, Count, Case, When, F, DecimalField
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework import status as http_status

from catalogo.models import Producto
from clientes.models import Cliente
from plantillas.models import DocumentoGenerado
from .models import (
    Contrato, EstadoContrato, TipoContrato, FrecuenciaFacturacion,
    EtapaContrato, SLA, HistorialEtapaContrato, ArchivoAdjunto,
    ObligacionSLA, ObligacionSLAAuditLog,
)
from django.core.exceptions import ValidationError
from tenants.permissions import DeleteRequiresTenantAdmin, EditRequiresPermiso, IsPlatformClienteAccess, IsTenantMember, RequiresFeature
from tenants.scoping import enforce_quota, resolve_tenant_for_write, scoped

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


# MRR normalizado: contratos con facturación ANUAL aportan monto/12 por mes.
MRR_EXPR = Case(
    When(frecuencia_facturacion=FrecuenciaFacturacion.ANUAL, then=F('monto') / 12),
    default=F('monto'),
    output_field=DecimalField(max_digits=15, decimal_places=4),
)

# Etapas que componen el pipeline de trabajo (excluye TERMINADO/ENMENDADO).
ETAPAS_PIPELINE = [
    EtapaContrato.BORRADOR,
    EtapaContrato.REVISION,
    EtapaContrato.APROBADO,
    EtapaContrato.PENDIENTE_FIRMA,
    EtapaContrato.ACTIVO,
]


def _cliente_qparam(request):
    """Lee ?cliente= — la "Vista activa" del frontend manda este parámetro para
    acotar las métricas a un solo cliente. Un valor no numérico se ignora
    (equivale a la vista global); scoped() ya garantiza que un id de otro
    tenant no devuelva datos ajenos."""
    raw = request.query_params.get('cliente')
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class DashboardView(APIView):
    """
    GET /api/dashboard/
    Agrega KPIs, pipeline por etapa, renovaciones, serie MRR, contratos que
    requieren acción y actividad reciente. Todas las métricas se derivan de
    datos reales; sin datos cargados los valores caen a cero/listas vacías.
    """
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    # Querysets base acotados al tenant del solicitante (superadmin ve todo).
    # Con ?cliente= (vista de cliente activa) se acotan además a ese cliente.
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
            'kpis': self._build_kpis(today),
            'mrr_series': self._build_mrr_series(today),
            'pipeline': self._build_pipeline(),
            'renovaciones': self._build_renovaciones(today),
            'urgent_contracts': self._build_urgent_contracts(today),
            'actividad': self._build_actividad(),
            'cartera_estado': self._build_cartera_estado(),
            'valor_negociado': self._build_valor_negociado_series(today),
        })

    # ── KPIs ─────────────────────────────────────────────────────────────────
    def _build_kpis(self, today):
        month_start, month_end = _month_bounds(today)
        prev_month_anchor = _shift_months(today, -1)
        prev_start, prev_end = _month_bounds(prev_month_anchor)

        contratos_activos = self._contratos().filter(status=EstadoContrato.ACTIVO).count()
        nuevos_contratos_mes = self._contratos().filter(
            fecha_inicio__gte=month_start, fecha_inicio__lte=month_end
        ).count()

        clientes_activos = (
            self._contratos().filter(status=EstadoContrato.ACTIVO)
            .values('cliente_id').distinct().count()
        )
        clientes_nuevos_mes = self._clientes().filter(
            fecha_registro__date__gte=month_start, fecha_registro__date__lte=month_end
        ).count()

        mrr = self._mrr_en(month_start, month_end)
        mrr_anterior = self._mrr_en(prev_start, prev_end)
        if mrr_anterior > 0:
            variacion_mrr = round(float((mrr - mrr_anterior) / mrr_anterior) * 100, 1)
        else:
            variacion_mrr = 100.0 if mrr > 0 else 0.0

        renov_30 = self._contratos().filter(
            status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA],
            fecha_vencimiento__gte=today,
            fecha_vencimiento__lte=today + timedelta(days=30),
        ).aggregate(n=Count('id'), monto=Sum('monto'))

        requieren_accion = self._contratos().filter(
            Q(status__in=[EstadoContrato.MORA, EstadoContrato.GRACIA]) |
            Q(status=EstadoContrato.ACTIVO, fecha_vencimiento__lte=today + timedelta(days=7))
        ).count()

        sin_documento = self._contratos().filter(
            status=EstadoContrato.ACTIVO, documentos_generados__isnull=True
        ).distinct().count()

        return {
            'mrr': {'value': float(mrr), 'variacion_pct': variacion_mrr},
            'contratos_activos': {'value': contratos_activos, 'nuevos_mes': nuevos_contratos_mes},
            'clientes_activos': {'value': clientes_activos, 'nuevos_mes': clientes_nuevos_mes},
            'renovaciones_30d': {'value': renov_30['n'] or 0, 'monto': float(renov_30['monto'] or 0)},
            'requieren_accion': {'value': requieren_accion},
            'sin_documento': {'value': sin_documento},
        }

    def _mrr_en(self, start, end):
        """MRR de contratos RECURRENTE cuyo rango de vigencia solapa [start, end]."""
        total = self._contratos().filter(
            tipo_contrato=TipoContrato.RECURRENTE,
            fecha_inicio__lte=end,
        ).filter(
            Q(fecha_vencimiento__gte=start) | Q(fecha_vencimiento__isnull=True)
        ).annotate(mrr=MRR_EXPR).aggregate(total=Sum('mrr'))['total']
        return total or Decimal('0')

    # ── Serie MRR (6 meses, por software; top 5 + "Otros") ───────────────────
    def _build_mrr_series(self, today, max_series=5):
        softwares = list(scoped(Producto.objects.all(), self.request)
                         .filter(categoria='Software').order_by('nombre'))
        software_nombre = {sw.id: sw.nombre for sw in softwares}
        meses = [_shift_months(today, -i) for i in range(5, -1, -1)]
        bounds = [_month_bounds(m) for m in meses]
        etiquetas_mes = [MESES_ES[m.month - 1] for m in meses]

        # Antes: 1 query aggregate por (mes × software) = hasta 6*N round-trips.
        # Ahora: 1 sola query trae los contratos RECURRENTE que solapan la ventana
        # completa de 6 meses, y el solape mes a mes se resuelve en Python.
        valores = {sw.nombre: [Decimal('0')] * len(meses) for sw in softwares}
        if software_nombre:
            ventana_inicio, ventana_fin = bounds[0][0], bounds[-1][1]
            contratos = (
                self._contratos()
                .filter(
                    software_id__in=software_nombre.keys(),
                    tipo_contrato=TipoContrato.RECURRENTE,
                    fecha_inicio__lte=ventana_fin,
                )
                .filter(Q(fecha_vencimiento__gte=ventana_inicio) | Q(fecha_vencimiento__isnull=True))
                .annotate(mrr=MRR_EXPR)
                .values('software_id', 'fecha_inicio', 'fecha_vencimiento', 'mrr')
            )
            for c in contratos:
                nombre = software_nombre.get(c['software_id'])
                if nombre is None:
                    continue
                for i, (m_start, m_end) in enumerate(bounds):
                    if c['fecha_inicio'] <= m_end and (c['fecha_vencimiento'] is None or c['fecha_vencimiento'] >= m_start):
                        valores[nombre][i] += c['mrr']

        valores = {nombre: [round(float(v) / 1000, 2) for v in serie] for nombre, serie in valores.items()}

        # Más de max_series softwares no se distinguen en un gráfico apilado:
        # se conservan los de mayor MRR acumulado y el resto se pliega en "Otros".
        con_datos = [n for n in valores if any(v > 0 for v in valores[n])]
        con_datos.sort(key=lambda n: sum(valores[n]), reverse=True)
        top = sorted(con_datos[:max_series])  # orden alfabético estable para colores
        resto = con_datos[max_series:]

        series = list(top)
        if resto:
            valores['Otros'] = [
                round(sum(valores[n][i] for n in resto), 2) for i in range(len(meses))
            ]
            series.append('Otros')

        data = []
        for i, etiqueta in enumerate(etiquetas_mes):
            punto = {'date': etiqueta}
            for nombre in series:
                punto[nombre] = valores[nombre][i]
            data.append(punto)

        return {'softwares': series, 'data': data}

    # ── Pipeline por etapa ───────────────────────────────────────────────────
    def _build_pipeline(self):
        agregados = {
            row['etapa']: row
            for row in self._contratos().filter(etapa__in=ETAPAS_PIPELINE)
            .values('etapa').annotate(n=Count('id'), monto=Sum('monto'))
        }
        labels = dict(EtapaContrato.choices)
        return [
            {
                'etapa': etapa,
                'label': labels[etapa],
                'count': agregados.get(etapa, {}).get('n', 0),
                'monto': float(agregados.get(etapa, {}).get('monto') or 0),
            }
            for etapa in ETAPAS_PIPELINE
        ]

    # ── Contratos por estado de cobranza (volumen + riesgo de cartera) ───────
    def _build_cartera_estado(self):
        ESTADOS = [EstadoContrato.ACTIVO, EstadoContrato.MORA, EstadoContrato.GRACIA, EstadoContrato.SUSPENDIDO]
        labels = dict(EstadoContrato.choices)

        rows = (
            self._contratos().filter(status__in=ESTADOS)
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

        return {
            'por_estado': por_estado,
            'monto_total': monto_total,
            'monto_riesgo': monto_riesgo,
            'pct_riesgo': pct_riesgo,
        }

    # ── Valor negociado (monto sin normalizar, todos los tipos de contrato) ──
    def _build_valor_negociado_series(self, today, meses=6):
        anchors = [_shift_months(today, -i) for i in range(meses - 1, -1, -1)]
        bounds = [_month_bounds(m) for m in anchors]
        etiquetas = [MESES_ES[m.month - 1] for m in anchors]

        agg = (
            self._contratos().filter(fecha_inicio__gte=bounds[0][0], fecha_inicio__lte=bounds[-1][1])
            .values('fecha_inicio').annotate(monto=Sum('monto'))
        )
        montos_por_mes = [Decimal('0')] * len(anchors)
        for row in agg:
            for i, (m_start, m_end) in enumerate(bounds):
                if m_start <= row['fecha_inicio'] <= m_end:
                    montos_por_mes[i] += row['monto'] or Decimal('0')
                    break

        data = [
            {'date': etiqueta, 'monto_k': round(float(monto) / 1000, 2)}
            for etiqueta, monto in zip(etiquetas, montos_por_mes)
        ]

        total_vigente = self._contratos().filter(
            status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA, EstadoContrato.MORA]
        ).aggregate(total=Sum('monto'))['total'] or Decimal('0')

        return {'data': data, 'total_vigente': float(total_vigente)}

    # ── Renovaciones próximas (30/60/90 días) ────────────────────────────────
    def _build_renovaciones(self, today):
        buckets = [('0–30 días', 0, 30), ('31–60 días', 31, 60), ('61–90 días', 61, 90)]
        results = []
        for label, desde, hasta in buckets:
            agg = self._contratos().filter(
                status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA],
                fecha_vencimiento__gte=today + timedelta(days=desde),
                fecha_vencimiento__lte=today + timedelta(days=hasta),
            ).aggregate(n=Count('id'), monto=Sum('monto'))
            results.append({
                'label': label,
                'count': agg['n'] or 0,
                'monto': float(agg['monto'] or 0),
            })
        return results

    # ── Actividad reciente (cambios de etapa) ────────────────────────────────
    def _build_actividad(self, limit=8):
        labels = dict(EtapaContrato.choices)
        qs = scoped(HistorialEtapaContrato.objects.all(), self.request, 'contrato__tenant')
        cliente_id = _cliente_qparam(self.request)
        if cliente_id:
            qs = qs.filter(contrato__cliente_id=cliente_id)
        qs = (
            qs.select_related('contrato__cliente', 'contrato__software', 'usuario')
            .order_by('-fecha_cambio')[:limit]
        )
        return [
            {
                'id': h.id,
                'contrato_id': h.contrato_id,
                'cliente': str(h.contrato.cliente),
                'software': h.contrato.software.nombre if h.contrato.software_id else '',
                'etapa_anterior': labels.get(h.etapa_anterior, h.etapa_anterior or ''),
                'etapa_nueva': labels.get(h.etapa_nueva, h.etapa_nueva),
                'usuario': (h.usuario.get_full_name() or h.usuario.username) if h.usuario_id else 'Sistema',
                'fecha': h.fecha_cambio.isoformat(),
            }
            for h in qs
        ]

    # ── Tabla de contratos que requieren acción ──────────────────────────────
    def _build_urgent_contracts(self, today, limit=50):
        qs = self._contratos().filter(
            status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA, EstadoContrato.MORA]
        ).filter(
            Q(fecha_vencimiento__lte=today + timedelta(days=7)) |
            Q(status__in=[EstadoContrato.GRACIA, EstadoContrato.MORA])
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

            if c.status == EstadoContrato.MORA:
                estado_label, status_class = 'En mora', 'db-status-danger'
            elif c.status == EstadoContrato.GRACIA:
                estado_label, status_class = 'En gracia', 'db-status-warning'
            elif date_value <= 0:
                estado_label, status_class = ('Vence hoy' if date_value == 0 else 'Vencido'), 'db-status-danger'
            else:
                estado_label, status_class = 'Por vencer', 'db-status-warning'

            results.append({
                'id': c.id,
                'client': str(c.cliente),
                'app': c.software.nombre if c.software_id else '',
                'date': date_label,
                'date_value': date_value,
                'plan': c.sla.nombre if c.sla_id else '',
                'monto': float(c.monto or 0),
                'status': estado_label,
                'status_class': status_class,
                'date_class': 'db-date-urgent' if date_value <= 0 else ('db-date-warn' if date_value <= 1 else 'db-td-date'),
            })

        return results


def _sla_a_dict(s):
    return {
        'id': s.id,
        'nombre': s.nombre,
        'uptime_garantizado': str(s.uptime_garantizado),
        'tiempo_respuesta_horas': s.tiempo_respuesta_horas,
        'detalles': s.detalles or '',
    }


class SLAListView(APIView):
    """GET  /api/slas/ — catálogo de SLA del tenant (para selects y obligaciones).
    POST /api/slas/ — crea un SLA propio del tenant."""
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def get(self, request):
        qs = scoped(SLA.objects.all(), request).order_by('nombre')
        return Response([_sla_a_dict(s) for s in qs])

    def post(self, request):
        data = request.data
        nombre = (data.get('nombre') or '').strip()
        if not nombre:
            raise DRFValidationError({'nombre': 'Este campo es requerido.'})

        tenant = resolve_tenant_for_write(request, data)
        if SLA.objects.filter(tenant=tenant, nombre__iexact=nombre).exists():
            raise DRFValidationError({'nombre': 'Ya existe un SLA con ese nombre.'})

        try:
            uptime = Decimal(str(data.get('uptime_garantizado', '99.9')))
            horas = int(data.get('tiempo_respuesta_horas', 24))
        except (InvalidOperation, TypeError, ValueError):
            raise DRFValidationError({'error': 'uptime_garantizado u horas inválidos.'})

        sla = SLA.objects.create(
            tenant=tenant,
            nombre=nombre,
            uptime_garantizado=uptime,
            tiempo_respuesta_horas=horas,
            detalles=data.get('detalles', ''),
        )
        return Response(_sla_a_dict(sla), status=http_status.HTTP_201_CREATED)


class SLANAView(APIView):
    """GET /api/slas/na/ — SLA técnico "N/A" del tenant, para contratos que
    documentan algo sin nivel de servicio (NDA, memorándums, fichas de
    requerimientos: ver PlantillaDocumento.requiere_sla_facturacion). Se
    crea una sola vez por tenant, reutilizable por cualquier contrato
    administrativo — evita forzar al usuario a elegir un SLA que no aplica."""
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def get(self, request):
        tenant = resolve_tenant_for_write(request, {})
        sla, _ = SLA.objects.get_or_create(
            tenant=tenant, nombre='N/A — Documento administrativo',
            defaults=dict(
                uptime_garantizado=Decimal('0'),
                tiempo_respuesta_horas=0,
                detalles='SLA técnico asignado automáticamente a documentos que no son un '
                         'servicio con nivel de servicio ni cobro (NDA, memorándums, fichas '
                         'de requerimientos, etc.).',
            ),
        )
        return Response(_sla_a_dict(sla))


# ─── Contratos: CRUD ──────────────────────────────────────────────────────────

def _compute_mrr_arr(monto, tipo_contrato, frecuencia):
    """MRR/ARR solo tienen sentido para contratos RECURRENTE. `monto` se interpreta
    como el valor cobrado en cada ciclo de facturación (mensual o anual)."""
    if tipo_contrato != TipoContrato.RECURRENTE or monto is None:
        return Decimal('0'), Decimal('0')
    if frecuencia == FrecuenciaFacturacion.ANUAL:
        mrr = monto / Decimal('12')
    else:
        mrr = monto
    return mrr, mrr * Decimal('12')


def _parse_fecha_iso(valor):
    """Acepta date o string 'YYYY-MM-DD'; devuelve date o None si es inválido."""
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor))
    except (TypeError, ValueError):
        return None


def _dias_restantes(fecha_vencimiento, today):
    if not fecha_vencimiento:
        return None
    return (fecha_vencimiento - today).days


def _contrato_nombre(c, doc_info):
    if getattr(c, 'nombre', None):
        return c.nombre
    
    if doc_info and doc_info.get('plantilla_nombre'):
        tipo_label = doc_info['plantilla_nombre']
    else:
        tipo_label = c.get_tipo_contrato_display()
        
    software_nombre = c.software.nombre if c.software_id else 's/software'
    return f"{tipo_label} — {software_nombre}"


def _contrato_list_dict(c, responsable_map, docs_map, today):
    mrr, arr = _compute_mrr_arr(c.monto, c.tipo_contrato, c.frecuencia_facturacion)
    doc_info = docs_map.get(c.id)
    return {
        'id': c.id,
        'nombre': _contrato_nombre(c, doc_info),
        'cliente': {'id': c.cliente_id, 'nombre': str(c.cliente)},
        'software': {'id': c.software_id, 'nombre': c.software.nombre if c.software_id else ''},
        'sla': {'id': c.sla_id, 'nombre': c.sla.nombre if c.sla_id else ''},
        'etapa': c.etapa,
        'etapa_display': c.get_etapa_display(),
        'status': c.status,
        'status_display': c.get_status_display(),
        'tipo_contrato': c.tipo_contrato,
        'tipo_contrato_display': c.get_tipo_contrato_display(),
        'monto': str(c.monto),
        'frecuencia_facturacion': c.frecuencia_facturacion,
        'mrr': str(mrr),
        'arr': str(arr),
        'fecha_inicio': c.fecha_inicio,
        'fecha_vencimiento': c.fecha_vencimiento,
        'fecha_creacion': c.fecha_creacion,
        'dias_restantes': _dias_restantes(c.fecha_vencimiento, today),
        'responsable': responsable_map.get(c.id, ''),
        'tiene_documento': bool(doc_info),
        'documento_id': doc_info['id'] if doc_info else None,
    }


def _build_responsable_map(contrato_ids):
    """Responsable = usuario que registró la creación inicial del contrato (historial)."""
    entradas = (
        HistorialEtapaContrato.objects
        .filter(contrato_id__in=contrato_ids, etapa_anterior__isnull=True)
        .select_related('usuario')
    )
    return {
        e.contrato_id: (e.usuario.get_full_name() or e.usuario.username) if e.usuario_id else ''
        for e in entradas
    }


class ContratoListCreateView(APIView):
    """
    GET /api/contratos/?search=&etapa=&software=&cliente=&page=&page_size=
    Lista paginada de contratos con datos reales (para tabla/kanban de Contratos).

    POST /api/contratos/
    Crea un nuevo contrato en etapa BORRADOR.
    Body: { cliente_id, software_id, sla_id, tipo_contrato, monto, fecha_inicio,
            fecha_vencimiento?, frecuencia_facturacion? (requerido si tipo_contrato=RECURRENTE),
            dias_gracia_autorizados? }
    """
    permission_classes = [(IsTenantMember & RequiresFeature('contratos')) | IsPlatformClienteAccess]

    def get(self, request):
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
        except (ValueError, TypeError):
            page, page_size = 1, 20

        qs = scoped(Contrato.objects.all(), request, cliente_field='cliente_id').select_related('cliente', 'software', 'sla')

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(id__icontains=search) |
                Q(cliente__personanatural__nombre_completo__icontains=search) |
                Q(cliente__personajuridica__razon_social__icontains=search) |
                Q(cliente__email_principal__icontains=search) |
                Q(software__nombre__icontains=search)
            )

        etapa = request.query_params.get('etapa', 'Todos')
        if etapa and etapa != 'Todos':
            qs = qs.filter(etapa=etapa)

        software_id = request.query_params.get('software')
        if software_id:
            qs = qs.filter(software_id=software_id)

        cliente_id = request.query_params.get('cliente')
        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)

        ordering = request.query_params.get('ordering', '').strip()
        if ordering:
            reverse = ordering.startswith('-')
            field = ordering.lstrip('-')

            if field == 'id':
                qs = qs.order_by('-id' if reverse else 'id')
            elif field == 'contrato':
                qs = qs.order_by('-tipo_contrato' if reverse else 'tipo_contrato', '-software__nombre' if reverse else 'software__nombre')
            elif field == 'cliente':
                from django.db.models.functions import Coalesce
                qs = qs.annotate(cliente_name=Coalesce('cliente__personajuridica__razon_social', 'cliente__personanatural__nombre_completo'))
                qs = qs.order_by('-cliente_name' if reverse else 'cliente_name')
            elif field == 'software':
                qs = qs.order_by('-software__nombre' if reverse else 'software__nombre')
            elif field == 'tipo_contrato':
                qs = qs.order_by('-tipo_contrato' if reverse else 'tipo_contrato')
            elif field == 'etapa':
                qs = qs.order_by('-etapa' if reverse else 'etapa')
            elif field == 'mrr':
                qs = qs.order_by('-monto' if reverse else 'monto')
            elif field == 'facturacion':
                qs = qs.order_by('-frecuencia_facturacion' if reverse else 'frecuencia_facturacion')
            elif field == 'renovacion':
                qs = qs.order_by('-fecha_vencimiento' if reverse else 'fecha_vencimiento')
            elif field == 'responsable':
                from django.db.models import Subquery, OuterRef, Value
                from django.db.models.functions import Concat
                from .models import HistorialEtapaContrato
                initial_history = HistorialEtapaContrato.objects.filter(
                    contrato=OuterRef('pk'), etapa_anterior__isnull=True
                )
                creator_fullname = Subquery(
                    initial_history.annotate(
                        full_name=Concat('usuario__first_name', Value(' '), 'usuario__last_name')
                    ).values('full_name')[:1]
                )
                qs = qs.annotate(responsable_name=creator_fullname).order_by('-responsable_name' if reverse else 'responsable_name')
            else:
                qs = qs.order_by('-fecha_creacion')
        else:
            qs = qs.order_by('-fecha_creacion')

        total = qs.count()
        offset = (page - 1) * page_size
        page_items = list(qs[offset: offset + page_size])

        today = timezone.localdate()
        ids = [c.id for c in page_items]
        responsable_map = _build_responsable_map(ids)
        
        docs = (
            DocumentoGenerado.objects.filter(contrato_id__in=ids)
            .select_related('plantilla')
            .order_by('-fecha_generacion')
        )
        docs_map = {}
        for d in docs:
            if d.contrato_id not in docs_map:
                docs_map[d.contrato_id] = {
                    'id': d.id,
                    'plantilla_nombre': d.plantilla.nombre if d.plantilla_id else None
                }

        results = [_contrato_list_dict(c, responsable_map, docs_map, today) for c in page_items]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, -(-total // page_size)),
            'results': results,
        })

    def post(self, request):
        data = request.data

        cliente_id = data.get('cliente_id')
        software_id = data.get('software_id')
        sla_id = data.get('sla_id')
        tipo_contrato = data.get('tipo_contrato')

        errors = {}
        if not cliente_id:
            errors['cliente_id'] = 'Este campo es requerido.'
        if not software_id:
            errors['software_id'] = 'Este campo es requerido.'
        if not sla_id:
            errors['sla_id'] = 'Este campo es requerido.'
        if tipo_contrato not in TipoContrato.values:
            errors['tipo_contrato'] = 'Tipo de contrato inválido.'

        monto = data.get('monto')
        try:
            monto = Decimal(str(monto)) if monto not in (None, '') else Decimal('0')
            if monto < 0:
                errors['monto'] = 'El monto no puede ser negativo.'
        except InvalidOperation:
            errors['monto'] = 'Monto inválido.'
            monto = Decimal('0')

        fecha_inicio = _parse_fecha_iso(data.get('fecha_inicio'))
        if fecha_inicio is None:
            errors['fecha_inicio'] = 'Fecha de inicio requerida (formato YYYY-MM-DD).'

        fecha_vencimiento = None
        if data.get('fecha_vencimiento'):
            fecha_vencimiento = _parse_fecha_iso(data.get('fecha_vencimiento'))
            if fecha_vencimiento is None:
                errors['fecha_vencimiento'] = 'Fecha inválida (formato YYYY-MM-DD).'
            elif fecha_inicio and fecha_vencimiento < fecha_inicio:
                errors['fecha_vencimiento'] = 'Debe ser igual o posterior a la fecha de inicio.'

        try:
            dias_gracia = int(data.get('dias_gracia_autorizados') or 0)
            if dias_gracia < 0:
                errors['dias_gracia_autorizados'] = 'Debe ser un número mayor o igual a 0.'
        except (TypeError, ValueError):
            errors['dias_gracia_autorizados'] = 'Debe ser un número entero.'
            dias_gracia = 0

        frecuencia = data.get('frecuencia_facturacion') or None
        if tipo_contrato == TipoContrato.RECURRENTE:
            if not frecuencia:
                errors['frecuencia_facturacion'] = 'Requerido para contratos recurrentes.'
            elif frecuencia not in FrecuenciaFacturacion.values:
                errors['frecuencia_facturacion'] = 'Frecuencia de facturación inválida.'
        else:
            frecuencia = None

        if errors:
            raise DRFValidationError(errors)

        tenant = resolve_tenant_for_write(request, data)
        enforce_quota(tenant, 'contratos')

        # Las referencias deben pertenecer al mismo tenant: evita colgar un
        # contrato propio de un cliente/producto/SLA de otra empresa.
        if not Cliente.objects.filter(pk=cliente_id, tenant=tenant).exists():
            raise DRFValidationError({'cliente_id': 'Cliente no encontrado.'})
        if not Producto.objects.filter(pk=software_id, tenant=tenant).exists():
            raise DRFValidationError({'software_id': 'Producto no encontrado.'})
        if not SLA.objects.filter(pk=sla_id, tenant=tenant).exists():
            raise DRFValidationError({'sla_id': 'SLA no encontrado.'})

        from django.db import transaction, IntegrityError
        try:
            with transaction.atomic():
                contrato = Contrato.objects.create(
                    tenant=tenant,
                    nombre=data.get('nombre') or None,
                    cliente_id=cliente_id,
                    software_id=software_id,
                    sla_id=sla_id,
                    tipo_contrato=tipo_contrato,
                    monto=monto,
                    frecuencia_facturacion=frecuencia,
                    fecha_inicio=fecha_inicio,
                    fecha_vencimiento=fecha_vencimiento,
                    dias_gracia_autorizados=dias_gracia,
                )

                # Seed default obligations
                ObligacionSLA.objects.create(
                    contrato=contrato,
                    tipo_obligacion="Disponibilidad de plataforma",
                    descripcion=f"Garantizar un {contrato.sla.uptime_garantizado}% de tiempo en línea mensual",
                    penalizacion="Descuento del 10% en la siguiente factura si no se cumple"
                )
                ObligacionSLA.objects.create(
                    contrato=contrato,
                    tipo_obligacion="Tiempo de respuesta soporte",
                    descripcion=f"Tiempo de respuesta máximo de {contrato.sla.tiempo_respuesta_horas} horas para incidentes",
                    penalizacion="Compensación según acuerdo comercial"
                )

                # El registro inicial del historial define al "responsable" del
                # contrato; debe crearse (o no) junto con el contrato mismo.
                HistorialEtapaContrato.objects.create(
                    contrato=contrato,
                    etapa_anterior=None,
                    etapa_nueva=contrato.etapa,
                    usuario=request.user,
                    notas='Creación inicial desde el CLM',
                )
        except IntegrityError:
            raise DRFValidationError({
                'error': 'Los datos del contrato violan una restricción de integridad. Revisa montos y fechas.'
            })

        return Response(_contrato_detail_dict(contrato), status=http_status.HTTP_201_CREATED)


def _contrato_detail_dict(c):
    today = timezone.localdate()
    mrr, arr = _compute_mrr_arr(c.monto, c.tipo_contrato, c.frecuencia_facturacion)

    historial = [
        {
            'fecha': h.fecha_cambio,
            'actor': (h.usuario.get_full_name() or h.usuario.username) if h.usuario_id else 'Sistema',
            'etapa_anterior': h.etapa_anterior,
            'etapa_nueva': h.etapa_nueva,
            'etapa_nueva_display': h.get_etapa_nueva_display(),
            'notas': h.notas or '',
        }
        for h in c.historial_etapas.select_related('usuario').order_by('fecha_cambio')
    ]

    documentos = [
        {
            'id': d.id,
            'plantilla_version': d.plantilla.version_codigo,
            'plantilla_nombre': d.plantilla.nombre,
            'hash_sha256': d.hash_sha256,
            'fecha_generacion': d.fecha_generacion,
        }
        for d in c.documentos_generados.select_related('plantilla').order_by('-fecha_generacion')
    ]

    anexos = [
        {
            'id': a.id,
            'nombre': a.nombre,
            'descripcion': a.descripcion or '',
            'fecha_subida': a.fecha_subida,
            'archivo': a.archivo.url if a.archivo else None,
        }
        for a in c.archivos.order_by('-fecha_subida')
    ]

    obs = list(c.obligaciones.all().order_by('id'))
    if obs:
        obligaciones = [
            {
                'id': ob.id,
                'tipo_obligacion': ob.tipo_obligacion,
                'descripcion': ob.descripcion,
                'penalizacion': ob.penalizacion,
            }
            for ob in obs
        ]
    else:
        obligaciones = []
        if c.sla_id:
            obligaciones = [
                {
                    'id': None,
                    'tipo_obligacion': 'Disponibilidad de plataforma',
                    'descripcion': f"{c.sla.uptime_garantizado}% mensual",
                    'penalizacion': 'No especificada',
                },
                {
                    'id': None,
                    'tipo_obligacion': 'Tiempo de respuesta soporte',
                    'descripcion': f"< {c.sla.tiempo_respuesta_horas}h",
                    'penalizacion': 'No especificada',
                },
            ]

    # Versiones
    root = c.parent_contrato if c.parent_contrato else c
    siblings = Contrato.objects.filter(Q(id=root.id) | Q(parent_contrato=root)).order_by('version')
    versiones = [
        {
            'id': sib.id,
            'version': sib.version,
            'etapa': sib.etapa,
            'etapa_display': sib.get_etapa_display(),
            'fecha_creacion': sib.fecha_creacion,
        }
        for sib in siblings
    ]

    responsable_map = _build_responsable_map([c.id])

    try:
        from plantillas.services.renderizado import resolver_plantilla_activa, SinPlantillaActivaError
        plantilla_activa_obj = resolver_plantilla_activa(c.tipo_contrato, c.software_id, c.tenant)
        plantilla_activa_info = {
            'id': plantilla_activa_obj.id,
            'nombre': plantilla_activa_obj.nombre,
            'version_codigo': plantilla_activa_obj.version_codigo,
            'modo_origen': plantilla_activa_obj.modo_origen,
        }
    except Exception:
        plantilla_activa_info = None

    doc_info = documentos[0] if documentos else None

    return {
        'id': c.id,
        'nombre': _contrato_nombre(c, doc_info),
        'cliente': {'id': c.cliente_id, 'nombre': str(c.cliente), 'email': c.cliente.email_principal},
        'software': {'id': c.software_id, 'nombre': c.software.nombre if c.software_id else ''},
        'sla': {'id': c.sla_id, 'nombre': c.sla.nombre if c.sla_id else ''},
        'etapa': c.etapa,
        'etapa_display': c.get_etapa_display(),
        'status': c.status,
        'status_display': c.get_status_display(),
        'tipo_contrato': c.tipo_contrato,
        'tipo_contrato_display': c.get_tipo_contrato_display(),
        'monto': str(c.monto),
        'frecuencia_facturacion': c.frecuencia_facturacion,
        'mrr': str(mrr),
        'arr': str(arr),
        'fecha_inicio': c.fecha_inicio,
        'fecha_vencimiento': c.fecha_vencimiento,
        'fecha_creacion': c.fecha_creacion,
        'dias_gracia_autorizados': c.dias_gracia_autorizados,
        'fin_periodo_gracia': c.fin_periodo_gracia,
        'dias_restantes': _dias_restantes(c.fecha_vencimiento, today),
        'responsable': responsable_map.get(c.id, ''),
        'historial': historial,
        'documentos': documentos,
        'anexos': anexos,
        'obligaciones_sla': obligaciones,
        'version': c.version,
        'parent_contrato_id': c.parent_contrato_id,
        'versiones': versiones,
        'plantilla_activa': plantilla_activa_info,
        'texto_adicional_clausulas': c.texto_adicional_clausulas,
        'external_editor': c.external_editor,
        'external_doc_id': c.external_doc_id,
        'external_sync_status': c.external_sync_status,
        'external_last_sync': c.external_last_sync,
        'external_locked_by': c.external_locked_by.username if c.external_locked_by else None,
        'external_lock_expires': c.external_lock_expires,
        'firma_proveedor': c.firma_proveedor,
        'firma_status': c.firma_status,
        'firma_envelope_id': c.firma_envelope_id,
        'firma_fecha_envio': c.firma_fecha_envio,
        'firma_fecha_firma': c.firma_fecha_firma,
        'firma_documento_firmado_url': c.firma_documento_firmado.url if c.firma_documento_firmado else None,
    }


class ContratoDetailView(APIView):
    """
    GET    /api/contratos/<id>/   — detalle completo (historial, documentos, anexos, SLA)
    PATCH  /api/contratos/<id>/   — actualiza campos comerciales o transiciona etapa
    DELETE /api/contratos/<id>/   — solo permitido en etapa BORRADOR
    """
    permission_classes = [
        (IsTenantMember & RequiresFeature('contratos') & DeleteRequiresTenantAdmin & EditRequiresPermiso('contratos'))
        | IsPlatformClienteAccess
    ]

    def get(self, request, pk):
        c = get_object_or_404(
            scoped(Contrato.objects.all(), request, cliente_field='cliente_id')
            .select_related('cliente', 'software', 'sla'), pk=pk
        )
        return Response(_contrato_detail_dict(c))

    def patch(self, request, pk):
        c = get_object_or_404(
            scoped(Contrato.objects.all(), request, cliente_field='cliente_id')
            .select_related('cliente', 'software', 'sla'), pk=pk
        )
        data = request.data

        nueva_etapa = data.get('etapa')
        if nueva_etapa:
            if nueva_etapa not in EtapaContrato.values:
                raise DRFValidationError({'etapa': 'Etapa inválida.'})
            c.transicionar_etapa(nueva_etapa, usuario=request.user, notas=data.get('notas', ''))

        if 'sla_id' in data and data['sla_id'] is not None:
            # El SLA referenciado debe pertenecer al mismo tenant que el
            # contrato (mismo chequeo que en la creación, ver línea ~596) -
            # si no, se filtra el nombre/uptime/tiempo_respuesta de un SLA
            # ajeno vía el detalle del contrato.
            if not SLA.objects.filter(pk=data['sla_id'], tenant=c.tenant_id).exists():
                raise DRFValidationError({'sla_id': 'SLA no encontrado.'})

        campo_simple = [
            'nombre', 'monto', 'status', 'sla_id', 'fecha_inicio', 'fecha_vencimiento',
            'dias_gracia_autorizados', 'frecuencia_facturacion',
            'texto_adicional_clausulas',
        ]
        dirty = False
        for campo in campo_simple:
            if campo in data:
                setattr(c, campo, data[campo])
                dirty = True
        if dirty:
            c.save()
            from .models import HistorialEtapaContrato
            HistorialEtapaContrato.objects.create(
                contrato=c,
                etapa_anterior=c.etapa,
                etapa_nueva=c.etapa,
                usuario=request.user,
                notas="Actualización manual del contrato (datos comerciales/fechas)."
            )

        return Response(_contrato_detail_dict(c))

    def delete(self, request, pk):
        c = get_object_or_404(scoped(Contrato.objects.all(), request), pk=pk)
        if c.etapa != EtapaContrato.BORRADOR:
            return Response(
                {'error': 'Solo se pueden eliminar contratos en etapa Borrador.'},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        c.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class ContratoStatsView(APIView):
    """GET /api/contratos/stats/ — KPIs para el StatsStrip de la vista Contratos."""
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    PIPELINE_ETAPAS = [
        EtapaContrato.BORRADOR, EtapaContrato.REVISION,
        EtapaContrato.APROBADO, EtapaContrato.PENDIENTE_FIRMA,
    ]

    def get(self, request):
        today = timezone.localdate()

        base = scoped(Contrato.objects.all(), request)
        cliente_id = _cliente_qparam(request)
        if cliente_id:
            base = base.filter(cliente_id=cliente_id)

        activos = base.filter(status=EstadoContrato.ACTIVO)
        contratos_activos = activos.count()

        # Agregación en SQL (evita traer miles de filas a Python).
        mrr_expr = Case(
            When(frecuencia_facturacion=FrecuenciaFacturacion.ANUAL, then=F('monto') / Decimal('12')),
            default=F('monto'),
            output_field=DecimalField(max_digits=15, decimal_places=4),
        )
        mrr_total = activos.filter(tipo_contrato=TipoContrato.RECURRENTE).aggregate(
            total=Sum(mrr_expr)
        )['total'] or Decimal('0')
        arr_total = mrr_total * Decimal('12')

        por_renovar = base.filter(
            status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA],
            fecha_vencimiento__gte=today,
            fecha_vencimiento__lte=today + timedelta(days=60),
        ).count()

        en_pipeline = base.filter(etapa__in=self.PIPELINE_ETAPAS).count()

        return Response({
            'contratos_activos': contratos_activos,
            'mrr_total': str(mrr_total),
            'arr_total': str(arr_total),
            'por_renovar': por_renovar,
            'en_pipeline': en_pipeline,
        })


class ObligacionListCreateView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def get(self, request, contrato_id):
        contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=contrato_id)
        obs = contrato.obligaciones.all().order_by('id')
        data = [
            {
                'id': ob.id,
                'tipo_obligacion': ob.tipo_obligacion,
                'descripcion': ob.descripcion,
                'penalizacion': ob.penalizacion,
            }
            for ob in obs
        ]
        return Response(data)

    def post(self, request, contrato_id):
        contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=contrato_id)
        if contrato.etapa != EtapaContrato.BORRADOR:
            return Response(
                {'error': 'No se pueden añadir obligaciones a un contrato que no esté en estado Borrador.'},
                status=http_status.HTTP_400_BAD_REQUEST
            )
        
        data = request.data
        tipo_obligacion = data.get('tipo_obligacion')
        descripcion = data.get('descripcion')
        penalizacion = data.get('penalizacion')

        if not tipo_obligacion or not descripcion or not penalizacion:
            return Response(
                {'error': 'Todos los campos (tipo_obligacion, descripcion, penalizacion) son requeridos.'},
                status=http_status.HTTP_400_BAD_REQUEST
            )

        ob = ObligacionSLA(
            contrato=contrato,
            tipo_obligacion=tipo_obligacion,
            descripcion=descripcion,
            penalizacion=penalizacion
        )
        try:
            ob.save(usuario=request.user)
        except ValidationError as e:
            return Response({'error': str(e)}, status=http_status.HTTP_400_BAD_REQUEST)

        return Response({
            'id': ob.id,
            'tipo_obligacion': ob.tipo_obligacion,
            'descripcion': ob.descripcion,
            'penalizacion': ob.penalizacion,
        }, status=http_status.HTTP_201_CREATED)


class ObligacionDetailView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('contratos'), DeleteRequiresTenantAdmin]

    def patch(self, request, pk):
        ob = get_object_or_404(scoped(ObligacionSLA.objects.all(), request, 'contrato__tenant'), pk=pk)
        if ob.contrato.etapa != EtapaContrato.BORRADOR:
            return Response(
                {'error': 'No se pueden editar obligaciones de un contrato que no esté en estado Borrador.'},
                status=http_status.HTTP_400_BAD_REQUEST
            )
        
        data = request.data
        if 'tipo_obligacion' in data:
            ob.tipo_obligacion = data['tipo_obligacion']
        if 'descripcion' in data:
            ob.descripcion = data['descripcion']
        if 'penalizacion' in data:
            ob.penalizacion = data['penalizacion']

        try:
            ob.save(usuario=request.user)
        except ValidationError as e:
            return Response({'error': str(e)}, status=http_status.HTTP_400_BAD_REQUEST)

        return Response({
            'id': ob.id,
            'tipo_obligacion': ob.tipo_obligacion,
            'descripcion': ob.descripcion,
            'penalizacion': ob.penalizacion,
        })

    def delete(self, request, pk):
        ob = get_object_or_404(scoped(ObligacionSLA.objects.all(), request, 'contrato__tenant'), pk=pk)
        if ob.contrato.etapa != EtapaContrato.BORRADOR:
            return Response(
                {'error': 'No se pueden eliminar obligaciones de un contrato que no esté en estado Borrador.'},
                status=http_status.HTTP_400_BAD_REQUEST
            )
        
        try:
            ob.delete(usuario=request.user)
        except ValidationError as e:
            return Response({'error': str(e)}, status=http_status.HTTP_400_BAD_REQUEST)

        return Response(status=http_status.HTTP_204_NO_CONTENT)


class ObligacionHistorialView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def get(self, request, pk):
        logs = scoped(ObligacionSLAAuditLog.objects.all(), request, 'contrato__tenant') \
            .filter(obligacion_id=pk).order_by('-fecha_cambio')
        data = [
            {
                'id': log.id,
                'fecha': log.fecha_cambio,
                'usuario': log.actor_nombre,
                'accion': log.accion,
                'valor_anterior': log.valor_anterior,
                'valor_nuevo': log.valor_nuevo,
            }
            for log in logs
        ]
        return Response(data)


class ContratoEnmendarView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def post(self, request, pk):
        contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=pk)
        enforce_quota(contrato.tenant, 'contratos')

        from django.db import transaction
        with transaction.atomic():
            root = contrato.parent_contrato if contrato.parent_contrato else contrato
            num_versions = Contrato.objects.filter(parent_contrato=root).count()
            next_version = f"{num_versions + 2}.0"

            nuevo_contrato = Contrato.objects.create(
                tenant=contrato.tenant,
                cliente=contrato.cliente,
                software=contrato.software,
                sla=contrato.sla,
                etapa=EtapaContrato.BORRADOR,
                tipo_contrato=contrato.tipo_contrato,
                status=contrato.status,
                monto=contrato.monto,
                frecuencia_facturacion=contrato.frecuencia_facturacion,
                fecha_inicio=contrato.fecha_inicio,
                fecha_vencimiento=contrato.fecha_vencimiento,
                dias_gracia_autorizados=contrato.dias_gracia_autorizados,
                fin_periodo_gracia=contrato.fin_periodo_gracia,
                parent_contrato=root,
                version=next_version
            )
            
            obs = list(contrato.obligaciones.all())
            if obs:
                for ob in obs:
                    nueva_ob = ObligacionSLA(
                        contrato=nuevo_contrato,
                        tipo_obligacion=ob.tipo_obligacion,
                        descripcion=ob.descripcion,
                        penalizacion=ob.penalizacion
                    )
                    nueva_ob.save(usuario=request.user)
            else:
                # Create default obligations for the cloned contract
                if contrato.sla_id:
                    ObligacionSLA.objects.create(
                        contrato=nuevo_contrato,
                        tipo_obligacion="Disponibilidad de plataforma",
                        descripcion=f"Garantizar un {contrato.sla.uptime_garantizado}% de tiempo en línea mensual",
                        penalizacion="Descuento del 10% en la siguiente factura si no se cumple"
                    )
                    ObligacionSLA.objects.create(
                        contrato=nuevo_contrato,
                        tipo_obligacion="Tiempo de respuesta soporte",
                        descripcion=f"Tiempo de respuesta máximo de {contrato.sla.tiempo_respuesta_horas} horas para incidentes",
                        penalizacion="Compensación según acuerdo comercial"
                    )
            
            # Log in history
            HistorialEtapaContrato.objects.create(
                contrato=nuevo_contrato,
                etapa_anterior=None,
                etapa_nueva=EtapaContrato.BORRADOR,
                usuario=request.user,
                notas=f"Creado como enmienda / versión {next_version} a partir del contrato {contrato.id}"
            )
            
        return Response(_contrato_detail_dict(nuevo_contrato), status=http_status.HTTP_201_CREATED)


class ContratoExternalSyncView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def get(self, request, pk):
        contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=pk)
        return Response({
            'contrato_id': contrato.id,
            'external_editor': contrato.external_editor,
            'external_doc_id': contrato.external_doc_id,
            'external_sync_status': contrato.external_sync_status,
            'external_last_sync': contrato.external_last_sync,
            'external_locked_by': contrato.external_locked_by.username if contrato.external_locked_by else None,
            'external_lock_expires': contrato.external_lock_expires,
            'texto_adicional_clausulas': contrato.texto_adicional_clausulas or '',
        })

    def post(self, request, pk):
        contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=pk)
        action = request.data.get('action')
        editor = request.data.get('editor', contrato.external_editor or 'WORD')
        doc_id = request.data.get('doc_id')
        content = request.data.get('content')

        if not action:
            return Response({'error': 'Falta el parámetro "action"'}, status=http_status.HTTP_400_BAD_REQUEST)

        from django.utils import timezone
        from datetime import timedelta

        if action == 'link':
            contrato.external_editor = editor
            contrato.external_doc_id = doc_id or (f"gdoc-{contrato.id}-xyz" if editor == 'GDOCS' else f"word-{contrato.id}-xyz.docx")
            contrato.external_sync_status = 'SYNCED'
            contrato.external_last_sync = timezone.now()
            contrato.save()
            HistorialEtapaContrato.objects.create(
                contrato=contrato,
                etapa_anterior=contrato.etapa,
                etapa_nueva=contrato.etapa,
                usuario=request.user,
                notas=f"Contrato vinculado a {'Google Docs' if editor == 'GDOCS' else 'Microsoft Word'} (ID Documento: {contrato.external_doc_id}) para sincronización automática."
            )

        elif action == 'unlink':
            prev_editor = contrato.external_editor
            contrato.external_editor = None
            contrato.external_doc_id = None
            contrato.external_sync_status = 'NONE'
            contrato.external_last_sync = None
            contrato.external_locked_by = None
            contrato.external_lock_expires = None
            contrato.save()
            HistorialEtapaContrato.objects.create(
                contrato=contrato,
                etapa_anterior=contrato.etapa,
                etapa_nueva=contrato.etapa,
                usuario=request.user,
                notas=f"Vínculo deshecho con {'Google Docs' if prev_editor == 'GDOCS' else 'Microsoft Word' if prev_editor == 'WORD' else 'procesador de texto'}."
            )

        elif action == 'lock':
            contrato.external_sync_status = 'EDITING'
            contrato.external_locked_by = request.user
            contrato.external_lock_expires = timezone.now() + timedelta(hours=1)
            contrato.save()
            HistorialEtapaContrato.objects.create(
                contrato=contrato,
                etapa_anterior=contrato.etapa,
                etapa_nueva=contrato.etapa,
                usuario=request.user,
                notas=f"Bloqueado para edición en {'Google Docs' if editor == 'GDOCS' else 'Microsoft Word'} por el usuario {request.user.username}."
            )

        elif action == 'unlock':
            contrato.external_locked_by = None
            contrato.external_lock_expires = None
            if contrato.external_sync_status == 'EDITING':
                contrato.external_sync_status = 'SYNCED'
            contrato.save()
            HistorialEtapaContrato.objects.create(
                contrato=contrato,
                etapa_anterior=contrato.etapa,
                etapa_nueva=contrato.etapa,
                usuario=request.user,
                notas=f"Bloqueo de edición en procesador externo liberado."
            )

        elif action == 'sync_push':
            if content is not None:
                contrato.texto_adicional_clausulas = content
                contrato.external_last_sync = timezone.now()
                contrato.external_sync_status = 'SYNCED'
                contrato.save()
                HistorialEtapaContrato.objects.create(
                    contrato=contrato,
                    etapa_anterior=contrato.etapa,
                    etapa_nueva=contrato.etapa,
                    usuario=request.user,
                    notas=f"Cambios sincronizados automáticamente desde {'Google Docs' if editor == 'GDOCS' else 'Microsoft Word'}."
                )
            else:
                return Response({'error': 'El contenido no puede estar vacío en sync_push'}, status=http_status.HTTP_400_BAD_REQUEST)

        elif action == 'sync_pull':
            contrato.external_last_sync = timezone.now()
            contrato.save()

        else:
            return Response({'error': 'Acción inválida.'}, status=http_status.HTTP_400_BAD_REQUEST)

        return Response(_contrato_detail_dict(contrato))


class ContratoFirmaElectronicaView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('contratos')]

    def post(self, request, pk):
        contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=pk)
        action = request.data.get('action')
        proveedor = request.data.get('proveedor') # 'OTP', 'DOCUSIGN', 'ADOBE'

        if not action:
            return Response({'error': 'Parámetro "action" es requerido.'}, status=http_status.HTTP_400_BAD_REQUEST)

        from django.utils import timezone
        import uuid

        if action == 'send':
            if not proveedor or proveedor not in ['OTP', 'DOCUSIGN', 'ADOBE']:
                return Response({'error': 'Proveedor de firma inválido o no especificado.'}, status=http_status.HTTP_400_BAD_REQUEST)

            contrato.firma_proveedor = proveedor
            contrato.firma_status = 'PENDING'
            contrato.firma_envelope_id = str(uuid.uuid4())
            contrato.firma_fecha_envio = timezone.now()

            # Transicionar etapa del contrato a PENDIENTE_FIRMA si no estaba
            if contrato.etapa != EtapaContrato.PENDIENTE_FIRMA:
                contrato.transicionar_etapa(EtapaContrato.PENDIENTE_FIRMA, usuario=request.user, notas=f"Enviado para firma electrónica vía {proveedor}")
            else:
                contrato.save()
                HistorialEtapaContrato.objects.create(
                    contrato=contrato,
                    etapa_anterior=contrato.etapa,
                    etapa_nueva=contrato.etapa,
                    usuario=request.user,
                    notas=f"Sobre de firma electrónica reiniciado vía {proveedor} (Envelope ID: {contrato.firma_envelope_id})"
                )

        elif action == 'sign':
            if contrato.firma_status != 'PENDING':
                return Response({'error': 'No se puede firmar un contrato que no está pendiente de firma.'}, status=http_status.HTTP_400_BAD_REQUEST)

            contrato.firma_status = 'SIGNED'
            contrato.firma_fecha_firma = timezone.now()

            # Guardar documento mock firmado en PDF
            from django.core.files.base import ContentFile
            contrato.firma_documento_firmado.save(
                f"contrato_{contrato.id}_firmado.pdf",
                ContentFile(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n2 0 obj\n<<\n/Type /Pages\n/Kids [3 0 R]\n/Count 1\n>>\nendobj\n3 0 obj\n<<\n/Type /Page\n/Parent 2 0 R\n/Resources <<\n/Font <<\n/F1 <<\n/Type /Font\n/Subtype /Type1\n/BaseFont /Helvetica\n>>\n>>\n>>\n/MediaBox [0 0 595.275 841.889]\n/Contents 4 0 R\n>>\nendobj\n4 0 obj\n<<\n/Length 72\n>>\nstream\nBT\n/F1 12 Tf\n72 712 Td\n(CONTRATO FIRMADO DIGITALMENTE Y CERTIFICADO - ENFOQUE CLM) Tj\nET\nendstream\nendobj\nxref\n0 5\n0000000000 65535 f \n0000000015 00000 n \n0000000068 00000 n \n0000000127 00000 n \n0000000282 00000 n \ntrailer\n<<\n/Size 5\n/Root 1 0 R\n>>\nstartxref\n405\n%%EOF\n")
            )

            # Transicionar a ACTIVO
            contrato.transicionar_etapa(EtapaContrato.ACTIVO, usuario=request.user, notas=f"Contrato firmado digitalmente a través del portal de {contrato.firma_proveedor}. Documento certificado e inmutable guardado.")

        elif action == 'decline':
            if contrato.firma_status != 'PENDING':
                return Response({'error': 'El proceso de firma no está activo.'}, status=http_status.HTTP_400_BAD_REQUEST)

            contrato.firma_status = 'DECLINED'
            contrato.save()

            HistorialEtapaContrato.objects.create(
                contrato=contrato,
                etapa_anterior=contrato.etapa,
                etapa_nueva=contrato.etapa,
                usuario=request.user,
                notas=f"Proceso de firma electrónica RECHAZADO por el destinatario en {contrato.firma_proveedor}."
            )

        elif action == 'cancel':
            prev_status = contrato.firma_status
            prev_prov = contrato.firma_proveedor

            contrato.firma_status = 'NONE'
            contrato.firma_proveedor = 'NONE'
            contrato.firma_envelope_id = None
            contrato.firma_fecha_envio = None
            contrato.firma_fecha_firma = None
            contrato.save()

            # Regresar a APROBADO si corresponde
            if contrato.etapa == EtapaContrato.PENDIENTE_FIRMA:
                contrato.transicionar_etapa(EtapaContrato.APROBADO, usuario=request.user, notas=f"Envío de firma {prev_prov} cancelado por el usuario. Contrato devuelto a etapa Aprobado.")
            else:
                HistorialEtapaContrato.objects.create(
                    contrato=contrato,
                    etapa_anterior=contrato.etapa,
                    etapa_nueva=contrato.etapa,
                    usuario=request.user,
                    notas=f"Solicitud de firma electrónica cancelada por el usuario."
                )
        else:
            return Response({'error': 'Acción inválida.'}, status=http_status.HTTP_400_BAD_REQUEST)

        return Response(_contrato_detail_dict(contrato))
