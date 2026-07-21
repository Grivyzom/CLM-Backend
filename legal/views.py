from rest_framework.views import APIView
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Count, Q
from contratos.models import Contrato, EstadoContrato, EtapaContrato, ObligacionSLAAuditLog, HistorialEtapaContrato
from clientes.models import Cliente
from legal.models import LogAceptacion
from tenants.permissions import IsTenantMember, RequiresFeature
from tenants.scoping import scoped

class AuditoriaView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('legal')]

    def get(self, request):
        today = timezone.localdate()

        # 1. Fetch contracts to calculate KPIs — antes: 6 .count() separados sobre
        # el mismo queryset (7 con non_standard_clauses_count), ahora 1 sola
        # aggregate() con Count(filter=...) condicional por cada bucket.
        all_contracts = scoped(Contrato.objects.all(), request)
        counts = all_contracts.aggregate(
            total=Count('id'),
            mora_suspendido=Count('id', filter=Q(status__in=[EstadoContrato.MORA, EstadoContrato.SUSPENDIDO])),
            pending_audits=Count('id', filter=Q(etapa__in=[EtapaContrato.REVISION, EtapaContrato.APROBADO])),
            expired_active=Count('id', filter=Q(status=EstadoContrato.ACTIVO, fecha_vencimiento__lt=today)),
            medium_risk=Count('id', filter=Q(etapa__in=[EtapaContrato.BORRADOR, EtapaContrato.REVISION, EtapaContrato.PENDIENTE_FIRMA])),
            low_risk=Count('id', filter=(
                Q(status__in=[EstadoContrato.ACTIVO, EstadoContrato.GRACIA, EstadoContrato.VENCIDO])
                & ~Q(etapa__in=[EtapaContrato.BORRADOR, EtapaContrato.REVISION, EtapaContrato.PENDIENTE_FIRMA])
            )),
        )
        total_contracts = counts['total']
        mora_suspended_count = counts['mora_suspendido']

        # Non standard clauses: count of modifications in SLA obligations (EDITAR or ELIMINAR actions)
        non_standard_clauses_count = scoped(ObligacionSLAAuditLog.objects.all(), request, 'contrato__tenant') \
            .filter(accion__in=['EDITAR', 'ELIMINAR']).count()

        # Pending audits: contracts in REVISION or APROBADO stage
        pending_audits_count = counts['pending_audits']

        # Calculate Compliance Score
        # Base is 100.
        # Deduct 15 points per contract in MORA or SUSPENDIDO (max penalty 30)
        # Deduct 5 points per expired active contract (max penalty 20)
        # Minimum score is 50
        expired_active_count = counts['expired_active']

        compliance_penalty = (mora_suspended_count * 15) + (expired_active_count * 5)
        compliance_score = max(50, 100 - compliance_penalty)

        # If there are no contracts, baseline compliance is 100
        if total_contracts == 0:
            compliance_score = 100
            high_risk_count = 0
        else:
            high_risk_count = mora_suspended_count

        kpis = {
            'complianceScore': compliance_score,
            'highRiskContracts': high_risk_count,
            'nonStandardClauses': non_standard_clauses_count,
            'pendingAudits': pending_audits_count
        }

        # 2. Risk Distribution
        # Low risk: ACTIVE, GRACIA, VENCIDO, TERMINADO status + not Mora/Suspended
        # Medium risk: BORRADOR, REVISION, PENDIENTE_FIRMA stages
        # High risk: MORA, SUSPENDIDO status
        high_risk_db = mora_suspended_count
        medium_risk_db = counts['medium_risk']
        low_risk_db = counts['low_risk']

        risk_distribution = [
            { 'name': 'Riesgo Bajo', 'value': low_risk_db, 'color': 'var(--success-alt)' },
            { 'name': 'Riesgo Medio', 'value': medium_risk_db, 'color': 'var(--warning-vivid)' },
            { 'name': 'Riesgo Alto', 'value': high_risk_db, 'color': 'var(--danger-bright)' }
        ]

        # 3. Critical Contracts (up to 5)
        critical_contracts_list = []
        
        # Find actual contracts with issues
        # Issues: Mora, Suspended, Expired but Active, or very large amount (> 100k) in Draft stage
        db_critical = all_contracts.filter(
            Q(status__in=[EstadoContrato.MORA, EstadoContrato.SUSPENDIDO]) |
            Q(status=EstadoContrato.ACTIVO, fecha_vencimiento__lt=today) |
            Q(monto__gt=100000, etapa=EtapaContrato.BORRADOR)
        )[:5]

        for c in db_critical:
            issue_desc = "Desviación indeterminada"
            if c.status == EstadoContrato.MORA:
                issue_desc = "Contrato registrado en estado de Mora"
            elif c.status == EstadoContrato.SUSPENDIDO:
                issue_desc = "Servicio suspendido temporalmente"
            elif c.status == EstadoContrato.ACTIVO and c.fecha_vencimiento and c.fecha_vencimiento < today:
                issue_desc = f"Contrato vencido el {c.fecha_vencimiento.strftime('%Y-%m-%d')} pero activo"
            elif c.monto > 100000 and c.etapa == EtapaContrato.BORRADOR:
                issue_desc = "Monto elevado (>100K) en etapa de Borrador"

            critical_contracts_list.append({
                'id': f"C-{c.id}",
                'client': str(c.cliente),
                'issue': issue_desc,
                'type': c.get_tipo_contrato_display(),
                'status': c.get_status_display()
            })

        # 4. Audit Logs (Combine real tables)
        logs = []

        # A. Obligacion SLA logs
        sla_logs = scoped(ObligacionSLAAuditLog.objects.all(), request, 'contrato__tenant') \
            .select_related('contrato').order_by('-fecha_cambio')[:15]
        for sl in sla_logs:
            logs.append({
                'id': f"sla-{sl.id}",
                'user': sl.actor_nombre or (sl.usuario.username if sl.usuario else "Sistema"),
                'action': f"{sl.accion.lower().capitalize()} obligacion SLA",
                'target': f"Contrato #{sl.contrato_id}",
                'date': sl.fecha_cambio.isoformat(),
                'risk': 'high' if sl.accion == 'ELIMINAR' else ('medium' if sl.accion == 'EDITAR' else 'low'),
                'details': sl.valor_nuevo or sl.valor_anterior,
                'ip': 'N/A',
                'session': 'SISTEMA'
            })

        # B. Stage transitions
        stage_logs = scoped(HistorialEtapaContrato.objects.all(), request, 'contrato__tenant') \
            .select_related('contrato', 'usuario').order_by('-fecha_cambio')[:15]
        for st in stage_logs:
            user_name = st.usuario.username if st.usuario else "Sistema"
            action_text = "Actualizó contrato" if st.etapa_anterior == st.etapa_nueva else f"Transicionó a {st.etapa_nueva.lower()}"
            logs.append({
                'id': f"stage-{st.id}",
                'user': user_name,
                'action': action_text,
                'target': f"Contrato #{st.contrato_id}",
                'date': st.fecha_cambio.isoformat(),
                'risk': 'low',
                'details': st.notas or (f"Cambio de etapa de {st.etapa_anterior} a {st.etapa_nueva}." if st.etapa_anterior != st.etapa_nueva else "Actualización general"),
                'ip': 'N/A',
                'session': 'WEB'
            })

        # C. Acceptances
        acceptance_logs = scoped(LogAceptacion.objects.all(), request, 'cliente__tenant') \
            .select_related('cliente', 'software', 'documento_legal').order_by('-fecha_hora_registro')[:15]
        for ac in acceptance_logs:
            logs.append({
                'id': f"accept-{ac.id}",
                'user': str(ac.cliente),
                'action': f"Aceptó {ac.documento_legal.tipo} v{ac.documento_legal.version_codigo}",
                'target': f"Software: {ac.software.nombre}",
                'date': ac.fecha_hora_registro.isoformat(),
                'risk': 'low',
                'details': f"Navegador: {ac.user_agent}",
                'ip': ac.ip_direccion,
                'session': 'API'
            })

        # Sort combined logs by date descending
        logs.sort(key=lambda x: x['date'], reverse=True)
        logs = logs[:20] # Limit to top 20

        return Response({
            'kpis': kpis,
            'riskDistribution': risk_distribution,
            'criticalContracts': critical_contracts_list,
            'auditLogs': logs
        })


