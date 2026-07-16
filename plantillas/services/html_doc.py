"""Plantillas HTML de documentos (formato .dc.html de Claude Design).

Flujo: el equipo deja archivos .html en settings.DOCS_TEMPLATE_DIR
(clm_frontend/public/docs_template/). Esos archivos traen scaffolding de
preview de navegador (<x-dc>, <x-import>, doc-page.js) que ningún motor de
PDF server-side puede ejecutar. Este módulo:

1. extrae el contenido real del documento (los estilos van inline, no se pierden),
2. lo envuelve en una página imprimible con CSS @page (tamaño, márgenes,
   footer repetido en cada página, numeración real de páginas),
3. genera el PDF con WeasyPrint, que sí respeta flexbox/grid/@page
   (LibreOffice, usado para .docx, los ignora).

Los HTML "planos" (sin scaffolding dc) pasan sin modificación.
"""
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.conf import settings


class PlantillaHTMLNoEncontrada(Exception):
    """La ruta indicada no existe en DOCS_TEMPLATE_DIR ni en los template dirs de Django."""


# ---------------------------------------------------------------------------
# Carga segura de archivos
# ---------------------------------------------------------------------------

def _bases_permitidas():
    """Directorios desde los que se permite leer plantillas HTML (y sus assets)."""
    bases = [Path(settings.DOCS_TEMPLATE_DIR)]
    for cfg in settings.TEMPLATES:
        bases.extend(Path(d) for d in cfg.get('DIRS', []))
    return [b.resolve() for b in bases]


def _resolver_ruta_segura(ruta_relativa: str) -> Path:
    """Resuelve la ruta dentro de las bases permitidas, bloqueando path traversal."""
    for base in _bases_permitidas():
        candidata = (base / ruta_relativa).resolve()
        if candidata.is_file() and candidata.is_relative_to(base):
            return candidata
    raise PlantillaHTMLNoEncontrada(
        f"No existe la plantilla HTML '{ruta_relativa}' en docs_template ni en templates/."
    )


def cargar_plantilla_html(ruta_relativa: str) -> str:
    """Lee el HTML crudo de la plantilla y, si es formato dc, lo adapta a imprimible."""
    texto = _resolver_ruta_segura(ruta_relativa).read_text(encoding='utf-8')
    return adaptar_dc_a_imprimible(texto)


def listar_plantillas_docs():
    """Nombres de archivo .html disponibles en DOCS_TEMPLATE_DIR (para ofrecerlos
    como templates en el CLM)."""
    base = Path(settings.DOCS_TEMPLATE_DIR)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.glob('*.html'))


# ---------------------------------------------------------------------------
# Nomenclatura: TIPO__Nombre.dc.html
# ---------------------------------------------------------------------------

SEPARADOR_TIPO = '__'


def parsear_nombre_plantilla(nombre_archivo: str):
    """Extrae (tipo_contrato, nombre_legible) de la nomenclatura de archivo.

    Convención: `TIPO__Nombre libre.dc.html`, donde TIPO es un valor de
    TipoContrato (RECURRENTE, PERPETUO, PRO_BONO, INTERNO). Sin prefijo el
    template es global: sirve para cualquier tipo de contrato/documento.

    Ejemplos:
      'INTERNO__Memorandum Grivyzom.dc.html' -> ('INTERNO', 'Memorandum Grivyzom')
      'Memorandum Grivyzom.dc.html'          -> (None, 'Memorandum Grivyzom')
    """
    from contratos.models import TipoContrato

    base = nombre_archivo
    for sufijo in ('.dc.html', '.html'):
        if base.endswith(sufijo):
            base = base[:-len(sufijo)]
            break

    if SEPARADOR_TIPO in base:
        prefijo, resto = base.split(SEPARADOR_TIPO, 1)
        if prefijo.upper() in TipoContrato.values and resto.strip():
            return prefijo.upper(), resto.strip()
    return None, base.strip()


