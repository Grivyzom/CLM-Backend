"""Servidor MCP del CLM — consultas y creación/edición de contratos vía Claude.

Proceso independiente de Django (no corre dentro de runserver). Reutiliza el ORM
y el aislamiento multi-tenant existente: cada tool filtra con tenants.scoping.scoped(),
actuando como el usuario indicado en MCP_USERNAME. Así un tenant nunca ve datos
de otro, igual que en las views DRF.

Las tools de escritura (crear_contrato, actualizar_contrato) no duplican lógica:
invocan las views DRF reales con APIRequestFactory + force_authenticate, así
aplican las mismas validaciones, permisos, cuotas de plan e historial que la UI.

Uso local (stdio, para Claude Code / Claude Desktop):

    MCP_USERNAME=<usuario> python mcp_server.py

Registro en Claude Code:

    claude mcp add clm --env MCP_USERNAME=<usuario> -- python /grivyzom/webs/CLM/core/mcp_server.py
"""

import os
import sys
from types import SimpleNamespace

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django

django.setup()

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from mcp.server.fastmcp import FastMCP

from tenants.scoping import scoped

mcp = FastMCP("CLM")

_MAX_LIMITE = 100


def _request():
    """Shim con la única superficie que scoped() consulta: request.user.

    El usuario se resuelve en cada llamada (no al arrancar) para que cambios de
    rol/tenant o desactivación apliquen sin reiniciar el servidor.
    """
    username = os.environ.get("MCP_USERNAME")
    if not username:
        raise RuntimeError("Define MCP_USERNAME con el usuario que actúa en este servidor MCP.")
    User = get_user_model()
    try:
        user = User.objects.get(username=username, is_active=True)
    except User.DoesNotExist:
        raise RuntimeError(f"Usuario '{username}' inexistente o inactivo.")
    return SimpleNamespace(user=user)


def _call_view(view_cls, method, path, data=None, **url_kwargs):
    """Invoca una view DRF real como el usuario de MCP_USERNAME.

    Devuelve el body de la respuesta; si la view respondió >=400 se envuelve en
    {'error': ..., 'http_status': ...} para que el cliente MCP lo distinga sin
    que la tool reviente con una excepción opaca.
    """
    from rest_framework.test import APIRequestFactory, force_authenticate

    factory = APIRequestFactory()
    request = getattr(factory, method)(path, data or {}, format="json")
    force_authenticate(request, user=_request().user)
    response = view_cls.as_view()(request, **url_kwargs)
    if hasattr(response, "render"):
        response.render()
    body = getattr(response, "data", None)
    if response.status_code >= 400:
        return {"error": body, "http_status": response.status_code}
    return body


def _nombre_cliente(cliente):
    try:
        return cliente.personanatural.nombre_completo
    except Exception:
        pass
    try:
        return cliente.personajuridica.razon_social
    except Exception:
        return f"Cliente #{cliente.pk}"


def _contrato_dict(c):
    return {
        "id": c.pk,
        "nombre": c.nombre,
        "cliente_id": c.cliente_id,
        "cliente": _nombre_cliente(c.cliente),
        "etapa": c.etapa,
        "status": c.status,
        "tipo_contrato": c.tipo_contrato,
        "monto": str(c.monto),
        "frecuencia_facturacion": c.frecuencia_facturacion,
        "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
        "fecha_vencimiento": c.fecha_vencimiento.isoformat() if c.fecha_vencimiento else None,
        "firma_status": c.firma_status,
        "version": c.version,
    }


def _buscar_contratos(
    etapa: str = "",
    status: str = "",
    cliente_id: int = 0,
    limite: int = 20,
) -> list[dict]:
    """Lista contratos del CLM visibles para el usuario configurado.

    etapa: BORRADOR, REVISION, APROBADO, PENDIENTE_FIRMA, ACTIVO, ENMENDADO, TERMINADO.
    status (cobranza): ACTIVO, MORA, GRACIA, SUSPENDIDO, VENCIDO.
    cliente_id: restringe a un cliente puntual. Vacío/0 = sin filtro.
    """
    from contratos.models import Contrato

    qs = scoped(Contrato.objects.select_related("cliente"), _request(),
                cliente_field="cliente_id")
    if etapa:
        qs = qs.filter(etapa=etapa.upper())
    if status:
        qs = qs.filter(status=status.upper())
    if cliente_id:
        qs = qs.filter(cliente_id=cliente_id)
    limite = max(1, min(limite, _MAX_LIMITE))
    return [_contrato_dict(c) for c in qs.order_by("-fecha_creacion")[:limite]]


