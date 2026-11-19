<p align="center">
  <img src="assets/AudioAula.png" alt="Icono de AudioAula" width="128" />
</p>

# AudioAula

**AudioAula** es una aplicación de escritorio gratuita y de código abierto (FOSS) escrita en Python (`tkinter`) para **extraer, convertir, recortar y separar audio** a partir de archivos de video o pistas de audio utilizando **FFmpeg**. Está diseñada pensando en las necesidades prácticas de docentes y estudiantes, con una interfaz clara en español y sin dependencias externas de `pip`.

- 📦 **Descarga directa para Windows**: obtén `AudioAula.exe` desde la página de [Releases de GitHub](https://github.com/dmg-cmd/AudioAula/releases).
- 📖 **Documentación completa**: consulta el [Manual de Uso para Docentes](docs/MANUAL_USO.md), el [Índice de Documentación](docs/README.md) y el [Historial y Guía Técnica](docs/HISTORIAL.md).

---

## Usos recomendados en la práctica docente

- **Ahorro de datos y compatibilidad para estudiantes**: extrae únicamente la pista de audio de videos educativos pesados para compartirla en formato MP3 u Opus liviano, ideal para estudiantes con conectividad limitada o dispositivos móviles.
- **Materiales para idiomas, música y comprensión auditiva**: recorta fragmentos específicos con precisión visual sobre la forma de onda, ajusta el volumen y aplica fundidos suaves de entrada o salida.
- **Cápsulas a partir de clases grabadas**: divide exposiciones o clases extensas en **2 o 3 partes** de manera directa, o elimina tramos silenciosos e interrupciones uniendo el principio y el final.
- **Revisión ágil de grabaciones largas**: escucha y ubica puntos de corte rápidamente reproduciendo a velocidades de hasta **`3.0x`** desde el editor integrado.
- **Múltiples fragmentos en serie**: marca varios segmentos de una misma clase o entrevista en la lista de recortes y expórtalos todos en una sola operación.

---

## Características principales

- **Selección individual o por lotes**: permite agregar uno o varios archivos de video o audio, o escanear una carpeta completa (opcionalmente incluyendo subcarpetas).
- **Formatos admitidos (entrada)**:
  - **Videos**: `.mp4`, `.mkv`, `.webm`, `.avi` y `.mov`.
  - **Audios**: `.mp3`, `.wav`, `.opus`, `.ogg`, `.m4a`, `.flac`, `.aac`, `.wma`, `.aiff`, `.aif`, `.mka`, `.ac3`, `.eac3` y `.mp2`.
- **Carpeta inicial adaptable**: sugiere automáticamente carpetas multimedia estándar del usuario (`Videos`, `Downloads`/`Descargas`, `Music`/`Música` o la carpeta personal).
- **Protección de origen**: si el formato de salida coincide con el archivo de entrada en la misma carpeta, genera automáticamente un nombre numerado (`archivo (1).mp3`) para no sobrescribir el original.
- **Formatos de audio de salida**:
  - **MP3 (`.mp3`)**: recodificación con `libmp3lame` (VBR V0 de alta calidad por defecto, o CBR 320 / 192 / 128 kbps).
  - **Opus (`.opus` / `.ogg`)**: modo inteligente que realiza **copia directa sin pérdida** si el archivo ya contiene audio Opus, o recodifica con `libopus` si viene en otro códec (como AAC o MP3).
  - **WAV (`.wav`)**: audio PCM sin compresión (16-bit o 24-bit), ideal para edición posterior.
  - **M4A (`.m4a`)**: audio AAC (copia directa si ya es AAC o recodificación).
- **Resolución inteligente de incompatibilidades**: si solicitas copiar el stream original (`-c:a copy`) hacia un contenedor que no admite ese códec (por ejemplo, audio Opus hacia `.mp3` o `.m4a`), el programa avisa y permite **recodificar automáticamente**, **cambiar al contenedor compatible** (como `.opus` / `.ogg`) o **preguntar por cada conflicto**.
- **Detección flexible de FFmpeg**: busca automáticamente `ffmpeg`, `ffprobe` y `ffplay` en el `PATH` del sistema y en ubicaciones habituales de Windows (WinGet, Scoop, Chocolatey, `C:\ffmpeg\bin`), además de permitir indicar manualmente la ruta de `ffmpeg.exe` desde la interfaz (guardada localmente en `config.json`).
- **Editor interactivo de audio (Recortar y Separar)**:
  - Carga videos (`.mp4`, `.mkv`, `.webm`, `.avi`, `.mov`) o archivos de audio (`.mp3`, `.opus`, `.ogg`, `.wav`, `.m4a`, `.flac`, `.aac`) con visualización de **forma de onda** y **zoom hasta 32x**.
  - Permite seleccionar el tramo **Inicio / Fin** arrastrando sobre la forma de onda (clic izquierdo para Inicio, clic derecho para Fin), con botones de ajuste fino o capturando el punto durante la **previsualización de audio** (`ffplay`) a velocidades de `1.0x` a `3.0x`.
  - **Copiar y guardar el fragmento** seleccionado en un nuevo archivo `.mp3` (o `.opus`, `.wav`, `.m4a` o copia directa), con ajustes opcionales de **volumen** y **fundidos de entrada/salida**.
  - **Separar la pista** en 2 partes (en el punto de corte) o en 3 partes (antes / selección / después), o **eliminar un fragmento** uniendo el principio con el final.
  - **Lista rápida de múltiples recortes** para extraer varios segmentos de una misma pista de una sola vez.
- **Progreso y registro claro**: barras de progreso por tarea y globales, carpeta de salida configurable (por defecto la misma del archivo original) y registro de eventos en español.

---

## Requisitos e instalación (100 % software libre)

### 1. Python (biblioteca estándar)

La aplicación utiliza únicamente la biblioteca estándar de Python (`tkinter`, `subprocess`, `json`, `threading`, `pathlib`), por lo que **no requiere instalar paquetes adicionales con `pip`**.

- Se recomienda **Python 3.10 o superior** (disponible en [python.org](https://www.python.org/downloads/)). En Windows, asegúrate de que la opción *tcl/tk and IDLE* esté incluida (viene activada por defecto en el instalador oficial).

### 2. FFmpeg (software libre y gratuito)

Necesitas tener `ffmpeg` y `ffprobe` (y `ffplay` para escuchar vistas previas en el editor) en tu equipo. Puedes instalar la versión oficial gratuita de código abierto de cualquiera de estas formas:

- **Windows — Con WinGet (recomendada en Windows 10/11)** desde PowerShell o CMD:

  ```powershell
  winget install Gyan.FFmpeg
  ```

- **Windows — Con Scoop**:

  ```powershell
  scoop install ffmpeg
  ```

- **Windows — Descarga manual portátil (sin modificar el PATH)**:
  1. Descarga el paquete `ffmpeg-release-essentials.zip` desde [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) (o desde [ffmpeg.org](https://ffmpeg.org/download.html)).
  2. Descomprime el archivo en una carpeta (por ejemplo `C:\ffmpeg`).
  3. Abre AudioAula, haz clic en **"Configurar ruta de FFmpeg..."** y selecciona `ffmpeg.exe` dentro de la subcarpeta `bin`. El programa guardará esa ruta automáticamente en `config.json`.

- **GNU/Linux y macOS**: instala el paquete `ffmpeg` desde el gestor de paquetes libre de tu sistema (por ejemplo `sudo apt install ffmpeg`, `sudo dnf install ffmpeg` o `brew install ffmpeg`) junto con `python3-tk` si tu distribución lo separa.

---

## Cómo ejecutar AudioAula

### Opción 1 — Con el ejecutable portátil para Windows (sin usar terminal)

Si descargas `AudioAula.exe` desde la sección **Releases** de GitHub, solo haz doble clic sobre el archivo. No requiere tener Python instalado (únicamente necesita `ffmpeg`/`ffprobe`/`ffplay` en el sistema o configurados desde el botón **"Configurar ruta de FFmpeg..."**).

### Opción 2 — Desde código fuente Python

Clona el repositorio o descárgalo, abre una terminal en la carpeta del proyecto y ejecuta:

```powershell
git clone https://github.com/dmg-cmd/AudioAula.git
cd AudioAula
python app.py
```

Si prefieres ejecutarlo sin mantener una consola visible en Windows:

```powershell
pythonw app.py
```

---

## Cómo usar el Editor (Recortar y Separar Audio)

1. Abre la pestaña **"Editor: Recortar y Separar Audio"** (o haz **doble clic** sobre cualquier video o audio de la cola de lotes, o pulsa **"Editar / Recortar seleccionado..."**).
2. Selecciona un audio (`.mp3`, `.wav`, `.opus`, `.ogg`, `.m4a`, `.flac`, etc.) o video con **"Abrir Audio (MP3, WAV, Opus...) o Video..."** para ver su forma de onda.
3. Navega y haz **zoom** sobre la pista:
   - **Rueda del ratón** (o botones **"🔍+ Acercar"**, **"🔍- Alejar"**, **"🔍 Zoom a Selección"**, **"🔍 Ver Todo (1x)"**) para ampliar hasta **32x** y editar con precisión.
   - **Barra horizontal** debajo de la onda o **Shift + Rueda** para desplazarte cuando hay zoom activo.
4. Mueve el **cursor de ejecución** (amarillo) y escucha el audio:
   - Haz **clic izquierdo** en cualquier punto de la forma de onda (o en la regla superior), usa la barra **"Cursor:"**, escribe en **"Ir a tiempo:"** + **"Mover Cursor"**, o usa los saltos rápidos (**`-15s`**, **`-5s`**, **`+5s`**, **`+15s`**, **`+30s`**) y las flechas **`◀`/`▶`** del teclado (`Shift`: `±15s`).
   - Pulsa **"▶ Escuchar"** / **"⏸ Pausar"** y ajusta la **Velocidad** (`1.0x` a `3.0x`) para recorrer pistas largas rápidamente.
5. Marca el rango de recorte:
   - Posiciona el cursor de ejecución en el tiempo deseado (en pausa, en reproducción o moviéndolo manualmente) y pulsa **"[ Marcar Inicio en Cursor"** o **"Marcar Fin en Cursor ]"**.
   - También puedes arrastrar las manijas **`[`** / **`]`**, usar clic derecho para mover el **Fin**, escribir los tiempos (`MM:SS.cc`) o usar `-1s` / `+1s`.
6. Elige el formato de salida (por defecto **MP3 `libmp3lame`**), calidad, volumen o fundidos opcionales y ejecuta:
   - **"✂ Guardar Selección en Nuevo Archivo"** para copiar solo ese tramo en otro MP3/audio.
   - **"➗ Separar en 2 Partes"** o **"➗ Separar en 3 Partes"** para dividir la pista completa.
   - **"🗑 Eliminar Selección y Unir Resto"** para quitar un tramo intermedio.
   - **"+ Agregar selección a lista"** y **"Exportar todos los fragmentos"** para sacar múltiples recortes en serie.

---

## Compilar ejecutable portable para Windows (.exe)

Si modificas el código fuente y quieres generar `AudioAula.exe` localmente usando **PyInstaller** (software libre), ejecuta en PowerShell desde la carpeta del proyecto:

```powershell
.\compilar_exe.ps1
```

El script verificará o instalará PyInstaller automáticamente y generará `AudioAula.exe` en la carpeta raíz del proyecto (y dentro de `dist/`).

---

## Documentación

- [Índice de documentación (`docs/README.md`)](docs/README.md): estructura del repositorio y guía de publicación de versiones.
- [Manual de Uso para Docentes (`docs/MANUAL_USO.md`)](docs/MANUAL_USO.md): guía paso a paso de conversión por lotes, editor visual de ondas, atajos y solución de problemas.
- [Historial y Guía Técnica (`docs/HISTORIAL.md`)](docs/HISTORIAL.md): cronología de hitos, arquitectura de `app.py` y `converter_core.py`, decisiones de diseño y lineamientos para futuras reformas.

---

## Licencia

Este proyecto se distribuye bajo la **Licencia MIT**. Consulta el archivo [LICENSE](LICENSE) para más detalles.
