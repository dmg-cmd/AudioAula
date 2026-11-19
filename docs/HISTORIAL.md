# Historial del Proyecto y Guía Técnica — AudioAula

Este documento registra la evolución completa de **AudioAula**, detalla su arquitectura interna (`converter_core.py` y `app.py`) y establece pautas de seguridad técnica para futuras reformas.

---

## 1. Cronología e hitos realizados

| Etapa | Hito | Resumen de lo implementado |
| :--- | :--- | :--- |
| **Etapa 1** | **Núcleo de extracción por lotes** | Creación de `converter_core.py` y `app.py` en Python estándar + `tkinter` para extraer audio desde video (`.mp4`, `.mkv`, `.webm`, `.avi`, `.mov`) usando `ffmpeg` y `ffprobe`, con cola de archivos, escaneo de carpetas/subcarpetas, barras de progreso por tarea y global, cancelación segura y protección contra sobrescritura del archivo original. |
| **Etapa 2** | **Multi-formato y resolución inteligente de códecs** | Incorporación de contenedores de salida (`mp3`, `opus`, `ogg`, `wav`, `m4a`), ampliación de formatos de entrada de audio (`.mp3`, `.wav`, `.opus`, `.ogg`, `.m4a`, `.flac`, `.aac`, `.wma`, `.aiff`, `.aif`, `.mka`, `.ac3`, `.eac3`, `.mp2`), modos `auto` / `copy` / `force_recode` y diálogo `ConflictDialog` para resolver incompatibilidades entre códec de origen y contenedor de destino. |
| **Etapa 3** | **Editor visual por forma de onda** | Creación de `WaveformCanvas`, barra de zoom hasta `64x` (rueda y botones), desplazamiento horizontal (`Scrollbar` y `Shift + Rueda`), caché WAV PCM temporal (`prepare_editor_wav_cache`) para precisión exacta, cálculo de picos (`extract_waveform_from_wav`), previsualización con `ffplay` sincronizada por reloj (`_read_ffplay_clock`) y velocidades `1.0x`–`3.0x` (`build_atempo_filter`). |
| **Etapa 4** | **Operaciones de edición y lotes de fragmentos** | Implementación de recorte individual (`trim_and_export_segment`), separación en 2 o 3 partes (`split_audio_track`), eliminación de tramo intermedio con concatenación (`remove_segment_and_join`), filtros de volumen/fundidos (`_build_audio_filter_items`), tabla de múltiples fragmentos (`EditorClipItem`) y atajos de teclado (`◀`/`▶`, `Shift + ◀`/`▶`). |
| **Etapa 5** | **Identidad AudioAula y publicación en GitHub** | Adopción del nombre **AudioAula** con enfoque docente y Licencia MIT; limpieza de rutas personales en `converter_core.py` (`get_default_initial_dir()` portable); actualización de `compilar_exe.ps1`; creación de `.gitignore`, `LICENSE` y `README.md`; primer commit en rama `main` (`ddb94d9`) en `https://github.com/dmg-cmd/AudioAula`. |
| **Etapa 6** | **Icono oficial, documentación y CI/CD Releases** | Procesamiento del icono elegido a `assets/AudioAula.png` (512×512 RGBA con esquinas redondeadas transparentes) y `assets/AudioAula.ico` multi-resolución; integración del icono en `app.py` (`get_asset_path`, `_apply_window_icon`) y `compilar_exe.ps1`; exclusión de borradores `docs/iconos/` en `.gitignore`; creación de `docs/` (`README.md`, `MANUAL_USO.md`, `HISTORIAL.md`) y flujo automático de compilación/lanzamiento en `.github/workflows/release.yml` para la etiqueta `v1.0.0`. |

---

## 2. Arquitectura técnica del código

El proyecto se divide intencionadamente en dos módulos Python principales con separación estricta entre **lógica multimedia** (`converter_core.py`) e **interfaz gráfica** (`app.py`).

### 2.1. `converter_core.py` (Núcleo sin dependencia de GUI)

