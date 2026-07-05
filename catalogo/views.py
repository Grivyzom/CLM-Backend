from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db import IntegrityError

from .models import Producto


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
    }


class ProductoListCreateView(APIView):
    """
    GET  /api/catalogo/productos/?search=&categoria=
    POST /api/catalogo/productos/  { sku, name, desc, cat, price, currency, unit, status }
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
        data = request.data
        sku = (data.get('sku') or '').strip()
        nombre = (data.get('name') or '').strip()
        if not sku:
            raise DRFValidationError({'sku': 'Este campo es requerido.'})
        if not nombre:
            raise DRFValidationError({'name': 'Este campo es requerido.'})
        try:
            producto = Producto.objects.create(
                sku=sku,
                nombre=nombre,
                descripcion=data.get('desc', ''),
                categoria=data.get('cat', 'Software'),
                precio=data.get('price') or 0,
                moneda=data.get('currency', 'USD'),
                unidad=data.get('unit', ''),
                estado=data.get('status', 'Activo'),
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
        ]:
            if campo_api in data:
                setattr(producto, campo_modelo, data[campo_api])
        producto.save()
        return Response(_producto_a_dict(producto))

    def delete(self, request, pk):
        producto = get_object_or_404(Producto, pk=pk)
        producto.delete()
        return Response(status=204)
