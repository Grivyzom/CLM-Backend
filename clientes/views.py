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


def get_filtered_clientes_unified(query_params):
    """
    Aplica los filtros (search, estado, tipo, fecha_desde, fecha_hasta) leídos de
    query_params (dict-like: QueryDict de Django o request.query_params de DRF) y
    devuelve la lista unificada de clientes (PersonaJuridica + PersonaNatural)
    ordenada por fecha_registro desc, SIN paginar.

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

    # ── Querysets base ───────────────────────────────────────────────────
    pj_qs = PersonaJuridica.objects.all()
    pn_qs = PersonaNatural.objects.all()

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
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Crea PersonaNatural o PersonaJuridica."""
        tipo = request.data.get('tipo', '').strip().lower()
        if tipo not in ('natural', 'juridica'):
            return Response({'error': 'tipo debe ser "natural" o "juridica"'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if tipo == 'natural':
                cliente = PersonaNatural.objects.create(
                    email_principal=request.data.get('email_principal', '').strip(),
                    telefono_contacto=request.data.get('telefono_contacto', '').strip() or None,
                    run=request.data.get('run', '').strip(),
                    nombre_completo=request.data.get('nombre_completo', '').strip(),
                )
                extra_ctx = {cliente.id: {'contratos_count': 0, 'contacto': None}}
                data = PersonaNaturalSerializer(cliente, context={'extra': extra_ctx}).data
            else:  # juridica
                cliente = PersonaJuridica.objects.create(
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

            return Response(data, status=status.HTTP_201_CREATED)
        except IntegrityError as e:
            error_msg = str(e)
            if 'email_principal' in error_msg:
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
        unified = get_filtered_clientes_unified(request.query_params)

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
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        # Buscar en PersonaJuridica
        try:
            obj = PersonaJuridica.objects.get(pk=pk)
            contratos_count = Contrato.objects.filter(cliente_id=pk).count()
            statuses = set(Contrato.objects.filter(cliente_id=pk).values_list('status', flat=True))
            estado = _compute_estado(obj.is_active, pk, {pk: statuses})
            contactos = list(ContactoRepresentante.objects.filter(cliente_juridico_id=pk))
            contacto_data = {'nombre': contactos[0].nombre, 'telefono': contactos[0].telefono or '', 'cargo': contactos[0].cargo, 'email': contactos[0].email} if contactos else None
            extra_ctx = {pk: {'contratos_count': contratos_count, 'contacto': contacto_data}}
            data = PersonaJuridicaSerializer(obj, context={'extra': extra_ctx}).data
            data['estado'] = estado
            data['contratos_count'] = contratos_count
            data['contactos'] = [{'nombre': c.nombre, 'cargo': c.cargo, 'email': c.email, 'telefono': c.telefono or ''} for c in contactos]
            data['contratos_activos'] = _get_contratos_activos(pk)
            return Response(data)
        except PersonaJuridica.DoesNotExist:
            pass

        # Buscar en PersonaNatural
        try:
            obj = PersonaNatural.objects.get(pk=pk)
            contratos_count = Contrato.objects.filter(cliente_id=pk).count()
            statuses = set(Contrato.objects.filter(cliente_id=pk).values_list('status', flat=True))
            estado = _compute_estado(obj.is_active, pk, {pk: statuses})
            extra_ctx = {pk: {'contratos_count': contratos_count, 'contacto': None}}
            data = PersonaNaturalSerializer(obj, context={'extra': extra_ctx}).data
            data['estado'] = estado
            data['contratos_count'] = contratos_count
            data['contratos_activos'] = _get_contratos_activos(pk)
            return Response(data)
        except PersonaNatural.DoesNotExist:
            pass

        return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, pk):
        # Buscar en PersonaJuridica
        try:
            obj = PersonaJuridica.objects.get(pk=pk)

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
            if 'email_principal' in error_msg:
                return Response({'error': 'Email ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            elif 'rut' in error_msg:
                return Response({'error': 'RUT ya está registrado'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Datos duplicados'}, status=status.HTTP_400_BAD_REQUEST)

        # Buscar en PersonaNatural
        try:
            obj = PersonaNatural.objects.get(pk=pk)

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
            if 'email_principal' in error_msg:
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
            obj = PersonaJuridica.objects.get(pk=pk)
            obj.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except PersonaJuridica.DoesNotExist:
            pass
        except ProtectedError:
            return Response({'error': 'No se puede eliminar el cliente porque tiene contratos u otros registros asociados.'}, status=status.HTTP_400_BAD_REQUEST)

        # Buscar en PersonaNatural
        try:
            obj = PersonaNatural.objects.get(pk=pk)
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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        total = Cliente.objects.count()
        activos = Cliente.objects.filter(is_active=True).count()
        inactivos = Cliente.objects.filter(is_active=False).count()
        # "En revisión" = activos con contratos en MORA/GRACIA pero sin ACTIVO
        mora_ids = set(
            Contrato.objects
            .filter(status__in=['MORA', 'GRACIA'])
            .values_list('cliente_id', flat=True)
            .distinct()
        )
        activo_ids = set(
            Contrato.objects
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