def listar_plantillas_docs_info():
    """Plantillas de DOCS_TEMPLATE_DIR con su metadata de nomenclatura:
    [{'ruta': archivo, 'nombre': legible, 'tipo': 'INTERNO'|None}, ...]
    tipo=None significa plantilla global (válida para cualquier tipo)."""
    resultado = []
    for archivo in listar_plantillas_docs():
        tipo, nombre = parsear_nombre_plantilla(archivo)
        resultado.append({'ruta': archivo, 'nombre': nombre, 'tipo': tipo})
    return resultado


# ---------------------------------------------------------------------------
# Campos manuales de una plantilla HTML
# ---------------------------------------------------------------------------

# Variables que llegan del contrato (construir_contexto) o que inyecta el motor;
# todo lo demás que aparezca como {{ variable }} en la plantilla se considera
# campo manual que el usuario llena al generar el documento.
VARIABLES_RESERVADAS = {
    'cliente', 'software', 'sla', 'contrato', 'obligaciones',
    'terminos_legales', 'clausula_anexo', 'fecha_generacion', 'fecha_documento',
    'referencia',
}

_RE_VARIABLE = re.compile(r'\{\{\s*([a-zA-Z_]\w*)\s*(?:\|\s*default:"([^"]*)")?[^}]*\}\}')

_PREFIJOS_MULTILINEA = ('parrafo', 'cuerpo', 'texto', 'descripcion', 'detalle')


def extraer_campos_manuales(ruta_relativa: str):
    """Descubre los campos que la plantilla espera del usuario, escaneando las
    variables {{ nombre }} del HTML crudo y excluyendo las reservadas del
    contrato. Devuelve [{'nombre', 'label', 'default', 'multilinea'}] en el
    orden de aparición en el documento."""
    texto = _resolver_ruta_segura(ruta_relativa).read_text(encoding='utf-8')

    campos, vistos = [], set()
    for match in _RE_VARIABLE.finditer(texto):
        nombre, default = match.group(1), match.group(2) or ''
        if nombre in VARIABLES_RESERVADAS or nombre in vistos:
            continue
        vistos.add(nombre)
        campos.append({
            'nombre': nombre,
            'label': nombre.replace('_', ' ').capitalize(),
            'default': default,
            'multilinea': nombre.lower().startswith(_PREFIJOS_MULTILINEA),
        })
    return campos


_MESES_ES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
             'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']


def fecha_larga_es(fecha=None) -> str:
    """'14 de julio de 2026' — sin depender del locale del sistema."""
    import datetime
    fecha = fecha or datetime.date.today()
    return f"{fecha.day} de {_MESES_ES[fecha.month - 1]} de {fecha.year}"


# ---------------------------------------------------------------------------
# Adaptación dc -> HTML imprimible
# ---------------------------------------------------------------------------

_RE_NUM_PAGINA = re.compile(r'P[áa]gina\s+\d+\s+de\s+\d+')

_TAMANOS_PAGINA = {'letter': 'letter', 'a4': 'A4', 'legal': 'legal'}


def es_formato_dc(html: str) -> bool:
    return '<x-dc' in html or '<x-import' in html


