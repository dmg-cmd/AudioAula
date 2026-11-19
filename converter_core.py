"""Núcleo de extracción y conversión de audio desde archivos de video usando FFmpeg.

Este módulo gestiona:
- Detección automática o configurada de los binarios `ffmpeg` y `ffprobe`.
- Escaneo de archivos y carpetas con soporte para .mp4, .mkv, .webm, .avi y .mov.
- Inspección de pistas de audio y duración mediante `ffprobe`.
- Resolución inteligente de compatibilidad entre códecs de audio y contenedores.
- Ejecución de `ffmpeg` con seguimiento de progreso en tiempo real y cancelación.
"""

from __future__ import annotations

import array
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


def get_app_dir() -> Path:
    """Devuelve la carpeta base de la aplicación (soporta ejecución como script o .exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# Extensiones de video y audio admitidas (en minúsculas)
SUPPORTED_VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".webm", ".avi", ".mov")
SUPPORTED_AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".mp3",
    ".wav",
    ".opus",
    ".ogg",
    ".m4a",
    ".flac",
    ".aac",
    ".wma",
    ".aiff",
    ".aif",
    ".mka",
    ".ac3",
    ".eac3",
    ".mp2",
)
SUPPORTED_MEDIA_EXTENSIONS: tuple[str, ...] = (
    SUPPORTED_VIDEO_EXTENSIONS + SUPPORTED_AUDIO_EXTENSIONS
)
SUPPORTED_EDITOR_EXTENSIONS: tuple[str, ...] = SUPPORTED_MEDIA_EXTENSIONS

# Archivo de configuración persistente junto al script o ejecutable
CONFIG_FILE_PATH: Path = get_app_dir() / "config.json"

# Contenedores de salida disponibles en la aplicación
OUTPUT_FORMATS: dict[str, dict[str, object]] = {
    "mp3": {
        "label": "MP3 (.mp3) - libmp3lame",
        "extension": ".mp3",
        "encoder": "libmp3lame",
        "compatible_source_codecs": {"mp3"},
        "default_quality": "v0",
        "qualities": [
            ("v0", "VBR Alta Calidad V0 (~245 kbps - Recomendado)"),
            ("320k", "CBR 320 kbps (Máxima)"),
            ("192k", "CBR 192 kbps (Estándar)"),
            ("128k", "CBR 128 kbps (Compacto)"),
        ],
    },
    "opus": {
        "label": "Opus (.opus) - Copia directa o libopus",
        "extension": ".opus",
        "encoder": "libopus",
        "compatible_source_codecs": {"opus"},
        "default_quality": "160k",
        "qualities": [
            ("160k", "160 kbps (Alta calidad - Por defecto)"),
            ("128k", "128 kbps (Recomendado)"),
            ("192k", "192 kbps (Muy alta)"),
            ("256k", "256 kbps (Máxima)"),
            ("96k", "96 kbps (Voz / Compacto)"),
        ],
    },
    "ogg": {
        "label": "Ogg Opus (.ogg) - Copia directa o libopus",
        "extension": ".ogg",
        "encoder": "libopus",
        "compatible_source_codecs": {"opus", "vorbis", "flac"},
        "default_quality": "160k",
        "qualities": [
            ("160k", "160 kbps (Alta calidad - Por defecto)"),
            ("128k", "128 kbps (Recomendado)"),
            ("192k", "192 kbps (Muy alta)"),
            ("256k", "256 kbps (Máxima)"),
            ("96k", "96 kbps (Voz / Compacto)"),
        ],
    },
    "wav": {
        "label": "WAV (.wav) - PCM sin compresión (Edición)",
        "extension": ".wav",
        "encoder": "pcm_s16le",
        "compatible_source_codecs": {
            "pcm_s16le",
            "pcm_s24le",
            "pcm_s32le",
            "pcm_f32le",
            "pcm_u8",
        },
        "default_quality": "pcm_s16le",
        "qualities": [
            ("pcm_s16le", "PCM 16-bit (Estándar CD / Edición)"),
            ("pcm_s24le", "PCM 24-bit (Estudio)"),
        ],
    },
    "m4a": {
        "label": "M4A (.m4a) - Audio AAC",
        "extension": ".m4a",
        "encoder": "aac",
        "compatible_source_codecs": {"aac", "alac", "mp3"},
        "default_quality": "192k",
        "qualities": [
            ("192k", "192 kbps (Alta calidad)"),
            ("256k", "256 kbps (Máxima)"),
            ("128k", "128 kbps (Estándar)"),
        ],
    },
}

# Mapeo de códec de origen hacia el contenedor nativo ideal para copia sin pérdida
NATIVE_CONTAINER_BY_CODEC: dict[str, tuple[str, str]] = {
    "opus": (".opus", "Opus nativo (.opus)"),
    "vorbis": (".ogg", "Ogg Vorbis (.ogg)"),
    "aac": (".m4a", "Contenedor MPEG-4 Audio (.m4a)"),
    "alac": (".m4a", "Contenedor Apple Lossless (.m4a)"),
    "mp3": (".mp3", "MP3 nativo (.mp3)"),
    "mp2": (".mp2", "MPEG Audio Layer II (.mp2)"),
    "flac": (".flac", "FLAC nativo (.flac)"),
    "ac3": (".ac3", "Dolby AC-3 (.ac3)"),
    "eac3": (".eac3", "Dolby E-AC-3 (.eac3)"),
    "wmav1": (".wma", "Windows Media Audio (.wma)"),
    "wmav2": (".wma", "Windows Media Audio (.wma)"),
    "wmapro": (".wma", "Windows Media Audio (.wma)"),
    "pcm_s16le": (".wav", "WAV PCM (.wav)"),
    "pcm_s24le": (".wav", "WAV PCM (.wav)"),
    "pcm_s32le": (".wav", "WAV PCM (.wav)"),
    "pcm_f32le": (".wav", "WAV PCM (.wav)"),
    "pcm_u8": (".wav", "WAV PCM (.wav)"),
}

# Parámetros de creación de subprocesos para ocultar la consola en Windows
_SUBPROCESS_FLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class FFmpegLocation:
    """Resultado de la localización de los ejecutables de FFmpeg."""

    ffmpeg_path: Optional[str] = None
    ffprobe_path: Optional[str] = None
    ffplay_path: Optional[str] = None
    source: str = "no_encontrado"  # 'config', 'path', 'auto', 'no_encontrado'

    @property
    def is_ready(self) -> bool:
        """Indica si al menos ffmpeg fue localizado correctamente."""
        return bool(self.ffmpeg_path and Path(self.ffmpeg_path).is_file())

    @property
    def has_ffprobe(self) -> bool:
        """Indica si ffprobe está disponible."""
        return bool(self.ffprobe_path and Path(self.ffprobe_path).is_file())

    @property
    def has_ffplay(self) -> bool:
        """Indica si ffplay está disponible para previsualizar audio."""
        return bool(self.ffplay_path and Path(self.ffplay_path).is_file())


@dataclass
class MediaInfo:
    """Información técnica extraída de un archivo multimedia."""

    file_path: Path
    has_audio: bool
    audio_codec: Optional[str] = None
    audio_codec_long: Optional[str] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    bit_rate: Optional[int] = None
    duration_seconds: Optional[float] = None
    container_format: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def formatted_duration(self) -> str:
        """Devuelve la duración formateada como MM:SS o HH:MM:SS."""
        if self.duration_seconds is None or self.duration_seconds < 0:
            return "--:--"
        total = int(round(self.duration_seconds))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


@dataclass
class ConversionPlan:
    """Plan resuelto de conversión o copia para un archivo específico."""

    target_format_key: str
    output_extension: str
    codec_args: list[str]
    is_stream_copy: bool
    action_description: str
    has_conflict: bool = False
    conflict_reason: Optional[str] = None
    suggested_extension: Optional[str] = None
    suggested_container_desc: Optional[str] = None
    warning_message: Optional[str] = None
    resolution_applied: str = "direct"  # 'direct', 'auto_copy', 'auto_recode', 'changed_container', 'conflict_pending'


@dataclass
class ConversionResult:
    """Resultado de la ejecución de conversión para un archivo."""

    success: bool
    cancelled: bool = False
    input_path: Optional[Path] = None
    output_path: Optional[Path] = None
    plan: Optional[ConversionPlan] = None
    error_message: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def load_config(config_path: Path = CONFIG_FILE_PATH) -> dict[str, object]:
    """Carga la configuración persistente desde config.json."""
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict[str, object], config_path: Path = CONFIG_FILE_PATH) -> bool:
    """Guarda la configuración persistente en config.json."""
    try:
        current = load_config(config_path)
        current.update(data)
        with config_path.open("w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


def get_default_initial_dir() -> str:
    """Devuelve una carpeta multimedia estándar del usuario que exista en el equipo."""
    home = Path.home()
    for folder_name in ("Videos", "Downloads", "Descargas", "Music", "Música"):
        candidate = home / folder_name
        if candidate.is_dir():
            return str(candidate)
    return str(home)


def _find_companion_tool(ffmpeg_exe: str, tool_name: str) -> Optional[str]:
    """Busca ffprobe o ffplay en el mismo directorio que ffmpeg o en el PATH."""
    ffmpeg_dir = Path(ffmpeg_exe).resolve().parent
    exe_name = f"{tool_name}.exe" if os.name == "nt" else tool_name
    sibling = ffmpeg_dir / exe_name
    if sibling.is_file():
        return str(sibling)
    return shutil.which(tool_name)


def _build_ffmpeg_location(ffmpeg_exe: str, source: str) -> FFmpegLocation:
    """Construye una instancia completa de FFmpegLocation a partir de ffmpeg_exe."""
    return FFmpegLocation(
        ffmpeg_path=ffmpeg_exe,
        ffprobe_path=_find_companion_tool(ffmpeg_exe, "ffprobe"),
        ffplay_path=_find_companion_tool(ffmpeg_exe, "ffplay"),
        source=source,
    )


def find_ffmpeg_binaries(custom_ffmpeg_path: Optional[str] = None) -> FFmpegLocation:
    """Localiza ffmpeg, ffprobe y ffplay en configuración, PATH o rutas habituales de Windows."""
    # 1. Revisar ruta personalizada (parámetro explícito o config.json)
    configured = custom_ffmpeg_path
    if not configured:
        cfg = load_config()
        raw_cfg = cfg.get("ffmpeg_path")
        if isinstance(raw_cfg, str) and raw_cfg.strip():
            configured = raw_cfg.strip()

    if configured:
        candidate = Path(configured)
        if candidate.is_dir():
            exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            candidate = candidate / exe_name
        if candidate.is_file():
            return _build_ffmpeg_location(str(candidate), "config")

    # 2. Revisar PATH del sistema
    in_path = shutil.which("ffmpeg")
    if in_path and Path(in_path).is_file():
        return _build_ffmpeg_location(in_path, "path")

    # 3. Búsqueda automática en ubicaciones comunes (Windows y local)
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    app_dir = get_app_dir()
    candidate_paths: list[Path] = [
        app_dir / exe_name,
        app_dir / "bin" / exe_name,
        app_dir / "ffmpeg" / "bin" / exe_name,
    ]

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        user_profile = os.environ.get("USERPROFILE", "")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")

        static_dirs = [
            Path(r"C:\ffmpeg\bin"),
            Path(program_files) / "ffmpeg" / "bin",
            Path(program_data) / "chocolatey" / "bin",
        ]
        if local_app_data:
            static_dirs.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Links")
        if user_profile:
            static_dirs.append(Path(user_profile) / "scoop" / "shims")
            static_dirs.append(Path(user_profile) / "scoop" / "apps" / "ffmpeg" / "current" / "bin")

        for folder in static_dirs:
            candidate_paths.append(folder / exe_name)

        if local_app_data:
            winget_pattern = str(
                Path(local_app_data)
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "Gyan.FFmpeg*"
                / "ffmpeg-*"
                / "bin"
                / exe_name
            )
            for match in sorted(glob.glob(winget_pattern), reverse=True):
                candidate_paths.append(Path(match))

            winget_pattern_alt = str(
                Path(local_app_data)
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "*FFmpeg*"
                / "**"
                / exe_name
            )
            for match in sorted(glob.glob(winget_pattern_alt, recursive=True), reverse=True):
                candidate_paths.append(Path(match))

    for candidate in candidate_paths:
        if candidate.is_file():
            return _build_ffmpeg_location(str(candidate), "auto")

    return FFmpegLocation(
        ffmpeg_path=None, ffprobe_path=None, ffplay_path=None, source="no_encontrado"
    )


def collect_video_files_from_folder(
    folder_path: Path | str,
    recursive: bool = False,
    include_audio: bool = True,
) -> list[Path]:
    """Recopila los archivos de video y audio compatibles dentro de una carpeta."""
    base = Path(folder_path)
    if not base.is_dir():
        return []

    allowed = SUPPORTED_MEDIA_EXTENSIONS if include_audio else SUPPORTED_VIDEO_EXTENSIONS
    files: list[Path] = []
    if recursive:
        for root, _, filenames in os.walk(base):
            for name in sorted(filenames):
                path = Path(root) / name
                if path.suffix.lower() in allowed:
                    files.append(path)
    else:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and entry.suffix.lower() in allowed:
                files.append(entry)
    return files


def probe_media_file(
    file_path: Path | str,
    ffprobe_path: Optional[str] = None,
) -> MediaInfo:
    """Inspecciona un archivo de video con ffprobe para conocer su códec de audio y duración."""
    path = Path(file_path)
    if not path.is_file():
        return MediaInfo(
            file_path=path,
            has_audio=False,
            error_message="El archivo no existe o fue movido.",
        )

    probe_bin = ffprobe_path
    if not probe_bin:
        loc = find_ffmpeg_binaries()
        probe_bin = loc.ffprobe_path

    if not probe_bin or not Path(probe_bin).is_file():
        return MediaInfo(
            file_path=path,
            has_audio=False,
            error_message="No se encontró ffprobe para analizar el archivo.",
        )

    cmd = [
        probe_bin,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_SUBPROCESS_FLAGS,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return MediaInfo(
            file_path=path,
            has_audio=False,
            error_message="Tiempo de espera agotado al inspeccionar el archivo con ffprobe.",
        )
    except OSError as exc:
        return MediaInfo(
            file_path=path,
            has_audio=False,
            error_message=f"No se pudo ejecutar ffprobe: {exc}",
        )

    if proc.returncode != 0:
        return MediaInfo(
            file_path=path,
            has_audio=False,
            error_message="ffprobe no pudo leer el archivo. Puede estar dañado o tener un formato inválido.",
        )

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return MediaInfo(
            file_path=path,
            has_audio=False,
            error_message="Respuesta inesperada de ffprobe al analizar el archivo.",
        )

    streams = payload.get("streams", [])
    fmt = payload.get("format", {})

    audio_stream: Optional[dict[str, object]] = None
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            audio_stream = stream
            break

    if audio_stream is None:
        return MediaInfo(
            file_path=path,
            has_audio=False,
            container_format=str(fmt.get("format_name") or ""),
            error_message="El archivo de video no contiene ninguna pista de audio.",
        )

    codec_name = str(audio_stream.get("codec_name") or "").lower().strip() or None
    codec_long = str(audio_stream.get("codec_long_name") or "").strip() or None

    duration: Optional[float] = None
    for raw_dur in (audio_stream.get("duration"), fmt.get("duration")):
        if raw_dur is not None:
            try:
                val = float(raw_dur)
                if val > 0:
                    duration = val
                    break
            except (TypeError, ValueError):
                pass

    sample_rate: Optional[int] = None
    if audio_stream.get("sample_rate") is not None:
        try:
            sample_rate = int(audio_stream["sample_rate"])
        except (TypeError, ValueError):
            pass

    channels: Optional[int] = None
    if audio_stream.get("channels") is not None:
        try:
            channels = int(audio_stream["channels"])
        except (TypeError, ValueError):
            pass

    bit_rate: Optional[int] = None
    for raw_br in (audio_stream.get("bit_rate"), fmt.get("bit_rate")):
        if raw_br is not None:
            try:
                bit_rate = int(raw_br)
                break
            except (TypeError, ValueError):
                pass

    return MediaInfo(
        file_path=path,
        has_audio=True,
        audio_codec=codec_name,
        audio_codec_long=codec_long,
        sample_rate=sample_rate,
        channels=channels,
        bit_rate=bit_rate,
        duration_seconds=duration,
        container_format=str(fmt.get("format_name") or ""),
    )


def build_encode_args(format_key: str, quality_key: str) -> list[str]:
    """Construye los argumentos de recodificación de audio para FFmpeg."""
    if format_key == "mp3":
        if quality_key == "v0":
            return ["-c:a", "libmp3lame", "-q:a", "0"]
        return ["-c:a", "libmp3lame", "-b:a", quality_key]

    if format_key in ("opus", "ogg"):
        return ["-c:a", "libopus", "-b:a", quality_key, "-vbr", "on"]

    if format_key == "wav":
        pcm_codec = quality_key if quality_key in ("pcm_s16le", "pcm_s24le") else "pcm_s16le"
        return ["-c:a", pcm_codec]

    if format_key == "m4a":
        return ["-c:a", "aac", "-b:a", quality_key]

    raise ValueError(f"Formato de salida no soportado: {format_key}")


def get_compatible_container_for_codec(source_codec: Optional[str]) -> tuple[str, str]:
    """Devuelve la extensión y descripción de un contenedor compatible para copia directa."""
    if not source_codec:
        return (".mka", "Matroska Audio universal (.mka)")
    codec = source_codec.lower().strip()
    if codec in NATIVE_CONTAINER_BY_CODEC:
        return NATIVE_CONTAINER_BY_CODEC[codec]
    return (".mka", f"Matroska Audio (.mka, compatible con {codec})")


def resolve_conversion_plan(
    source_codec: Optional[str],
    target_format_key: str,
    quality_key: str,
    codec_mode: str = "auto",
    conflict_policy: str = "ask",
) -> ConversionPlan:
    """Resuelve cómo procesar el audio según el códec de origen, el formato y la política de conflictos.

    Parámetros:
        source_codec: Códec de audio detectado en el video (ej. 'opus', 'aac', 'mp3').
        target_format_key: Clave en OUTPUT_FORMATS ('mp3', 'opus', 'ogg', 'wav', 'm4a').
        quality_key: Clave de calidad dentro del formato elegido.
        codec_mode: 'auto' (inteligente), 'copy' (copia sin pérdida), 'force_recode' (siempre recodificar).
        conflict_policy: En modo 'copy' cuando es incompatible: 'ask', 'recode', 'change_container'.
    """
    if target_format_key not in OUTPUT_FORMATS:
        raise ValueError(f"Formato de salida desconocido: {target_format_key}")

    fmt_spec = OUTPUT_FORMATS[target_format_key]
    target_ext = str(fmt_spec["extension"])
    compatible_codecs: set[str] = set(fmt_spec["compatible_source_codecs"])  # type: ignore[arg-type]
    normalized_source = source_codec.lower().strip() if source_codec else None

    # Modo 1: Forzar recodificación siempre
    if codec_mode == "force_recode":
        args = build_encode_args(target_format_key, quality_key)
        return ConversionPlan(
            target_format_key=target_format_key,
            output_extension=target_ext,
            codec_args=args,
            is_stream_copy=False,
            action_description=f"Recodificar a {target_format_key.upper()} ({args[1]})",
            resolution_applied="direct",
        )

    # Modo 2: Automático inteligente
    if codec_mode == "auto":
        # Para Opus / Ogg / M4A se aprovecha copia sin pérdida si el códec ya es compatible
        if (
            target_format_key in ("opus", "ogg", "m4a")
            and normalized_source
            and normalized_source in compatible_codecs
        ):
            return ConversionPlan(
                target_format_key=target_format_key,
                output_extension=target_ext,
                codec_args=["-c:a", "copy"],
                is_stream_copy=True,
                action_description=(
                    f"Copia directa sin pérdida (el origen ya es {normalized_source.upper()})"
                ),
                resolution_applied="auto_copy",
            )

        # En MP3, WAV o cuando el origen trae otro códec (ej. AAC -> Opus), se recodifica
        args = build_encode_args(target_format_key, quality_key)
        origin_label = normalized_source.upper() if normalized_source else "desconocido"
        return ConversionPlan(
            target_format_key=target_format_key,
            output_extension=target_ext,
            codec_args=args,
            is_stream_copy=False,
            action_description=(
                f"Recodificar de {origin_label} a {target_format_key.upper()} ({args[1]})"
            ),
            resolution_applied="direct",
        )

    # Modo 3: Intentar copiar stream sin pérdida (-c:a copy)
    if codec_mode == "copy":
        if normalized_source and normalized_source in compatible_codecs:
            return ConversionPlan(
                target_format_key=target_format_key,
                output_extension=target_ext,
                codec_args=["-c:a", "copy"],
                is_stream_copy=True,
                action_description=(
                    f"Copia directa sin pérdida ({normalized_source.upper()} -> {target_ext})"
                ),
                resolution_applied="direct",
            )

        # Se detectó incompatibilidad entre el códec de origen y el contenedor de salida
        suggested_ext, suggested_desc = get_compatible_container_for_codec(normalized_source)
        origin_label = normalized_source.upper() if normalized_source else "desconocido"
        conflict_reason = (
            f"El contenedor '{target_ext}' no admite copiar directamente el códec de audio "
            f"'{origin_label}' sin recodificar."
        )

        if conflict_policy == "change_container":
            warning = (
                f"Aviso de compatibilidad: {conflict_reason} "
                f"Se cambió automáticamente la salida al contenedor '{suggested_ext}' "
                f"({suggested_desc}) para conservar la copia directa sin pérdida (-c:a copy)."
            )
            return ConversionPlan(
                target_format_key=target_format_key,
                output_extension=suggested_ext,
                codec_args=["-c:a", "copy"],
                is_stream_copy=True,
                action_description=f"Copia sin pérdida en contenedor compatible ({suggested_ext})",
                has_conflict=True,
                conflict_reason=conflict_reason,
                suggested_extension=suggested_ext,
                suggested_container_desc=suggested_desc,
                warning_message=warning,
                resolution_applied="changed_container",
            )

        if conflict_policy == "recode":
            args = build_encode_args(target_format_key, quality_key)
            warning = (
                f"Aviso de compatibilidad: {conflict_reason} "
                f"Se aplicó recodificación automática con '{args[1]}' para generar '{target_ext}'."
            )
            return ConversionPlan(
                target_format_key=target_format_key,
                output_extension=target_ext,
                codec_args=args,
                is_stream_copy=False,
                action_description=f"Recodificación automática a {target_format_key.upper()} ({args[1]})",
                has_conflict=True,
                conflict_reason=conflict_reason,
                suggested_extension=suggested_ext,
                suggested_container_desc=suggested_desc,
                warning_message=warning,
                resolution_applied="auto_recode",
            )

        # conflict_policy == "ask": retornar el conflicto pendiente para que la UI consulte al usuario
        return ConversionPlan(
            target_format_key=target_format_key,
            output_extension=target_ext,
            codec_args=["-c:a", "copy"],
            is_stream_copy=True,
            action_description=f"Conflicto: {origin_label} no es compatible con {target_ext}",
            has_conflict=True,
            conflict_reason=conflict_reason,
            suggested_extension=suggested_ext,
            suggested_container_desc=suggested_desc,
            resolution_applied="conflict_pending",
        )

    raise ValueError(f"Modo de códec desconocido: {codec_mode}")


def sanitize_filename_stem(stem: str) -> str:
    """Limpia caracteres no válidos en nombres de archivo de Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip().rstrip(". ")
    return cleaned or "audio_editado"