- **Detección de entorno y binarios**:
  - `get_app_dir()`: resuelve la carpeta base tanto al ejecutarse como script Python como empaquetado con PyInstaller (`sys.frozen`).
  - `get_default_initial_dir()`: busca en `Path.home()` las carpetas estándar `Videos`, `Downloads`, `Descargas`, `Music`, `Música` y usa `Path.home()` como respaldo.
  - `find_ffmpeg_binaries()`: busca en orden: ruta guardada en `config.json`, `PATH` del sistema (`shutil.which`) y ubicaciones habituales en Windows (carpeta local, `C:\ffmpeg\bin`, Chocolatey, WinGet, Scoop). Localiza `ffmpeg`, `ffprobe` y `ffplay`.
- **Configuración persistente**: `load_config()` y `save_config()` leen y actualizan `config.json` en formato UTF-8.
- **Inspección y planificación**:
  - `OUTPUT_FORMATS` y `NATIVE_CONTAINER_BY_CODEC`: tablas declarativas de códecs, calidades y compatibilidad entre contenedores.
  - `probe_media_file()`: ejecuta `ffprobe -of json` y retorna un `MediaInfo` con códec, frecuencia, canales, bitrate, duración y formato contenedor.
  - `resolve_conversion_plan()`: decide según `codec_mode` (`auto`, `copy`, `force_recode`) y `conflict_policy` (`ask`, `recode`, `change_container`) si aplica copia directa, recodificación, cambio de contenedor o conflicto pendiente.
- **Ejecución y seguimiento de progreso**:
  - `_execute_ffmpeg_command()`: lanza `ffmpeg` con `-progress pipe:1`, lee las claves `out_time_us` / `out_time_ms` / `out_time` en tiempo real para reportar porcentaje `[0..100]`, respeta `cancel_event` terminando el proceso limpio y borrando archivos parciales si se cancela o falla.
  - `translate_ffmpeg_error()`: traduce errores frecuentes de FFmpeg a mensajes claros en español.
  - `resolve_output_path()` y `sanitize_filename_stem()`: protegen el archivo origen agregando sufijos numéricos (`archivo (1).ext`) cuando no se habilita sobrescritura o cuando origen y destino coinciden.
- **Caché y operaciones del Editor**:
  - `prepare_editor_wav_cache()`: decodifica la pista `0:a:0` a WAV `pcm_s16le` en carpeta temporal para garantizar sincronía exacta en reproducción y cortes.
  - `extract_waveform_from_wav()` y `extract_waveform_peaks()`: generan la envolvente normalizada `[0.0 .. 1.0]` de picos leyendo bloques del WAV PCM (o vía pipe `s16le` a `8000 Hz` como respaldo).
  - `_build_audio_filter_items()`: arma filtros `-af` para `volume` y `afade` (entrada/salida limitados a 45 % de la duración del tramo para evitar solapamientos).
  - `trim_and_export_segment()`, `split_audio_track()` y `remove_segment_and_join()`: realizan recorte simple (con `-ss/-t` sobre caché PCM o `atrim` en comprimidos), división en 2 o 3 partes y unión mediante filtro `concat` o `trim` si un extremo es menor a `0.05 s`.

### 2.2. `app.py` (Interfaz Tkinter y orquestación)

- **Resolución de recursos y estilo**:
  - `get_asset_path(filename)`: resuelve rutas dentro de `assets/` usando `sys._MEIPASS` cuando corre en modo PyInstaller `--onefile`, o la carpeta del script en desarrollo.
  - `_apply_window_icon()`: aplica `assets/AudioAula.ico` (`iconbitmap` en Windows) y `assets/AudioAula.png` (`tk.PhotoImage` + `iconphoto(True, ...)`, heredado también por diálogos como `ConflictDialog`).
- **Componentes visuales**:
  - `WaveformCanvas(tk.Canvas)`: lienzo con regla temporal dinámica, selección sombreada, dibujo píxel a píxel de picos, marcadores verde (`[`), naranja (`]`) y cursor amarillo, zoom `1x–64x` centrado en puntero/cursor y manejo de arrastre (`playhead`, `start`, `end`).
  - `ConflictDialog(tk.Toplevel)`: diálogo modal centrado en la ventana padre para resolver conflictos de códec por archivo o por resto del lote.
  - `AudioExtractorApp`: gestiona las pestañas de Lotes y Editor, preferencias, menú de FFmpeg, lista de fragmentos `EditorClipItem` y ciclo de cierre seguro (`_on_close()` detiene `ffplay`, guarda `config.json` y limpia `_editor_temp_dir`).
