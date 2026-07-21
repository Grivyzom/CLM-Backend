"""Sugerencias de estructura documental para un contrato.

Analiza los bloques de cláusulas de un contrato y detecta si le faltan los
textos de apoyo que dan inicio y cierre profesional al documento (saludo o
introducción al comienzo; despedida, cierre legal o bloque de firmas al final).
Para cada faltante propone candidatas concretas de la biblioteca del tenant,
listas para insertar (clausula_id + version_id).

Consumido por el servidor MCP (tool `sugerir_estructura_contrato`) y reutilizable
desde cualquier view. Solo lectura: nunca modifica el contrato.
"""

import re

from plantillas.models import Clausula, TipoTextoClausula

# Tipos que abren y cierran un documento profesional.
TIPOS_APERTURA = (TipoTextoClausula.SALUDO, TipoTextoClausula.INTRODUCCION)
TIPOS_CIERRE = (TipoTextoClausula.DESPEDIDA, TipoTextoClausula.CIERRE,
                TipoTextoClausula.FIRMA)

# Heurística para bloques personalizados (sin clausula_id): si el título o el
# arranque del texto contiene estas señales, se considera que ese rol ya está
# cubierto aunque el bloque no venga de la biblioteca.
_RE_APERTURA = re.compile(
    r'\b(saludo|estimad[oa]s?|de\s+nuestra\s+consideraci[oó]n|presente|'
    r'comparec|pre[aá]mbulo|introducci[oó]n|antecedentes)\b', re.IGNORECASE)
_RE_CIERRE = re.compile(
    r'\b(atentamente|cordialmente|se\s+despide|despedida|sin\s+otro\s+particular|'
    r'en\s+comprobante|en\s+se[ñn]al\s+de\s+conformidad|firman?|firmas?)\b',
    re.IGNORECASE)

_MAX_CANDIDATAS = 3
_LARGO_EXTRACTO = 180
# Cuántos bloques desde cada extremo se inspeccionan al buscar apertura/cierre.
_VENTANA_BORDES = 2


def _extracto(texto):
    texto = ' '.join((texto or '').split())
    if len(texto) <= _LARGO_EXTRACTO:
        return texto
    return texto[:_LARGO_EXTRACTO].rsplit(' ', 1)[0] + '…'


def _rol_bloque(bloque, tipos_por_id):
    """Rol estructural de un bloque: 'apertura', 'cierre' o None."""
    tipo = tipos_por_id.get(bloque.get('clausula_id'))
    if tipo in TIPOS_APERTURA:
        return 'apertura'
    if tipo in TIPOS_CIERRE:
        return 'cierre'
    if tipo is not None and tipo != TipoTextoClausula.OTRO:
        return None
    # Personalizada (o tipo OTRO): heurística por texto.
    muestra = f"{bloque.get('titulo') or ''} {(bloque.get('texto') or '')[:300]}"
    if _RE_APERTURA.search(muestra):
        return 'apertura'
    if _RE_CIERRE.search(muestra):
        return 'cierre'
    return None


def _candidatas(tenant_id, tipos):
    """Hasta _MAX_CANDIDATAS textos activos de la biblioteca para esos tipos,
    con su versión estándar lista para insertar."""
    qs = (
        Clausula.objects.filter(tenant_id=tenant_id, activa=True, tipo_texto__in=tipos)
        .prefetch_related('versiones')
        .order_by('tipo_texto', 'categoria', 'nombre')[:_MAX_CANDIDATAS]
    )
    out = []
    for c in qs:
        versiones = [v for v in c.versiones.all() if v.activa]
        std = next((v for v in versiones if v.tipo == 'Estándar'), None) or (
            versiones[0] if versiones else None)
        if not std:
            continue
        out.append({
            'clausula_id': c.id,
            'nombre': c.nombre,
            'categoria': c.categoria,
            'tipo_texto': c.tipo_texto,
            'riesgo': c.riesgo,
            'version_id': std.id,
            'extracto': _extracto(std.texto),
        })
    return out


def sugerir_estructura(contrato):
    """Diagnóstico de apertura/cierre del contrato + candidatas de la biblioteca.

    Devuelve un dict serializable:
    {
      'contrato_id', 'total_bloques',
      'apertura_presente': bool, 'cierre_presente': bool,
      'sugerencias': [{'posicion': 'inicio'|'final', 'tipos': [...],
                       'motivo', 'candidatas': [...]}],
      'nota': str opcional (p.ej. contrato sin bloques estructurados),
    }
    """
    bloques = contrato.clausulas_estructuradas or []
    resultado = {
        'contrato_id': contrato.pk,
        'total_bloques': len(bloques),
        'apertura_presente': False,
        'cierre_presente': False,
        'sugerencias': [],
    }
    if not bloques:
        resultado['nota'] = (
            'El contrato no tiene cláusulas estructuradas todavía; se sugiere '
            'partir con un saludo o introducción, el cuerpo de cláusulas y un '
            'cierre con bloque de firmas.'
        )
        if contrato.texto_adicional_clausulas:
            resultado['nota'] += (
                ' Existe texto legado sin estructurar: conviene convertirlo a '
                'bloques desde el editor para poder analizarlo.'
            )

    ids = [b.get('clausula_id') for b in bloques if b.get('clausula_id')]
    tipos_por_id = dict(
        Clausula.objects.filter(pk__in=ids).values_list('id', 'tipo_texto')
    ) if ids else {}

    inicio = bloques[:_VENTANA_BORDES]
    final = bloques[-_VENTANA_BORDES:]
    resultado['apertura_presente'] = any(
        _rol_bloque(b, tipos_por_id) == 'apertura' for b in inicio)
    resultado['cierre_presente'] = any(
        _rol_bloque(b, tipos_por_id) == 'cierre' for b in final)

    if not resultado['apertura_presente']:
        resultado['sugerencias'].append({
            'posicion': 'inicio',
            'tipos': [str(t) for t in TIPOS_APERTURA],
            'motivo': ('El documento no comienza con un saludo o introducción: '
                       'agregar uno le da un inicio profesional.'),
            'candidatas': _candidatas(contrato.tenant_id, TIPOS_APERTURA),
        })
    if not resultado['cierre_presente']:
        resultado['sugerencias'].append({
            'posicion': 'final',
            'tipos': [str(t) for t in TIPOS_CIERRE],
            'motivo': ('El documento no termina con despedida, cierre legal o '
                       'bloque de firmas: agregar uno lo finaliza formalmente.'),
            'candidatas': _candidatas(contrato.tenant_id, TIPOS_CIERRE),
        })
    return resultado