def resolve_output_path(
    input_file: Path,
    extension: str,
    output_dir: Optional[Path | str] = None,
    overwrite: bool = False,
    custom_stem: Optional[str] = None,
) -> Path:
    """Calcula la ruta final de salida evitando colisiones y auto-sobrescritura."""
    ext = extension if extension.startswith(".") else f".{extension}"
    target_dir = Path(output_dir) if output_dir else input_file.parent
    stem = sanitize_filename_stem(custom_stem) if custom_stem else input_file.stem
    candidate = target_dir / f"{stem}{ext}"

    try:
        same_as_input = candidate.resolve() == input_file.resolve()
    except OSError:
        same_as_input = str(candidate).lower() == str(input_file).lower()

    if not same_as_input and (overwrite or not candidate.exists()):
        return candidate

    counter = 1
    while True:
        numbered = target_dir / f"{stem} ({counter}){ext}"
        try:
            numbered_same = numbered.resolve() == input_file.resolve()
        except OSError:
            numbered_same = False
        if not numbered_same and not numbered.exists():
            return numbered
        counter += 1


def translate_ffmpeg_error(stderr_text: str, return_code: int) -> str:
    """Traduce errores comunes de FFmpeg a mensajes claros en español neutro."""
    lower = stderr_text.lower()

    if "does not contain any stream" in lower or "output file does not contain any stream" in lower:
        return "El archivo de video no contiene una pista de audio utilizable."
    if "no such file or directory" in lower:
        return "No se pudo encontrar el archivo de entrada o la carpeta de destino."
    if "permission denied" in lower:
        return "Permiso denegado al intentar leer el video o escribir el archivo de salida."
    if "no space left on device" in lower:
        return "No hay espacio suficiente en el disco de destino."
    if "invalid data found when processing input" in lower:
        return "El archivo de entrada está dañado o contiene datos inválidos."
    if "unknown encoder" in lower or "encoder" in lower and "not found" in lower:
        return "Tu instalación de FFmpeg no incluye el codificador de audio requerido."
    if "could not write header" in lower or "muxer" in lower and "not support" in lower:
        return (
            "El contenedor de salida no admite el códec de audio seleccionado. "
            "Prueba recodificar o usar un contenedor compatible."
        )

    # Extraer última línea significativa de stderr como contexto
    lines = [ln.strip() for ln in stderr_text.splitlines() if ln.strip()]
    tail = lines[-1] if lines else f"Código de salida {return_code}"
    return f"FFmpeg finalizó con un error (código {return_code}): {tail}"


