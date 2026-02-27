import streamlit as st
import pandas as pd
import sqlite3
import gspread
import re
import os
import time
import threading
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials
import base64
from io import StringIO
from supabase import create_client, Client

# Configuración
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
CREDENTIALS_FILE = "ProductoTerminado.json"

# Cache extremo para máxima velocidad
@st.cache_resource
def get_google_client():
    # Intento 1: Usar archivo local (Prioridad en local para evitar errores de JWT)
    if os.path.exists(CREDENTIALS_FILE):
        try:
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPE)
            client = gspread.authorize(creds)
            return client
        except Exception as e:
            st.error(f"❌ Error cargando archivo local {CREDENTIALS_FILE}: {e}")
    
    # Intento 2: Usar st.secrets (Para Streamlit Cloud)
    try:
        if "gcp_service_account" in st.secrets:
            creds_dict = dict(st.secrets["gcp_service_account"])
            # Asegurar que los saltos de línea se procesen correctamente
            if "private_key" in creds_dict:
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPE)
            client = gspread.authorize(creds)
            return client
    except Exception as e:
        st.error(f"❌ Error con secrets: {e}")
    
    st.error(f"❌ No se econtraron credenciales válidas")
    st.stop()

# Inicializar cliente de Supabase
@st.cache_resource
def get_supabase_client():
    """Inicializa y cachea el cliente de Supabase"""
    try:
        supabase_url = "https://lunlhlpoeuxrgnmlecfj.supabase.co"
        supabase_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imx1bmxobHBvZXV4cmdubWxlY2ZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzIwNDU5NTAsImV4cCI6MjA4NzYyMTk1MH0.iyDYMqrK-wlwGIpM-osSUpWtAlLx0COMb4CxS4rAbR4"
        supabase: Client = create_client(supabase_url, supabase_key)
        return supabase
    except Exception as e:
        st.error(f"❌ Error inicializando Supabase: {e}")
        return None

@st.cache_data(ttl=600)
def load_all_data(_client, sheet_id):
    start_time = time.time()
    
    spreadsheet = _client.open_by_key(sheet_id)
    sheet = spreadsheet.sheet1
    all_values = sheet.get_all_values()
    
    # Buscar encabezados
    header_row_index = 0
    target_headers = ['CAMION', 'PALLET INICIAL', 'PALLET FINAL', 'LISTO PARA ENTREGA']
    
    for i, row in enumerate(all_values[:10]):
        row_upper = [str(cell).upper().strip() for cell in row]
        found_headers = sum(1 for target in target_headers if any(target in cell for cell in row_upper))
        if found_headers >= 2:
            header_row_index = i
            break
    
    # Crear DataFrame con headers únicos
    headers = []
    header_count = {}
    for i, cell in enumerate(all_values[header_row_index]):
        header_str = str(cell).strip()
        if not header_str:
            header_str = f"Columna_{i+1}"
        
        if header_str in header_count:
            header_count[header_str] += 1
            header_str = f"{header_str}_{header_count[header_str]}"
        else:
            header_count[header_str] = 1
        
        headers.append(header_str)
    
    data = all_values[header_row_index + 1:]
    shipment_df = pd.DataFrame(data, columns=headers)
    
    # Mapear columnas
    column_mapping = {}
    for req_col in target_headers:
        for actual_col in shipment_df.columns:
            if req_col in actual_col.upper():
                column_mapping[req_col] = actual_col
                break
    
    for req_col, actual_col in column_mapping.items():
        if actual_col in shipment_df.columns:
            col_data = shipment_df[actual_col]
            
            if isinstance(col_data, pd.DataFrame):
                shipment_df[req_col] = col_data.iloc[:, 0]
            else:
                shipment_df[req_col] = col_data
    
    shipment_df = shipment_df[list(column_mapping.keys())].copy()
    
    for col in shipment_df.columns:
        shipment_df[col] = shipment_df[col].astype(str).str.strip()
    
    shipment_df = shipment_df[shipment_df['CAMION'] != ''].reset_index(drop=True)
    
    load_time = time.time() - start_time
    return shipment_df, header_row_index, sheet, load_time

@st.cache_data
def load_packing_data(uploaded_packing):
    packing_df = pd.read_excel(uploaded_packing, sheet_name='All number')
    
    # CORREGIDO: Reemplazar fillna(method='ffill') con ffill()
    packing_df['Box number'] = packing_df['Box number'].ffill()
    packing_df['Pallet number'] = packing_df['Pallet number'].ffill()
    packing_df['Pallet number'] = packing_df['Pallet number'].astype(str).str.strip()
    
    pallet_summary = packing_df.groupby('Pallet number').agg({
        'Serial number': ['first', 'last'],
        'Box number': 'count'
    }).reset_index()
    
    pallet_summary.columns = ['Pallet number', 'first_serial', 'last_serial', 'box_count']
    
    return packing_df, pallet_summary

# ==== NUEVAS FUNCIONES MEJORADAS PARA DETECCIÓN DE CAMIONES DISPONIBLES ====

def extraer_numero_pallet(codigo):
    """Extrae el número de pallet del código escaneado"""
    try:
        # Buscar patrones comunes en códigos de pallet
        # Ejemplo: "PALLET003", "PLT003", "003", "P003", etc.
        
        # Intentar extraer números al final del código
        match = re.search(r'(\d{2,3})$', codigo)
        if match:
            return int(match.group(1))
        
        # Intentar extraer números después de "PALLET", "PLT", "P", etc.
        match = re.search(r'(?:PALLET|PLT|P)[_-]?(\d{2,3})', codigo, re.IGNORECASE)
        if match:
            return int(match.group(1))
        
        # Si no se encuentra patrón, usar los últimos 2-3 dígitos
        if len(codigo) >= 2:
            ultimos_digitos = codigo[-3:] if codigo[-3:].isdigit() else codigo[-2:]
            if ultimos_digitos.isdigit():
                return int(ultimos_digitos)
                
        return None
    except:
        return None

def detectar_camiones_del_layout():
    """Detecta automáticamente los camiones disponibles en el layout SVG"""
    if not st.session_state.layout_locations:
        return []
    
    camiones = set()
    for location in st.session_state.layout_locations:
        match = re.match(r'C(\d+)-\d+', location)
        if match:
            camiones.add(int(match.group(1)))
    
    return sorted(camiones)

