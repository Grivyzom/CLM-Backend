"""
Servicios de importación: archivos → Django models.
Cada función recibe un objeto file-like y devuelve un resumen de lo procesado.
"""
import io
from datetime import datetime

import pandas as pd
import pdfplumber
from docx import Document
from pptx import Presentation


# ─── PARSERS (archivo → datos crudos) ────────────────────────────────────────

def extraer_texto_pdf(archivo):
    """Extrae texto de PDF. Devuelve str."""
    texto = []
    with pdfplumber.open(archivo) as pdf:
        for pagina in pdf.pages:
            t = pagina.extract_text()
            if t:
                texto.append(t)
    return "\n".join(texto)


def extraer_tablas_pdf(archivo):
    """Extrae tablas de PDF como lista de DataFrames."""
    tablas = []
    with pdfplumber.open(archivo) as pdf:
        for pagina in pdf.pages:
            for tabla in pagina.extract_tables():
                if tabla:
                    df = pd.DataFrame(tabla[1:], columns=tabla[0])
                    tablas.append(df)
    return tablas


def extraer_texto_word(archivo):
    """Extrae texto de Word (.docx). Devuelve str."""
    doc = Document(archivo)
    parrafos = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(parrafos)


def extraer_tablas_word(archivo):
    """Extrae tablas de Word como lista de DataFrames."""
    doc = Document(archivo)
    tablas = []
    for tabla in doc.tables:
        filas = [[celda.text.strip() for celda in fila.cells] for fila in tabla.rows]
        if filas:
            df = pd.DataFrame(filas[1:], columns=filas[0])
            tablas.append(df)
    return tablas


def extraer_texto_pptx(archivo):
    """Extrae texto de PowerPoint (.pptx). Devuelve dict {slide_num: texto}."""
    prs = Presentation(archivo)
    slides = {}
    for i, slide in enumerate(prs.slides, start=1):
        textos = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                textos.append(shape.text.strip())
        slides[i] = "\n".join(textos)
    return slides


# ─── INGESTORES (datos → modelos Django) ─────────────────────────────────────

def excel_a_clientes(archivo):
    """
    Ingesta clientes desde Excel.

    Columnas esperadas para Persona Natural:
        tipo | run | nombre_completo | email | telefono

    Columnas esperadas para Persona Jurídica:
        tipo | rut | razon_social | giro | email | telefono

    'tipo' debe ser 'natural' o 'juridica'.
    Devuelve dict con listas: creados, actualizados, errores.
    """
    from clientes.models import Cliente, PersonaNatural, PersonaJuridica

    df = pd.read_excel(archivo, dtype=str).fillna("")
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    creados, actualizados, errores = [], [], []

    for idx, fila in df.iterrows():
        tipo = fila.get("tipo", "").strip().lower()
        email = fila.get("email", "").strip()
        telefono = fila.get("telefono", "").strip() or None
        num_fila = idx + 2  # 1-indexed + encabezado

        if not email:
            errores.append({"fila": num_fila, "error": "email vacío"})
            continue

        try:
            if tipo == "natural":
                run = fila.get("run", "").strip()
                nombre = fila.get("nombre_completo", "").strip()
                if not run or not nombre:
                    errores.append({"fila": num_fila, "error": "run o nombre_completo vacío"})
                    continue

                cliente_base, creado = Cliente.objects.update_or_create(
                    email_principal=email,
                    defaults={"telefono_contacto": telefono, "is_active": True},
                )
                PersonaNatural.objects.update_or_create(
                    cliente_ptr=cliente_base,
                    defaults={"run": run, "nombre_completo": nombre},
                )

            elif tipo == "juridica":
                rut = fila.get("rut", "").strip()
                razon = fila.get("razon_social", "").strip()
                giro = fila.get("giro", "").strip()
                if not rut or not razon:
                    errores.append({"fila": num_fila, "error": "rut o razon_social vacío"})
                    continue

                cliente_base, creado = Cliente.objects.update_or_create(
                    email_principal=email,
                    defaults={"telefono_contacto": telefono, "is_active": True},
                )
                PersonaJuridica.objects.update_or_create(
                    cliente_ptr=cliente_base,
                    defaults={"rut": rut, "razon_social": razon, "giro": giro},
                )
            else:
                errores.append({"fila": num_fila, "error": f"tipo desconocido: '{tipo}'"})
                continue

            if creado:
                creados.append(email)
            else:
                actualizados.append(email)

        except Exception as e:
            errores.append({"fila": num_fila, "error": str(e)})

    return {"creados": creados, "actualizados": actualizados, "errores": errores}


def excel_a_contratos(archivo):
    """
    Ingesta contratos desde Excel.

    Columnas esperadas:
        cliente_email | software_nombre | sla_nombre | tipo_contrato |
        monto | fecha_inicio | fecha_vencimiento | dias_gracia

    Devuelve dict con listas: creados, errores.
    """
    from clientes.models import Cliente
    from catalogo.models import Software
    from contratos.models import SLA, Contrato, TipoContrato

    df = pd.read_excel(archivo, dtype=str).fillna("")
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    creados, errores = [], []

    for idx, fila in df.iterrows():
        num_fila = idx + 2
        try:
            cliente = Cliente.objects.get(email_principal=fila["cliente_email"].strip())
            software = Software.objects.get(nombre=fila["software_nombre"].strip())
            sla = SLA.objects.get(nombre=fila["sla_nombre"].strip())

            tipo_raw = fila.get("tipo_contrato", "").strip().upper()
            tipos_validos = {t.value: t for t in TipoContrato}
            if tipo_raw not in tipos_validos:
                errores.append({"fila": num_fila, "error": f"tipo_contrato inválido: '{tipo_raw}'"})
                continue

            fecha_inicio = pd.to_datetime(fila["fecha_inicio"]).date()
            fecha_venc_raw = fila.get("fecha_vencimiento", "").strip()
            fecha_venc = pd.to_datetime(fecha_venc_raw).date() if fecha_venc_raw else None
            monto = float(fila.get("monto", 0) or 0)
            dias_gracia = int(fila.get("dias_gracia", 0) or 0)

            contrato = Contrato.objects.create(
                cliente=cliente,
                software=software,
                sla=sla,
                tipo_contrato=tipo_raw,
                monto=monto,
                fecha_inicio=fecha_inicio,
                fecha_vencimiento=fecha_venc,
                dias_gracia_autorizados=dias_gracia,
            )
            creados.append(contrato.id)

        except Cliente.DoesNotExist:
            errores.append({"fila": num_fila, "error": f"cliente no encontrado: {fila.get('cliente_email')}"})
        except Software.DoesNotExist:
            errores.append({"fila": num_fila, "error": f"software no encontrado: {fila.get('software_nombre')}"})
        except SLA.DoesNotExist:
            errores.append({"fila": num_fila, "error": f"SLA no encontrado: {fila.get('sla_nombre')}"})
        except Exception as e:
            errores.append({"fila": num_fila, "error": str(e)})

    return {"creados": creados, "errores": errores}
