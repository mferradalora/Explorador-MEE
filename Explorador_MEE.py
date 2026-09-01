import io
import time
import zipfile
import datetime
import requests
import re
import unicodedata
import numpy as np
import pandas as pd
import geopandas as gpd
import folium
import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path

# -------------------------------------------------------------------------
# 1. CONFIGURACIÓN DE PÁGINA Y ORDENAMIENTO GEOGRÁFICO
# -------------------------------------------------------------------------
st.set_page_config(
    page_title="Plataforma MEE - DGA Chile",
    page_icon="💧",
    layout="wide"
)

st.title("💧 Explorador MEE - DGA Chile")
st.caption("📅 Actualización: Julio 2026")

st.markdown("""
La plataforma permite visualizar la ubicación geográfica de las obras registradas en el software de Monitoreo de Extracciones Efectivas (MEE) de la Dirección General de Aguas (DGA) 
y descargar las series históricas de extracciones y restituciones disponibles. Toda la información es recogida directamente desde 
la web https://dga.mop.gob.cl/informacion-de-extracciones-contenidas-en-software-mee/.
""")

# Referencia latitudinal de Norte a Sur para ordenamiento administrativo
ORDEN_NORTE_SUR = [
    "arica", "tarapaca", "antofagasta", "atacama", "coquimbo", 
    "valparaiso", "metropolitana", "higgins", "maule", "nuble", 
    "bio", "araucania", "rios", "lagos", "aysen", "magallanes"
]

def normalizar_texto(texto):
    if pd.isna(texto):
        return ""
    texto_str = str(texto).lower()
    texto_str = ''.join(c for c in unicodedata.normalize('NFKD', texto_str) if unicodedata.category(c) != 'Mn')
    texto_str = re.sub(r'[^a-z0-9\s]', ' ', texto_str)
    return ' '.join(texto_str.split())

def obtener_indice_norte_sur(nombre_region):
    norm = normalizar_texto(nombre_region)
    for idx, ref in enumerate(ORDEN_NORTE_SUR):
        if ref in norm:
            return idx
    return 99

# -------------------------------------------------------------------------
# 2. CARGA Y PROCESAMIENTO ESPACIAL DE DATOS (CACHED)
# -------------------------------------------------------------------------
@st.cache_data
def cargar_y_procesar_datos(path_csv):
    df = pd.read_csv(path_csv)
    
    # Sanitización de variables territoriales e identificadores
    df['Region'] = df['Region'].fillna('Sin Región').astype(str).str.strip()
    df['Provincia'] = df['Provincia'].fillna('Sin Provincia').astype(str).str.strip()
    df['Comuna'] = df['Comuna'].fillna('Sin Comuna').astype(str).str.strip()
    df['Codigo_Obra'] = df['Codigo_Obra'].fillna('S/C').astype(str).str.strip()
    df['ID_Obra'] = pd.to_numeric(df['ID_Obra'], errors='coerce').fillna(0).astype(int)
    
    # Normalización de Fecha_Registro_DGA
    if 'Fecha_Registro_DGA' in df.columns:
        df['Fecha_Registro_Clean'] = pd.to_datetime(
            df['Fecha_Registro_DGA'], dayfirst=True, errors='coerce'
        ).dt.date
    else:
        df['Fecha_Registro_Clean'] = datetime.date(2020, 1, 1)
        
    df['Fecha_Registro_Clean'] = df['Fecha_Registro_Clean'].fillna(datetime.date(2020, 1, 1))

    # --- FIX DTYPE: Conversión explícita a float antes de aplicar ruido ---
    df['UTM_Norte'] = pd.to_numeric(df['UTM_Norte'], errors='coerce').astype(float)
    df['UTM_Este'] = pd.to_numeric(df['UTM_Este'], errors='coerce').astype(float)
    df['Huso'] = pd.to_numeric(df['Huso'], errors='coerce')
    
    # Ruido aleatorio (+-10m) para superposición de obras en la misma coordenada
    filas_duplicadas = df.duplicated(subset=['UTM_Este', 'UTM_Norte', 'Huso'], keep=False)
    if filas_duplicadas.any():
        df.loc[filas_duplicadas, 'UTM_Este'] += np.random.uniform(-10, 10, size=filas_duplicadas.sum())
        df.loc[filas_duplicadas, 'UTM_Norte'] += np.random.uniform(-10, 10, size=filas_duplicadas.sum())

    gdfs = []
    for huso in [18, 19]:
        sub_df = df[df['Huso'] == huso].dropna(subset=['UTM_Este', 'UTM_Norte']).copy()
        if not sub_df.empty:
            epsg = 32718 if huso == 18 else 32719
            gdf_huso = gpd.GeoDataFrame(
                sub_df,
                geometry=gpd.points_from_xy(sub_df['UTM_Este'], sub_df['UTM_Norte']),
                crs=f"EPSG:{epsg}"
            ).to_crs(epsg=4326)
            gdfs.append(gdf_huso)

    if gdfs:
        gdf_final = pd.concat(gdfs, ignore_index=True)
        gdf_final['lat'] = gdf_final.geometry.y
        gdf_final['lon'] = gdf_final.geometry.x
        return pd.DataFrame(gdf_final.drop(columns='geometry'))
    else:
        df['lat'] = np.nan
        df['lon'] = np.nan
        return df