def _parse_out_time_seconds(line: str) -> Optional[float]:
    """Interpreta líneas de tiempo de `-progress pipe:1` de FFmpeg en segundos."""
    if line.startswith("out_time_us="):
        raw = line.split("=", 1)[1].strip()
        if raw.isdigit():
            return int(raw) / 1_000_000.0
    elif line.startswith("out_time_ms="):
        # Nota: en algunas versiones de FFmpeg out_time_ms en -progress también está en microsegundos
        raw = line.split("=", 1)[1].strip()
        if raw.isdigit():
            return int(raw) / 1_000_000.0
    elif line.startswith("out_time="):
        raw = line.split("=", 1)[1].strip()
        match = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", raw)
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    return None


def _execute_ffmpeg_command(
    cmd: list[str],
    out_path: Path,
    duration_seconds: Optional[float] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> tuple[bool, bool, Optional[str]]:
    """Ejecuta un comando FFmpeg con lectura de progreso, cancelación y manejo de errores.

    Retorna una tupla `(exito, cancelado, mensaje_error)`.
    """
    stderr_lines: list[str] = []

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_SUBPROCESS_FLAGS,
        )
    except OSError as exc:
        return (False, False, f"No se pudo iniciar FFmpeg: {exc}")

    def _drain_stderr() -> None:
        if process.stderr is None:
            return
        for err_line in process.stderr:
            stderr_lines.append(err_line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    cancelled = False
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    process.terminate()
                    break

                line = raw_line.strip()
                if not line:
                    continue

                if duration_seconds and duration_seconds > 0 and progress_callback:
                    elapsed = _parse_out_time_seconds(line)
                    if elapsed is not None:
                        pct = min(99.5, max(0.0, (elapsed / duration_seconds) * 100.0))
                        progress_callback(pct)
                if line == "progress=end" and progress_callback:
                    progress_callback(100.0)

        while process.poll() is None:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                if cancelled:
                    process.kill()
                    break

        stderr_thread.join(timeout=2.0)
    except Exception as exc:
        try:
            process.kill()
        except OSError:
            pass
        return (False, False, f"Error inesperado durante la operación con FFmpeg: {exc}")

    if cancelled:
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        return (False, True, "Operación cancelada por el usuario.")

    if process.returncode != 0:
        try:
            if out_path.exists() and out_path.stat().st_size == 0:
                out_path.unlink()
        except OSError:
            pass
        friendly_error = translate_ffmpeg_error("".join(stderr_lines), process.returncode or 1)
        return (False, False, friendly_error)

    if progress_callback:
        progress_callback(100.0)

    return (True, False, None)


def convert_audio(
    input_path: Path | str,
    plan: ConversionPlan,
    ffmpeg_path: str,
    output_dir: Optional[Path | str] = None,
    overwrite: bool = False,
    duration_seconds: Optional[float] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ConversionResult:
    """Ejecuta FFmpeg para extraer o convertir el audio de un video según el plan resuelto."""
    src = Path(input_path)
    warnings: list[str] = []

    if plan.warning_message:
        warnings.append(plan.warning_message)
        if log_callback:
            log_callback("AVISO", plan.warning_message)

    if not src.is_file():
        return ConversionResult(
            success=False,
            input_path=src,
            plan=plan,
            error_message=f"El archivo de origen no existe: {src}",
            warnings=warnings,
        )

    target_dir = Path(output_dir) if output_dir else src.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ConversionResult(
            success=False,
            input_path=src,
            plan=plan,
            error_message=f"No se pudo crear la carpeta de destino '{target_dir}': {exc}",
            warnings=warnings,
        )

    out_path = resolve_output_path(
        input_file=src,
        extension=plan.output_extension,
        output_dir=target_dir,
        overwrite=overwrite,
    )

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0",
        *plan.codec_args,
        "-progress",
        "pipe:1",
        "-nostats",
        str(out_path),
    ]

    if log_callback:
        log_callback("INFO", f"Ejecutando: {src.name} -> {out_path.name} ({plan.action_description})")

    ok, cancelled, err_msg = _execute_ffmpeg_command(
        cmd=cmd,
        out_path=out_path,
        duration_seconds=duration_seconds,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )

    return ConversionResult(
        success=ok,
        cancelled=cancelled,
        input_path=src,
        output_path=out_path,
        plan=plan,
        error_message=err_msg,
        warnings=warnings,
    )


def format_timestamp(seconds: float, include_ms: bool = True) -> str:
    """Formatea una marca temporal en segundos como MM:SS.cc o HH:MM:SS.cc."""
    clamped = max(0.0, float(seconds))
    total_centis = int(round(clamped * 100.0))
    total_secs, centis = divmod(total_centis, 100)
    hours, rem = divmod(total_secs, 3600)
    minutes, secs = divmod(rem, 60)

    if include_ms:
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}.{centis:02d}"
        return f"{minutes:02d}:{secs:02d}.{centis:02d}"

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_timestamp_for_filename(seconds: float) -> str:
    """Formatea una marca de tiempo segura para usar dentro de nombres de archivo."""
    clamped = max(0.0, float(seconds))
    total_secs = int(round(clamped))
    hours, rem = divmod(total_secs, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}h{minutes:02d}m{secs:02d}s"
    return f"{minutes:02d}m{secs:02d}s"