def adaptar_dc_a_imprimible(html: str) -> str:
    """Convierte un .dc.html exportado de Claude Design en HTML imprimible.

    - <x-import size margin> -> regla CSS @page equivalente.
    - <div slot="footer"> -> elemento running() repetido al pie de cada página.
    - Texto "Página N de M" -> contadores CSS reales (counter(page)/counter(pages)).
    - Estilos de <helmet> se conservan; scripts de preview se descartan.
    - Calibri -> Carlito (sustituto métrico disponible en el servidor).

    HTML sin scaffolding dc se devuelve tal cual (solo con el reemplazo de fuente).
    """
    if not es_formato_dc(html):
        return html.replace('Calibri', 'Carlito')

    soup = BeautifulSoup(html, 'html.parser')

    ximport = soup.find('x-import')
    tamano = _TAMANOS_PAGINA.get((ximport.get('size') or 'letter').lower(), 'letter') if ximport else 'letter'
    margen = (ximport.get('margin') or '1in') if ximport else '1in'

    # Estilos propios del documento definidos en <helmet>.
    estilos_helmet = ''
    for style in soup.select('helmet style'):
        estilos_helmet += style.decode_contents() + '\n'

    contenedor = ximport if ximport is not None else (soup.find('x-dc') or soup.body or soup)

    footer_html = ''
    footer = contenedor.find(attrs={'slot': 'footer'})
    if footer is not None:
        footer.extract()
        del footer['slot']
        footer['class'] = (footer.get('class') or []) + ['dc-footer']
        footer_html = str(footer)

    cuerpo_html = contenedor.decode_contents()

    # Numeración real de páginas: el texto estático "Página 1 de 1" del template
    # se reemplaza por contadores CSS que WeasyPrint evalúa página a página.
    reemplazo_num = '<span class="dc-num-pagina"></span>'
    footer_html = _RE_NUM_PAGINA.sub(reemplazo_num, footer_html)
    cuerpo_html = _RE_NUM_PAGINA.sub(reemplazo_num, cuerpo_html)

    imprimible = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: {tamano};
    margin: {margen};
    @bottom-center {{ content: element(dc-footer); }}
  }}
  .dc-footer {{ position: running(dc-footer); width: 100%; }}
  .dc-num-pagina::before {{ content: "Página " counter(page) " de " counter(pages); }}
  body {{ margin: 0; }}
{estilos_helmet}</style>
</head>
<body>
{footer_html}
{cuerpo_html}
</body>
</html>"""
    return imprimible.replace('Calibri', 'Carlito')


# ---------------------------------------------------------------------------
# HTML -> PDF con WeasyPrint
# ---------------------------------------------------------------------------

def _fetcher_local_seguro(url: str):
    """url_fetcher restringido: solo archivos locales dentro de las bases
    permitidas (assets de las plantillas). Bloquea http/https y cualquier
    otro esquema para evitar SSRF desde una plantilla."""
    from weasyprint import default_url_fetcher

    if url.startswith('data:'):
        return default_url_fetcher(url)
    if not url.startswith('file://'):
        raise ValueError(f"Recurso externo bloqueado en plantilla PDF: {url}")

    from urllib.parse import unquote, urlparse
    ruta = Path(unquote(urlparse(url).path)).resolve()
    if not any(ruta.is_relative_to(base) for base in _bases_permitidas()):
        raise ValueError(f"Recurso fuera de los directorios de plantillas: {ruta}")
    return default_url_fetcher(url)


def html_a_pdf(html: str) -> bytes:
    """Genera el PDF del HTML (ya adaptado/renderizado) con WeasyPrint.
    base_url apunta a DOCS_TEMPLATE_DIR para que rutas relativas tipo
    assets/logo.png resuelvan a los assets que acompañan a las plantillas."""
    from weasyprint import HTML

    base_url = str(Path(settings.DOCS_TEMPLATE_DIR)) + os.sep
    return HTML(
        string=html,
        base_url=base_url,
        url_fetcher=_fetcher_local_seguro,
    ).write_pdf()


# ---------------------------------------------------------------------------
# Correlativo de "Referencia" (REF: NDA-2026-004, etc.)
# ---------------------------------------------------------------------------

PREFIJO_REFERENCIA_DEFECTO = 'DOC'


def siguiente_referencia(tenant, prefijo: str = None) -> str:
    """Asigna y consume el siguiente número del correlativo PREFIJO-AÑO-NNN
    para el tenant dado. Un contador por (tenant, prefijo, año) — se resetea
    cada año calendario. select_for_update evita colisiones si dos documentos
    del mismo tipo se generan al mismo tiempo."""
    import datetime
    from django.db import transaction
    from ..models import SecuenciaReferencia

    prefijo = (prefijo or PREFIJO_REFERENCIA_DEFECTO).strip().upper() or PREFIJO_REFERENCIA_DEFECTO
    anio = datetime.date.today().year

    with transaction.atomic():
        secuencia, _ = SecuenciaReferencia.objects.select_for_update().get_or_create(
            tenant=tenant, prefijo=prefijo, anio=anio,
        )
        secuencia.ultimo_numero += 1
        secuencia.save(update_fields=['ultimo_numero'])

    return f"{prefijo}-{anio}-{secuencia.ultimo_numero:03d}"