def _detalle_contrato(contrato_id: int) -> dict:
    """Detalle de un contrato por ID, incluyendo SLA y datos de firma."""
    from contratos.models import Contrato

    qs = scoped(Contrato.objects.select_related("cliente", "sla", "software"),
                _request(), cliente_field="cliente_id")
    try:
        c = qs.get(pk=contrato_id)
    except Contrato.DoesNotExist:
        return {"error": f"Contrato {contrato_id} no existe o no es visible para este usuario."}
    d = _contrato_dict(c)
    d.update({
        "software": str(c.software),
        "sla": str(c.sla),
        "dias_gracia_autorizados": c.dias_gracia_autorizados,
        "fin_periodo_gracia": c.fin_periodo_gracia.isoformat() if c.fin_periodo_gracia else None,
        "firma_proveedor": c.firma_proveedor,
        "firma_fecha_firma": c.firma_fecha_firma.isoformat() if c.firma_fecha_firma else None,
        "external_editor": c.external_editor,
        "external_sync_status": c.external_sync_status,
    })
    return d


def _buscar_clientes(texto: str = "", solo_activos: bool = True, limite: int = 20) -> list[dict]:
    """Lista clientes (personas naturales y jurídicas) visibles para el usuario.

    texto: busca en nombre completo, razón social, RUN/RUT y email principal.
    """
    from django.db.models import Q

    from clientes.models import Cliente

    qs = scoped(Cliente.objects.all(), _request(), cliente_field="pk")
    if solo_activos:
        qs = qs.filter(is_active=True)
    if texto:
        qs = qs.filter(
            Q(personanatural__nombre_completo__icontains=texto)
            | Q(personanatural__run__icontains=texto)
            | Q(personajuridica__razon_social__icontains=texto)
            | Q(personajuridica__rut__icontains=texto)
            | Q(email_principal__icontains=texto)
        )
    limite = max(1, min(limite, _MAX_LIMITE))
    return [
        {
            "id": c.pk,
            "nombre": _nombre_cliente(c),
            "email_principal": c.email_principal,
            "is_active": c.is_active,
        }
        for c in qs.order_by("pk")[:limite]
    ]


def _resumen_cartera() -> dict:
    """Conteos de contratos por etapa y por status de cobranza, y total de clientes."""
    from django.db.models import Count

    from clientes.models import Cliente
    from contratos.models import Contrato

    req = _request()
    contratos = scoped(Contrato.objects.all(), req, cliente_field="cliente_id")
    clientes = scoped(Cliente.objects.all(), req, cliente_field="pk")
    return {
        "total_contratos": contratos.count(),
        "por_etapa": {r["etapa"]: r["n"] for r in contratos.values("etapa").annotate(n=Count("id"))},
        "por_status": {r["status"]: r["n"] for r in contratos.values("status").annotate(n=Count("id"))},
        "total_clientes": clientes.count(),
    }


@mcp.tool(description=_buscar_contratos.__doc__)
async def buscar_contratos(etapa: str = "", status: str = "", cliente_id: int = 0, limite: int = 20) -> list[dict]:
    return await sync_to_async(_buscar_contratos)(etapa, status, cliente_id, limite)


@mcp.tool(description=_detalle_contrato.__doc__)
async def detalle_contrato(contrato_id: int) -> dict:
    return await sync_to_async(_detalle_contrato)(contrato_id)


@mcp.tool(description=_buscar_clientes.__doc__)
async def buscar_clientes(texto: str = "", solo_activos: bool = True, limite: int = 20) -> list[dict]:
    return await sync_to_async(_buscar_clientes)(texto, solo_activos, limite)


def _indice_textos_legales(tipo: str = "") -> dict:
    """Índice de la biblioteca de textos legales del tenant, agrupado por tipo.

    Tipos: CLAUSULA, SALUDO, INTRODUCCION, DESPEDIDA, CIERRE, FIRMA, OTRO.
    tipo: restringe a un tipo puntual; vacío = todos. No trae los cuerpos
    completos: usar version_id/clausula_id para insertarlos en un contrato.
    """
    from plantillas.models import Clausula, TipoTextoClausula

    qs = scoped(Clausula.objects.all(), _request()).filter(activa=True)
    tipo = tipo.upper().strip()
    if tipo:
        if tipo not in TipoTextoClausula.values:
            return {"error": f"Tipo '{tipo}' inválido. Usa uno de: "
                             f"{', '.join(TipoTextoClausula.values)}."}
        qs = qs.filter(tipo_texto=tipo)
    labels = dict(TipoTextoClausula.choices)
    grupos: dict[str, list] = {}
    filas = qs.values("id", "nombre", "categoria", "riesgo", "tipo_texto").order_by(
        "tipo_texto", "categoria", "nombre")
    for f in filas:
        grupos.setdefault(f["tipo_texto"], []).append({
            "id": f["id"], "nombre": f["nombre"],
            "categoria": f["categoria"], "riesgo": f["riesgo"],
        })
    return {
        "total": sum(len(v) for v in grupos.values()),
        "tipos": [
            {"tipo": t, "label": labels.get(t, t), "items": items}
            for t, items in grupos.items()
        ],
    }