def parse_timestamp(text: str) -> float:
    """Interpreta una marca temporal ('90', '1:30', '01:30.50', '00:01:30.500') en segundos."""
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        raise ValueError("El campo de tiempo está vacío.")

    parts = cleaned.split(":")
    try:
        if len(parts) == 1:
            value = float(parts[0])
        elif len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            if minutes < 0 or seconds < 0:
                raise ValueError
            value = minutes * 60.0 + seconds
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            if hours < 0 or minutes < 0 or seconds < 0:
                raise ValueError
            value = hours * 3600.0 + minutes * 60.0 + seconds
        else:
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            f"Formato de tiempo no válido: '{text}'. Usa MM:SS.cc, HH:MM:SS.cc o segundos."
        ) from exc

    if value < 0:
        raise ValueError("El tiempo no puede ser negativo.")
    return value


def prepare_editor_wav_cache(
    input_path: Path | str,
    ffmpeg_path: str,
    output_wav_path: Path | str,
) -> bool:
    """Decodifica la pista de audio principal a un WAV PCM temporal exacto para el editor."""
    src = Path(input_path)
    out_wav = Path(output_wav_path)
    if not src.is_file():
        return False

    try:
        out_wav.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            creationflags=_SUBPROCESS_FLAGS,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    return proc.returncode == 0 and out_wav.is_file() and out_wav.stat().st_size > 44


