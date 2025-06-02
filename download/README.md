# 🎬 Movie Download System

Sistema completo para descargar películas con nombres TMDB y subirlas automáticamente a Google Photos.

## 📁 Archivos

### Scripts Principales
- **`download_movies.py`** - Descargador con integración TMDB
- **`movie_workflow.py`** - Orquestador del flujo completo
- **`movies.jsonl`** - Archivo de ejemplo con datos de películas

## 🚀 Uso Rápido

### Workflow Automático (Recomendado)
```bash
# Desde la raíz del proyecto
python download/movie_workflow.py --cycles 10

# Con archivo personalizado
python download/movie_workflow.py --input mi_archivo.jsonl --cycles 5

# Continuar desde donde se quedó
python download/movie_workflow.py --start-index 40 --cycles 5

# Modo dry-run para ver qué haría
python download/movie_workflow.py --cycles 3 --dry-run
```

### Descarga Manual
```bash
# Listar películas disponibles
python download/download_movies.py --list

# Descargar batch específico
python download/download_movies.py --batch 4 --start 0

# Con archivo personalizado
python download/download_movies.py --input mi_archivo.jsonl --batch 4
```

## 📋 Formato del JSONL

Cada línea debe ser un JSON válido con esta estructura:

```json
{"tmdb":"1297028","url":"https://example.com/movie.mp4","quality":"720p","size":"324.04 MB","timestamp":"2025-05-28T07:54:44.408Z"}
```

### Campos Requeridos
- **`tmdb`** - ID de TMDB para obtener el nombre real
- **`url`** - URL de descarga directa del archivo

### Campos Opcionales
- **`quality`** - Calidad del video (720p, 1080p, etc.)
- **`size`** - Tamaño del archivo
- **`timestamp`** - Marca de tiempo

## 🔄 Flujo de Trabajo

### Proceso Automático
1. **Descarga** 4 películas con nombres TMDB
2. **Subida** a Google Photos con gpmc
3. **Eliminación** automática con `--delete-from-host`
4. **Repetición** del ciclo

### Nombres de Archivos
Los archivos se guardan con formato:
```
MovieName_tmdbid.extension
```

Ejemplos:
- `Inception_27205.mp4`
- `The_Avengers_24428.mp4`
- `Blade_Runner_2049_335984.mp4`

## ⚙️ Configuración

### TMDB API
- **API Key**: Configurada en `download_movies.py`
- **Rate Limiting**: Respeta límites de TMDB
- **Fallback**: Si falla, usa `Movie_tmdbid`

### Carpetas
- **Descarga**: `movies/` (se crea automáticamente)
- **Subida**: gpmc sube desde `movies/`
- **Limpieza**: `--delete-from-host` elimina después de subir

### gpmc Command
```bash
gpmc upload movies/ --album ENG --recursive --progress --delete-from-host --threads 4
```

## 🎯 Características

### Robustez
- ✅ Manejo de errores y recuperación
- ✅ Verificación de archivos existentes
- ✅ Logging detallado del progreso
- ✅ Continuación desde cualquier punto

### Eficiencia
- ✅ Batch processing (4 películas por defecto)
- ✅ Gestión automática de espacio (100GB)
- ✅ Eliminación automática después de subida
- ✅ Escalable para miles de películas

### Integración
- ✅ TMDB API para nombres reales
- ✅ gpmc para subida a Google Photos
- ✅ Formato de archivos limpio y consistente

## 🔧 Troubleshooting

### Errores Comunes
- **TMDB API Error**: Verifica la API key y conexión
- **gpmc Error**: Verifica autenticación GP_AUTH_DATA
- **Archivo no encontrado**: Verifica ruta del JSONL
- **Espacio insuficiente**: Verifica espacio en disco

### Logs
Todos los scripts proporcionan logging detallado para diagnosticar problemas.

## 📊 Ejemplo de Uso

```bash
# 1. Preparar archivo JSONL con tus películas
cp download/movies.jsonl mi_lista.jsonl

# 2. Ejecutar workflow automático
python download/movie_workflow.py --input mi_lista.jsonl --cycles 20

# 3. El sistema procesará automáticamente:
#    - Descarga 4 películas
#    - Sube a Google Photos
#    - Elimina archivos locales
#    - Repite para siguientes 4
```

¡Sistema listo para procesar miles de películas automáticamente! 🚀