def _sugerir_estructura_contrato(contrato_id: int) -> dict:
    """Sugiere qué textos le faltan a un contrato para iniciarlo y finalizarlo
    de manera profesional (saludo/introducción al comienzo; despedida, cierre
    legal o firmas al final), con candidatas concretas de la biblioteca listas
    para insertar (clausula_id + version_id)."""
    from contratos.models import Contrato
    from plantillas.services.sugerencias import sugerir_estructura

    qs = scoped(Contrato.objects.all(), _request(), cliente_field="cliente_id")
    try:
        contrato = qs.get(pk=contrato_id)
    except Contrato.DoesNotExist:
        return {"error": f"Contrato {contrato_id} no existe o no es visible para este usuario."}
    return sugerir_estructura(contrato)


@mcp.tool(description=_resumen_cartera.__doc__)
async def resumen_cartera() -> dict:
    return await sync_to_async(_resumen_cartera)()


@mcp.tool(description=_indice_textos_legales.__doc__)
async def indice_textos_legales(tipo: str = "") -> dict:
    return await sync_to_async(_indice_textos_legales)(tipo)


@mcp.tool(description=_sugerir_estructura_contrato.__doc__)
async def sugerir_estructura_contrato(contrato_id: int) -> dict:
    return await sync_to_async(_sugerir_estructura_contrato)(contrato_id)


def _catalogo_para_contrato() -> dict:
    """Productos (software) y SLAs disponibles para crear un contrato.

    Devuelve los IDs que crear_contrato necesita como software_id y sla_id,
    limitados al tenant del usuario configurado."""
    from catalogo.models import Producto
    from contratos.models import SLA

    req = _request()
    productos = scoped(Producto.objects.all(), req).filter(estado="Activo")
    slas = scoped(SLA.objects.all(), req)
    return {
        "productos": [
            {"id": p.pk, "sku": p.sku, "nombre": p.nombre, "categoria": p.categoria,
             "precio": str(p.precio), "moneda": p.moneda}
            for p in productos.order_by("nombre")[:_MAX_LIMITE]
        ],
        "slas": [
            {"id": s.pk, "nombre": str(s)}
            for s in slas.order_by("pk")[:_MAX_LIMITE]
        ],
    }


_DESC_CLAUSULAS = (
    "clausulas: lista opcional de bloques del editor de cláusulas. Cada bloque es "
    "{titulo: str, texto: str, nivel?: 0|1|2}. nivel controla la numeración "
    "jerárquica (0 → '1.', 1 → '1.1', 2 → 'a)'). El texto plano del contrato se "
    "deriva automáticamente de los bloques. Para reusar textos de la biblioteca "
    "(ver indice_textos_legales) añade clausula_id/version_id al bloque."
)


def _crear_contrato(
    cliente_id: int,
    software_id: int,
    sla_id: int,
    tipo_contrato: str,
    monto: str,
    fecha_inicio: str,
    nombre: str = "",
    fecha_vencimiento: str = "",
    frecuencia_facturacion: str = "",
    dias_gracia_autorizados: int = 0,
    clausulas: list[dict] | None = None,
    tenant_id: int = 0,
) -> dict:
    from contratos.views import ContratoListCreateView

    data = {
        "cliente_id": cliente_id,
        "software_id": software_id,
        "sla_id": sla_id,
        "tipo_contrato": tipo_contrato.upper().strip(),
        "monto": monto,
        "fecha_inicio": fecha_inicio,
        "dias_gracia_autorizados": dias_gracia_autorizados,
    }
    if nombre:
        data["nombre"] = nombre
    if fecha_vencimiento:
        data["fecha_vencimiento"] = fecha_vencimiento
    if frecuencia_facturacion:
        data["frecuencia_facturacion"] = frecuencia_facturacion.upper().strip()
    if clausulas:
        data["clausulas_estructuradas"] = clausulas
    if tenant_id:
        data["tenant_id"] = tenant_id
    return _call_view(ContratoListCreateView, "post", "/api/contratos/", data)