# -------------------------------------------------------------------------
# 3. CONEXIÓN API DGA SNIA
# -------------------------------------------------------------------------
def obtener_token_dga():
    url_auth = "https://snia.mop.gob.cl/mee-auth-rest/v1/authorization"
    headers = {
        'accept': 'application/json, text/plain, */*',
        'content-type': 'application/json',
        'origin': 'https://snia.mop.gob.cl',
        'referer': 'https://snia.mop.gob.cl/cExtracciones2/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'
    }
    payload = {
        'apiCode': 'MEESECINT',
        'apiKey': '73A58577C1CCB258DFD79116EAD8F',
        'app': 'exponline'
    }
    resp = requests.post(url_auth, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("accessToken")

def descargar_reporte_dga(codigo_obra, id_obra, fecha_desde_dt):
    token = obtener_token_dga()
    
    fecha_desde_iso = f"{fecha_desde_dt.strftime('%Y-%m-%d')}T03:00:00.000Z"
    fecha_hasta_iso = f"{datetime.date.today().strftime('%Y-%m-%d')}T02:59:59.000Z"
    
    # Diferenciación por prefijo: Extracción (OB) vs Restitución (OR)
    es_restitucion = str(codigo_obra).upper().startswith("OR")

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'es-ES,es;q=0.9',
        'authorization': f'Bearer {token}',
        'content-type': 'application/json',
        'origin': 'https://snia.mop.gob.cl',
        'referer': 'https://snia.mop.gob.cl/cExtracciones2/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
    }

    payload = {
        'metaData': {'paginator': None, 'token': None, 'userName': None},
        'data': {
            'fechaDesde': fecha_desde_iso,
            'fechaHasta': fecha_hasta_iso,
            'fechaDesdeLog': None,
            'fechaHastaLog': None,
            'codigoObraLog': None,
            'obras': [] if es_restitucion else [int(id_obra)],
            'puntosRestitucion': [int(id_obra)] if es_restitucion else [],
            'tipoReporte': {'clave': 4, 'valor': None},
            'anio': None,
            'mes': None,
            'naturaleza': 2 if es_restitucion else 1,
            'todasAlertasObra': None,
            'tipoAlerta': None,
            'mideConCaudalimetro': not es_restitucion,
        },
    }

    resp = requests.post(
        'https://snia.mop.gob.cl/extracciones/data/reporte/obrasDetallado',
        headers=headers,
        json=payload,
        timeout=35
    )
    resp.raise_for_status()
    return resp.content

# -------------------------------------------------------------------------
# 4. CARGA DE DATOS Y FILTROS LATITUDINALES (SIDEBAR)
# -------------------------------------------------------------------------
PATH_CSV = "Listado_Obras_MEE.csv"

try:
    df_obras = cargar_y_procesar_datos(PATH_CSV)
except Exception as e:
    st.error(f"❌ Error al procesar la base de datos '{PATH_CSV}': {e}")
    st.stop()

st.sidebar.header("🗺️ Filtros Administrativos")

