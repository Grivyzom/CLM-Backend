from django.db import models
from clientes.models import PersonaNatural, PersonaJuridica
from documentos.models import RequisitoDocumental

def obtener_documentos_necesarios(tenant_id, cliente=None, producto=None):
    """
    Retorna una lista de diccionarios con información de los documentos requeridos
    basado en el tipo de cliente y el tipo/categoría de producto.
    """
    qs = RequisitoDocumental.objects.filter(tenant_id=tenant_id).select_related('tipo_documento')
    
    # 1. Filtrar por Tipo de Cliente
    if isinstance(cliente, PersonaNatural):
        qs = qs.filter(models.Q(tipo_cliente='NATURAL') | models.Q(tipo_cliente='TODOS'))
    elif isinstance(cliente, PersonaJuridica):
        qs = qs.filter(models.Q(tipo_cliente='JURIDICA') | models.Q(tipo_cliente='TODOS'))
    else:
        # Si no se provee cliente, asumimos que puede aplicar cualquiera. O solo traemos TODOS.
        pass
    
    # 2. Filtrar por Producto
    if producto:
        qs = qs.filter(
            models.Q(producto_especifico=producto) |
            models.Q(producto_especifico__isnull=True, categoria_producto=producto.categoria) |
            models.Q(producto_especifico__isnull=True, categoria_producto__isnull=True)
        )
    else:
        # Si no hay producto específico, filtramos los que aplican en general
        qs = qs.filter(producto_especifico__isnull=True, categoria_producto__isnull=True)
        
    # Obtener documentos únicos priorizando obligatorios si hay solapamientos de reglas
    resultados = {}
    for req in qs:
        doc_id = req.tipo_documento.id
        if doc_id not in resultados or req.es_obligatorio:
            resultados[doc_id] = {
                'tipo_documento': req.tipo_documento,
                'es_obligatorio': req.es_obligatorio
            }
            
    return list(resultados.values())