class AnalisisIAView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('legal')]

    def get(self, request, contrato_id):
        contratos = scoped(Contrato.objects.all(), request)
        try:
            contrato = contratos.get(pk=contrato_id)
        except Contrato.DoesNotExist:
            return Response({'error': 'Contrato no encontrado'}, status=404)

        from legal.models import AnalisisContratoIA
        analisis = AnalisisContratoIA.objects.filter(contrato=contrato).order_by('-fecha_analisis').first()
        if not analisis:
            return Response({
                'id': None,
                'fecha_analisis': None,
                'checklist_cumplido': False,
                'resultado_checklist_json': {'items': []},
                'riesgos_detectados_json': [],
                'contrato_categoria': contrato.software.categoria if contrato.software else 'Software'
            })

        return Response({
            'id': analisis.id,
            'fecha_analisis': analisis.fecha_analisis.isoformat(),
            'checklist_cumplido': analisis.checklist_cumplido,
            'resultado_checklist_json': analisis.resultado_checklist_json,
            'riesgos_detectados_json': analisis.riesgos_detectados_json,
            'contrato_categoria': contrato.software.categoria if contrato.software else 'Software'
        })


class AnalizarIAView(APIView):
    permission_classes = [IsTenantMember, RequiresFeature('legal')]

    def post(self, request, contrato_id):
        contratos = scoped(Contrato.objects.all(), request)
        try:
            contrato = contratos.get(pk=contrato_id)
        except Contrato.DoesNotExist:
            return Response({'error': 'Contrato no encontrado'}, status=404)

        from legal.services import analizar_contrato_cumplimiento
        analisis = analizar_contrato_cumplimiento(contrato_id)
        if not analisis:
            return Response({'error': 'No se pudo analizar el contrato'}, status=400)

        return Response({
            'id': analisis.id,
            'fecha_analisis': analisis.fecha_analisis.isoformat(),
            'checklist_cumplido': analisis.checklist_cumplido,
            'resultado_checklist_json': analisis.resultado_checklist_json,
            'riesgos_detectados_json': analisis.riesgos_detectados_json,
            'contrato_categoria': contrato.software.categoria if contrato.software else 'Software'
        })