def _actualizar_contrato(
    contrato_id: int,
    nombre: str | None = None,
    etapa: str | None = None,
    notas: str = "",
    monto: str | None = None,
    sla_id: int | None = None,
    fecha_inicio: str | None = None,
    fecha_vencimiento: str | None = None,
    frecuencia_facturacion: str | None = None,
    dias_gracia_autorizados: int | None = None,
    clausulas: list[dict] | None = None,
    texto_adicional_clausulas: str | None = None,
) -> dict:
    from contratos.views import ContratoDetailView

    data = {}
    for clave, valor in (
        ("nombre", nombre),
        ("monto", monto),
        ("sla_id", sla_id),
        ("fecha_inicio", fecha_inicio),
        ("fecha_vencimiento", fecha_vencimiento),
        ("frecuencia_facturacion", frecuencia_facturacion),
        ("dias_gracia_autorizados", dias_gracia_autorizados),
        ("clausulas_estructuradas", clausulas),
        ("texto_adicional_clausulas", texto_adicional_clausulas),
    ):
        if valor is not None:
            data[clave] = valor
    if etapa:
        data["etapa"] = etapa.upper().strip()
        if notas:
            data["notas"] = notas
    if not data:
        return {"error": "Nada que actualizar: indica al menos un campo."}
    return _call_view(ContratoDetailView, "patch", f"/api/contratos/{contrato_id}/",
                      data, pk=contrato_id)


@mcp.tool(description=_catalogo_para_contrato.__doc__)
async def catalogo_para_contrato() -> dict:
    return await sync_to_async(_catalogo_para_contrato)()


@mcp.tool(description=(
    "Crea un contrato nuevo en etapa BORRADOR, con las mismas validaciones, "
    "permisos y cuotas de plan que la UI del CLM (siembra obligaciones SLA e "
    "historial). tipo_contrato: RECURRENTE, PERPETUO, PRO_BONO, INTERNO, "
    "REQUERIMIENTO, ERS. frecuencia_facturacion (MENSUAL/ANUAL) es requerida si "
    "tipo_contrato=RECURRENTE. monto: decimal como string. Fechas YYYY-MM-DD. "
    "Usa buscar_clientes y catalogo_para_contrato para obtener los IDs. "
    "tenant_id solo aplica si el usuario es superadmin de plataforma. "
    + _DESC_CLAUSULAS
))
async def crear_contrato(
    cliente_id: int,
    software_id: int,
    sla_id: int,
    tipo_contrato: str,
    monto: str,
    fecha_inicio: str,
    nombre: str = "",
    fecha_vencimiento: str = "",
    frecuencia_facturacion: str = "",
    dias_gracia_autorizados: int = 0,
    clausulas: list[dict] | None = None,
    tenant_id: int = 0,
) -> dict:
    return await sync_to_async(_crear_contrato)(
        cliente_id, software_id, sla_id, tipo_contrato, monto, fecha_inicio,
        nombre, fecha_vencimiento, frecuencia_facturacion,
        dias_gracia_autorizados, clausulas, tenant_id,
    )


@mcp.tool(description=(
    "Actualiza un contrato existente: datos comerciales (nombre, monto, sla_id, "
    "fechas, frecuencia_facturacion, dias_gracia_autorizados), cláusulas, o "
    "transición de etapa (BORRADOR, REVISION, APROBADO, PENDIENTE_FIRMA, ACTIVO, "
    "ENMENDADO, TERMINADO; notas opcionales para el historial). Solo se tocan "
    "los campos enviados. Ojo: enviar texto_adicional_clausulas sin clausulas "
    "descarta los bloques estructurados previos (el texto plano manda). "
    + _DESC_CLAUSULAS
))
async def actualizar_contrato(
    contrato_id: int,
    nombre: str | None = None,
    etapa: str | None = None,
    notas: str = "",
    monto: str | None = None,
    sla_id: int | None = None,
    fecha_inicio: str | None = None,
    fecha_vencimiento: str | None = None,
    frecuencia_facturacion: str | None = None,
    dias_gracia_autorizados: int | None = None,
    clausulas: list[dict] | None = None,
    texto_adicional_clausulas: str | None = None,
) -> dict:
    return await sync_to_async(_actualizar_contrato)(
        contrato_id, nombre, etapa, notas, monto, sla_id, fecha_inicio,
        fecha_vencimiento, frecuencia_facturacion, dias_gracia_autorizados,
        clausulas, texto_adicional_clausulas,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
