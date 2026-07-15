from django.db.models import Count, Q, Subquery, OuterRef, IntegerField, ProtectedError
from django.db.models.functions import Coalesce
from django.db import IntegrityError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from rest_framework.exceptions import ValidationError

from .models import PersonaJuridica, PersonaNatural, Cliente, ContactoRepresentante
from .serializers import PersonaJuridicaSerializer, PersonaNaturalSerializer
from contratos.models import Contrato
from .utils import enviar_correo_bienvenida
from tenants.permissions import DeleteRequiresTenantAdmin, EditRequiresPermiso, IsPlatformClienteAccess, IsTenantMember, RequiresFeature
from tenants.scoping import enforce_quota, resolve_tenant_for_write, scoped


# ─── Helper: calcular estado de cliente ───────────────────────────────────────
def _compute_estado(is_active, cliente_id, contrato_status_map):
    """Devuelve 'Activo', 'En revisión' o 'Inactivo' dado el estado del cliente."""
    if not is_active:
        return 'Inactivo'
    statuses = contrato_status_map.get(cliente_id, set())
    if 'ACTIVO' in statuses:
        return 'Activo'
    if statuses & {'MORA', 'GRACIA'}:
        return 'En revisión'
    return 'Activo'


def _get_contratos_activos(cliente_id):
    """Lista de contratos con status ACTIVO para un cliente."""
    contratos = (
        Contrato.objects
        .filter(cliente_id=cliente_id, status='ACTIVO')
        .select_related('software')
        .order_by('-fecha_inicio')
    )
    return [
        {
            'id': c.id,
            'software': c.software.nombre,
            'tipo_contrato': c.tipo_contrato,
            'fecha_inicio': c.fecha_inicio,
            'fecha_vencimiento': c.fecha_vencimiento,
            'monto': str(c.monto),
        }
        for c in contratos
    ]


def _resolve_cliente_scoped(request, pk):
    """Resuelve un cliente (jurídica o natural) dentro del alcance del usuario.
    Devuelve (obj, tipo) o (None, None) si no existe o está fuera de alcance."""
    try:
        obj = scoped(PersonaJuridica.objects.all(), request, cliente_field='pk').get(pk=pk)
        return obj, 'juridica'
    except PersonaJuridica.DoesNotExist:
        pass
    try:
        obj = scoped(PersonaNatural.objects.all(), request, cliente_field='pk').get(pk=pk)
        return obj, 'natural'
    except PersonaNatural.DoesNotExist:
        return None, None


def _serialize_cliente_detail(obj, tipo, pk):
    """Payload de detalle de cliente compartido por ClienteDetailView y el
    workspace: serializer según tipo + estado calculado + contactos + contratos
    activos."""
    contratos_count = Contrato.objects.filter(cliente_id=pk).count()
    statuses = set(Contrato.objects.filter(cliente_id=pk).values_list('status', flat=True))
    estado = _compute_estado(obj.is_active, pk, {pk: statuses})

    if tipo == 'juridica':
        contactos = list(ContactoRepresentante.objects.filter(cliente_juridico_id=pk))
        contacto_data = {'nombre': contactos[0].nombre, 'telefono': contactos[0].telefono or '', 'cargo': contactos[0].cargo, 'email': contactos[0].email} if contactos else None
        extra_ctx = {pk: {'contratos_count': contratos_count, 'contacto': contacto_data}}
        data = PersonaJuridicaSerializer(obj, context={'extra': extra_ctx}).data
        data['contactos'] = [{'nombre': c.nombre, 'cargo': c.cargo, 'email': c.email, 'telefono': c.telefono or ''} for c in contactos]
    else:
        extra_ctx = {pk: {'contratos_count': contratos_count, 'contacto': None}}
        data = PersonaNaturalSerializer(obj, context={'extra': extra_ctx}).data

    data['estado'] = estado
    data['contratos_count'] = contratos_count
    data['contratos_activos'] = _get_contratos_activos(pk)
    return data


