from plantillas.models import PlantillaDocumento, PreguntaFormulario, OpcionRespuesta, ReglaInclusionClausula
from plantillas.models import Clausula, VersionClausula
from tenants.models import Tenant

tenant = Tenant.objects.first()
if not tenant:
    print("No tenant found.")
else:
    plantilla = PlantillaDocumento.objects.filter(modo_origen='clausulas', tenant=tenant).first()
    if not plantilla:
        print("No plantilla 'clausulas' found. Please create one first.")
    else:
        print(f"Adding dynamic form to {plantilla.nombre}...")
        
        c1, _ = Clausula.objects.get_or_create(tenant=tenant, nombre="Cláusula Internacional", defaults={'categoria': 'Jurisdicción'})
        v1, _ = VersionClausula.objects.get_or_create(clausula=c1, etiqueta="Estándar", defaults={'texto': 'Este contrato se rige por las leyes internacionales.'})
        
        c2, _ = Clausula.objects.get_or_create(tenant=tenant, nombre="Cláusula de Alto Valor", defaults={'categoria': 'Finanzas'})
        v2, _ = VersionClausula.objects.get_or_create(clausula=c2, etiqueta="Estándar", defaults={'texto': 'Debido al alto valor, se requiere seguro especial.'})

        plantilla.preguntas.all().delete()
        
        q1 = PreguntaFormulario.objects.create(
            plantilla=plantilla,
            texto="¿El contrato es internacional?",
            tipo="booleano",
            orden=1
        )
        ReglaInclusionClausula.objects.create(
            plantilla=plantilla,
            pregunta=q1,
            respuesta_booleana=True,
            clausula_version=v1
        )
        
        q2 = PreguntaFormulario.objects.create(
            plantilla=plantilla,
            texto="¿Cuál es el rango del monto?",
            tipo="opcion_multiple",
            orden=2
        )
        o1 = OpcionRespuesta.objects.create(pregunta=q2, texto="Menor a $10,000")
        o2 = OpcionRespuesta.objects.create(pregunta=q2, texto="Mayor a $10,000")
        
        ReglaInclusionClausula.objects.create(
            plantilla=plantilla,
            pregunta=q2,
            opcion_respuesta=o2,
            clausula_version=v2
        )

        print("Dynamic form created successfully.")