def detectar_camion_disponible(truck_packing_list):
    """Detecta el primer camión disponible basado en el layout y los camiones ya usados"""
    try:
        # Obtener camiones del layout
        camiones_layout = detectar_camiones_del_layout()
        if not camiones_layout:
            return None
        
        # 1. SI YA TIENE ESCANEOS PREVIOS EN LA DB:
        # Mantener el camión físico donde ya empezó a escanear
        conn = sqlite3.connect('scans.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ubicacion 
            FROM pallet_scans 
            WHERE camion = ? 
            AND ubicacion IS NOT NULL 
            LIMIT 1
        ''', (str(truck_packing_list),))
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            match = re.match(r'^(C\d+)-', row[0])
            if match:
                return match.group(1)

        # 2. SI ES NUEVO: Buscar el primer camión físico (C1, C2...) que esté libre
        # Un camión está libre si no tiene NINGÚN pallet de NINGÚN camión de packing list
        
        # Obtener todos los camiones físicos que tienen algún pallet asignado actualmente
        camiones_fisicos_ocupados = set()
        for loc, assignments in st.session_state.pallet_assignments.items():
            if assignments:
                match = re.match(r'^(C\d+)-', loc)
                if match:
                    camiones_fisicos_ocupados.add(match.group(1))
        
        for num_camion in camiones_layout:
            id_fisico = f"C{num_camion}"
            if id_fisico not in camiones_fisicos_ocupados:
                return id_fisico
        
        # 3. Todos los camiones físicos están ocupados → sin espacio disponible
        return None

    except Exception as e:
        print(f"Error detectando camión disponible: {e}")
        return None

def calcular_ubicacion_pallet(numero_pallet, camion):
    """Calcula la ubicación basada en el número de pallet y el camión"""
    try:
        # Cada ubicación contiene 2 pallets
        # Pallet 1 y 2 -> C1-1
        # Pallet 3 y 4 -> C1-2
        # Pallet 5 y 6 -> C1-3
        # etc.
        
        numero_ubicacion = ((numero_pallet - 1) // 2) + 1
        return f"{camion}-{numero_ubicacion}"
        
    except Exception as e:
        print(f"Error calculando ubicación: {e}")
        return f"{camion}-1"

def parse_svg_xml(xml_content):
    """Parsea un archivo SVG/XML con el layout del almacén"""
    try:
        root = ET.fromstring(xml_content)
        
        locations = []
        shapes_data = []
        
        # Buscar todos los elementos que representen ubicaciones
        namespace = '{http://www.w3.org/2000/svg}'
        
        # Rectángulos
        for rect in root.findall(f'.//{namespace}rect'):
            ubicacion = rect.get('id') or rect.get('data-ubicacion')
            if ubicacion and re.match(r'^C\d+-\d+$', ubicacion):
                locations.append(ubicacion)
                shapes_data.append({
                    'type': 'rect',
                    'ubicacion': ubicacion,
                    'x': float(rect.get('x', 0)),
                    'y': float(rect.get('y', 0)),
                    'width': float(rect.get('width', 0)),
                    'height': float(rect.get('height', 0)),
                    'fill': rect.get('fill', '#cccccc'),
                    'stroke': rect.get('stroke', '#000000')
                })
        
        # Polígonos
        for polygon in root.findall(f'.//{namespace}polygon'):
            ubicacion = polygon.get('id') or polygon.get('data-ubicacion')
            if ubicacion and re.match(r'^C\d+-\d+$', ubicacion):
                locations.append(ubicacion)
                points = polygon.get('points', '').split()
                shapes_data.append({
                    'type': 'polygon',
                    'ubicacion': ubicacion,
                    'points': points,
                    'fill': polygon.get('fill', '#cccccc'),
                    'stroke': polygon.get('stroke', '#000000')
                })
        
        # Textos (etiquetas)
        for text in root.findall(f'.//{namespace}text'):
            ubicacion = text.get('id') or text.get('data-ubicacion')
            text_content = text.text
            if ubicacion and re.match(r'^C\d+-\d+$', ubicacion):
                locations.append(ubicacion)
                shapes_data.append({
                    'type': 'text',
                    'ubicacion': ubicacion,
                    'x': float(text.get('x', 0)),
                    'y': float(text.get('y', 0)),
                    'content': text_content,
                    'fill': text.get('fill', '#000000')
                })
        
        return locations, shapes_data
    
    except Exception as e:
        st.error(f"Error parsing SVG/XML layout: {e}")
        return [], []

def generate_enhanced_svg_layout(shapes_data, pallet_assignments, selected_truck, truck_pallets, camion_asignado=None):
    """Genera SVG robusto con escalado forzado y compatibilidad total"""
    
    def get_color_for_loc(loc_id):
        """Colores globales. Amarillo = zona del camión con al menos 1 escaneo. Azul = ubicación con pallet."""
        assignments = pallet_assignments.get(loc_id, [])
        if not isinstance(assignments, list): assignments = [assignments]
        assignments = [a for a in assignments if a]

        # Prefijo de camión físico de esta ubicación: "C1-5" -> "C1"
        loc_prefix = loc_id.split('-')[0].upper()

        # ¿Hay algún escaneo en cualquier ubicación de este mismo camión físico?
        truck_has_any_scan = any(
            loc.split('-')[0].upper() == loc_prefix and pallet_assignments.get(loc)
            for loc in pallet_assignments
        )

        if not assignments:
            if truck_has_any_scan:
                # AMARILLO: Este camión físico tiene escaneos en otras ubicaciones
                return "#d97706", "#fbbf24"
            # VERDE: Zona sin ninguna actividad
            return "#16a34a", "#4ade80"

        # AZUL: Esta ubicación tiene pallet escaneado
        return "#2563eb", "#60a5fa"

    def build_tooltip(loc_id):
        assignments = pallet_assignments.get(loc_id, [])
        if not isinstance(assignments, list): assignments = [assignments]
        assignments = [a for a in assignments if a]
        
        if not assignments:
            return f"Ubicación: {loc_id}\nEstado: Libre"
        
        lines = [f"Ubicación: {loc_id}"]
        for a in assignments:
            p_num = a.get('pallet', 'N/A')
            slot = a.get('slot', '?')
            truck = a.get('camion', '?')
            lines.append(f"Slot {slot}: Pallet {p_num} (Camión {truck})")
        return "\n".join(lines)

    # MODO RECONSTRUCCIÓN (Fallback o Texto)
    if not st.session_state.original_svg_content or st.session_state.current_layout_type == "text":
        if not shapes_data: return ""
        min_x, min_y, max_x, max_y = 100000.0, 100000.0, -100000.0, -100000.0
        for s in shapes_data:
            if s['type'] == 'rect':
                min_x, min_y = min(min_x, s['x']), min(min_y, s['y'])
                max_x, max_y = max(max_x, s['x']+s['width']), max(max_y, s['y']+s['height'])
        if min_x > 90000: min_x, min_y, max_x, max_y = 0, 0, 1000, 1000
        w, h = max_x - min_x + 200, max_y - min_y + 200
        svg = f'<svg id="warehouse-svg" width="100%" height="100%" viewBox="{min_x-100} {min_y-100} {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        svg += '<rect x="-5000" y="-5000" width="10000" height="10000" fill="#ffffff"/>'
        for s in shapes_data:
            u = s.get('ubicacion', '')
            if s['type'] == 'rect':
                f, st_col = get_color_for_loc(u)
                tooltip = build_tooltip(u)
                sw = s['width']
                sh = s['height']
                sx = s['x']
                sy = s['y']
                # Mostrar pallet text si hay asignaciones
                assignments = [a for a in (pallet_assignments.get(u, []) if isinstance(pallet_assignments.get(u, []), list) else [pallet_assignments.get(u, [])]) if a]
                svg += f'<g class="location-group" style="cursor:pointer;">'
                svg += f'<title>{tooltip}</title>'
                svg += f'<rect id="{u}" x="{sx}" y="{sy}" width="{sw}" height="{sh}" fill="{f}" stroke="{st_col}" stroke-width="2" rx="3"/>'
                # Nombre de ubicación
                svg += f'<text x="{sx+sw/2}" y="{sy+sh/2 - (6 if assignments else 0)}" text-anchor="middle" dominant-baseline="middle" font-size="10" font-weight="bold" fill="#e5e7eb" pointer-events="none">{u}</text>'
                # Info de pallets abajo
                if assignments:
                    pallet_nums = ", ".join([str(a.get('pallet','?')) for a in assignments[:2]])
                    svg += f'<text x="{sx+sw/2}" y="{sy+sh/2 + 9}" text-anchor="middle" dominant-baseline="middle" font-size="8" fill="#fde68a" pointer-events="none">{pallet_nums}</text>'
                svg += '</g>'
        return svg + '</svg>'

    # MODO PRESERVACIÓN (SVG Original)
    svg_raw = st.session_state.original_svg_content
    
    # 1. Limpiar tag <svg> y poner dimensiones correctas
    svg_tag_match = re.search(r'<svg([^>]*)>', svg_raw, re.IGNORECASE)
    if not svg_tag_match: return svg_raw
    attrs = svg_tag_match.group(1)
    
    # 2. Calcular viewBox desde shapes_data
    vbox_attr = ""
    if shapes_data:
        xs = [s.get('x', 0) for s in shapes_data if 'x' in s] + [s.get('x', 0) + s.get('width', 0) for s in shapes_data if 'x' in s]
        ys = [s.get('y', 0) for s in shapes_data if 'y' in s] + [s.get('y', 0) + s.get('height', 0) for s in shapes_data if 'y' in s]
        if xs and ys:
            mx, my = min(xs), min(ys)
            Mw = max(max(xs) - mx, 100)
            Mh = max(max(ys) - my, 100)
            vbox_attr = f' viewBox="{mx-100} {my-100} {Mw+200} {Mh+200}"'
    if not vbox_attr:
        orig_vbox = re.search(r'viewBox=["\']([^"\']+)["\']', attrs, re.I)
        if orig_vbox:
            vbox_attr = f' viewBox="{orig_vbox.group(1)}"'
    
    # 3. Construir tag SVG limpio
    attrs_clean = re.sub(r'\s+(?:id|width|height|viewBox)=["\'][^"\']*["\']', '', attrs, flags=re.I)
    new_tag = f'<svg id="warehouse-svg" width="100%" height="750px"{vbox_attr}{attrs_clean}>'
    svg_clean = re.sub(r'<svg[^>]*>', new_tag, svg_raw, count=1, flags=re.I)
    
    # 4. Fondo oscuro como primer elemento
    bg_rect = '<rect x="-99999" y="-99999" width="199999" height="199999" fill="#0f172a" pointer-events="none"/>'
    svg_clean = re.sub(r'(<svg[^>]*>)', r'\1' + bg_rect, svg_clean, count=1, flags=re.I)
    
    # 5. CAPA DE COLOR: Construir un grupo overlay usando shapes_data (COORDENADAS YA CONOCIDAS)
    #    Este método es 100% confiable porque no depende de modificar el SVG original.
    #    En cambio, dibujamos rect coloreados encima usando x,y,w,h ya parseados.
    shape_lookup = {s['ubicacion']: s for s in shapes_data if 'ubicacion' in s}
    
    overlay_parts = ['<g id="color-overlays">']
    for lid in st.session_state.layout_locations:
        s = shape_lookup.get(lid)
        if not s or s.get('type') != 'rect':
            continue
        fill_color, stroke_color = get_color_for_loc(lid)
        tooltip = build_tooltip(lid)
        x, y, w, h = s.get('x', 0), s.get('y', 0), s.get('width', 40), s.get('height', 25)
        tooltip_safe = tooltip.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        overlay_parts.append(
            f'<g style="cursor:pointer;">'
            f'<title>{tooltip_safe}</title>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="{fill_color}" stroke="{stroke_color}" stroke-width="2" opacity="0.92" rx="2"/>'
            f'<text x="{x+w/2}" y="{y+h/2}" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="{max(7, min(11, int(h*0.35)))}" font-weight="bold" fill="#ffffff" pointer-events="none">{lid}</text>'
            f'</g>'
        )
    overlay_parts.append('</g>')
    overlay_svg = '\n'.join(overlay_parts)
    
    # Insertar overlay justo antes de </svg>
    svg_clean = re.sub(r'</svg>', overlay_svg + '</svg>', svg_clean, flags=re.I)
    
    # 6. CSS mínimo para hover
    style = '<style type="text/css">'
    style += 'svg#warehouse-svg { display: block; background: #0f172a; width: 100%; height: 100%; min-height: 750px; }'
    style += '#color-overlays g { transition: filter 0.15s; }'
    style += '#color-overlays g:hover rect { filter: brightness(1.5); stroke-width: 3px; }'
    style += '</style>'
    svg_clean = re.sub(r'(<svg[^>]*>)', r'\1' + style, svg_clean, count=1, flags=re.I)
    
    return svg_clean

def extract_sheet_id(url):
    patterns = [r'/spreadsheets/d/([a-zA-Z0-9-_]+)', r'id=([a-zA-Z0-9-_]+)', r'/d/([a-zA-Z0-9-_]+)']
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url if len(url) > 30 else None

# Inicialización de estado de sesión
if 'scanned_pallets' not in st.session_state:
    st.session_state.scanned_pallets = set()
if 'current_truck' not in st.session_state:
    st.session_state.current_truck = None
if 'truck_pallets' not in st.session_state:
    st.session_state.truck_pallets = pd.DataFrame()
if 'last_scan_time' not in st.session_state:
    st.session_state.last_scan_time = 0
if 'scanned_count' not in st.session_state:
    st.session_state.scanned_count = 0
if 'layout_locations' not in st.session_state:
    st.session_state.layout_locations = []
if 'layout_shapes' not in st.session_state:
    st.session_state.layout_shapes = []
if 'pallet_assignments' not in st.session_state:
    st.session_state.pallet_assignments = {}
if 'current_layout_type' not in st.session_state:
    st.session_state.current_layout_type = None
if 'delivered_trucks' not in st.session_state:
    st.session_state.delivered_trucks = set()
if 'delivered_trucks' not in st.session_state:
    st.session_state.delivered_trucks = set()
if 'camion_asignado_actual' not in st.session_state:
    st.session_state.camion_asignado_actual = None
if 'original_svg_content' not in st.session_state:
    st.session_state.original_svg_content = None
if 'svg_viewbox' not in st.session_state:
    st.session_state.svg_viewbox = None

# Aplicación principal
st.markdown("<div style='text-align: center; font-size: 4rem; font-weight: 600;'>Control de embarques Wasion</div>",unsafe_allow_html=True)
st.markdown("<div style='text-align: center; font-size: 1.5rem; font-weight: 600;'>Codificación pendiente por SGC</div>",unsafe_allow_html=True)
st.markdown("---")

# Obtener cliente
client = get_google_client()

all_loaded = ('packing_data' in st.session_state and 'layout_locations' in st.session_state)
if not all_loaded:
    # Configuración del Layout
    st.sidebar.header("Configurar proyecto")    
    st.sidebar.subheader("Cargar Layout")
    uploaded_xml = st.sidebar.file_uploader(
        "Sube tu archivo SVG",
        type=['svg'],
        help="Archivo SVG con formas que tengan IDs como C1-1, C1-2, etc."
    )
    if uploaded_xml:
        if st.sidebar.button("Cargar Layout", type="primary"):
            try:
                xml_content = uploaded_xml.getvalue().decode('utf-8')
                locations, shapes_data = parse_svg_xml(xml_content)
                st.session_state.layout_locations = locations
                st.session_state.layout_shapes = shapes_data
                st.session_state.original_svg_content = xml_content
                st.session_state.current_layout_type = "svg"
                # Detectar camiones del layout
                st.session_state.camiones_layout = detectar_camiones_del_layout()
                st.sidebar.success(f"✅ Layout cargado: {len(locations)} ubicaciones")
            except Exception as e:
                st.sidebar.error(f"❌ Error cargando SVG: {e}")

    # URL input
    sheet_url = st.sidebar.text_input("URL Google Sheets:")
    if sheet_url:
        sheet_id = extract_sheet_id(sheet_url)
        if sheet_id:
            try:
                if 'shipment_data' not in st.session_state:
                    with st.spinner("🔄 Cargando datos..."):
                        shipment_df, header_row, sheet, load_time = load_all_data(client, sheet_id)
                        st.session_state.shipment_data = shipment_df
                        st.session_state.header_row = header_row
                        st.session_state.sheet = sheet
                        st.sidebar.success(f"✅ Datos cargados en {load_time:.1f}s")
                else:
                    shipment_df = st.session_state.shipment_data
                    header_row = st.session_state.header_row
                    sheet = st.session_state.sheet
                uploaded_packing = st.sidebar.file_uploader("Packing List (Excel)", type=['xlsx', 'xls'])
                if uploaded_packing:
                    if 'packing_data' not in st.session_state:
                        with st.spinner("Cargando packing list..."):
                            packing_df, pallet_summary = load_packing_data(uploaded_packing)
                            st.session_state.packing_data = packing_df
                            st.session_state.pallet_summary = pallet_summary
                            st.rerun()
            except Exception as e:
                st.error(f"❌ Error: {str(e)}")
                st.info("💡 Si el error persiste, intenta recargar la página o limpiar la base de datos desde la barra lateral.")
else:
    st.sidebar.success("✅ Layout y Datos Cargados")
    if st.sidebar.button("🔄 Cambiar Proyecto", type="primary", use_container_width=True):
        keys_to_clear = ['layout_locations', 'layout_shapes', 'original_svg_content', 'shipment_data', 'packing_data', 'pallet_summary', 'current_layout_type', 'scans_db', 'pallet_assignments']
        for k in keys_to_clear:
            if k in st.session_state:
                del st.session_state[k]
        st.rerun()

    shipment_df = st.session_state.shipment_data
    header_row = st.session_state.header_row
    sheet = st.session_state.sheet
    packing_df = st.session_state.packing_data
    pallet_summary = st.session_state.pallet_summary

    def refresh_supabase_data():
        """Carga datos frescos de Supabase y sincroniza el estado local"""
        st.session_state.scans_db = set()
        st.session_state.pallet_assignments = {}
        st.session_state.delivered_trucks = set()
        try:
            supabase = get_supabase_client()
            if supabase:
                resp = supabase.table('warehouse_occupancy').select('camion,pallet_number,ubicacion,slot,status').execute()
                rows = resp.data or []

                for row in rows:
                    camion = str(row.get('camion', ''))
                    pallet = str(row.get('pallet_number', ''))
                    ubicacion = row.get('ubicacion', '')
                    slot = row.get('slot', 1)
                    status = str(row.get('status', '')).lower()

                    if status == 'entregado':
                        st.session_state.delivered_trucks.add(camion)
                        continue

                    st.session_state.scans_db.add((camion, pallet))

                    if ubicacion:
                        assignment = {'camion': camion, 'pallet': pallet, 'slot': slot}
                        if ubicacion in st.session_state.pallet_assignments:
                            curr = st.session_state.pallet_assignments[ubicacion]
                            if isinstance(curr, list):
                                curr.append(assignment)
                            else:
                                st.session_state.pallet_assignments[ubicacion] = [curr, assignment]
                        else:
                            st.session_state.pallet_assignments[ubicacion] = [assignment]
                return True
        except Exception as e:
            st.error(f"⚠️ Error sincronizando: {e}")
            return False

    if 'scans_db' not in st.session_state:
        refresh_supabase_data()

    # Botón de sincronización manual en el sidebar
    if st.sidebar.button("🔄 Sincronizar Supabase", use_container_width=True):
        if refresh_supabase_data():
            st.sidebar.success("✅ Datos actualizados")
            st.rerun()
 
    # Diagnóstico de Layout (Barra Lateral)

    def is_pallet_scanned(truck, pallet):
        return (str(truck), str(pallet)) in st.session_state.scans_db

    def get_pallet_location(truck, pallet):
        for location, assignments in st.session_state.pallet_assignments.items():
            if isinstance(assignments, list):
                for assignment in assignments:
                    if (str(assignment.get('camion', '')) == str(truck) and 
                        str(assignment.get('pallet', '')) == str(pallet)):
                        return location, assignment.get('slot', 1)
            else:
                assignment = assignments
                if (str(assignment.get('camion', '')) == str(truck) and 
                    str(assignment.get('pallet', '')) == str(pallet)):
                    return location, assignment.get('slot', 1)
        return None, None

    def assign_pallet_location(truck_packing_list, pallet):
        if not st.session_state.layout_locations:
            return None, None
                    
        # DETECTAR CAMIÓN DISPONIBLE AUTOMÁTICAMENTE
        camion_actual = detectar_camion_disponible(truck_packing_list)
        if not camion_actual:
            st.error("❌ No hay camiones disponibles en el layout")
            return None, None
                    
        numero_pallet = extraer_numero_pallet(str(pallet))
                    
        if numero_pallet is None:
            return None, None
                    
        # CALCULAR UBICACIÓN BASADA EN NÚMERO DE PALLET Y CAMIÓN DETECTADO
        ubicacion = calcular_ubicacion_pallet(numero_pallet, camion_actual)
                    
        # Verificar si la ubicación calculada existe en el layout
        if ubicacion not in st.session_state.layout_locations:
            # Buscar la ubicación más cercana disponible
            ubicaciones_camion = [loc for loc in st.session_state.layout_locations if loc.startswith(f'{camion_actual}-')]
            if not ubicaciones_camion:
                return None, None
                        
            # Ordenar ubicaciones y tomar la primera disponible
            ubicaciones_camion.sort(key=lambda x: int(x.split('-')[1]))
            ubicacion = ubicaciones_camion[0]
                    
        # Verificar si hay espacio en la ubicación (máximo 2 pallets)
        current_assignments = []
        if ubicacion in st.session_state.pallet_assignments:
            if isinstance(st.session_state.pallet_assignments[ubicacion], list):
                current_assignments = st.session_state.pallet_assignments[ubicacion]
            else:
                current_assignments = [st.session_state.pallet_assignments[ubicacion]]
                    
        # Verificar si hay espacio (máximo 2 pallets por ubicación)
        if len(current_assignments) < 2:
            # Encontrar slot disponible
            used_slots = {assig.get('slot', 1) for assig in current_assignments}
            available_slot = 1 if 1 not in used_slots else 2
                        
            new_assignment = {
                'camion': str(truck_packing_list),  # Guardamos el camión del packing list
                'pallet': str(pallet),
                'slot': available_slot
            }
                        
            # Actualizar asignaciones
            if ubicacion in st.session_state.pallet_assignments:
                if isinstance(st.session_state.pallet_assignments[ubicacion], list):
                    st.session_state.pallet_assignments[ubicacion].append(new_assignment)
                else:
                    st.session_state.pallet_assignments[ubicacion] = [st.session_state.pallet_assignments[ubicacion], new_assignment]
            else:
                st.session_state.pallet_assignments[ubicacion] = [new_assignment]
                        
            return ubicacion, available_slot
                    
        return None, None

    def save_scan_to_supabase(truck, pallet, ubicacion, slot, project_id="default"):
        """Guarda el escaneo en Supabase con status='escaneado'"""
        try:
            supabase = get_supabase_client()
            if supabase is None:
                return False
                        
            data = {
                "ubicacion": str(ubicacion),
                "camion": str(truck),
                "pallet_number": str(pallet),
                "slot": int(slot),
                "project_id": str(project_id),
                "status": "escaneado",
                "scanned_at": "now()"
            }
                        
            response = supabase.table('warehouse_occupancy').insert(data).execute()
            return True
        except Exception as e:
            st.warning(f"⚠️ Error guardando en Supabase: {e}")
            return False

    def register_pallet_scan(truck_packing_list, pallet, first_serial, last_serial):
        try:
            ubicacion, slot = assign_pallet_location(truck_packing_list, pallet)

            def save_to_supabase():
                try:
                    supabase = get_supabase_client()
                    if supabase:
                        data = {
                            "ubicacion": str(ubicacion) if ubicacion else None,
                            "camion": str(truck_packing_list),
                            "pallet_number": str(pallet),
                            "slot": int(slot) if slot else 1,
                            "project_id": "default",
                            "status": "escaneado",
                        }
                        supabase.table('warehouse_occupancy').insert(data).execute()
                except Exception as e:
                    print(f"Error guardando en Supabase: {e}")

            threading.Thread(target=save_to_supabase, daemon=True).start()

            st.session_state.scans_db.add((str(truck_packing_list), str(pallet)))
            return True, ubicacion, slot

        except Exception as e:
            print(f"register_pallet_scan error: {e}")
            return False, None, None

    def update_shipment_status_async(truck, status="Listo"):
        def update_async():
            try:
                time.sleep(1)
                truck_cells = sheet.findall(str(truck))
                for cell in truck_cells:
                    if cell.row > header_row:
                        sheet.update_cell(cell.row, 19, status)
                        break
            except Exception:
                pass
                    
        thread = threading.Thread(target=update_async)
        thread.daemon = True
        thread.start()

    def get_truck_pallets(truck_data, pallet_summary):
        """CORREGIDO: Maneja correctamente la comparación de pallets sin errores de Serie"""
        try:
            pallet_start = str(truck_data['PALLET INICIAL']).strip()
            pallet_end = str(truck_data['PALLET FINAL']).strip()
                        
            # Crear una lista para almacenar los pallets que coinciden
            matching_pallets = []
                        
            for _, pallet_row in pallet_summary.iterrows():
                pallet_num = str(pallet_row['Pallet number'])
                            
                # Intentar comparar como números si es posible
                try:
                    pallet_num_float = float(pallet_num)
                    start_float = float(pallet_start)
                    end_float = float(pallet_end)
                                
                    if start_float <= pallet_num_float <= end_float:
                        matching_pallets.append(pallet_row)
                except (ValueError, TypeError):
                    # Si no se pueden convertir a números, comparar como strings
                    if pallet_start <= pallet_num <= pallet_end:
                        matching_pallets.append(pallet_row)
                        
            if matching_pallets:
                return pd.DataFrame(matching_pallets)
            else:
                return pd.DataFrame(columns=pallet_summary.columns)
                    
        except Exception as e:
            st.error(f"Error en get_truck_pallets: {e}")
            return pd.DataFrame()

    def deliver_truck(truck):
        """Marcar camión como entregado en Supabase y liberar memoria local"""
        try:
            # Actualizar Supabase: marcar todos los pallets del camión como 'entregado'
            def update_supabase_delivery():
                try:
                    supabase = get_supabase_client()
                    if supabase:
                        supabase.table('warehouse_occupancy') \
                            .update({"status": "entregado"}) \
                            .eq("camion", str(truck)) \
                            .execute()
                except Exception as e:
                    print(f"Error actualizando Supabase en entrega: {e}")
            threading.Thread(target=update_supabase_delivery, daemon=True).start()
            # Liberar asignaciones en memoria
            locations_to_remove = []
            for ubicacion, assignments in st.session_state.pallet_assignments.items():
                if isinstance(assignments, list):
                    # Filtrar solo los assignments que no son del camión
                    remaining_assignments = [a for a in assignments if str(a.get('camion', '')) != str(truck)]
                    if remaining_assignments:
                        st.session_state.pallet_assignments[ubicacion] = remaining_assignments
                    else:
                        locations_to_remove.append(ubicacion)
                else:
                    if str(assignments.get('camion', '')) == str(truck):
                        locations_to_remove.append(ubicacion)
                        
            for ubicacion in locations_to_remove:
                del st.session_state.pallet_assignments[ubicacion]
                        
            # Actualizar scans_db
            st.session_state.scans_db = {scan for scan in st.session_state.scans_db if scan[0] != str(truck)}
                        
            # Marcar como entregado
            st.session_state.delivered_trucks.add(str(truck))

            # Actualizar Google Sheets
            update_shipment_status_async(truck, "Entregado")

            # Refrescar datos frescos de Supabase
            refresh_supabase_data()

            return True
        except Exception as e:
            st.error(f"Error al entregar camión: {e}")
            return False

    # Interfaz principal con pestañas
    available_trucks = shipment_df.copy()
                
    if 'ESTATUS' in shipment_df.columns:
        mask = (
            shipment_df['ESTATUS'].isna() | 
            (shipment_df['ESTATUS'] == '') |
            (shipment_df['ESTATUS'] == 'None')
        )
        try:
            listo_mask = shipment_df['ESTATUS'].str.contains('LISTO', case=False, na=False)
            mask = mask | ~listo_mask
        except:
            pass
        available_trucks = shipment_df[mask]
                

    # Crear pestañas (Fuera del IF de ESTATUS para que siempre se vean)
    st.markdown("""<style>button[data-baseweb="tab"] {font-size: 28px !important;font-weight: bold;}</style>""", unsafe_allow_html=True)
    tab1, tab2, tab3 = st.tabs(["𝄃𝄃𝄂𝄂𝄀𝄁𝄃𝄂𝄂𝄃 Escanear Pallet", "📍🗺️ Layout PT", "🚚 Entregar camión al almacén"])

    with tab1:
        if len(available_trucks) == 0:
            st.success("🎉 Todos los camiones listos en el Shipment!")
            selected_truck = None
        else:
            selected_truck = st.selectbox(
                "🚛 Selecciona camión para Escanear (Packing List):",
                available_trucks['CAMION'].values,
                key="truck_selector"
            )

            if selected_truck:
                if st.session_state.current_truck != selected_truck:
                    st.session_state.current_truck = selected_truck
                    truck_data = available_trucks[available_trucks['CAMION'] == selected_truck].iloc[0]
                    st.session_state.truck_pallets = get_truck_pallets(truck_data, pallet_summary)
                    st.session_state.scanned_count = sum(
                        1 for _, row in st.session_state.truck_pallets.iterrows() 
                        if is_pallet_scanned(selected_truck, row['Pallet number'])
                    )
                    # DETECTAR CAMIÓN DISPONIBLE PARA ESTE TRUCK
                    st.session_state.camion_asignado_actual = detectar_camion_disponible(selected_truck)

                truck_pallets = st.session_state.truck_pallets
                total_pallets = len(truck_pallets)
                scanned_count = st.session_state.scanned_count

                # Mostrar tabla de pallets del camión seleccionado
                st.subheader("📋 Información del Camión Seleccionado")
                            
                # Crear tabla con información del camión
                truck_info = available_trucks[available_trucks['CAMION'] == selected_truck].iloc[0]
                            
                info_cols = st.columns(4)
                with info_cols[0]:
                    st.metric("🚛 Camión (Packing List)", selected_truck)
                with info_cols[1]:
                    st.metric("📦 Pallets Inicial", truck_info['PALLET INICIAL'])
                with info_cols[2]:
                    st.metric("📦 Pallets Final", truck_info['PALLET FINAL'])
                with info_cols[3]:
                    st.metric("📊 Estatus", truck_info.get('ESTATUS', 'Pendiente'))
                            
                # INFORMACIÓN MEJORADA DE ASIGNACIÓN AUTOMÁTICA
                st.subheader("🎯 Asignación Automática de Camión")
                            
                # DETERMINAR SI EL CAMIÓN PUEDE ESCANEAR
                truck_ya_entregado = str(selected_truck) in st.session_state.delivered_trucks
                layout_lleno = (st.session_state.camion_asignado_actual is None)
                puede_escanear = not truck_ya_entregado and not layout_lleno

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.info(f"**📋 Camión Packing List:**\n# {selected_truck}")
                with col2:
                    if truck_ya_entregado:
                        st.error("🚧 Camión ya entregado")
                    elif layout_lleno:
                        st.error("🔴 Sin espacio disponible\nLibera ubicaciones antes de escanear")
                    else:
                        st.success(f"**🏗️ Camión en Layout:**\n# {st.session_state.camion_asignado_actual}")
                with col3:
                    if st.session_state.camiones_layout:
                        st.info(f"**🗺️ Camiones en Layout:**\n{', '.join([f'C{c}' for c in st.session_state.camiones_layout])}")                    
                            
                # Métricas de progreso
                st.subheader("📊 Progreso de Escaneo")
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("📦 Pallets Escaneados", f"{scanned_count}/{total_pallets}")
                with col2:
                    st.metric("⏳ Pendientes", total_pallets - scanned_count)
                with col3:
                    progress = scanned_count / total_pallets if total_pallets > 0 else 0
                    st.metric("📊 % Completado", f"{progress:.1%}")
                            
                st.progress(progress)

                # Tabla detallada de pallets
                st.subheader("📋 Tabla de Pallets del Camión")
                            
                if not truck_pallets.empty:
                    # Para camión entregado: cargar ubicaciones históricas desde Supabase
                    historical_locations = {}
                    if truck_ya_entregado:
                        try:
                            supabase_client = get_supabase_client()
                            if supabase_client:
                                resp = supabase_client.table('warehouse_occupancy') \
                                    .select('pallet_number,ubicacion,slot') \
                                    .eq('camion', str(selected_truck)) \
                                    .eq('status', 'entregado') \
                                    .execute()
                                for r in (resp.data or []):
                                    historical_locations[str(r.get('pallet_number',''))] = (
                                        r.get('ubicacion', ''), r.get('slot', 1)
                                    )
                        except Exception:
                            pass

                    pallet_table_data = []
                    for _, pallet in truck_pallets.iterrows():
                        pallet_number = pallet['Pallet number']
                        is_scanned = is_pallet_scanned(selected_truck, pallet_number)
                        location, slot = get_pallet_location(selected_truck, pallet_number)

                        if not is_scanned and truck_ya_entregado:
                            # Mostrar como escaneado con ubicación histórica
                            hist = historical_locations.get(str(pallet_number))
                            is_scanned = hist is not None
                            if hist:
                                location, slot = hist

                        pallet_table_data.append({
                            'Pallet': pallet_number,
                            'Primer Serial': pallet['first_serial'],
                            'Último Serial': pallet['last_serial'],
                            'Cajas': pallet['box_count'],
                            'Estatus': '✅ Escaneado' if is_scanned else '⏳ Pendiente',
                            'Ubicación': f"{location} (Slot {slot})" if location else 'No asignada',
                        })

                    pallet_df = pd.DataFrame(pallet_table_data)
                    st.dataframe(pallet_df, width='stretch')
                else:
                    st.warning("No se encontraron pallets para este camión en el rango especificado.")

                if not puede_escanear:
                    if truck_ya_entregado:
                        st.info("ℹ️ Este camión ya fue entregado al almacén. Solo puedes ver su información.")
                    else:
                        st.warning("⚠️ No hay espacio disponible en el layout. Entrega un camión para liberar ubicaciones antes de continuar.")
                else:
                    # ESCANEO POR PALLET - El escáner envía CR (carriage return) al terminar cada escaneo
                    st.subheader("🔍 Escaneo por Pallet")

                    # Inicializar estado
                    if 'scan_first' not in st.session_state:
                        st.session_state.scan_first = ''
                    if 'scan_last' not in st.session_state:
                        st.session_state.scan_last = ''
                    if 'scan_ready' not in st.session_state:
                        st.session_state.scan_ready = False
                    if 'scan_success_msg' not in st.session_state:
                        st.session_state.scan_success_msg = ''
                    if 'scan_error_msg' not in st.session_state:
                        st.session_state.scan_error_msg = ''

                    # Mostrar mensaje de resultado del último escaneo
                    if st.session_state.scan_success_msg:
                        st.success(st.session_state.scan_success_msg)
                        st.session_state.scan_success_msg = ''
                    if st.session_state.scan_error_msg:
                        st.error(st.session_state.scan_error_msg)
                        st.session_state.scan_error_msg = ''

                    # Determinar qué campo tiene el foco (segundo si ya tenemos el primero)
                    focus_last = bool(st.session_state.scan_first)

                    def on_first_serial_change():
                        val = st.session_state.get('_first_serial_widget', '').strip().rstrip('\r')
                        if val:
                            st.session_state.scan_first = val

                    def on_last_serial_change():
                        val = st.session_state.get('_last_serial_widget', '').strip().rstrip('\r')
                        if val:
                            st.session_state.scan_last = val
                            st.session_state.scan_ready = True

                    col1, col2 = st.columns(2)
                    with col1:
                        st.text_input(
                            "Primer Serial del Pallet:",
                            key="_first_serial_widget",
                            on_change=on_first_serial_change,
                            value='' if not st.session_state.scan_first else st.session_state.get('_first_serial_widget',''),
                            help="Escanea el primer serial y el escáner dará Enter."
                        )
                    with col2:
                        st.text_input(
                            "Último Serial del Pallet:",
                            key="_last_serial_widget",
                            on_change=on_last_serial_change,
                            help="Escanea el último serial. El pallet se registra automáticamente."
                        )

                    # JS: mover foco al segundo campo después de que el primero fue escaneado
                    if focus_last:
                        st.components.v1.html("""
                        <script>
                        (function() {
                            function focusLast() {
                                // Los inputs de Streamlit tienen aria-label igual al label del widget
                                var inputs = window.parent.document.querySelectorAll('input[type="text"]');
                                // El segundo text_input visible en la página
                                if (inputs.length >= 2) {
                                    // Buscar el que corresponde al último serial
                                    for (var i = 0; i < inputs.length; i++) {
                                        var label = inputs[i].getAttribute('aria-label') || '';
                                        if (label.includes('ltimo') || label.includes('ltimo Serial')) {
                                            inputs[i].focus();
                                            return;
                                        }
                                    }
                                    // Fallback: el último input visible
                                    inputs[inputs.length - 1].focus();
                                }
                            }
                            setTimeout(focusLast, 150);
                        })();
                        </script>
                        """, height=0)

                    # Procesar escaneo cuando el último serial está listo
                    if st.session_state.scan_ready:
                        first_serial = st.session_state.scan_first.rstrip('\r').strip()
                        last_serial = st.session_state.scan_last.rstrip('\r').strip()
                        st.session_state.scan_ready = False
                        st.session_state.scan_first = ''
                        st.session_state.scan_last = ''

                        if first_serial and last_serial:
                            current_time = time.time()
                            if current_time - st.session_state.last_scan_time < 0.5:
                                st.warning("⌛ Demasiado rápido, espera un momento")
                            else:
                                st.session_state.last_scan_time = current_time
                                submitted = True
                        else:
                            submitted = False
                            if not first_serial:
                                st.session_state.scan_error_msg = "⚠️ Falta el primer serial."
                    else:
                        submitted = False
                        first_serial = ''
                        last_serial = ''

                    if submitted and first_serial and last_serial:
                        matching_pallet = None
                        for _, pallet in truck_pallets.iterrows():
                            if (str(pallet['first_serial']) == first_serial and
                                str(pallet['last_serial']) == last_serial):
                                matching_pallet = pallet
                                break

                        if matching_pallet is not None:
                            pallet_number = matching_pallet['Pallet number']

                            if not is_pallet_scanned(selected_truck, pallet_number):
                                success, ubicacion, slot = register_pallet_scan(
                                    selected_truck, pallet_number, first_serial, last_serial
                                )

                                if success:
                                    st.session_state.scanned_count += 1
                                    msg = f"✅ Pallet {pallet_number} escaneado!"
                                    if ubicacion:
                                        msg += f"  📍 Ubicación: {ubicacion} (Slot {slot})"
                                    st.session_state.scan_success_msg = msg
                                    # Refrescar datos antes de re-renderizar para actualizar alertas de espacio
                                    refresh_supabase_data()
                                    st.rerun()
                                else:
                                    st.session_state.scan_error_msg = "❌ Error al registrar en base de datos"
                                    st.rerun()
                            else:
                                st.session_state.scan_error_msg = f"⚠️ Pallet ya fue escaneado previamente"
                                st.rerun()
                        else:
                            st.session_state.scan_error_msg = "❌ Los seriales no coinciden con ningún pallet del camión"
                            st.rerun()


                st.markdown("---")
            else:
                st.info("👋 Por favor, selecciona un camión del Packing List para ver sus detalles y comenzar el escaneo.")
                st.image("https://img.icons8.com/clouds/200/000000/delivery-truck.png")

        with tab2:
            # VISUALIZACIÓN SVG INTERACTIVA EN PESTAÑA SEPARADA
            if st.session_state.layout_locations and st.session_state.layout_shapes:
                # Mapa interactivo
                st.subheader("🗺️ Mapa SVG Interactivo del Almacén")
                            
                # Generar SVG con camión seleccionado y asignado para colorear correctamente
                camion_asignado_num = st.session_state.get('camion_asignado_actual', None)
                svg_content = generate_enhanced_svg_layout(
                    st.session_state.layout_shapes,
                    st.session_state.pallet_assignments,
                    selected_truck,
                    truck_pallets if 'truck_pallets' in dir() and not truck_pallets.empty else pd.DataFrame(),
                    camion_asignado=camion_asignado_num
                )
                            
                if svg_content:
                    # Escapar caracteres especiales en el SVG para que no rompan el HTML
                    svg_escaped = svg_content.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")
                                
                    # Componente Interactivo Pro
                    st.components.v1.html(
                        f"""
                        <div id="container" style="border: 2px solid #374151; border-radius: 12px; background: #0f172a; height: 750px; width: 100%; position: relative; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.3);">
                            <div id="debug-info" style="position: absolute; top: 10px; left: 10px; background: rgba(0,0,0,0.8); color: #10b981; padding: 6px 12px; font-family: monospace; font-size: 11px; z-index: 2000; border-radius: 6px; border: 1px solid #10b981; pointer-events: none;">
                                Motor: Inicializando...
                            </div>
                            <div id="loading-msg" style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #9ca3af; font-family: sans-serif; z-index: 500;">
                                Conectando con el layout...
                            </div>
                            <!-- Leyenda de Colores -->
                            <div style="position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.85); padding: 10px 14px; border-radius: 8px; border: 1px solid #374151; font-family: sans-serif; font-size: 12px; color: #e5e7eb; z-index: 2000; pointer-events: none; line-height: 1.9;">
                                <div style="font-weight:bold; margin-bottom:4px; color:#9ca3af;">Leyenda</div>
                                <div><span style="display:inline-block;width:14px;height:14px;background:#16a34a;border:2px solid #4ade80;border-radius:3px;vertical-align:middle;margin-right:6px;"></span>Libre</div>
                                <div><span style="display:inline-block;width:14px;height:14px;background:#d97706;border:2px solid #fbbf24;border-radius:3px;vertical-align:middle;margin-right:6px;"></span>Camión en uso</div>
                                <div><span style="display:inline-block;width:14px;height:14px;background:#2563eb;border:2px solid #60a5fa;border-radius:3px;vertical-align:middle;margin-right:6px;"></span>Pallet escaneado</div>
                            </div>
                            <div id="svg-wrapper" style="width: 100%; height: 100%; overflow: hidden; position: relative;">
                                {svg_content}
                            </div>
                                        
                            <!-- Controles Flotantes -->
                            <div style="position: absolute; bottom: 20px; right: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 1000;">
                                <button id="z-in" title="Zoom In" style="width: 44px; height: 44px; border-radius: 22px; border: 1px solid #ddd; background: #ffffff; color: #333; font-size: 24px; cursor: pointer; box-shadow: 0 4px 8px rgba(0,0,0,0.2); display: flex; align-items: center; justify-content: center;">＋</button>
                                <button id="z-out" title="Zoom Out" style="width: 44px; height: 44px; border-radius: 22px; border: 1px solid #ddd; background: #ffffff; color: #333; font-size: 24px; cursor: pointer; box-shadow: 0 4px 8px rgba(0,0,0,0.2); display: flex; align-items: center; justify-content: center;">－</button>
                                <button id="z-res" title="Centrar Mapa" style="width: 44px; height: 44px; border-radius: 22px; border: none; background: #ff4b4b; color: white; font-size: 20px; cursor: pointer; box-shadow: 0 4px 8px rgba(0,0,0,0.2); display: flex; align-items: center; justify-content: center;">🎯</button>
                            </div>
                        </div>

                        <script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js"></script>
                        <script>
                            function updateDebug(msg, isError = false) {{
                                const el = document.getElementById('debug-info');
                                el.innerText = (isError ? "❌ " : "📍 ") + msg;
                                if (isError) el.style.borderColor = "#ef4444";
                            }}

                            let retryCount = 0;
                            function startApp() {{
                                const svg = document.getElementById('warehouse-svg');
                                            
                                if (typeof svgPanZoom === 'undefined') {{
                                    updateDebug("Cargando motor...");
                                    setTimeout(startApp, 200);
                                    return;
                                }}

                                 if (!svg) {{
                                     setTimeout(startApp, 300);
                                     return;
                                 }}

                                 // Defensive check: ensure SVG is painted and has dimensions
                                 try {{
                                     const bbox = svg.getBBox();
                                     if (!bbox || bbox.width === 0 || bbox.height === 0 || svg.clientWidth === 0) {{
                                         updateDebug("Esperando renderizado...");
                                         setTimeout(startApp, 200);
                                         return;
                                     }}
                                 }} catch(e) {{}}

                                 try {{
                                    const loading = document.getElementById('loading-msg');
                                    if (loading) loading.style.display = 'none';

                                    updateDebug("Iniciando motor...");
                                                
                                    const pz = svgPanZoom('#warehouse-svg', {{
                                        zoomEnabled: true,
                                        panEnabled: true,
                                        controlIconsEnabled: false,
                                        fit: true,
                                        center: true,
                                        minZoom: 0.001,
                                        maxZoom: 10.0,
                                        zoomScaleSensitivity: 0.3,
                                        mouseWheelZoomEnabled: true,
                                        preventMouseEventsDefault: true
                                    }});

                                    // Prevenir que el scroll del mapa haga scroll de la página
                                    const container = document.getElementById('container');
                                    container.addEventListener('wheel', function(e) {{
                                        e.preventDefault();
                                    }}, {{ passive: false }});

                                    // Ajuste post-carga con múltiples intentos
                                    function doFit(attempt) {{
                                        try {{
                                            pz.resize();
                                            pz.fit();
                                            pz.center();
                                            // Aplicar zoom mínimo DESPUES del fit para no bloquearlo
                                            pz.setMinZoom(0.5);
                                            const b = svg.getBBox();
                                            if (b.width > 0 && b.height > 0) {{
                                                updateDebug("SVG listo (" + Math.round(b.width) + "x" + Math.round(b.height) + ")");
                                            }} else if (attempt < 5) {{
                                                setTimeout(() => doFit(attempt + 1), 500);
                                            }} else {{
                                                updateDebug("SVG cargado correctamente");
                                            }}
                                        }} catch(e) {{
                                            if (attempt < 5) {{
                                                setTimeout(() => doFit(attempt + 1), 500);
                                            }} else {{
                                                updateDebug("SVG cargado correctamente");
                                            }}
                                        }}
                                    }}
                                    setTimeout(() => doFit(0), 400);

                                    document.getElementById('z-in').onclick = () => {{ pz.zoomIn(); }};
                                    document.getElementById('z-out').onclick = () => {{ pz.zoomOut(); }};
                                    document.getElementById('z-res').onclick = () => {{
                                        pz.resize();
                                        pz.fit();
                                        pz.center();
                                        pz.setMinZoom(0.5);
                                    }};
                                                
                                }} catch (err) {{
                                    console.error("Critical SVG Error:", err);
                                    updateDebug("ERROR: " + err.message, true);
                                    // Fallback: Mostrar el SVG normal sin zoom si el motor falla
                                    if (err.message.includes('matrix')) {{
                                        updateDebug("Error de Matriz - Usando modo estático", true);
                                        svg.style.width = "100%";
                                        svg.style.height = "auto";
                                    }}
                                }}
                            }}

                            window.onload = startApp;
                            setTimeout(startApp, 1000); // Doble disparo por seguridad
                        </script>
                        """,
                        height=800
                    )
                            
                            
                # Instrucciones de navegación
                            
            else:
                st.warning("⚠️ No hay layout cargado. Usa la barra lateral para cargar un layout SVG/XML.")
                st.info("💡 Puedes cargar un layout desde la barra lateral usando:")
                st.markdown("- 🖼️ **SVG/XML**: Sube un archivo SVG con el diseño del almacén")
                st.markdown("- 📝 **Texto**: Pega una lista de ubicaciones (C1-1, C1-2, etc.)")

        with tab3:
            st.subheader("🚚 Entregar Embarques")
                        
            # Listar camiones listos para entregar (completados pero no entregados)
            completed_trucks = []
            for truck in shipment_df['CAMION'].unique():
                if str(truck) in st.session_state.delivered_trucks:
                    continue
                                
                truck_data = shipment_df[shipment_df['CAMION'] == truck].iloc[0]
                truck_pallets_for_delivery = get_truck_pallets(truck_data, pallet_summary)
                total_pallets_for_delivery = len(truck_pallets_for_delivery)
                scanned_count_for_delivery = sum(
                    1 for _, row in truck_pallets_for_delivery.iterrows() 
                    if is_pallet_scanned(truck, row['Pallet number'])
                )
                            
                if scanned_count_for_delivery >= total_pallets_for_delivery and total_pallets_for_delivery > 0:
                    # Verificar si tiene ubicaciones asignadas
                    has_assignments = any(
                        any(str(a.get('camion', '')) == str(truck) for a in (assignments if isinstance(assignments, list) else [assignments]))
                        for assignments in st.session_state.pallet_assignments.values()
                    )
                                
                    if has_assignments:
                        completed_trucks.append({
                            'camion': truck,
                            'pallets_escaneados': scanned_count_for_delivery,
                            'total_pallets': total_pallets_for_delivery
                        })
                        
            if not completed_trucks:
                st.success("🎉 No hay camiones listos para entregar.")
                st.info("Los camiones aparecerán aquí cuando estén completados (todos los pallets escaneados)")
            else:
                st.subheader("📋 Camiones Listos para Entregar")
                            
                for truck_info in completed_trucks:
                    with st.container():
                        col1, col2, col3 = st.columns([2, 1, 1])
                        with col1:
                            st.write(f"**🚛 Camión {truck_info['camion']}**")
                            st.write(f"📦 Pallets: {truck_info['pallets_escaneados']}/{truck_info['total_pallets']}")
                                    
                        with col2:
                            # Mostrar ubicaciones asignadas
                            locations_count = 0
                            for ubicacion, assignments in st.session_state.pallet_assignments.items():
                                if isinstance(assignments, list):
                                    truck_assignments = [a for a in assignments if str(a.get('camion', '')) == str(truck_info['camion'])]
                                    locations_count += 1 if truck_assignments else 0
                                else:
                                    if str(assignments.get('camion', '')) == str(truck_info['camion']):
                                        locations_count += 1
                            st.write(f"📍 Ubicaciones: {locations_count}")
                                    
                        with col3:
                            if st.button(f"📦 Entregar", key=f"deliver_{truck_info['camion']}"):
                                if deliver_truck(truck_info['camion']):
                                    st.success(f"✅ Camión {truck_info['camion']} entregado exitosamente!")
                                    st.rerun()
                                else:
                                    st.error(f"❌ Error al entregar camión {truck_info['camion']}")
                                    
                        st.divider()
                            
                st.divider()
                st.info(f"✅ Camiones entregados hoy: {len(st.session_state.delivered_trucks)}")