def get_filtered_clientes_unified(query_params, request=None):
    """
    Aplica los filtros (search, estado, tipo, fecha_desde, fecha_hasta) leídos de
    query_params (dict-like: QueryDict de Django o request.query_params de DRF) y
    devuelve la lista unificada de clientes (PersonaJuridica + PersonaNatural)
    ordenada por fecha_registro desc, SIN paginar.

    Si se pasa request, el resultado queda acotado al tenant del usuario.

    Reutilizado por ClienteListView.get (paginación) y por las vistas de exportación
    de documentos (que necesitan el mismo conjunto filtrado completo).

    Cada item: {obj, tipo, estado, fecha_registro, contratos_count, contacto}
    """
    search      = query_params.get('search', '').strip()
    estado_q    = query_params.get('estado', 'Todos').strip()
    tipo_q      = query_params.get('tipo', 'Todos').strip()
    fecha_desde = query_params.get('fecha_desde', None)
    fecha_hasta = query_params.get('fecha_hasta', None)
    ordering    = query_params.get('ordering', '').strip()

    # ── Querysets base (acotados al tenant del solicitante) ──────────────
    pj_qs = PersonaJuridica.objects.all()
    pn_qs = PersonaNatural.objects.all()
    if request is not None:
        pj_qs = scoped(pj_qs, request, cliente_field='pk')
        pn_qs = scoped(pn_qs, request, cliente_field='pk')

    # Filtro de fechas (fecha_registro en la tabla base Cliente)
    if fecha_desde:
        pj_qs = pj_qs.filter(fecha_registro__date__gte=fecha_desde)
        pn_qs = pn_qs.filter(fecha_registro__date__gte=fecha_desde)
    if fecha_hasta:
        pj_qs = pj_qs.filter(fecha_registro__date__lte=fecha_hasta)
        pn_qs = pn_qs.filter(fecha_registro__date__lte=fecha_hasta)

    # Filtro de tipo
    include_juridica = tipo_q in ('Todos', 'juridica', 'Enterprise', 'Pyme', 'Startup')
    include_natural  = tipo_q in ('Todos', 'natural', 'Persona Natural')

    # Búsqueda de texto
    if search:
        pj_qs = pj_qs.filter(
            Q(razon_social__icontains=search) |
            Q(rut__icontains=search) |
            Q(giro__icontains=search) |
            Q(email_principal__icontains=search)
        )
        pn_qs = pn_qs.filter(
            Q(nombre_completo__icontains=search) |
            Q(run__icontains=search) |
            Q(email_principal__icontains=search)
        )

    # ── Precalcular conteo de contratos por cliente ──────────────────────
    pj_ids = list(pj_qs.values_list('id', flat=True)) if include_juridica else []
    pn_ids = list(pn_qs.values_list('id', flat=True)) if include_natural else []
    all_ids = pj_ids + pn_ids

    # Mapa: cliente_id → {status_set, count}
    contratos_raw = (
        Contrato.objects
        .filter(cliente_id__in=all_ids)
        .values('cliente_id', 'status')
        .annotate(cnt=Count('id'))
    )
    contrato_status_map = {}   # id → set of statuses
    contrato_count_map  = {}   # id → total count
    for row in contratos_raw:
        cid = row['cliente_id']
        contrato_status_map.setdefault(cid, set()).add(row['status'])
        contrato_count_map[cid] = contrato_count_map.get(cid, 0) + row['cnt']

    # ── Construir lista unificada con estado calculado ───────────────────
    unified = []

    if include_juridica:
        for obj in pj_qs.order_by('-fecha_registro'):
            computed_estado = _compute_estado(obj.is_active, obj.id, contrato_status_map)
            unified.append({
                'obj': obj,
                'tipo': 'juridica',
                'estado': computed_estado,
                'fecha_registro': obj.fecha_registro,
                'contratos_count': contrato_count_map.get(obj.id, 0),
                'contacto': None,
            })

    if include_natural:
        for obj in pn_qs.order_by('-fecha_registro'):
            computed_estado = _compute_estado(obj.is_active, obj.id, contrato_status_map)
            unified.append({
                'obj': obj,
                'tipo': 'natural',
                'estado': computed_estado,
                'fecha_registro': obj.fecha_registro,
                'contratos_count': contrato_count_map.get(obj.id, 0),
                'contacto': None,
            })

    # Ordenar unificado por el campo indicado en ordering
    if ordering:
        reverse = ordering.startswith('-')
        field = ordering.lstrip('-')

        # Pre-cargar representantes si ordenamos por contacto
        representatives_map = {}
        if field == 'contacto':
            pj_ids = [u['obj'].id for u in unified if u['tipo'] == 'juridica']
            if pj_ids:
                from .models import ContactoRepresentante
                for cr in ContactoRepresentante.objects.filter(cliente_juridico_id__in=pj_ids):
                    if cr.cliente_juridico_id not in representatives_map:
                        representatives_map[cr.cliente_juridico_id] = cr.nombre

        def get_sort_value(item):
            obj = item['obj']
            if field == 'razon_social':
                val = obj.razon_social if item['tipo'] == 'juridica' else obj.nombre_completo
            elif field == 'id_fiscal':
                val = obj.rut if item['tipo'] == 'juridica' else obj.run
            elif field == 'sector':
                val = obj.giro if item['tipo'] == 'juridica' else 'Persona Natural'
            elif field == 'contacto':
                if item['tipo'] == 'juridica':
                    val = representatives_map.get(obj.id, '')
                else:
                    val = obj.nombre_completo
            elif field == 'tipo':
                val = item['tipo']
            elif field == 'estado':
                val = item['estado']
            elif field == 'contratos':
                val = item['contratos_count']
            elif field == 'fecha_registro':
                val = item['fecha_registro']
            else:
                val = item['fecha_registro']

            if isinstance(val, str):
                return val.lower()
            if val is None:
                return ''
            return val

        unified.sort(key=get_sort_value, reverse=reverse)
    else:
        # Ordenar por fecha_registro desc por defecto
        unified.sort(key=lambda x: x['fecha_registro'], reverse=True)

    # ── Filtro de estado (se aplica tras calcular) ───────────────────────
    if estado_q not in ('Todos', '', None):
        unified = [u for u in unified if u['estado'] == estado_q]

    return unified