def extract_waveform_from_wav(
    wav_path: Path | str,
    points_per_sec: int = 100,
    min_buckets: int = 4000,
    max_buckets: int = 120000,
) -> tuple[list[float], float]:
    """Lee un WAV PCM de 16 bits y retorna `(picos_normalizados, duracion_exacta_seg)`."""
    path = Path(wav_path)
    if not path.is_file():
        return ([], 0.0)

    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sr = wf.getframerate()
            nframes = wf.getnframes()

            if channels <= 0 or sr <= 0 or nframes <= 0 or sampwidth != 2:
                return ([], 0.0)

            exact_duration = nframes / float(sr)
            buckets = int(
                min(max_buckets, max(min_buckets, round(exact_duration * points_per_sec)))
            )
            buckets = max(20, buckets)
            peaks: list[float] = [0.0] * buckets

            chunk_frames = max(sr * 10, 65536)
            frame_cursor = 0

            while frame_cursor < nframes:
                raw = wf.readframes(chunk_frames)
                if not raw:
                    break
                usable_len = (len(raw) // (2 * channels)) * (2 * channels)
                if usable_len <= 0:
                    break

                samples = array.array("h")
                samples.frombytes(raw[:usable_len])
                if sys.byteorder != "little":
                    samples.byteswap()

                frames_in_chunk = len(samples) // channels
                chunk_end = frame_cursor + frames_in_chunk

                b_start = max(0, (frame_cursor * buckets) // nframes)
                b_end = min(buckets - 1, ((chunk_end - 1) * buckets) // nframes)

                for b in range(b_start, b_end + 1):
                    f0 = (b * nframes) // buckets
                    f1 = max(f0 + 1, ((b + 1) * nframes) // buckets)
                    local_f0 = max(0, max(f0, frame_cursor) - frame_cursor)
                    local_f1 = min(frames_in_chunk, min(f1, chunk_end) - frame_cursor)
                    rel0 = local_f0 * channels
                    rel1 = local_f1 * channels
                    if rel1 <= rel0:
                        rel0 = min(max(0, len(samples) - channels), rel0)
                        rel1 = min(len(samples), rel0 + channels)
                    if rel1 > rel0:
                        seg = samples[rel0:rel1]
                        chunk_peak = max(abs(min(seg)), abs(max(seg)))
                        if chunk_peak > peaks[b]:
                            peaks[b] = float(chunk_peak)

                frame_cursor = chunk_end
    except (OSError, wave.Error, EOFError):
        return ([], 0.0)

    max_peak = max(peaks, default=0.0)
    if max_peak <= 0.0:
        return ([0.0] * len(peaks), exact_duration)

    return ([min(1.0, p / max_peak) for p in peaks], exact_duration)


def extract_waveform_peaks(
    file_path: Path | str,
    ffmpeg_path: str,
    num_buckets: int = 4000,
) -> list[float]:
    """Extrae una envolvente normalizada de picos de audio (0.0 a 1.0) para dibujar la forma de onda."""
    buckets = max(20, int(num_buckets))
    src = Path(file_path)
    if not src.is_file():
        return [0.0] * buckets

    if src.suffix.lower() == ".wav":
        wav_peaks, exact_dur = extract_waveform_from_wav(
            src,
            points_per_sec=100,
            min_buckets=buckets,
            max_buckets=max(buckets, 120000),
        )
        if exact_dur > 0.0 and wav_peaks:
            return wav_peaks

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "s16le",
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            creationflags=_SUBPROCESS_FLAGS,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [0.0] * buckets

    if proc.returncode != 0 or not proc.stdout:
        return [0.0] * buckets

    raw = proc.stdout
    usable_len = (len(raw) // 2) * 2
    if usable_len < 2:
        return [0.0] * buckets

    samples = array.array("h")
    samples.frombytes(raw[:usable_len])
    if sys.byteorder != "little":
        samples.byteswap()

    total_samples = len(samples)
    if total_samples == 0:
        return [0.0] * buckets

    peaks: list[float] = []
    for i in range(buckets):
        start_idx = (i * total_samples) // buckets
        end_idx = ((i + 1) * total_samples) // buckets
        if end_idx <= start_idx:
            end_idx = min(total_samples, start_idx + 1)
        bucket_slice = samples[start_idx:end_idx]
        peak = max(abs(min(bucket_slice)), abs(max(bucket_slice)))
        peaks.append(float(peak))

    max_peak = max(peaks, default=0.0)
    if max_peak <= 0.0:
        return [0.0] * buckets

    return [min(1.0, p / max_peak) for p in peaks]


def _build_audio_filter_items(
    volume_factor: float = 1.0,
    fade_in_sec: float = 0.0,
    fade_out_sec: float = 0.0,
    segment_duration: Optional[float] = None,
) -> list[str]:
    """Construye los elementos de filtro de audio (-af) para volumen y fundidos."""
    filters: list[str] = []
    if abs(volume_factor - 1.0) > 1e-3:
        filters.append(f"volume={max(0.05, volume_factor):.3f}")
    if fade_in_sec > 0.01:
        dur_in = fade_in_sec
        if segment_duration and segment_duration > 0.05:
            dur_in = min(fade_in_sec, segment_duration * 0.45)
        filters.append(f"afade=t=in:st=0:d={dur_in:.3f}")
    if fade_out_sec > 0.01 and segment_duration and segment_duration > 0.05:
        dur_out = min(fade_out_sec, segment_duration * 0.45)
        start_out = max(0.0, segment_duration - dur_out)
        filters.append(f"afade=t=out:st={start_out:.3f}:d={dur_out:.3f}")
    return filters


def _resolve_editor_codec_and_ext(
    src: Path,
    source_codec: Optional[str],
    target_format_key: str,
    quality_key: str,
    requires_reencode: bool,
) -> tuple[str, list[str], bool, Optional[str]]:
    """Resuelve extensión, argumentos de códec y posible aviso para operaciones de edición."""
    if target_format_key == "copy":
        if not requires_reencode:
            src_ext = src.suffix.lower()
            # Si ya es un archivo de audio compatible con su propia extensión, conservarla
            for fmt_spec in OUTPUT_FORMATS.values():
                if src_ext == fmt_spec["extension"]:
                    compat: set[str] = set(fmt_spec["compatible_source_codecs"])  # type: ignore[arg-type]
                    if source_codec and source_codec.lower() in compat:
                        return (src_ext, ["-c:a", "copy"], True, None)
            if src_ext in SUPPORTED_AUDIO_EXTENSIONS and src_ext not in (
                ".mp3",
                ".opus",
                ".ogg",
                ".wav",
                ".m4a",
            ):
                return (src_ext, ["-c:a", "copy"], True, None)
            native_ext, _ = get_compatible_container_for_codec(source_codec)
            return (native_ext, ["-c:a", "copy"], True, None)

        # Se requieren filtros o concatenación: recodificar al equivalente compatible (por defecto MP3)
        fallback_fmt = "mp3"
        src_ext = src.suffix.lower()
        for fmt_key, fmt_spec in OUTPUT_FORMATS.items():
            if src_ext == fmt_spec["extension"]:
                fallback_fmt = fmt_key
                break
        fallback_spec = OUTPUT_FORMATS[fallback_fmt]
        fallback_ext = str(fallback_spec["extension"])
        fallback_q = str(fallback_spec["default_quality"])
        args = build_encode_args(fallback_fmt, fallback_q)
        warning = (
            f"Aviso de edición: la copia directa (-c:a copy) no es compatible con filtros de "
            f"volumen, fundido o unión de segmentos. Se recodificó automáticamente a '{fallback_ext}' ({args[1]})."
        )
        return (fallback_ext, args, False, warning)

    if target_format_key not in OUTPUT_FORMATS:
        raise ValueError(f"Formato de salida no soportado: {target_format_key}")

    fmt_spec = OUTPUT_FORMATS[target_format_key]
    ext = str(fmt_spec["extension"])
    q = quality_key or str(fmt_spec["default_quality"])
    args = build_encode_args(target_format_key, q)
    return (ext, args, False, None)


def trim_and_export_segment(
    input_path: Path | str,
    ffmpeg_path: str,
    start_sec: float,
    end_sec: float,
    target_format_key: str = "mp3",
    quality_key: str = "v0",
    output_dir: Optional[Path | str] = None,
    custom_stem: Optional[str] = None,
    overwrite: bool = False,
    source_codec: Optional[str] = None,
    volume_factor: float = 1.0,
    fade_in_sec: float = 0.0,
    fade_out_sec: float = 0.0,
    pcm_source_path: Optional[Path | str] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ConversionResult:
    """Recorta un tramo [start_sec, end_sec] del audio y lo guarda en un nuevo archivo."""
    src = Path(input_path)
    if not src.is_file():
        return ConversionResult(
            success=False,
            input_path=src,
            error_message=f"El archivo de origen no existe: {src}",
        )

    start = max(0.0, float(start_sec))
    end = float(end_sec)
    seg_duration = end - start
    if seg_duration < 0.05:
        return ConversionResult(
            success=False,
            input_path=src,
            error_message="El tramo seleccionado debe durar al menos 0.05 segundos.",
        )

    filter_items = _build_audio_filter_items(
        volume_factor=volume_factor,
        fade_in_sec=fade_in_sec,
        fade_out_sec=fade_out_sec,
        segment_duration=seg_duration,
    )
    has_filters = bool(filter_items)

    ext, codec_args, is_copy, warning = _resolve_editor_codec_and_ext(
        src=src,
        source_codec=source_codec,
        target_format_key=target_format_key,
        quality_key=quality_key,
        requires_reencode=has_filters,
    )

    warnings: list[str] = []
    if warning:
        warnings.append(warning)
        if log_callback:
            log_callback("AVISO", warning)

    target_dir = Path(output_dir) if output_dir else src.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ConversionResult(
            success=False,
            input_path=src,
            error_message=f"No se pudo crear la carpeta de destino '{target_dir}': {exc}",
            warnings=warnings,
        )

    default_stem = (
        f"{src.stem}_recorte_{format_timestamp_for_filename(start)}_a_{format_timestamp_for_filename(end)}"
    )
    out_path = resolve_output_path(
        input_file=src,
        extension=ext,
        output_dir=target_dir,
        overwrite=overwrite,
        custom_stem=custom_stem or default_stem,
    )

    use_pcm = (
        (not is_copy)
        and pcm_source_path is not None
        and Path(pcm_source_path).is_file()
    )

    if is_copy:
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{start:.4f}",
            "-i",
            str(src),
            "-t",
            f"{seg_duration:.4f}",
            "-vn",
            "-sn",
            "-dn",
            "-map",
            "0:a:0",
            "-avoid_negative_ts",
            "make_zero",
            *codec_args,
            "-progress",
            "pipe:1",
            "-nostats",
            str(out_path),
        ]
    elif use_pcm:
        # El caché WAV PCM permite saltar al byte exacto de la muestra de forma instantánea
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{start:.4f}",
            "-t",
            f"{seg_duration:.4f}",
            "-i",
            str(Path(pcm_source_path)),  # type: ignore[arg-type]
            "-vn",
            "-sn",
            "-dn",
            "-map",
            "0:a:0",
        ]
        if filter_items:
            cmd.extend(["-af", ",".join(filter_items)])
        cmd.extend(
            [
                *codec_args,
                "-progress",
                "pipe:1",
                "-nostats",
                str(out_path),
            ]
        )
    else:
        # Sobre archivos comprimidos directos, atrim recorta al sample exacto tras decodificar
        af_chain = [
            f"atrim=start={start:.4f}:end={end:.4f}",
            "asetpts=PTS-STARTPTS",
            *filter_items,
        ]
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-sn",
            "-dn",
            "-map",
            "0:a:0",
            "-af",
            ",".join(af_chain),
            *codec_args,
            "-progress",
            "pipe:1",
            "-nostats",
            str(out_path),
        ]

    plan = ConversionPlan(
        target_format_key=target_format_key,
        output_extension=ext,
        codec_args=codec_args,
        is_stream_copy=is_copy,
        action_description=f"Recorte {format_timestamp(start)} - {format_timestamp(end)}",
        warning_message=warning,
    )

    if log_callback:
        log_callback(
            "INFO",
            f"Recortando [{format_timestamp(start)} -> {format_timestamp(end)}] hacia '{out_path.name}'...",
        )

    ok, cancelled, err_msg = _execute_ffmpeg_command(
        cmd=cmd,
        out_path=out_path,
        duration_seconds=seg_duration,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )

    return ConversionResult(
        success=ok,
        cancelled=cancelled,
        input_path=src,
        output_path=out_path,
        plan=plan,
        error_message=err_msg,
        warnings=warnings,
    )


def split_audio_track(
    input_path: Path | str,
    ffmpeg_path: str,
    total_duration: float,
    mode: str = "two_parts",
    cut_sec: float = 0.0,
    start_sec: float = 0.0,
    end_sec: float = 0.0,
    target_format_key: str = "mp3",
    quality_key: str = "v0",
    output_dir: Optional[Path | str] = None,
    overwrite: bool = False,
    source_codec: Optional[str] = None,
    volume_factor: float = 1.0,
    fade_in_sec: float = 0.0,
    fade_out_sec: float = 0.0,
    pcm_source_path: Optional[Path | str] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[ConversionResult]:
    """Separa la pista de audio en 2 partes (en cut_sec) o 3 partes (antes, selección, después)."""
    src = Path(input_path)
    if total_duration <= 0.1:
        return [
            ConversionResult(
                success=False,
                input_path=src,
                error_message="No se conoce la duración total del archivo para separarlo.",
            )
        ]

    segments: list[tuple[float, float, str]] = []
    if mode == "two_parts":
        c = max(0.0, min(total_duration, float(cut_sec)))
        if c < 0.05 or (total_duration - c) < 0.05:
            return [
                ConversionResult(
                    success=False,
                    input_path=src,
                    error_message="El punto de corte debe estar dentro de la pista (dejando al menos 0.05 s por lado).",
                )
            ]
        segments = [
            (0.0, c, f"{src.stem}_parte1"),
            (c, total_duration, f"{src.stem}_parte2"),
        ]
    elif mode == "three_parts":
        s = max(0.0, min(total_duration, float(start_sec)))
        e = max(s, min(total_duration, float(end_sec)))
        if (e - s) < 0.05:
            return [
                ConversionResult(
                    success=False,
                    input_path=src,
                    error_message="La selección central debe durar al menos 0.05 segundos.",
                )
            ]
        candidates = [
            (0.0, s, f"{src.stem}_parte1_inicio"),
            (s, e, f"{src.stem}_parte2_seleccion"),
            (e, total_duration, f"{src.stem}_parte3_final"),
        ]
        segments = [(a, b, name) for a, b, name in candidates if (b - a) >= 0.05]
    else:
        return [
            ConversionResult(
                success=False,
                input_path=src,
                error_message=f"Modo de separación desconocido: {mode}",
            )
        ]

    results: list[ConversionResult] = []
    count = len(segments)
    for idx, (seg_start, seg_end, stem_name) in enumerate(segments):
        if cancel_event and cancel_event.is_set():
            break

        def _part_progress(pct: float, part_index: int = idx) -> None:
            if progress_callback:
                overall = ((part_index + (pct / 100.0)) / count) * 100.0
                progress_callback(overall)

        res = trim_and_export_segment(
            input_path=src,
            ffmpeg_path=ffmpeg_path,
            start_sec=seg_start,
            end_sec=seg_end,
            target_format_key=target_format_key,
            quality_key=quality_key,
            output_dir=output_dir,
            custom_stem=stem_name,
            overwrite=overwrite,
            source_codec=source_codec,
            volume_factor=volume_factor,
            fade_in_sec=fade_in_sec,
            fade_out_sec=fade_out_sec,
            pcm_source_path=pcm_source_path,
            progress_callback=_part_progress,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        results.append(res)
        if not res.success:
            break

    return results


def remove_segment_and_join(
    input_path: Path | str,
    ffmpeg_path: str,
    total_duration: float,
    start_sec: float,
    end_sec: float,
    target_format_key: str = "mp3",
    quality_key: str = "v0",
    output_dir: Optional[Path | str] = None,
    custom_stem: Optional[str] = None,
    overwrite: bool = False,
    source_codec: Optional[str] = None,
    volume_factor: float = 1.0,
    fade_in_sec: float = 0.0,
    fade_out_sec: float = 0.0,
    pcm_source_path: Optional[Path | str] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ConversionResult:
    """Elimina el tramo [start_sec, end_sec] y une las partes restantes en un nuevo archivo."""
    src = Path(input_path)
    if not src.is_file():
        return ConversionResult(
            success=False,
            input_path=src,
            error_message=f"El archivo de origen no existe: {src}",
        )

    if total_duration <= 0.1:
        return ConversionResult(
            success=False,
            input_path=src,
            error_message="No se conoce la duración total del archivo.",
        )

    s = max(0.0, min(total_duration, float(start_sec)))
    e = max(s, min(total_duration, float(end_sec)))
    if (e - s) < 0.05:
        return ConversionResult(
            success=False,
            input_path=src,
            error_message="El segmento a eliminar debe durar al menos 0.05 segundos.",
        )

    keep_left = s > 0.05
    keep_right = (total_duration - e) > 0.05

    if not keep_left and not keep_right:
        return ConversionResult(
            success=False,
            input_path=src,
            error_message="La selección cubre todo el archivo; no quedaría audio restante.",
        )

    stem = custom_stem or f"{src.stem}_sin_fragmento"
    if not keep_left:
        return trim_and_export_segment(
            input_path=src,
            ffmpeg_path=ffmpeg_path,
            start_sec=e,
            end_sec=total_duration,
            target_format_key=target_format_key,
            quality_key=quality_key,
            output_dir=output_dir,
            custom_stem=stem,
            overwrite=overwrite,
            source_codec=source_codec,
            volume_factor=volume_factor,
            fade_in_sec=fade_in_sec,
            fade_out_sec=fade_out_sec,
            pcm_source_path=pcm_source_path,
            progress_callback=progress_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )

    if not keep_right:
        return trim_and_export_segment(
            input_path=src,
            ffmpeg_path=ffmpeg_path,
            start_sec=0.0,
            end_sec=s,
            target_format_key=target_format_key,
            quality_key=quality_key,
            output_dir=output_dir,
            custom_stem=stem,
            overwrite=overwrite,
            source_codec=source_codec,
            volume_factor=volume_factor,
            fade_in_sec=fade_in_sec,
            fade_out_sec=fade_out_sec,
            pcm_source_path=pcm_source_path,
            progress_callback=progress_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )

    resulting_duration = s + (total_duration - e)
    ext, codec_args, _, warning = _resolve_editor_codec_and_ext(
        src=src,
        source_codec=source_codec,
        target_format_key=target_format_key,
        quality_key=quality_key,
        requires_reencode=True,
    )

    warnings: list[str] = []
    if warning:
        warnings.append(warning)
        if log_callback:
            log_callback("AVISO", warning)

    target_dir = Path(output_dir) if output_dir else src.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ConversionResult(
            success=False,
            input_path=src,
            error_message=f"No se pudo crear la carpeta de destino '{target_dir}': {exc}",
            warnings=warnings,
        )

    out_path = resolve_output_path(
        input_file=src,
        extension=ext,
        output_dir=target_dir,
        overwrite=overwrite,
        custom_stem=stem,
    )

    audio_input = (
        Path(pcm_source_path)
        if pcm_source_path is not None and Path(pcm_source_path).is_file()
        else src
    )

    extra_filters = _build_audio_filter_items(
        volume_factor=volume_factor,
        fade_in_sec=fade_in_sec,
        fade_out_sec=fade_out_sec,
        segment_duration=resulting_duration,
    )
    filter_chain = (
        f"[0:a:0]atrim=0:{s:.4f},asetpts=PTS-STARTPTS[a1];"
        f"[0:a:0]atrim=start={e:.4f},asetpts=PTS-STARTPTS[a2];"
        f"[a1][a2]concat=n=2:v=0:a=1"
    )
    if extra_filters:
        filter_chain += "[joined];[joined]" + ",".join(extra_filters) + "[outa]"
    else:
        filter_chain += "[outa]"

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(audio_input),
        "-filter_complex",
        filter_chain,
        "-map",
        "[outa]",
        *codec_args,
        "-progress",
        "pipe:1",
        "-nostats",
        str(out_path),
    ]

    plan = ConversionPlan(
        target_format_key=target_format_key,
        output_extension=ext,
        codec_args=codec_args,
        is_stream_copy=False,
        action_description=f"Eliminar [{format_timestamp(s)} - {format_timestamp(e)}] y unir",
        warning_message=warning,
    )

    if log_callback:
        log_callback(
            "INFO",
            f"Eliminando tramo [{format_timestamp(s)} -> {format_timestamp(e)}] y uniendo en '{out_path.name}'...",
        )

    ok, cancelled, err_msg = _execute_ffmpeg_command(
        cmd=cmd,
        out_path=out_path,
        duration_seconds=resulting_duration,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )

    return ConversionResult(
        success=ok,
        cancelled=cancelled,
        input_path=src,
        output_path=out_path,
        plan=plan,
        error_message=err_msg,
        warnings=warnings,
    )