# 1. Región (Ordenada de Norte a Sur)
regiones_presentes = list(df_obras['Region'].unique())
regiones_ordenadas = sorted(regiones_presentes, key=obtener_indice_norte_sur)
regiones_opt = ["Todas"] + regiones_ordenadas

region_sel = st.sidebar.selectbox("Seleccionar Región:", regiones_opt)

df_filtrado = df_obras.copy()
if region_sel != "Todas":
    df_filtrado = df_filtrado[df_filtrado['Region'] == region_sel]

# 2. Provincia
provincias_opt = ["Todas"] + sorted(list(df_filtrado['Provincia'].unique()))
provincia_sel = st.sidebar.selectbox("Seleccionar Provincia:", provincias_opt)

if provincia_sel != "Todas":
    df_filtrado = df_filtrado[df_filtrado['Provincia'] == provincia_sel]

# 3. Comuna
comunas_opt = ["Todas"] + sorted(list(df_filtrado['Comuna'].unique()))
comuna_sel = st.sidebar.selectbox("Seleccionar Comuna:", comunas_opt)

if comuna_sel != "Todas":
    df_filtrado = df_filtrado[df_filtrado['Comuna'] == comuna_sel]

# -------------------------------------------------------------------------
# 5. VISUALIZACIÓN ESPACIAL (CAPA PREDETERMINADA: OPENSTREETMAP)
# -------------------------------------------------------------------------
col_left, col_right = st.columns([1.2, 1])

with col_left:
    st.subheader("📍 Mapa de Obras")
    
    df_mapa = df_filtrado.dropna(subset=['lat', 'lon'])
    
    if df_mapa.empty:
        mapa = folium.Map(location=[-35.6751, -71.5430], zoom_start=4)
        folium.TileLayer('OpenStreetMap', name='OpenStreetMap', overlay=False, control=True, show=True).add_to(mapa)
    else:
        centro_lat = df_mapa['lat'].mean()
        centro_lon = df_mapa['lon'].mean()
        mapa = folium.Map(location=[centro_lat, centro_lon], zoom_start=9, tiles=None)
        
        # Capa base principal activa por defecto
        folium.TileLayer('OpenStreetMap', name='OpenStreetMap', overlay=False, control=True, show=True).add_to(mapa)
        
        # Capa secundaria seleccionable
        folium.TileLayer(
            tiles='https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', 
            attr='Google', 
            name='Híbrido (Google)',
            overlay=False,
            control=True,
            show=False
        ).add_to(mapa)

        for _, row in df_mapa.iterrows():
            nat = str(row.get('Naturaleza', '')).lower()
            color = "#007bff" if "superficial" in nat else "#6c757d"
            
            popup_html = f"""
            <div style="font-family: Arial; font-size: 12px; width: 210px;">
                <b>Código Obra:</b> {row['Codigo_Obra']}<br>
                <b>ID Obra DGA:</b> {row['ID_Obra']}<br>
                <b>Titular:</b> {row.get('Usuario', 'N/I')}<br>
                <b>Naturaleza:</b> {row.get('Naturaleza', 'N/I')}<br>
                <b>Cuenca:</b> {row.get('Cuenca', 'N/I')}<br>
                <b>Último Caudal:</b> {row.get('Ultimo_Caudal_Medido_ls', 'N/I')} L/s
            </div>
            """
            
            folium.CircleMarker(
                location=[row['lat'], row['lon']],
                radius=5,
                color="#000000",
                weight=1,
                fill=True,
                fill_color=color,
                fill_opacity=0.85,
                popup=folium.Popup(popup_html, max_width=260),
                tooltip=f"{row['Codigo_Obra']} | {row.get('Usuario', '')}"
            ).add_to(mapa)

        if len(df_mapa) > 1:
            bounds = [[df_mapa['lat'].min(), df_mapa['lon'].min()], [df_mapa['lat'].max(), df_mapa['lon'].max()]]
            mapa.fit_bounds(bounds)
            
        folium.LayerControl(collapsed=False).add_to(mapa)

    components.html(mapa._repr_html_(), height=500)

