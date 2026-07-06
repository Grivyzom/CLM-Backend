from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db import IntegrityError

from .models import Producto, Software


def _software_a_dict(s):
    return {
        'id': s.id,
        'nombre': s.nombre,
        'slug': s.slug,
    }


class SoftwareListView(APIView):
    """GET /api/catalogo/software/ — catálogo de productos de software (para selects)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Software.objects.all().order_by('nombre')
        return Response([_software_a_dict(s) for s in qs])


def _producto_a_dict(p):
    return {
        'id': p.id,
        'sku': p.sku,
        'name': p.nombre,
        'desc': p.descripcion or '',
        'cat': p.categoria,
        'price': str(p.precio),
        'currency': p.moneda,
        'unit': p.unidad,
        'status': p.estado,
        'tipo_licencia': p.tipo_licencia,
        'datos_adicionales': p.datos_adicionales or {},
    }


class ProductoListCreateView(APIView):
    """
    GET  /api/catalogo/productos/?search=&categoria=
    POST /api/catalogo/productos/  { name, desc, cat, price, currency, unit, status, tipo_licencia, datos_adicionales }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Producto.objects.all()
        search = request.GET.get('search')
        categoria = request.GET.get('categoria')
        if search:
            qs = qs.filter(nombre__icontains=search) | qs.filter(sku__icontains=search)
        if categoria and categoria != 'Todos':
            qs = qs.filter(categoria=categoria)
        return Response([_producto_a_dict(p) for p in qs])

    def post(self, request):
        import datetime
        import random
        import string

        data = request.data
        nombre = (data.get('name') or '').strip()
        if not nombre:
            raise DRFValidationError({'name': 'Este campo es requerido.'})

        # Auto-generate a unique SKU: CAT-YYYYMMDD-XXX
        date_str = datetime.date.today().strftime('%Y%m%d')
        while True:
            suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
            sku = f"CAT-{date_str}-{suffix}"
            if not Producto.objects.filter(sku=sku).exists():
                break

        try:
            producto = Producto.objects.create(
                sku=sku,
                nombre=nombre,
                descripcion=data.get('desc', ''),
                categoria=data.get('cat', 'Software'),
                tipo_licencia=data.get('tipo_licencia', 'Comercial'),
                precio=data.get('price') or 0,
                moneda=data.get('currency', 'USD'),
                unidad=data.get('unit', ''),
                estado=data.get('status', 'Activo'),
                datos_adicionales=data.get('datos_adicionales', {}),
            )
        except IntegrityError:
            raise DRFValidationError({'sku': 'Ya existe un producto con ese SKU.'})
        return Response(_producto_a_dict(producto), status=201)


class ProductoDetailView(APIView):
    """
    PATCH  /api/catalogo/productos/<pk>/
    DELETE /api/catalogo/productos/<pk>/
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        producto = get_object_or_404(Producto, pk=pk)
        data = request.data
        for campo_api, campo_modelo in [
            ('sku', 'sku'), ('name', 'nombre'), ('desc', 'descripcion'),
            ('cat', 'categoria'), ('price', 'precio'), ('currency', 'moneda'),
            ('unit', 'unidad'), ('status', 'estado'),
            ('tipo_licencia', 'tipo_licencia'), ('datos_adicionales', 'datos_adicionales'),
        ]:
            if campo_api in data:
                setattr(producto, campo_modelo, data[campo_api])
        producto.save()
        return Response(_producto_a_dict(producto))

    def delete(self, request, pk):
        producto = get_object_or_404(Producto, pk=pk)
        producto.delete()
        return Response(status=204)