- **Concurrencia segura en Tkinter**:
  - Todas las operaciones bloqueantes (`probe_media_file`, `convert_audio`, `prepare_editor_wav_cache`, `trim_and_export_segment`, etc.) corren en hilos `threading.Thread(daemon=True)`.
  - Los hilos secundarios nunca tocan widgets de Tk directamente: envían tuplas a `self.ui_queue` (`queue.Queue`), procesadas periódicamente en el hilo principal mediante `self.root.after(60, self._process_ui_queue)`.
  - Para consultas interactivas de conflicto desde un hilo de trabajo, usa `threading.Event` coordinado con `ui_queue` para mostrar `ConflictDialog` en el hilo de UI y esperar la respuesta.
- **Reproducción y reloj maestro (`ffplay`)**:
  - `_start_playback_from()` inicia `ffplay -nodisp -autoexit -stats -loglevel quiet` apuntando preferentemente al caché WAV y aplicando `build_atempo_filter()` cuando la velocidad supera `1.0x` (encadenando etapas `atempo=2.0` para llegar hasta `3.0x`).
  - `_read_ffplay_clock()` lee la salida de estado (`M-A:`) carácter a carácter en un hilo lector identificado por generación (`_ffplay_gen`) para evitar condiciones de carrera al hacer *seek* rápido.

---

## 3. Pautas y precauciones para futuras reformas

1. **Cero dependencias externas en tiempo de ejecución**:
   - El programa solo debe usar la biblioteca estándar de Python (`tkinter`, `subprocess`, `json`, `wave`, `array`, `threading`, `pathlib`, etc.). No introducir imports de `Pillow` ni librerías de terceros dentro de `app.py` o `converter_core.py` (Pillow se utilizó únicamente offline para generar los archivos en `assets/`).
2. **Hilo único para Tkinter**:
   - Cualquier nueva operación en segundo plano debe comunicar progreso, logs o diálogos exclusivamente a través de `self.ui_queue`.
3. **Compatibilidad con PyInstaller `--onefile`**:
   - Todo recurso estático nuevo (imágenes, traducciones, iconos) debe guardarse en `assets/` y cargarse con `get_asset_path()`, verificando que tanto `compilar_exe.ps1` como `.github/workflows/release.yml` incluyan `--add-data "assets;assets"`.
4. **Higiene del repositorio y privacidad**:
   - No guardar rutas locales absolutas ni archivos de configuración de usuario (`config.json`).
   - Los binarios compilados (`*.exe`), `build/`, `dist/`, `*.spec` y borradores de diseño (`docs/iconos/`) deben permanecer excluidos en `.gitignore` y publicarse únicamente en **GitHub Releases**.
5. **Subprocesos en Windows**:
   - Mantener `creationflags=_SUBPROCESS_FLAGS` (`CREATE_NO_WINDOW`) en cada llamada a `ffmpeg`, `ffprobe` y `ffplay` para evitar parpadeos de ventanas de consola al ejecutar el `.exe` con `--windowed`.
6. **Versionado y Releases**:
   - Seguir etiquetas `vX.Y.Z` (por ejemplo `v1.0.0`, `v1.1.0`) para que `.github/workflows/release.yml` genere automáticamente el ejecutable y su `.sha256`.

---

## 4. Ideas evaluadas para futuras versiones

- **Normalización de volumen docente (`loudnorm`)**: opción de un clic para nivelar audios de clases con volumen irregular (grabaciones con micrófonos distintos).
- **Atajo de barra espaciadora (`<space>`)**: alternar reproducir/pausar cuando el foco no está en un `ttk.Entry`.
- **Exportación de marcadores / capítulos**: guardar la lista rápida de recortes en un archivo `.txt` o `.json` para reutilizar cortes en futuras ediciones.
- **Empaquetado nativo para GNU/Linux (AppImage / `.deb`) y macOS**: ampliar `.github/workflows/release.yml` con jobs adicionales si crece la demanda fuera de Windows.