class ClienteListView(APIView):
    """
    GET /api/clientes/
    Devuelve lista paginada de todos los clientes (PersonaJuridica + PersonaNatural)
    con conteo de contratos, filtros y búsqueda.

    POST /api/clientes/
    Crea un nuevo cliente (PersonaNatural o PersonaJuridica).

    Query params (GET):
      - search      : texto libre (busca en razón social, RUT/RUN, giro, nombre_completo)
      - estado      : Activo | En revisión | Inactivo | Todos
      - tipo        : juridica | natural | Todos
      - fecha_desde : YYYY-MM-DD
      - fecha_hasta : YYYY-MM-DD
      - page        : número de página (default 1)
      - page_size   : registros por página (default 20, máx 100)
    """
    permission_classes = [(IsTenantMember & RequiresFeature('clientes')) | IsPlatformClienteAccess]

    def post(self, request):
        """Crea PersonaNatural o PersonaJuridica."""
        tipo = request.data.get('tipo', '').strip().lower()
        if tipo not in ('natural', 'juridica'):
            return Response({'error': 'tipo debe ser "natural" o "juridica"'}, status=status.HTTP_400_BAD_REQUEST)

        tenant_name = request.data.get('nombre_completo', '').strip() if tipo == 'natural' else request.data.get('razon_social', '').strip()
        if not tenant_name:
            return Response({'error': 'Nombre o razón social es requerido'}, status=status.HTTP_400_BAD_REQUEST)

        # El Cliente siempre se crea dentro del tenant del usuario autenticado
        # (resolve_tenant_for_write ignora cualquier tenant_id del payload para
        # usuarios de tenant). Antes esto hacía Tenant.objects.get_or_create()
        # por razon_social, lo que permitía a un usuario de un tenant recuperar
        # y mutar (categoria/estado) el Tenant de otra empresa si adivinaba su
        # razón social — nunca se debe crear ni modificar un Tenant desde acá.
        try:
            tenant = resolve_tenant_for_write(request, request.data)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        # Enforce quota for the tenant (even if just created, it might have defaults)
        enforce_quota(tenant, 'clientes')
        # Unicidad por tenant de RUN/RUT: la DB no puede imponerla (el tenant
        # vive en la tabla padre del multi-table inheritance), se valida aquí.
        run = request.data.get('run', '').strip()
        rut = request.data.get('rut', '').strip()
        if tipo == 'natural' and run and PersonaNatural.objects.filter(tenant=tenant, run=run).exists():
            return Response({'error': 'RUN ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
        if tipo == 'juridica' and rut and PersonaJuridica.objects.filter(tenant=tenant, rut=rut).exists():
            return Response({'error': 'RUT ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if tipo == 'natural':
                cliente = PersonaNatural.objects.create(
                    tenant=tenant,
                    email_principal=request.data.get('email_principal', '').strip(),
                    telefono_contacto=request.data.get('telefono_contacto', '').strip() or None,
                    run=request.data.get('run', '').strip(),
                    nombre_completo=request.data.get('nombre_completo', '').strip(),
                )
                extra_ctx = {cliente.id: {'contratos_count': 0, 'contacto': None}}
                data = PersonaNaturalSerializer(cliente, context={'extra': extra_ctx}).data
            else:  # juridica
                cliente = PersonaJuridica.objects.create(
                    tenant=tenant,
                    email_principal=request.data.get('email_principal', '').strip(),
                    telefono_contacto=request.data.get('telefono_contacto', '').strip() or None,
                    rut=request.data.get('rut', '').strip(),
                    razon_social=request.data.get('razon_social', '').strip(),
                    giro=request.data.get('giro', '').strip(),
                )
                # Crear contacto representante si se proporciona
                contacto_data = request.data.get('contacto_representante')
                if contacto_data:
                    ContactoRepresentante.objects.create(
                        cliente_juridico=cliente,
                        nombre=contacto_data.get('nombre', '').strip(),
                        cargo=contacto_data.get('cargo', '').strip(),
                        email=contacto_data.get('email', '').strip(),
                        telefono=contacto_data.get('telefono', '').strip() or None,
                    )
                extra_ctx = {cliente.id: {'contratos_count': 0, 'contacto': None}}
                data = PersonaJuridicaSerializer(cliente, context={'extra': extra_ctx}).data

            # Enviar correo de bienvenida
            enviar_correo_bienvenida(cliente)

            return Response(data, status=status.HTTP_201_CREATED)
        except IntegrityError as e:
            error_msg = str(e)
            if 'email' in error_msg:  # cubre la constraint uniq_cliente_email_por_tenant
                return Response({'error': 'Email ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            elif 'run' in error_msg:
                return Response({'error': 'RUN ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            elif 'rut' in error_msg:
                return Response({'error': 'RUT ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Datos duplicados'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        try:
            page      = max(1, int(request.query_params.get('page', 1)))
            page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
        except (ValueError, TypeError):
            page, page_size = 1, 20

        offset = (page - 1) * page_size

        # Lista unificada (todos los clientes que matchean search/estado/tipo/fechas, sin paginar)
        unified = get_filtered_clientes_unified(request.query_params, request)

        # ── Totales para stats ───────────────────────────────────────────────
        total_all   = len(unified)
        activos_n   = sum(1 for u in unified if u['estado'] == 'Activo')
        revision_n  = sum(1 for u in unified if u['estado'] == 'En revisión')
        inactivos_n = sum(1 for u in unified if u['estado'] == 'Inactivo')

        # ── Paginación ────────────────────────────────────────────────────────
        page_items = unified[offset: offset + page_size]

        # Cargar contactos representantes solo para personasjuridicas en la página
        pj_page_ids = [u['obj'].id for u in page_items if u['tipo'] == 'juridica']
        contactos_map = {}
        if pj_page_ids:
            for cr in ContactoRepresentante.objects.filter(cliente_juridico_id__in=pj_page_ids):
                if cr.cliente_juridico_id not in contactos_map:
                    contactos_map[cr.cliente_juridico_id] = {
                        'nombre': cr.nombre,
                        'cargo': cr.cargo,
                        'telefono': cr.telefono,
                        'email': cr.email,
                    }

        # ── Serializar ────────────────────────────────────────────────────────
        results = []
        for item in page_items:
            obj   = item['obj']
            cid   = obj.id
            extra_ctx = {cid: {
                'contratos_count': item['contratos_count'],
                'contacto': contactos_map.get(cid),
            }}

            if item['tipo'] == 'juridica':
                data = PersonaJuridicaSerializer(obj, context={'extra': extra_ctx}).data
            else:
                data = PersonaNaturalSerializer(obj, context={'extra': extra_ctx}).data

            # Override estado calculado (más eficiente que re-calcular en el serializer)
            data['estado'] = item['estado']
            data['contratos_count'] = item['contratos_count']
            results.append(data)

        return Response({
            'count': total_all,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, -(-total_all // page_size)),  # ceil division
            'stats': {
                'total': total_all,
                'activos': activos_n,
                'en_revision': revision_n,
                'inactivos': inactivos_n,
            },
            'results': results,
        })


class ClienteDetailView(APIView):
    """
    GET /api/clientes/<id>/
    Devuelve el detalle completo de un cliente (jurídico o natural).

    PATCH /api/clientes/<id>/
    Actualiza datos de un cliente. Soporta:
      - is_active: booleano (cambiar estado Activo/Inactivo)
      - email_principal, telefono_contacto: texto
      - Campos específicos según tipo (run/rut, nombre_completo/razon_social, giro)

    DELETE /api/clientes/<id>/
    Elimina un cliente (PersonaJuridica o PersonaNatural).
    """
    permission_classes = [
        (IsTenantMember & RequiresFeature('clientes') & DeleteRequiresTenantAdmin & EditRequiresPermiso('clientes'))
        | IsPlatformClienteAccess
    ]

    def get(self, request, pk):
        obj, tipo = _resolve_cliente_scoped(request, pk)
        if obj is None:
            return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_cliente_detail(obj, tipo, pk))

    def patch(self, request, pk):
        # Buscar en PersonaJuridica
        try:
            obj = scoped(PersonaJuridica.objects.all(), request, cliente_field='pk').get(pk=pk)

            # Actualizar campos
            if 'is_active' in request.data:
                obj.is_active = request.data.get('is_active', obj.is_active)
            if 'email_principal' in request.data:
                obj.email_principal = request.data.get('email_principal', obj.email_principal).strip()
            if 'telefono_contacto' in request.data:
                obj.telefono_contacto = request.data.get('telefono_contacto', obj.telefono_contacto).strip() or None
            if 'razon_social' in request.data:
                obj.razon_social = request.data.get('razon_social', obj.razon_social).strip()
            if 'rut' in request.data:
                obj.rut = request.data.get('rut', obj.rut).strip()
            if 'giro' in request.data:
                obj.giro = request.data.get('giro', obj.giro).strip()

            obj.save()

            contratos_count = Contrato.objects.filter(cliente_id=pk).count()
            statuses = set(Contrato.objects.filter(cliente_id=pk).values_list('status', flat=True))
            estado = _compute_estado(obj.is_active, pk, {pk: statuses})
            contactos = list(ContactoRepresentante.objects.filter(cliente_juridico_id=pk))
            contacto_data = {'nombre': contactos[0].nombre, 'telefono': contactos[0].telefono or '', 'cargo': contactos[0].cargo, 'email': contactos[0].email} if contactos else None
            extra_ctx = {pk: {'contratos_count': contratos_count, 'contacto': contacto_data}}
            data = PersonaJuridicaSerializer(obj, context={'extra': extra_ctx}).data
            data['estado'] = estado
            data['contratos_count'] = contratos_count
            return Response(data, status=status.HTTP_200_OK)
        except PersonaJuridica.DoesNotExist:
            pass
        except IntegrityError as e:
            error_msg = str(e)
            if 'email' in error_msg:  # cubre la constraint uniq_cliente_email_por_tenant
                return Response({'error': 'Email ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            elif 'rut' in error_msg:
                return Response({'error': 'RUT ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Datos duplicados'}, status=status.HTTP_400_BAD_REQUEST)

        # Buscar en PersonaNatural
        try:
            obj = scoped(PersonaNatural.objects.all(), request, cliente_field='pk').get(pk=pk)

            # Actualizar campos
            if 'is_active' in request.data:
                obj.is_active = request.data.get('is_active', obj.is_active)
            if 'email_principal' in request.data:
                obj.email_principal = request.data.get('email_principal', obj.email_principal).strip()
            if 'telefono_contacto' in request.data:
                obj.telefono_contacto = request.data.get('telefono_contacto', obj.telefono_contacto).strip() or None
            if 'nombre_completo' in request.data:
                obj.nombre_completo = request.data.get('nombre_completo', obj.nombre_completo).strip()
            if 'run' in request.data:
                obj.run = request.data.get('run', obj.run).strip()

            obj.save()

            contratos_count = Contrato.objects.filter(cliente_id=pk).count()
            statuses = set(Contrato.objects.filter(cliente_id=pk).values_list('status', flat=True))
            estado = _compute_estado(obj.is_active, pk, {pk: statuses})
            extra_ctx = {pk: {'contratos_count': contratos_count, 'contacto': None}}
            data = PersonaNaturalSerializer(obj, context={'extra': extra_ctx}).data
            data['estado'] = estado
            data['contratos_count'] = contratos_count
            return Response(data, status=status.HTTP_200_OK)
        except PersonaNatural.DoesNotExist:
            pass
        except IntegrityError as e:
            error_msg = str(e)
            if 'email' in error_msg:  # cubre la constraint uniq_cliente_email_por_tenant
                return Response({'error': 'Email ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            elif 'run' in error_msg:
                return Response({'error': 'RUN ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Datos duplicados'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

    def delete(self, request, pk):
        from legal.models import LogAceptacion
        if Contrato.objects.filter(cliente_id=pk).exists() or LogAceptacion.objects.filter(cliente_id=pk).exists():
            return Response({'error': 'No se puede eliminar el cliente porque tiene contratos u otros registros asociados.'}, status=status.HTTP_400_BAD_REQUEST)

        # Buscar en PersonaJuridica
        try:
            obj = scoped(PersonaJuridica.objects.all(), request, cliente_field='pk').get(pk=pk)
            obj.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except PersonaJuridica.DoesNotExist:
            pass
        except ProtectedError:
            return Response({'error': 'No se puede eliminar el cliente porque tiene contratos u otros registros asociados.'}, status=status.HTTP_400_BAD_REQUEST)

        # Buscar en PersonaNatural
        try:
            obj = scoped(PersonaNatural.objects.all(), request, cliente_field='pk').get(pk=pk)
            obj.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except PersonaNatural.DoesNotExist:
            pass
        except ProtectedError:
            return Response({'error': 'No se puede eliminar el cliente porque tiene contratos u otros registros asociados.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)


class ClienteStatsView(APIView):
    """
    GET /api/clientes/stats/
    Devuelve solo las estadísticas globales de forma liviana.
    """
    permission_classes = [(IsTenantMember & RequiresFeature('clientes')) | IsPlatformClienteAccess]

    def get(self, request):
        clientes_qs = scoped(Cliente.objects.all(), request, cliente_field='pk')
        contratos_qs = scoped(Contrato.objects.all(), request, cliente_field='cliente_id')
        total = clientes_qs.count()
        activos = clientes_qs.filter(is_active=True).count()
        inactivos = clientes_qs.filter(is_active=False).count()
        # "En revisión" = activos con contratos en MORA/GRACIA pero sin ACTIVO
        mora_ids = set(
            contratos_qs
            .filter(status__in=['MORA', 'GRACIA'])
            .values_list('cliente_id', flat=True)
            .distinct()
        )
        activo_ids = set(
            contratos_qs
            .filter(status='ACTIVO')
            .values_list('cliente_id', flat=True)
            .distinct()
        )
        en_revision = len(mora_ids - activo_ids)

        return Response({
            'total': total,
            'activos': activos - en_revision,
            'en_revision': en_revision,
            'inactivos': inactivos,
        })


# ─── Workspace de cliente ─────────────────────────────────────────────────────

def _es_usuario_cliente(request):
    from tenants.models import RolTenant
    return (request.user.tenant_id is not None
            and getattr(request.user, 'role', None) == RolTenant.CLIENTE)


class ClienteWorkspaceView(APIView):
    """
    GET /api/clientes/<id>/workspace/
    Payload agregado para la vista workspace: perfil completo, contratos,
    incidencias recientes, usuarios-cuenta vinculados (solo staff/tenant),
    membresía (plan del tenant) y feed de actividad.
    """
    permission_classes = [(IsTenantMember & RequiresFeature('clientes')) | IsPlatformClienteAccess]

    def get(self, request, pk):
        from django.contrib.auth import get_user_model
        from tenants.plans import plan_payload
        from notificaciones.models import Notificacion
        from .models import CorreoEnviado

        obj, tipo = _resolve_cliente_scoped(request, pk)
        if obj is None:
            return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        perfil = _serialize_cliente_detail(obj, tipo, pk)

        contratos = [
            {
                'id': c.id,
                'software': c.software.nombre,
                'categoria_producto': c.software.categoria,
                'tipo_contrato': c.tipo_contrato,
                'etapa': c.etapa,
                'status': c.status,
                'monto': str(c.monto),
                'frecuencia_facturacion': c.frecuencia_facturacion,
                'fecha_inicio': c.fecha_inicio,
                'fecha_vencimiento': c.fecha_vencimiento,
                'fin_periodo_gracia': c.fin_periodo_gracia,
            }
            for c in Contrato.objects.filter(cliente_id=pk).select_related('software').order_by('-fecha_inicio')
        ]

        incidencias = [
            {
                'id': i.id,
                'titulo': i.titulo,
                'severidad': i.severidad,
                'estado': i.estado,
                'fecha_creacion': i.fecha_creacion,
            }
            for i in obj.incidencias.order_by('-fecha_creacion')[:10]
        ]

        data = {
            'perfil': perfil,
            'tipo': tipo,
            'contratos': contratos,
            'incidencias': incidencias,
            'membresia': {
                **plan_payload(obj.tenant),
                'tenant': {
                    'razon_social': obj.tenant.razon_social,
                    'estado': obj.tenant.estado,
                    'categoria': obj.tenant.categoria,
                },
            },
        }

        # Los usuarios-cuenta son información de gestión: un usuario-cliente
        # no debe ver las cuentas de acceso de su propia empresa.
        if not _es_usuario_cliente(request):
            User = get_user_model()
            data['usuarios_cuenta'] = [
                {
                    'id': u.id,
                    'username': u.username,
                    'email': u.email,
                    'last_login': u.last_login,
                    'is_active': u.is_active,
                }
                for u in User.objects.filter(cliente_id=pk).order_by('username')
            ]

        # Feed de actividad: fusión de eventos recientes de distintas fuentes.
        actividad = [
            {'tipo': 'REGISTRO', 'fecha': obj.fecha_registro, 'detalle': 'Cliente registrado'},
        ]
        if obj.fecha_modificacion and obj.fecha_modificacion != obj.fecha_registro:
            actividad.append({'tipo': 'MODIFICACION', 'fecha': obj.fecha_modificacion, 'detalle': 'Ficha modificada'})
        for correo in CorreoEnviado.objects.filter(cliente_id=pk)[:5]:
            actividad.append({
                'tipo': 'CORREO', 'fecha': correo.fecha_envio,
                'detalle': f"Correo {'enviado' if correo.estado == 'ENVIADO' else 'fallido'}: {correo.asunto}",
            })
        for notif in Notificacion.objects.filter(cliente_id=pk)[:5]:
            actividad.append({
                'tipo': 'NOTIFICACION', 'fecha': notif.fecha_creacion,
                'detalle': f"Notificación [{notif.tipo}]: {notif.titulo}",
            })
        from contratos.models import HistorialEtapaContrato
        for h in HistorialEtapaContrato.objects.filter(contrato__cliente_id=pk).select_related('contrato')[:5]:
            actividad.append({
                'tipo': 'ETAPA_CONTRATO', 'fecha': h.fecha_cambio,
                'detalle': f"Contrato #{h.contrato_id}: {h.etapa_anterior or '—'} → {h.etapa_nueva}",
            })
        for i in obj.incidencias.order_by('-fecha_creacion')[:5]:
            actividad.append({
                'tipo': 'INCIDENCIA', 'fecha': i.fecha_creacion,
                'detalle': f"Incidencia #{i.id}: {i.titulo}",
            })
        actividad.sort(key=lambda e: e['fecha'], reverse=True)
        data['actividad'] = actividad[:20]

        return Response(data)


class ClienteTimelinePagosView(APIView):
    """
    GET /api/clientes/<id>/timeline-pagos/
    Timeline derivado de facturación (solo lectura): no hay modelo Pago, los
    eventos se construyen desde los contratos (inicio, vencimientos de cuota
    según frecuencia, cambios de etapa, gracia) y los perdonazos.
    """
    permission_classes = [(IsTenantMember & RequiresFeature('clientes')) | IsPlatformClienteAccess]

    def get(self, request, pk):
        from datetime import date
        from dateutil.relativedelta import relativedelta
        from contratos.models import HistorialEtapaContrato, RegistroPerdonazo

        obj, tipo = _resolve_cliente_scoped(request, pk)
        if obj is None:
            return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        hoy = date.today()
        eventos = []

        def _push(tipo_ev, fecha, contrato, detalle, monto=None):
            # fecha puede ser date o datetime; se normaliza para ordenar.
            fecha_orden = fecha.date() if hasattr(fecha, 'date') else fecha
            eventos.append({
                'tipo': tipo_ev,
                'fecha': fecha,
                '_orden': fecha_orden,
                'contrato_id': contrato.id,
                'contrato_nombre': contrato.software.nombre,
                'monto': str(monto) if monto is not None else None,
                'detalle': detalle,
            })

        contratos = list(Contrato.objects.filter(cliente_id=pk).select_related('software'))
        en_mora = 0
        proximo_vencimiento = None

        for c in contratos:
            _push('INICIO_CONTRATO', c.fecha_inicio, c, 'Inicio del contrato', c.monto)

            # Serie de vencimientos de cuota para contratos recurrentes
            if c.frecuencia_facturacion in ('MENSUAL', 'ANUAL'):
                paso = relativedelta(months=1) if c.frecuencia_facturacion == 'MENSUAL' else relativedelta(years=1)
                limite = min(hoy, c.fecha_vencimiento) if c.fecha_vencimiento else hoy
                cuota = c.fecha_inicio + paso
                n = 0
                while cuota <= limite and n < 120:  # tope defensivo: 10 años de cuotas mensuales
                    _push('VENCIMIENTO_CUOTA', cuota, c,
                          f"Cuota {c.get_frecuencia_facturacion_display().lower()}", c.monto)
                    cuota += paso
                    n += 1
                # Próxima cuota futura del cliente (para el resumen)
                if c.status not in ('VENCIDO', 'SUSPENDIDO') and (c.fecha_vencimiento is None or cuota <= c.fecha_vencimiento):
                    if proximo_vencimiento is None or cuota < proximo_vencimiento:
                        proximo_vencimiento = cuota

            if c.fecha_vencimiento and c.fecha_vencimiento <= hoy:
                _push('VENCIMIENTO_CONTRATO', c.fecha_vencimiento, c, 'Fin de vigencia del contrato')

            if c.status in ('MORA', 'GRACIA', 'SUSPENDIDO'):
                en_mora += 1
                detalle = f"Contrato en {c.get_status_display().lower()}"
                if c.status == 'GRACIA' and c.fin_periodo_gracia:
                    detalle += f" (gracia hasta {c.fin_periodo_gracia.strftime('%d/%m/%Y')})"
                _push('ESTADO_COBRANZA', c.fin_periodo_gracia or hoy, c, detalle)

        for h in HistorialEtapaContrato.objects.filter(contrato__cliente_id=pk).select_related('contrato__software'):
            _push('CAMBIO_ETAPA', h.fecha_cambio, h.contrato,
                  f"Etapa: {h.etapa_anterior or '—'} → {h.etapa_nueva}")

        for p in RegistroPerdonazo.objects.filter(contrato__cliente_id=pk).select_related('contrato__software'):
            _push('PERDONAZO', p.fecha_concesion, p.contrato,
                  f"Perdonazo: +{p.dias_extendidos} días (vencía {p.fecha_vencimiento_anterior.strftime('%d/%m/%Y')}). {p.motivo}")

        eventos.sort(key=lambda e: e['_orden'], reverse=True)
        for e in eventos:
            del e['_orden']

        return Response({
            'eventos': eventos,
            'resumen': {
                'total_contratos': len(contratos),
                'en_mora': en_mora,
                'proximo_vencimiento': proximo_vencimiento,
            },
        })


class ClienteCorreosView(APIView):
    """
    GET  /api/clientes/<id>/correos/        → historial de correos (últimos 50)
    POST /api/clientes/<id>/enviar-correo/  → envía correo y registra intento
    (el POST vive en ClienteEnviarCorreoView; esta clase solo lista).
    """
    permission_classes = [(IsTenantMember & RequiresFeature('clientes')) | IsPlatformClienteAccess]

    def get(self, request, pk):
        from .models import CorreoEnviado

        obj, tipo = _resolve_cliente_scoped(request, pk)
        if obj is None:
            return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        correos = [
            {
                'id': c.id,
                'destinatario': c.destinatario,
                'asunto': c.asunto,
                'cuerpo': c.cuerpo,
                'estado': c.estado,
                'error': c.error,
                'enviado_por': c.enviado_por.username if c.enviado_por else None,
                'fecha_envio': c.fecha_envio,
            }
            for c in CorreoEnviado.objects.filter(cliente_id=pk).select_related('enviado_por')[:50]
        ]
        return Response({'results': correos})


class ClienteEnviarCorreoView(APIView):
    """
    POST /api/clientes/<id>/enviar-correo/
    Body: {asunto, cuerpo, destinatario?} — destinatario default email_principal.
    Registra el intento en CorreoEnviado aunque el envío falle.
    """
    permission_classes = [(IsTenantMember & RequiresFeature('clientes')) | IsPlatformClienteAccess]

    def post(self, request, pk):
        from .models import CorreoEnviado, EstadoCorreo
        from .utils import enviar_correo_cliente

        # IsTenantMember no distingue al rol CLIENTE en escrituras: un
        # usuario-cliente no puede enviarse correos desde el workspace.
        if _es_usuario_cliente(request):
            return Response({'error': 'No tienes permiso para enviar correos.'},
                            status=status.HTTP_403_FORBIDDEN)

        obj, tipo = _resolve_cliente_scoped(request, pk)
        if obj is None:
            return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        asunto = (request.data.get('asunto') or '').strip()
        cuerpo = (request.data.get('cuerpo') or '').strip()
        destinatario = (request.data.get('destinatario') or '').strip() or obj.email_principal
        if not asunto or not cuerpo:
            return Response({'error': 'Asunto y cuerpo son requeridos'}, status=status.HTTP_400_BAD_REQUEST)

        registro = CorreoEnviado(
            tenant=obj.tenant,
            cliente_id=pk,
            destinatario=destinatario,
            asunto=asunto,
            cuerpo=cuerpo,
            enviado_por=request.user,
        )
        try:
            enviar_correo_cliente(obj, asunto, cuerpo, destinatario)
            registro.estado = EstadoCorreo.ENVIADO
            registro.save()
        except Exception as e:
            registro.estado = EstadoCorreo.FALLIDO
            registro.error = str(e)
            registro.save()
            return Response({
                'error': f'No se pudo enviar el correo: {e}',
                'registro_id': registro.id,
            }, status=status.HTTP_502_BAD_GATEWAY)

        return Response({
            'id': registro.id,
            'destinatario': destinatario,
            'asunto': asunto,
            'estado': registro.estado,
            'fecha_envio': registro.fecha_envio,
        }, status=status.HTTP_201_CREATED)