with col_right:
    st.subheader("📊 Listado de Obras")
    st.metric("Total Obras Identificadas", f"{len(df_filtrado):,}")
    
    cols_mostrar = ['Codigo_Obra', 'ID_Obra', 'Usuario', 'Naturaleza', 'Cuenca', 'Fecha_Registro_DGA']
    cols_validas = [c for c in cols_mostrar if c in df_filtrado.columns]
    
    st.dataframe(
        df_filtrado[cols_validas],
        use_container_width=True,
        height=400,
        hide_index=True
    )

# -------------------------------------------------------------------------
# 6. MÓDULO DE EXTRACCIÓN MEE DGA (MULTISELECCIÓN HASTA 10 OBRAS)
# -------------------------------------------------------------------------
st.markdown("---")
st.subheader("📥 Descarga de Registros de Extracciones Efectivas")

if df_filtrado.empty:
    st.warning("⚠️ No existen obras disponibles con los filtros seleccionados.")
else:
    obras_lista = sorted(df_filtrado['Codigo_Obra'].unique())
    
    obras_seleccionadas = st.multiselect(
        "Seleccione los Códigos de las Obras a Descargar (Máximo 10):",
        options=obras_lista,
        max_selections=10,
        help="Puedes elegir entre 1 y 10 obras. Si seleccionas varias, se compilarán en un archivo comprimido .zip."
    )
    
    if not obras_seleccionadas:
        st.info("💡 Por favor, seleccione al menos una obra del listado para habilitar la descarga.")
    else:
        st.write(f"📋 **Obras seleccionadas ({len(obras_seleccionadas)}/10):** {', '.join(obras_seleccionadas)}")
        
        if st.button("📡 Solicitar Registros a la API DGA"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            # Buffer temporal en memoria RAM para crear el ZIP
            zip_buffer = io.BytesIO()
            archivos_exitosos = 0
            
            with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
                for idx, obra_sel in enumerate(obras_seleccionadas):
                    status_text.text(f"⏳ Consultando DGA para {obra_sel} ({idx+1}/{len(obras_seleccionadas)})...")
                    
                    row_sel = df_filtrado[df_filtrado['Codigo_Obra'] == obra_sel].iloc[0]
                    id_sel = row_sel['ID_Obra']
                    fecha_ini = row_sel['Fecha_Registro_Clean']
                    
                    try:
                        contenido_binario = descargar_reporte_dga(obra_sel, id_sel, fecha_ini)
                        # Agregar el archivo binario al ZIP
                        zip_file.writestr(f"{obra_sel}.xls", contenido_binario)
                        archivos_exitosos += 1
                    except Exception as err:
                        st.error(f"❌ Error al descargar {obra_sel}: {err}")
                    
                    # Actualización de la barra de progreso
                    progress_bar.progress((idx + 1) / len(obras_seleccionadas))
                    time.sleep(0.5) # Pausa de cortesía entre peticiones
            
            status_text.text("✅ Proceso de extracción finalizado.")
            
            if archivos_exitosos > 0:
                zip_buffer.seek(0)
                
                if len(obras_seleccionadas) == 1:
                    # Si eligió solo 1 obra, descarga directa en .xls
                    obra_unica = obras_seleccionadas[0]
                    row_u = df_filtrado[df_filtrado['Codigo_Obra'] == obra_unica].iloc[0]
                    binario_u = descargar_reporte_dga(obra_unica, row_u['ID_Obra'], row_u['Fecha_Registro_Clean'])
                    
                    st.success(f"✅ Reporte listo para {obra_unica}.")
                    st.download_button(
                        label=f"💾 Descargar {obra_unica}.xls",
                        data=binario_u,
                        file_name=f"{obra_unica}.xls",
                        mime="application/vnd.ms-excel"
                    )
                else:
                    # Si eligió entre 2 y 10 obras, descarga el paquete .zip
                    st.success(f"✅ Se compilaron exitosamente {archivos_exitosos} reportes en un paquete comprimido.")
                    nombre_zip = f"reportes_MEE_DGA_{datetime.date.today().strftime('%Y%m%d')}.zip"
                    
                    st.download_button(
                        label=f"💾 Descargar Registros (.zip)",
                        data=zip_buffer,
                        file_name=nombre_zip,
                        mime="application/zip"
                    )
