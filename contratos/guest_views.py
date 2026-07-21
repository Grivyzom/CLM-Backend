import uuid
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status
from django.shortcuts import get_object_or_404
from .models import Contrato, GuestLink, ComentarioContrato, TipoComentario
from tenants.permissions import IsTenantMember

class GenerateGuestLinkView(APIView):
    permission_classes = [IsTenantMember]

    def post(self, request, pk):
        contrato = get_object_or_404(Contrato, pk=pk, tenant=request.tenant)
        can_comment = request.data.get('can_comment', True)
        can_sign = request.data.get('can_sign', False)
        
        # Invalidate previous valid links if any (optional, but good practice)
        GuestLink.objects.filter(contrato=contrato, fecha_expiracion__isnull=True).update(fecha_expiracion=timezone.now())
        GuestLink.objects.filter(contrato=contrato, fecha_expiracion__gt=timezone.now()).update(fecha_expiracion=timezone.now())

        import secrets
        token = secrets.token_urlsafe(32)
        
        guest_link = GuestLink.objects.create(
            contrato=contrato,
            token=token,
            can_comment=can_comment,
            can_sign=can_sign
        )
        
        return Response({
            'token': token,
            'can_comment': can_comment,
            'can_sign': can_sign
        })

class GuestContractView(APIView):
    permission_classes = [] # Public access via token
    
    def get(self, request, token):
        guest_link = get_object_or_404(GuestLink, token=token)
        if not guest_link.is_valid():
            return Response({'error': 'Enlace expirado o inválido.'}, status=http_status.HTTP_403_FORBIDDEN)
            
        contrato = guest_link.contrato
        data = {
            'id': contrato.id,
            'nombre': contrato.nombre,
            'cliente': contrato.cliente.nombre,
            'software': contrato.software.nombre,
            'fecha_inicio': contrato.fecha_inicio,
            'fecha_vencimiento': contrato.fecha_vencimiento,
            'etapa': contrato.etapa,
            'monto': contrato.monto,
            'texto_adicional_clausulas': contrato.texto_adicional_clausulas,
            'clausulas_estructuradas': contrato.clausulas_estructuradas,
            'can_comment': guest_link.can_comment,
            'can_sign': guest_link.can_sign,
            'comentarios': []
        }
        
        for c in contrato.comentarios.all().order_by('-fecha_creacion'):
            data['comentarios'].append({
                'id': c.id,
                'texto': c.texto,
                'tipo': c.tipo,
                'fecha_creacion': c.fecha_creacion,
                'autor': c.usuario.get_full_name() if c.usuario else (c.guest_name or 'Invitado')
            })
            
        return Response(data)

class GuestCommentView(APIView):
    permission_classes = []
    
    def post(self, request, token):
        guest_link = get_object_or_404(GuestLink, token=token)
        if not guest_link.is_valid() or not guest_link.can_comment:
            return Response({'error': 'Acción no permitida.'}, status=http_status.HTTP_403_FORBIDDEN)
            
        texto = request.data.get('texto', '').strip()
        guest_name = request.data.get('guest_name', 'Invitado')
        tipo = request.data.get('tipo', TipoComentario.SUGERENCIA)
        
        if not texto:
            return Response({'error': 'El texto es obligatorio.'}, status=http_status.HTTP_400_BAD_REQUEST)
            
        comentario = ComentarioContrato.objects.create(
            contrato=guest_link.contrato,
            texto=texto,
            guest_name=guest_name,
            tipo=tipo
        )
        
        return Response({
            'id': comentario.id,
            'texto': comentario.texto,
            'tipo': comentario.tipo,
            'fecha_creacion': comentario.fecha_creacion,
            'autor': comentario.guest_name
        }, status=http_status.HTTP_201_CREATED)

class GuestSignView(APIView):
    permission_classes = []
    
    def post(self, request, token):
        guest_link = get_object_or_404(GuestLink, token=token)
        if not guest_link.is_valid() or not guest_link.can_sign:
            return Response({'error': 'Acción no permitida.'}, status=http_status.HTTP_403_FORBIDDEN)
            
        contrato = guest_link.contrato
        
        # Lógica simple de firma, asumiendo que el invitado acepta.
        # Podríamos usar la misma infraestructura de OTP o solo marcarlo firmado.
        # Por simplicidad para el MVP:
        
        guest_name = request.data.get('guest_name', 'Invitado')
        
        if contrato.firma_status == 'SIGNED':
            return Response({'error': 'El contrato ya está firmado.'}, status=http_status.HTTP_400_BAD_REQUEST)
            
        contrato.firma_status = 'SIGNED'
        contrato.firma_fecha_firma = timezone.now()
        # Transicionar a activo si estaba pendiente de firma
        if contrato.etapa == 'PENDIENTE_FIRMA':
            contrato.transicionar_etapa('ACTIVO', notas=f'Firmado a través del Guest Portal por {guest_name}')
        else:
            contrato.save(update_fields=['firma_status', 'firma_fecha_firma'])
            
        # Opcionalmente invalidar el token
        guest_link.fecha_expiracion = timezone.now()
        guest_link.save()
        
        return Response({'status': 'ok', 'message': 'Contrato firmado exitosamente.'})
