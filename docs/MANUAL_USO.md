# Manual de Uso de AudioAula

Guía completa y práctica para utilizar **AudioAula** en la preparación de materiales sonoros para clases, idiomas, música, podcasts escolares y estudio accesible.

---

## 1. Requisitos previos y puesta en marcha

**AudioAula** es 100 % software libre y gratuito (FOSS). No tiene versiones de prueba ni funciones de pago.

### 1.1. Elegir cómo ejecutar AudioAula

- **Opción A — Ejecutable portátil para Windows (`AudioAula.exe`)**:
  1. Descarga `AudioAula.exe` desde la sección [Releases de GitHub](https://github.com/dmg-cmd/AudioAula/releases).
  2. Haz doble clic sobre el archivo. No necesitas instalar Python.
- **Opción B — Desde código fuente Python (Windows, GNU/Linux o macOS)**:
  1. Instala **Python 3.10 o superior** (en Windows asegúrate de conservar activada la opción *tcl/tk and IDLE*). No hace falta instalar paquetes con `pip`, ya que la aplicación usa únicamente la biblioteca estándar.
  2. Descarga o clona el repositorio y ejecuta:
     ```powershell
     python app.py
     ```
     *(En Windows puedes usar `pythonw app.py` si prefieres no mantener abierta la consola).*

### 1.2. Instalar FFmpeg (motor multimedia libre obligatorio)

AudioAula utiliza **FFmpeg** (`ffmpeg`, `ffprobe` y `ffplay`) para leer metadatos, convertir, recortar y previsualizar audio. Puedes instalarlo gratis con cualquiera de estas opciones:

- **Windows 10/11 con WinGet** *(recomendado)*:
  ```powershell
  winget install Gyan.FFmpeg
  ```
- **Windows con Scoop**:
  ```powershell
  scoop install ffmpeg
  ```
- **Windows portátil (sin permisos de administrador)**:
  1. Descarga `ffmpeg-release-essentials.zip` desde [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) o [ffmpeg.org](https://ffmpeg.org/download.html).
  2. Descomprime la carpeta en una ubicación fija (por ejemplo `C:\ffmpeg`).
  3. Dentro de AudioAula, haz clic en **"Configurar ruta de FFmpeg..."** y selecciona `ffmpeg.exe` dentro de la subcarpeta `bin`. AudioAula buscará automáticamente `ffprobe.exe` y `ffplay.exe` en esa misma carpeta y guardará la ruta en `config.json`.
- **GNU/Linux y macOS**:
  - Debian / Ubuntu: `sudo apt install ffmpeg python3-tk`
  - Fedora: `sudo dnf install ffmpeg python3-tkinter`
  - macOS (Homebrew): `brew install ffmpeg python-tk`

---

## 2. Vista general de la interfaz

La ventana principal se organiza en dos pestañas arriba y dos paneles compartidos en la parte inferior:

1. **Pestaña 1 — Extracción y Conversión por Lotes**: para transformar rápidamente uno o muchos videos/audios a MP3, Opus, Ogg, WAV o M4A.
2. **Pestaña 2 — Editor: Recortar y Separar Audio**: para escuchar, explorar la forma de onda con zoom y obtener fragmentos exactos o dividir una pista larga.
3. **Panel inferior — Carpeta de Destino y Configuración de FFmpeg**: define si los archivos nuevos se guardan junto al original o en una carpeta elegida, qué hacer si el archivo de destino ya existe y muestra el estado de detección de FFmpeg.
4. **Panel inferior — Progreso y Registro de Actividad**: barras de progreso de la tarea actual y del lote completo, botón de cancelación y bitácora detallada de operaciones en español.

---

## 3. Pestaña 1: Extracción y Conversión por Lotes

### 3.1. Formatos de entrada admitidos

- **Videos**: `.mp4`, `.mkv`, `.webm`, `.avi`, `.mov`
- **Audios**: `.mp3`, `.wav`, `.opus`, `.ogg`, `.m4a`, `.flac`, `.aac`, `.wma`, `.aiff`, `.aif`, `.mka`, `.ac3`, `.eac3`, `.mp2`

### 3.2. Agregar archivos y revisar la cola

- **Agregar Archivo(s)...**: selecciona uno o varios archivos individuales.
- **Agregar Carpeta (Lote)...**: escanea una carpeta entera buscando medios compatibles. Marca **"Incluir subcarpetas"** antes de elegir la carpeta si deseas recorrer directorios anidados.
- La tabla muestra **Archivo**, **Origen**, **Códec Audio**, **Duración**, **Salida Prevista** y **Estado**.
- **Enviar al Editor**: haz **doble clic** sobre cualquier fila de la cola (o selecciónala y pulsa **"Editar / Recortar seleccionado..."**) para abrirla de inmediato en la pestaña del Editor.

### 3.3. Formatos de salida y calidades disponibles

| Formato | Códec / Motor | Opciones de calidad | Uso docente recomendado |
| :--- | :--- | :--- | :--- |
| **MP3 (`.mp3`)** | `libmp3lame` | VBR V0 (~245 kbps, por defecto), CBR 320k, 192k, 128k | Máxima compatibilidad en celulares antiguos, reproductores escolares y plataformas educativas. |
| **Opus (`.opus`)** | Copia directa o `libopus` | 160k (por defecto), 128k, 192k, 256k, 96k (Voz/Compacto) | Archivos muy livianos con excelente claridad de voz para enviar por mensajería con poco consumo de datos. |
| **Ogg Opus (`.ogg`)** | Copia directa o `libopus` | 160k (por defecto), 128k, 192k, 256k, 96k | Alternativa estándar abierta compatible con navegadores y lectores libres. |
| **WAV (`.wav`)** | PCM sin compresión | `pcm_s16le` (16-bit CD), `pcm_s24le` (24-bit estudio) | Máxima fidelidad sin compresión para edición posterior o trabajo musical. |
| **M4A (`.m4a`)** | Copia directa o `aac` | 192k (por defecto), 256k, 128k | Compatible con dispositivos Apple/iOS y reproductores multimedia modernos. |

### 3.4. Modos de códec y resolución de incompatibilidades

- **Automático inteligente** *(recomendado)*: en formatos como Opus, Ogg, M4A, MP3 o WAV, realiza **copia directa sin pérdida (`-c:a copy`)** cuando la pista original ya usa un códec compatible con ese contenedor; de lo contrario, recodifica automáticamente.
- **Intentar copiar stream sin pérdida (`-c:a copy`)**: prioriza extraer el audio tal como viene en el video sin recodificar. Si el códec original no cabe en el contenedor elegido (por ejemplo, intentar meter audio Opus en `.mp3`), se aplica la política elegida en **"Si se pide copia (Copy) y el códec es incompatible"**:
  - **Preguntar qué hacer**: abre un diálogo con botones para **Recodificar**, **Cambiar a contenedor compatible sin pérdida** u **Omitir archivo** (con casilla para aplicar la decisión a todo el resto del lote).
  - **Recodificar automáticamente**: convierte al formato elegido y deja aviso en el registro.
  - **Cambiar automáticamente a contenedor compatible**: exporta sin pérdida cambiando la extensión a la nativa del códec (`.opus`, `.ogg`, `.m4a`, `.mp3`, `.flac`, `.wav`, etc.).
- **Forzar recodificación siempre**: vuelve a codificar todas las pistas con el perfil elegido.

### 3.5. Destino y protección del archivo original

- Con **"Guardar en la misma carpeta del archivo original"** marcado, cada audio se crea junto a su archivo fuente.
- En **"Si el archivo existe"**:
  - **Renombrar automáticamente (`archivo (1).ext`)**: evita sobrescribir archivos previos o el propio archivo de entrada cuando el formato coincide.
  - **Sobrescribir archivo existente**: reemplaza el archivo de destino si ya existe (aunque si la ruta de origen y destino es idéntica, el programa igualmente protege el original).

---

## 4. Pestaña 2: Editor Visual (Recortar y Separar Audio)

### 4.1. Cargar una pista y preparar la forma de onda

Pulsa **"Abrir Audio (MP3, WAV, Opus...) o Video..."** (o envía un archivo desde la Pestaña 1 con doble clic).

En segundo plano, AudioAula decodifica la pista a un archivo temporal WAV PCM mediante `prepare_editor_wav_cache()` y calcula la envolvente de picos para dibujar la forma de onda. Trabajar sobre este caché permite **posicionamiento y recorte exactos sin desfases** en archivos comprimidos.

### 4.2. Navegación, zoom y escucha previa

- **Zoom (de `1.0x` hasta `64.0x`)**:
  - Botones: **"🔍+ Acercar"**, **"🔍- Alejar"**, **"🔍 Zoom a Selección"**, **"🔍 Ver Todo (1x)"**.
  - Ratón sobre la forma de onda: gira la **Rueda del ratón** para acercar o alejar centrado en la posición del puntero.
- **Desplazamiento cuando hay zoom**: usa la barra horizontal bajo el lienzo o **`Shift + Rueda del ratón`**.
- **Mover el cursor amarillo de ejecución**:
  - Haz **clic izquierdo** en la regla superior o sobre cualquier zona de la onda (lejos de las manijas verdes/naranjas), o usa el **clic central** del ratón.
  - Arrastra la barra deslizante **"Cursor:"**.
  - Escribe un tiempo (`MM:SS.cc` o `HH:MM:SS.cc` o segundos) en **"Ir a tiempo:"** y pulsa **`Enter`** o **"Mover Cursor"**.
  - Botones de salto rápido: **`-15s`**, **`-5s`**, **`+5s`**, **`+15s`**, **`+30s`**.
- **Escuchar y ajustar velocidad**:
  - Pulsa **"▶ Escuchar"** / **"⏸ Pausar"** y **"⏹ Detener"** (usa `ffplay` en segundo plano).
  - Cambia **"Velocidad:"** entre **`1.0x`**, **`1.25x`**, **`1.5x`**, **`2.0x`** y **`3.0x`** para revisar exposiciones extensas en mucho menos tiempo.

### 4.3. Resumen de atajos de ratón y teclado

| Acción | Control |
| :--- | :--- |
| Mover el cursor amarillo | Clic izquierdo en la onda/regla o clic central |
| Ajustar **Inicio** (verde `[`) o **Fin** (naranja `]`) | Arrastrar la manija `[` o `]` (dentro de `10 px` de la línea) |
| Mover **Fin** rápidamente | Clic derecho (o arrastrar con botón derecho) sobre la onda |
| Acercar / alejar zoom (hasta `64x`) | Rueda del ratón sobre la onda |
| Desplazar vista horizontal con zoom | `Shift + Rueda del ratón` |
| Retroceder / avanzar cursor `5 s` | Teclas **`◀`** / **`▶`** (fuera de cuadros de texto) |
| Retroceder / avanzar cursor `15 s` | **`Shift + ◀`** / **`Shift + ▶`** |
| Aplicar tiempos escritos manualmente | **`Enter`** dentro de los campos *Inicio*, *Fin* o *Ir a tiempo* |

### 4.4. Marcar Inicio y Fin con precisión

- **Durante la escucha o en pausa**: cuando oigas el punto exacto, pulsa **"[ Marcar Inicio en Cursor"** o **"Marcar Fin en Cursor ]"**.
- **Ajuste fino**: usa los botones **`-1s`**, **`-0.1`**, **`+0.1`**, **`+1s`** junto a cada campo, o escribe el tiempo y pulsa **"Aplicar"**.
- **"Todo"**: restablece la selección para abarcar el 100 % de la pista.

### 4.5. Opciones de salida del fragmento

- **Formato salida** y **Calidad / Bitrate**: por defecto exporta en MP3 `libmp3lame` (`V0`), pero admite todos los formatos de salida y la opción **"Copia rápida sin recodificar (`-c:a copy`)"**.
- **Volumen**: `50% (-6 dB)`, `75% (-2.5 dB)`, `100% (Original)`, `125% (+2 dB)`, `150% (+3.5 dB)`, `200% (+6 dB)`.
- **Fundido entrada / Fundido salida**: `0 s` (Sin fundido), `0.5 s`, `1.0 s`, `2.0 s` o `3.0 s`.
  > *Nota*: si seleccionas *Copia rápida (`-c:a copy`)* pero aplicas volumen, fundido o eliminación de fragmento, AudioAula avisará en el registro y recodificará automáticamente porque FFmpeg no permite aplicar filtros ni unir segmentos sin decodificar.
- **Nombre nuevo archivo**: propone un nombre automático con los tiempos (por ejemplo `clase_recorte_01m10s_a_03m45s`). Puedes editarlo libremente o pulsar **"Nombre sugerido"** para regenerarlo.

### 4.6. Acciones de edición disponibles

1. **✂ Guardar Selección en Nuevo Archivo**: exporta únicamente el rango entre **Inicio** y **Fin** (duración mínima `0.05 s`).
2. **➗ Separar en 2 Partes (en Inicio)**: corta la pista completa en el marcador verde **Inicio** generando `<nombre>_parte1` y `<nombre>_parte2`.
3. **➗ Separar en 3 Partes**: divide la pista en `<nombre>_parte1_inicio` (de `0` a Inicio), `<nombre>_parte2_seleccion` (de Inicio a Fin) y `<nombre>_parte3_final` (de Fin al final). Omite automáticamente tramos extremos menores a `0.05 s`.
4. **🗑 Eliminar Selección y Unir Resto**: quita el tramo comprendido entre **Inicio** y **Fin** (por ejemplo un error, ruido o silencio largo) y concatena el principio con el final en `<nombre>_sin_fragmento`.

### 4.7. Lista rápida de múltiples recortes

Cuando necesitas sacar varios ejercicios o cápsulas de un mismo video o audio largo:

1. Marca el primer tramo y pulsa **"+ Agregar selección a lista"**.
2. Repite para cada fragmento adicional que quieras extraer (puedes asignarles nombres distintos antes de agregarlos).
3. Si te equivocas en alguno, selecciónalo en la tabla inferior y pulsa **"Quitar fragmento"** (o **"Limpiar fragmentos"**).
4. Pulsa **"Exportar todos los fragmentos"** para procesarlos en serie de una sola vez.

---

## 5. Flujos recomendados en el aula

- **Comprensión auditiva en idiomas**: carga un diálogo largo, acelera la escucha a `2.0x` para ubicar cada pregunta, marca Inicio/Fin en el cursor, añade un fundido de `0.5 s` en entrada/salida y usa la **lista rápida de múltiples recortes** para crear `dialogo_pregunta1.mp3`, `dialogo_pregunta2.mp3`, etc.
- **Compartir clases grabadas por WhatsApp / aula virtual**: en la Pestaña 1 elige **Opus (`.opus`)** a **`96k` (Voz / Compacto)** o **MP3** a **`128k`** para reducir drásticamente el tamaño de un video MP4 de clase manteniendo la voz clara.
- **Limpiar interrupciones de una grabación**: marca el intervalo de una pausa o interrupción y pulsa **"🗑 Eliminar Selección y Unir Resto"**.

---

## 6. Configuración persistente y solución de problemas

### Archivo `config.json`

En la carpeta junto a `app.py` o `AudioAula.exe`, AudioAula guarda automáticamente tus últimas preferencias (`default_format`, `codec_mode`, `conflict_policy`, `overwrite_mode`, `same_folder`, `custom_dest_folder`, `editor_format` y, si la configuraste manualmente, `ffmpeg_path`). Si alguna vez quieres restaurar los valores iniciales, basta con cerrar la app y borrar `config.json`.

### Problemas frecuentes

- **"FFmpeg no detectado" o "ffplay no disponible"**:
  - Si acabas de instalar FFmpeg con `winget`, cierra y vuelve a abrir AudioAula (o la terminal) para que tome el nuevo `PATH`, o pulsa **"Detectar de nuevo"**.
  - Si descargaste una versión portátil, usa **"Configurar ruta de FFmpeg..."** y elige `ffmpeg.exe`. Asegúrate de que `ffprobe.exe` y `ffplay.exe` estén en la misma carpeta `bin`.
- **"Windows protegió su PC" (SmartScreen) al abrir `AudioAula.exe`**:
  - Es habitual en ejecutables independientes de software libre que no cuentan con firma comercial de pago. Si descargaste el archivo desde la página oficial de [Releases](https://github.com/dmg-cmd/AudioAula/releases), puedes comprobar su integridad comparando su hash con el archivo `.sha256` adjunto:
    ```powershell
    Get-FileHash .\AudioAula.exe -Algorithm SHA256
    ```
  - Luego haz clic en **"Más información" -> "Ejecutar de todas formas"** (o ejecuta desde código fuente con `python app.py`).
- **La carpeta de trabajo del editor se ve desfasada en archivos VBR antiguos**:
  - Espera unos segundos tras abrir el archivo hasta que termine de generarse el caché WAV en la parte superior; una vez cargado el caché PCM, tanto la previsualización como los cortes operan con precisión al milisegundo.
