"""AudioAula: interfaz gráfica de escritorio para extraer, convertir y editar audio con FFmpeg."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

from converter_core import (
    OUTPUT_FORMATS,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_MEDIA_EXTENSIONS,
    SUPPORTED_VIDEO_EXTENSIONS,
    ConversionPlan,
    FFmpegLocation,
    MediaInfo,
    collect_video_files_from_folder,
    convert_audio,
    extract_waveform_from_wav,
    extract_waveform_peaks,
    find_ffmpeg_binaries,
    format_timestamp,
    format_timestamp_for_filename,
    get_default_initial_dir,
    load_config,
    parse_timestamp,
    prepare_editor_wav_cache,
    probe_media_file,
    remove_segment_and_join,
    resolve_conversion_plan,
    save_config,
    split_audio_track,
    trim_and_export_segment,
)


CODEC_MODE_OPTIONS: list[tuple[str, str]] = [
    (
        "auto",
        "Automático inteligente (En Opus/M4A copia sin pérdida si es compatible; si no, recodifica)",
    ),
    ("copy", "Intentar copiar stream sin pérdida (-c:a copy)"),
    ("force_recode", "Forzar recodificación siempre"),
]

CONFLICT_POLICY_OPTIONS: list[tuple[str, str]] = [
    ("ask", "Preguntar qué hacer (Ofrecer recodificar o cambiar contenedor)"),
    ("recode", "Recodificar automáticamente al formato elegido (y avisar en el log)"),
    (
        "change_container",
        "Cambiar automáticamente a contenedor compatible (.opus / .ogg / .m4a / .mp3) sin perder calidad",
    ),
]

OVERWRITE_OPTIONS: list[tuple[str, str]] = [
    ("rename", "Renombrar automáticamente (archivo (1).ext)"),
    ("overwrite", "Sobrescribir archivo existente"),
]

EDITOR_VOLUME_OPTIONS: list[tuple[float, str]] = [
    (1.0, "100% (Original)"),
    (1.25, "125% (+2 dB)"),
    (1.5, "150% (+3.5 dB)"),
    (2.0, "200% (+6 dB)"),
    (0.75, "75% (-2.5 dB)"),
    (0.5, "50% (-6 dB)"),
]

EDITOR_FADE_OPTIONS: list[tuple[float, str]] = [
    (0.0, "0 s (Sin fundido)"),
    (0.5, "0.5 s"),
    (1.0, "1.0 s"),
    (2.0, "2.0 s"),
    (3.0, "3.0 s"),
]

EDITOR_SPEED_OPTIONS: list[tuple[float, str]] = [
    (1.0, "1.0x (Normal)"),
    (1.25, "1.25x"),
    (1.5, "1.5x (Rápido)"),
    (2.0, "2.0x (Muy rápido)"),
    (3.0, "3.0x (Ultra rápido)"),
]

EDITOR_COPY_LABEL: str = "Copia rápida sin recodificar (-c:a copy)"
_SUBPROCESS_FLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def build_atempo_filter(speed: float) -> Optional[str]:
    """Construye la cadena de filtros `atempo` de FFmpeg para la velocidad indicada."""
    s = float(speed)
    if abs(s - 1.0) < 1e-3:
        return None
    stages: list[str] = []
    remaining = max(0.25, s)
    while remaining > 2.0 + 1e-6:
        stages.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-6:
        stages.append("atempo=0.5")
        remaining /= 0.5
    stages.append(f"atempo={remaining:.2f}".rstrip("0").rstrip("."))
    return ",".join(stages)


@dataclass
class QueueItem:
    """Representa un archivo de video en la cola de procesamiento."""

    item_id: str
    path: Path
    media_info: Optional[MediaInfo] = None
    status: str = "Analizando..."


@dataclass
class EditorClipItem:
    """Representa un fragmento guardado en la lista de múltiples recortes del editor."""

    clip_id: str
    start_sec: float
    end_sec: float
    output_stem: str


class WaveformCanvas(tk.Canvas):
    """Lienzo interactivo con zoom para visualizar la forma de onda, el cursor y el rango Inicio/Fin."""

    def __init__(
        self,
        parent: tk.Misc,
        on_range_changed: Optional[Callable[[float, float], None]] = None,
        on_seek_requested: Optional[Callable[[float], None]] = None,
        on_view_changed: Optional[Callable[[float, float, float], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            height=135,
            bg="#0F172A",
            highlightthickness=1,
            highlightbackground="#334155",
            cursor="crosshair",
        )
        self._on_range_changed = on_range_changed
        self._on_seek_requested = on_seek_requested
        self._on_view_changed = on_view_changed
        self.peaks: list[float] = []
        self.duration: float = 0.0
        self.start_sec: float = 0.0
        self.end_sec: float = 0.0
        self.playhead_sec: Optional[float] = None
        self.zoom_factor: float = 1.0
        self.view_start_sec: float = 0.0
        self.view_end_sec: float = 0.0
        self._active_drag: Optional[str] = None
        self._pad_x: int = 12
        self._ruler_h: int = 22

        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._on_left_press)
        self.bind("<B1-Motion>", self._on_left_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Button-2>", self._on_middle_action)
        self.bind("<B2-Motion>", self._on_middle_action)
        self.bind("<ButtonRelease-2>", self._on_release)
        self.bind("<Button-3>", self._on_right_action)
        self.bind("<B3-Motion>", self._on_right_action)
        self.bind("<ButtonRelease-3>", self._on_release)
        self.bind("<MouseWheel>", self._on_mouse_wheel)

    def _notify_view(self) -> None:
        if self._on_view_changed:
            vs, ve = self._visible_window()
            self._on_view_changed(vs, ve, self.zoom_factor)

    def _visible_window(self) -> tuple[float, float]:
        if self.duration <= 0.0:
            return (0.0, 0.0)
        span = max(0.05, self.duration / max(1.0, self.zoom_factor))
        if span >= self.duration - 1e-6:
            return (0.0, self.duration)
        vs = max(0.0, min(self.duration - span, self.view_start_sec))
        return (vs, min(self.duration, vs + span))

    def set_audio_data(self, peaks: list[float], duration: float) -> None:
        """Carga nuevos picos de onda y reinicia zoom, cursor y selección."""
        self.peaks = peaks
        self.duration = max(0.0, float(duration))
        self.start_sec = 0.0
        self.end_sec = self.duration
        self.zoom_factor = 1.0
        self.view_start_sec = 0.0
        self.view_end_sec = self.duration
        self.playhead_sec = 0.0 if self.duration > 0.0 else None
        self.redraw()
        self._notify_view()

    def set_range(self, start_sec: float, end_sec: float, notify: bool = False) -> None:
        """Actualiza los marcadores de inicio y fin respetando límites válidos."""
        if self.duration <= 0.05:
            self.start_sec = 0.0
            self.end_sec = 0.0
        else:
            s = max(0.0, min(self.duration - 0.05, float(start_sec)))
            e = max(s + 0.05, min(self.duration, float(end_sec)))
            self.start_sec = s
            self.end_sec = e
        self.redraw()
        if notify and self._on_range_changed:
            self._on_range_changed(self.start_sec, self.end_sec)

    def set_playhead(self, position_sec: Optional[float]) -> None:
        """Mueve o oculta el cursor amarillo de reproducción actual."""
        self.playhead_sec = position_sec
        self.redraw()

    def set_zoom(self, factor: float, center_sec: Optional[float] = None) -> None:
        """Ajusta el nivel de zoom (1x a 64x) centrado alrededor de center_sec."""
        if self.duration <= 0.05:
            return
        new_zoom = max(1.0, min(64.0, float(factor)))
        vs, ve = self._visible_window()
        anchor = (
            center_sec
            if center_sec is not None
            else (self.playhead_sec if self.playhead_sec is not None else (vs + ve) / 2.0)
        )
        anchor = max(0.0, min(self.duration, anchor))
        if vs <= anchor <= ve and (ve - vs) > 1e-6:
            rel = (anchor - vs) / (ve - vs)
        else:
            rel = 0.5

        new_span = max(0.05, self.duration / new_zoom)
        new_vs = max(0.0, min(self.duration - new_span, anchor - rel * new_span))
        self.zoom_factor = new_zoom
        self.view_start_sec = new_vs
        self.view_end_sec = min(self.duration, new_vs + new_span)
        self.redraw()
        self._notify_view()

    def zoom_to_selection(self) -> None:
        """Acerca el zoom para encuadrar el tramo seleccionado entre Inicio y Fin."""
        if self.duration <= 0.05:
            return
        sel_span = max(0.05, self.end_sec - self.start_sec)
        padded = sel_span * 1.10
        target_zoom = min(64.0, max(1.0, self.duration / padded))
        center = (self.start_sec + self.end_sec) / 2.0
        new_span = max(0.05, self.duration / target_zoom)
        new_vs = max(0.0, min(self.duration - new_span, center - new_span / 2.0))
        self.zoom_factor = target_zoom
        self.view_start_sec = new_vs
        self.view_end_sec = min(self.duration, new_vs + new_span)
        self.redraw()
        self._notify_view()

    def pan_to_fraction(self, first_fraction: float) -> None:
        """Desplaza la ventana visible según una fracción del total (0.0 a 1.0)."""
        if self.duration <= 0.05 or self.zoom_factor <= 1.001:
            return
        span = max(0.05, self.duration / self.zoom_factor)
        new_vs = max(0.0, min(self.duration - span, float(first_fraction) * self.duration))
        self.view_start_sec = new_vs
        self.view_end_sec = min(self.duration, new_vs + span)
        self.redraw()
        self._notify_view()

    def scroll_view(self, delta_pages: float) -> None:
        """Desplaza la vista horizontalmente en fracciones de la ventana visible actual."""
        if self.duration <= 0.05 or self.zoom_factor <= 1.001:
            return
        span = max(0.05, self.duration / self.zoom_factor)
        new_vs = max(0.0, min(self.duration - span, self.view_start_sec + delta_pages * span))
        self.view_start_sec = new_vs
        self.view_end_sec = min(self.duration, new_vs + span)
        self.redraw()
        self._notify_view()

    def ensure_time_visible(self, t: float) -> None:
        """Desplaza la vista si el tiempo t queda fuera de la ventana visible con zoom."""
        if self.duration <= 0.05 or self.zoom_factor <= 1.001:
            return
        vs, ve = self._visible_window()
        if vs <= t <= ve:
            return
        span = max(0.05, ve - vs)
        if t < vs:
            new_vs = max(0.0, t - span * 0.15)
        else:
            new_vs = min(self.duration - span, t - span * 0.85)
        self.view_start_sec = max(0.0, new_vs)
        self.view_end_sec = min(self.duration, self.view_start_sec + span)
        self.redraw()
        self._notify_view()

    def _x_to_time(self, x: float) -> float:
        vs, ve = self._visible_window()
        width = max(1, self.winfo_width() - 2 * self._pad_x)
        ratio = max(0.0, min(1.0, (x - self._pad_x) / width))
        return vs + ratio * max(0.0, ve - vs)

    def _time_to_x(self, t: float) -> float:
        if self.duration <= 0:
            return float(self._pad_x)
        vs, ve = self._visible_window()
        span = max(1e-6, ve - vs)
        width = max(1, self.winfo_width() - 2 * self._pad_x)
        return self._pad_x + ((t - vs) / span) * width

    def _on_mouse_wheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self.duration <= 0.05:
            return
        if int(getattr(event, "state", 0)) & 0x0001:
            self.scroll_view(-0.2 if event.delta > 0 else 0.2)
            return
        anchor = self._x_to_time(event.x)
        factor = self.zoom_factor * (1.35 if event.delta > 0 else (1.0 / 1.35))
        self.set_zoom(factor, center_sec=anchor)

    def _on_left_press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self.duration <= 0.05:
            return
        if event.y <= self._ruler_h:
            self._active_drag = "playhead"
            self._apply_drag_x(event.x)
            return

        vs, ve = self._visible_window()
        x_start = self._time_to_x(self.start_sec)
        x_end = self._time_to_x(self.end_sec)
        start_visible = (vs - 0.5) <= self.start_sec <= (ve + 0.5)
        end_visible = (vs - 0.5) <= self.end_sec <= (ve + 0.5)
        dist_start = abs(event.x - x_start) if start_visible else 9999.0
        dist_end = abs(event.x - x_end) if end_visible else 9999.0

        # Si hace clic cerca de las manijas Inicio o Fin (<= 10 px), arrastrarlas;
        # en cualquier otro punto mueve el cursor amarillo de ejecución libremente.
        if dist_start <= 10.0 or dist_end <= 10.0:
            self._active_drag = "end" if dist_end < dist_start else "start"
        else:
            self._active_drag = "playhead"
        self._apply_drag_x(event.x)

    def _on_left_drag(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self.duration <= 0.05 or not self._active_drag:
            return
        self._apply_drag_x(event.x)

    def _on_release(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self._active_drag = None

    def _on_middle_action(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self.duration <= 0.05:
            return
        self._active_drag = "playhead"
        self._apply_drag_x(event.x)

    def _on_right_action(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self.duration <= 0.05:
            return
        self._active_drag = "end"
        self._apply_drag_x(event.x)

    def _apply_drag_x(self, x: float) -> None:
        t = self._x_to_time(x)
        if self._active_drag == "playhead":
            self.set_playhead(t)
            if self._on_seek_requested:
                self._on_seek_requested(t)
            return
        if self._active_drag == "end":
            new_end = max(self.start_sec + 0.05, min(self.duration, t))
            self.set_range(self.start_sec, new_end, notify=True)
        else:
            new_start = max(0.0, min(self.end_sec - 0.05, t))
            self.set_range(new_start, self.end_sec, notify=True)

    def redraw(self) -> None:
        """Dibuja la regla, la zona seleccionada, la onda con zoom, marcadores y cursor."""
        self.delete("all")
        w = max(200, self.winfo_width())
        h = max(100, self.winfo_height())

        self.create_rectangle(0, 0, w, self._ruler_h, fill="#1E293B", outline="")

        if self.duration <= 0:
            self.create_text(
                w / 2,
                (h + self._ruler_h) / 2,
                text="Carga un archivo de video o audio para visualizar su forma de onda",
                fill="#94A3B8",
                font=("Segoe UI", 9),
            )
            return

        vs, ve = self._visible_window()
        span = max(1e-6, ve - vs)
        x_start = self._time_to_x(self.start_sec)
        x_end = self._time_to_x(self.end_sec)

        # Zona sombreada de la selección recortada al área visible
        rx1 = max(float(self._pad_x), min(float(w - self._pad_x), x_start))
        rx2 = max(float(self._pad_x), min(float(w - self._pad_x), x_end))
        if rx2 > rx1:
            self.create_rectangle(rx1, self._ruler_h, rx2, h, fill="#172554", outline="")

        # Marcas en la regla de tiempo para la ventana visible
        ticks = 8
        show_ms = span < 20.0
        for i in range(ticks + 1):
            t_tick = vs + (span * i) / ticks
            x_tick = self._time_to_x(t_tick)
            self.create_line(x_tick, self._ruler_h - 6, x_tick, self._ruler_h, fill="#64748B")
            anchor = "w" if i == 0 else ("e" if i == ticks else "center")
            self.create_text(
                x_tick,
                10,
                text=format_timestamp(t_tick, include_ms=show_ms),
                fill="#CBD5E1",
                font=("Consolas", 8),
                anchor=anchor,
            )

        # Línea central
        mid_y = self._ruler_h + (h - self._ruler_h) / 2
        max_amp_h = max(10.0, (h - self._ruler_h) / 2 - 8)
        self.create_line(self._pad_x, mid_y, w - self._pad_x, mid_y, fill="#334155")

        # Barras de la forma de onda alineadas píxel a píxel con el tiempo exacto
        if self.peaks:
            usable_px = max(1, int(w - 2 * self._pad_x))
            n = len(self.peaks)
            for px in range(usable_px):
                t0 = vs + (px / usable_px) * span
                t1 = vs + ((px + 1) / usable_px) * span
                idx0 = max(0, min(n - 1, int((t0 / self.duration) * n)))
                idx1 = max(idx0 + 1, min(n, int((t1 / self.duration) * n) + 1))
                peak = max(self.peaks[idx0:idx1])
                amp = max(1.0, peak * max_amp_h)
                bx = self._pad_x + px
                in_sel = x_start <= bx <= x_end
                color = "#38BDF8" if in_sel else "#64748B"
                self.create_line(
                    bx,
                    mid_y - amp,
                    bx,
                    mid_y + amp,
                    fill=color,
                )

        min_x_vis = self._pad_x - 2
        max_x_vis = w - self._pad_x + 2

        # Marcador de Inicio (verde)
        if min_x_vis <= x_start <= max_x_vis:
            self.create_line(x_start, self._ruler_h, x_start, h, fill="#22C55E", width=2)
            self.create_rectangle(
                x_start - 7, self._ruler_h, x_start + 7, self._ruler_h + 14, fill="#22C55E", outline=""
            )
            self.create_text(
                x_start, self._ruler_h + 7, text="[", fill="#052E16", font=("Segoe UI", 8, "bold")
            )

        # Marcador de Fin (naranja)
        if min_x_vis <= x_end <= max_x_vis:
            self.create_line(x_end, self._ruler_h, x_end, h, fill="#F97316", width=2)
            self.create_rectangle(
                x_end - 7, self._ruler_h, x_end + 7, self._ruler_h + 14, fill="#F97316", outline=""
            )
            self.create_text(
                x_end, self._ruler_h + 7, text="]", fill="#431407", font=("Segoe UI", 8, "bold")
            )

        # Cursor de reproducción (amarillo)
        if self.playhead_sec is not None:
            xp = self._time_to_x(self.playhead_sec)
            if min_x_vis <= xp <= max_x_vis:
                self.create_line(xp, 2, xp, h, fill="#FACC15", width=2)
                self.create_polygon(
                    xp - 5,
                    2,
                    xp + 5,
                    2,
                    xp,
                    11,
                    fill="#FACC15",
                    outline="#854D0E",
                )


class ConflictDialog(tk.Toplevel):
    """Diálogo modal en español para resolver incompatibilidad de códec y contenedor."""

    def __init__(
        self,
        parent: tk.Misc,
        filename: str,
        source_codec: str,
        target_ext: str,
        suggested_ext: str,
        suggested_desc: str,
    ) -> None:
        super().__init__(parent)
        self.title("Incompatibilidad de códec y contenedor")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.decision: str = "skip"
        self.apply_to_all: bool = False
        self._apply_var = tk.BooleanVar(value=False)

        self.configure(bg="#F5F7FA")
        container = ttk.Frame(self, padding=18)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text="El contenedor seleccionado no permite copiar este audio sin recodificar",
            font=("Segoe UI", 10, "bold"),
            foreground="#9A3412",
        ).pack(anchor="w", pady=(0, 8))

        detail_text = (
            f"Archivo: {filename}\n"
            f"Códec de audio original: {source_codec.upper()}\n"
            f"Formato solicitado (copia directa): {target_ext}\n\n"
            f"El contenedor '{target_ext}' no soporta almacenar audio '{source_codec.upper()}' "
            f"mediante copia directa (-c:a copy).\n"
            f"¿Cómo deseas proceder?"
        )
        ttk.Label(container, text=detail_text, justify="left", wraplength=480).pack(
            anchor="w", pady=(0, 12)
        )

        ttk.Checkbutton(
            container,
            text="Aplicar esta decisión a todos los conflictos restantes del lote",
            variable=self._apply_var,
        ).pack(anchor="w", pady=(0, 14))

        buttons_frame = ttk.Frame(container)
        buttons_frame.pack(fill="x")

        ttk.Button(
            buttons_frame,
            text=f"Recodificar a {target_ext}",
            command=lambda: self._finish("recode"),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            buttons_frame,
            text=f"Cambiar a {suggested_ext} (Sin pérdida)",
            command=lambda: self._finish("change_container"),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            buttons_frame,
            text="Omitir archivo",
            command=lambda: self._finish("skip"),
        ).pack(side="right")

        note = ttk.Label(
            container,
            text=f"Contenedor compatible sugerido: {suggested_desc}",
            foreground="#475569",
            font=("Segoe UI", 8),
        )
        note.pack(anchor="w", pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("skip"))
        self.update_idletasks()
        self._center_on_parent(parent)

    def _center_on_parent(self, parent: tk.Misc) -> None:
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        w = self.winfo_width()
        h = self.winfo_height()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 2)
        self.geometry(f"+{x}+{y}")

    def _finish(self, choice: str) -> None:
        self.decision = choice
        self.apply_to_all = bool(self._apply_var.get())
        self.grab_release()
        self.destroy()


class AudioExtractorApp:
    """Aplicación principal de escritorio en Tkinter para extracción y edición de audio con FFmpeg."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AudioAula — Extractor y Editor de Audio (FFmpeg)")
        self.root.geometry("1120x910")
        self.root.minsize(980, 780)

        self.ui_queue: queue.Queue[tuple[str, tuple[object, ...]]] = queue.Queue()
        self.items: dict[str, QueueItem] = {}
        self.item_order: list[str] = []
        self._counter = 0

        self.ffmpeg_loc: FFmpegLocation = find_ffmpeg_binaries()
        self.is_running: bool = False
        self.cancel_event = threading.Event()

        # Estado de la pestaña de extracción por lotes
        self.recursive_var = tk.BooleanVar(value=False)
        self.format_label_var = tk.StringVar()
        self.quality_label_var = tk.StringVar()
        self.codec_mode_label_var = tk.StringVar()
        self.conflict_policy_label_var = tk.StringVar()
        self.same_folder_var = tk.BooleanVar(value=True)
        self.dest_folder_var = tk.StringVar(value=get_default_initial_dir())
        self.overwrite_label_var = tk.StringVar()

        # Estado de la pestaña del editor de audio
        self.editor_file_path: Optional[Path] = None
        self.editor_media_info: Optional[MediaInfo] = None
        self._editor_temp_dir: Path = Path(tempfile.mkdtemp(prefix="audioaula_editor_"))
        self._editor_cache_wav: Optional[Path] = None
        self._editor_load_seq: int = 0
        self.editor_clips: dict[str, EditorClipItem] = {}
        self.editor_clip_order: list[str] = []
        self._clip_counter = 0
        self._custom_name_edited: bool = False
        self._ffplay_proc: Optional[subprocess.Popen[bytes]] = None
        self._ffplay_gen: int = 0
        self._is_playing: bool = False
        self._is_paused: bool = False
        self._current_play_pos: float = 0.0
        self._play_speed: float = 1.0
        self._play_started_mono: float = 0.0
        self._play_base_sec: float = 0.0
        self._play_limit_sec: float = 0.0
        self._hw_clock_ready: bool = False
        self._hw_audio_pos: float = 0.0
        self._hw_clock_mono: float = 0.0
        self._play_after_id: Optional[str] = None
        self._user_dragging_seek: bool = False
        self._updating_seek_scale: bool = False

        self.editor_file_info_var = tk.StringVar(
            value="Ningún archivo cargado. Abre un video o audio, o envía uno desde la cola."
        )
        self.editor_start_var = tk.StringVar(value="00:00.00")
        self.editor_end_var = tk.StringVar(value="00:00.00")
        self.editor_sel_dur_var = tk.StringVar(value="Duración sel.: 00:00.00")
        self.editor_play_pos_var = tk.StringVar(value="Posición: --:--")
        self.editor_speed_var = tk.StringVar(value=EDITOR_SPEED_OPTIONS[0][1])
        self.editor_seek_var = tk.DoubleVar(value=0.0)
        self.editor_zoom_var = tk.StringVar(value="Zoom: 1.0x")
        self.editor_goto_var = tk.StringVar(value="00:00.00")
        self.editor_format_var = tk.StringVar()
        self.editor_quality_var = tk.StringVar()
        self.editor_volume_var = tk.StringVar(value=EDITOR_VOLUME_OPTIONS[0][1])
        self.editor_fade_in_var = tk.StringVar(value=EDITOR_FADE_OPTIONS[0][1])
        self.editor_fade_out_var = tk.StringVar(value=EDITOR_FADE_OPTIONS[0][1])
        self.editor_output_name_var = tk.StringVar()

        self.ffmpeg_status_var = tk.StringVar()
        self.current_file_var = tk.StringVar(value="Listo para iniciar.")
        self.file_pct_var = tk.StringVar(value="0%")
        self.batch_status_var = tk.StringVar(value="Lote: 0 / 0")
        self.file_progress_var = tk.DoubleVar(value=0.0)
        self.batch_progress_var = tk.DoubleVar(value=0.0)

        # Mapas auxiliares entre etiquetas visibles y claves internas
        self._format_label_to_key: dict[str, str] = {
            str(spec["label"]): key for key, spec in OUTPUT_FORMATS.items()
        }
        self._editor_format_label_to_key: dict[str, str] = {
            **self._format_label_to_key,
            EDITOR_COPY_LABEL: "copy",
        }
        self._quality_label_to_key: dict[str, str] = {}
        self._editor_quality_label_to_key: dict[str, str] = {}
        self._codec_mode_label_to_key: dict[str, str] = {
            label: key for key, label in CODEC_MODE_OPTIONS
        }
        self._conflict_label_to_key: dict[str, str] = {
            label: key for key, label in CONFLICT_POLICY_OPTIONS
        }
        self._overwrite_label_to_key: dict[str, str] = {
            label: key for key, label in OVERWRITE_OPTIONS
        }
        self._volume_label_to_val: dict[str, float] = {
            label: val for val, label in EDITOR_VOLUME_OPTIONS
        }
        self._fade_label_to_val: dict[str, float] = {
            label: val for val, label in EDITOR_FADE_OPTIONS
        }
        self._speed_label_to_val: dict[str, float] = {
            label: val for val, label in EDITOR_SPEED_OPTIONS
        }

        self._configure_styles()
        self._build_ui()
        self._load_saved_preferences()
        self._refresh_ffmpeg_status(log_initial=True)
        self._update_quality_choices()
        self._update_editor_quality_choices()
        self._update_conflict_combo_state()
        self._toggle_dest_folder_state()

        # Atajos de teclado para avance/retroceso rápido en el editor
        self.root.bind("<Left>", lambda e: self._on_editor_arrow_key(e, -5.0))
        self.root.bind("<Right>", lambda e: self._on_editor_arrow_key(e, 5.0))
        self.root.bind("<Shift-Left>", lambda e: self._on_editor_arrow_key(e, -15.0))
        self.root.bind("<Shift-Right>", lambda e: self._on_editor_arrow_key(e, 15.0))

        self.root.after(60, self._process_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#F4F6F9"
        card_bg = "#FFFFFF"
        accent = "#2563EB"
        accent_hover = "#1D4ED8"
        text_main = "#0F172A"
        text_muted = "#475569"

        self.root.configure(bg=bg)
        style.configure(".", font=("Segoe UI", 9), background=bg, foreground=text_main)
        style.configure("Main.TFrame", background=bg)
        style.configure("Card.TLabelframe", background=card_bg, borderwidth=1, relief="solid")
        style.configure(
            "Card.TLabelframe.Label",
            background=bg,
            foreground="#1E293B",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure("Inner.TFrame", background=card_bg)
        style.configure("Inner.TLabel", background=card_bg, foreground=text_main)
        style.configure("Muted.TLabel", background=card_bg, foreground=text_muted, font=("Segoe UI", 8))
        style.configure("Inner.TCheckbutton", background=card_bg, foreground=text_main)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 6), font=("Segoe UI", 9, "bold"))

        style.configure(
            "Accent.TButton",
            background=accent,
            foreground="#FFFFFF",
            font=("Segoe UI", 9, "bold"),
            padding=(12, 6),
        )
        style.map(
            "Accent.TButton",
            background=[("active", accent_hover), ("disabled", "#94A3B8")],
            foreground=[("disabled", "#E2E8F0")],
        )
        style.configure("TButton", padding=(8, 4))

        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground=text_main,
            rowheight=23,
        )
        style.configure(
            "Treeview.Heading",
            background="#E2E8F0",
            foreground="#0F172A",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background=accent,
            thickness=14,
        )

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, style="Main.TFrame", padding=10)
        main.pack(fill="both", expand=True)

        # Encabezado
        header = ttk.Frame(main, style="Main.TFrame")
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(
            header,
            text="AudioAula — Extractor y Editor de Audio (FFmpeg)",
            font=("Segoe UI", 13, "bold"),
            foreground="#0F172A",
        ).pack(side="left")
        ttk.Label(
            header,
            text="Videos (.mp4, .mkv, .webm, .avi, .mov) y cualquier audio (.mp3, .wav, .opus, .ogg, .m4a, .flac...)",
            foreground="#475569",
            font=("Segoe UI", 9),
        ).pack(side="right", pady=(4, 0))

        # Cuaderno de pestañas principal
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True, pady=(0, 8))

        self.tab_batch = ttk.Frame(self.notebook, style="Main.TFrame", padding=6)
        self.tab_editor = ttk.Frame(self.notebook, style="Main.TFrame", padding=6)
        self.notebook.add(self.tab_batch, text="  Extracción y Conversión por Lotes  ")
        self.notebook.add(self.tab_editor, text="  Editor: Recortar y Separar Audio  ")

        # ==================== PESTAÑA 1: EXTRACCIÓN POR LOTES ====================
        files_frame = ttk.LabelFrame(
            self.tab_batch,
            text="1. Archivos de Video / Audio y Cola de Procesamiento (Doble clic para editar)",
            style="Card.TLabelframe",
            padding=8,
        )
        files_frame.pack(fill="both", expand=True, pady=(0, 6))

        toolbar = ttk.Frame(files_frame, style="Inner.TFrame")
        toolbar.pack(fill="x", pady=(0, 6))

        self.btn_add_files = ttk.Button(
            toolbar, text="Agregar Archivo(s)...", command=self._on_add_files
        )
        self.btn_add_files.pack(side="left", padx=(0, 6))

        self.btn_add_folder = ttk.Button(
            toolbar, text="Agregar Carpeta (Lote)...", command=self._on_add_folder
        )
        self.btn_add_folder.pack(side="left", padx=(0, 8))

        self.chk_recursive = ttk.Checkbutton(
            toolbar,
            text="Incluir subcarpetas",
            variable=self.recursive_var,
            style="Inner.TCheckbutton",
        )
        self.chk_recursive.pack(side="left", padx=(0, 12))

        self.btn_send_to_editor = ttk.Button(
            toolbar,
            text="Editar / Recortar seleccionado...",
            command=self._on_edit_selected_from_queue,
        )
        self.btn_send_to_editor.pack(side="left")

        self.btn_clear = ttk.Button(
            toolbar, text="Limpiar lista", command=self._on_clear_list
        )
        self.btn_clear.pack(side="right")

        self.btn_remove = ttk.Button(
            toolbar, text="Quitar seleccionados", command=self._on_remove_selected
        )
        self.btn_remove.pack(side="right", padx=(0, 6))

        tree_wrap = ttk.Frame(files_frame, style="Inner.TFrame")
        tree_wrap.pack(fill="both", expand=True)

        columns = ("archivo", "formato", "codec", "duracion", "salida", "estado")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings", height=6)
        self.tree.heading("archivo", text="Archivo")
        self.tree.heading("formato", text="Origen")
        self.tree.heading("codec", text="Códec Audio")
        self.tree.heading("duracion", text="Duración")
        self.tree.heading("salida", text="Salida Prevista")
        self.tree.heading("estado", text="Estado")

        self.tree.column("archivo", width=250, minwidth=150)
        self.tree.column("formato", width=70, minwidth=60, anchor="center")
        self.tree.column("codec", width=100, minwidth=80, anchor="center")
        self.tree.column("duracion", width=80, minwidth=65, anchor="center")
        self.tree.column("salida", width=260, minwidth=160)
        self.tree.column("estado", width=160, minwidth=110)

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        y_scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._on_edit_selected_from_queue())

        opts_frame = ttk.LabelFrame(
            self.tab_batch,
            text="2. Formato de Salida, Códec y Resolución de Incompatibilidades",
            style="Card.TLabelframe",
            padding=8,
        )
        opts_frame.pack(fill="x", pady=(0, 4))
        opts_frame.columnconfigure(1, weight=1)
        opts_frame.columnconfigure(3, weight=1)

        ttk.Label(opts_frame, text="Formato de salida:", style="Inner.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.combo_format = ttk.Combobox(
            opts_frame,
            textvariable=self.format_label_var,
            values=[str(spec["label"]) for spec in OUTPUT_FORMATS.values()],
            state="readonly",
        )
        self.combo_format.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=3)
        self.combo_format.bind("<<ComboboxSelected>>", self._on_format_changed)

        ttk.Label(opts_frame, text="Calidad / Bitrate:", style="Inner.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 8), pady=3
        )
        self.combo_quality = ttk.Combobox(
            opts_frame,
            textvariable=self.quality_label_var,
            state="readonly",
        )
        self.combo_quality.grid(row=0, column=3, sticky="ew", pady=3)
        self.combo_quality.bind("<<ComboboxSelected>>", lambda _e: self._refresh_planned_outputs())

        ttk.Label(opts_frame, text="Modo de códec:", style="Inner.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.combo_codec_mode = ttk.Combobox(
            opts_frame,
            textvariable=self.codec_mode_label_var,
            values=[label for _, label in CODEC_MODE_OPTIONS],
            state="readonly",
        )
        self.combo_codec_mode.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
        self.combo_codec_mode.bind("<<ComboboxSelected>>", self._on_codec_mode_changed)

        ttk.Label(
            opts_frame,
            text="Si se pide copia (Copy) y el códec es incompatible:",
            style="Inner.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=(0, 8), pady=3)
        self.combo_conflict_policy = ttk.Combobox(
            opts_frame,
            textvariable=self.conflict_policy_label_var,
            values=[label for _, label in CONFLICT_POLICY_OPTIONS],
            state="readonly",
        )
        self.combo_conflict_policy.grid(row=2, column=2, columnspan=2, sticky="ew", pady=3)
        self.combo_conflict_policy.bind(
            "<<ComboboxSelected>>", lambda _e: self._refresh_planned_outputs()
        )

        batch_action_bar = ttk.Frame(self.tab_batch, style="Main.TFrame")
        batch_action_bar.pack(fill="x", pady=(4, 0))
        self.btn_start = ttk.Button(
            batch_action_bar,
            text="Iniciar Extracción / Conversión por Lotes",
            style="Accent.TButton",
            command=self._on_start,
        )
        self.btn_start.pack(side="left")

        # ==================== PESTAÑA 2: EDITOR DE AUDIO ====================
        ed_top = ttk.LabelFrame(
            self.tab_editor,
            text="1. Pista Cargada y Forma de Onda (Clic izq.: Mover cursor | Arrastrar [ / ]: Rango | Clic der.: Fin | Rueda: Zoom)",
            style="Card.TLabelframe",
            padding=8,
        )
        ed_top.pack(fill="x", pady=(0, 6))

        ed_file_bar = ttk.Frame(ed_top, style="Inner.TFrame")
        ed_file_bar.pack(fill="x", pady=(0, 5))

        self.btn_ed_open = ttk.Button(
            ed_file_bar,
            text="Abrir Audio (MP3, WAV, Opus...) o Video...",
            command=self._on_editor_open_file,
        )
        self.btn_ed_open.pack(side="left", padx=(0, 10))

        ttk.Label(
            ed_file_bar,
            textvariable=self.editor_file_info_var,
            style="Inner.TLabel",
        ).pack(side="left", fill="x", expand=True)

        self.waveform = WaveformCanvas(
            ed_top,
            on_range_changed=self._on_waveform_range_changed,
            on_seek_requested=self._seek_playback,
            on_view_changed=self._on_waveform_view_changed,
        )
        self.waveform.pack(fill="x")

        self.waveform_hscroll = ttk.Scrollbar(
            ed_top, orient="horizontal", command=self._on_waveform_hscroll
        )
        self.waveform_hscroll.pack(fill="x", pady=(0, 4))
        self.waveform_hscroll.set(0.0, 1.0)

        # Barra de Zoom
        zoom_bar = ttk.Frame(ed_top, style="Inner.TFrame")
        zoom_bar.pack(fill="x", pady=(0, 4))

        ttk.Button(zoom_bar, text="🔍+ Acercar", command=self._on_editor_zoom_in).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(zoom_bar, text="🔍- Alejar", command=self._on_editor_zoom_out).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(
            zoom_bar, text="🔍 Zoom a Selección", command=self._on_editor_zoom_selection
        ).pack(side="left", padx=(0, 4))
        ttk.Button(zoom_bar, text="🔍 Ver Todo (1x)", command=self._on_editor_zoom_reset).pack(
            side="left", padx=(0, 10)
        )
        ttk.Label(
            zoom_bar,
            textvariable=self.editor_zoom_var,
            style="Inner.TLabel",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 12))
        ttk.Label(
            zoom_bar,
            text="Rueda del ratón: zoom sobre la pista | Shift + Rueda: desplazar vista",
            style="Muted.TLabel",
        ).pack(side="right")

        # Barra deslizadora de búsqueda rápida (seek)
        seek_bar = ttk.Frame(ed_top, style="Inner.TFrame")
        seek_bar.pack(fill="x", pady=(0, 4))

        ttk.Label(seek_bar, text="Cursor:", style="Inner.TLabel").pack(side="left", padx=(0, 6))
        self.seek_scale = ttk.Scale(
            seek_bar,
            from_=0.0,
            to=100.0,
            orient="horizontal",
            variable=self.editor_seek_var,
            command=self._on_seek_scale_changed,
        )
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.seek_scale.bind("<Button-1>", self._on_seek_scale_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_scale_release)

        ttk.Label(
            seek_bar,
            textvariable=self.editor_play_pos_var,
            style="Inner.TLabel",
            font=("Consolas", 9, "bold"),
        ).pack(side="right")

        # Controles de tiempo Inicio / Fin
        time_bar = ttk.Frame(ed_top, style="Inner.TFrame")
        time_bar.pack(fill="x", pady=(0, 4))

        ttk.Label(time_bar, text="Inicio:", style="Inner.TLabel").pack(side="left", padx=(0, 4))
        self.entry_ed_start = ttk.Entry(time_bar, textvariable=self.editor_start_var, width=11)
        self.entry_ed_start.pack(side="left", padx=(0, 3))
        self.entry_ed_start.bind("<Return>", lambda _e: self._apply_editor_time_entries())

        for delta, lbl in [(-1.0, "-1s"), (-0.1, "-0.1"), (0.1, "+0.1"), (1.0, "+1s")]:
            ttk.Button(
                time_bar,
                text=lbl,
                width=4,
                command=lambda d=delta: self._nudge_editor_time("start", d),
            ).pack(side="left", padx=1)

        ttk.Label(time_bar, text="  Fin:", style="Inner.TLabel").pack(side="left", padx=(8, 4))
        self.entry_ed_end = ttk.Entry(time_bar, textvariable=self.editor_end_var, width=11)
        self.entry_ed_end.pack(side="left", padx=(0, 3))
        self.entry_ed_end.bind("<Return>", lambda _e: self._apply_editor_time_entries())

        for delta, lbl in [(-1.0, "-1s"), (-0.1, "-0.1"), (0.1, "+0.1"), (1.0, "+1s")]:
            ttk.Button(
                time_bar,
                text=lbl,
                width=4,
                command=lambda d=delta: self._nudge_editor_time("end", d),
            ).pack(side="left", padx=1)

        self.btn_ed_apply_times = ttk.Button(
            time_bar, text="Aplicar", command=self._apply_editor_time_entries
        )
        self.btn_ed_apply_times.pack(side="left", padx=(6, 4))

        self.btn_ed_select_all = ttk.Button(
            time_bar, text="Todo", command=self._on_editor_select_all
        )
        self.btn_ed_select_all.pack(side="left", padx=(0, 8))

        ttk.Label(
            time_bar,
            textvariable=self.editor_sel_dur_var,
            style="Inner.TLabel",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right")

        # Fila de reproducción, avance rápido y velocidad
        play_bar = ttk.Frame(ed_top, style="Inner.TFrame")
        play_bar.pack(fill="x", pady=(0, 4))

        self.btn_ed_play = ttk.Button(
            play_bar, text="▶ Escuchar", command=self._on_editor_toggle_play_pause
        )
        self.btn_ed_play.pack(side="left", padx=(0, 4))

        self.btn_ed_stop = ttk.Button(
            play_bar, text="⏹ Detener", command=self._stop_playback, state="disabled"
        )
        self.btn_ed_stop.pack(side="left", padx=(0, 10))

        for delta, lbl in [
            (-15.0, "⏪ -15s"),
            (-5.0, "⏪ -5s"),
            (5.0, "⏩ +5s"),
            (15.0, "⏩ +15s"),
            (30.0, "⏩ +30s"),
        ]:
            ttk.Button(
                play_bar,
                text=lbl,
                command=lambda d=delta: self._skip_playback(d),
            ).pack(side="left", padx=2)

        ttk.Label(play_bar, text="  Velocidad:", style="Inner.TLabel").pack(
            side="left", padx=(10, 4)
        )
        self.combo_ed_speed = ttk.Combobox(
            play_bar,
            textvariable=self.editor_speed_var,
            values=[lbl for _, lbl in EDITOR_SPEED_OPTIONS],
            state="readonly",
            width=18,
        )
        self.combo_ed_speed.pack(side="left")
        self.combo_ed_speed.bind("<<ComboboxSelected>>", self._on_editor_speed_changed)

        # Fila de marcado rápido e ir a tiempo exacto del cursor
        mark_bar = ttk.Frame(ed_top, style="Inner.TFrame")
        mark_bar.pack(fill="x")

        self.btn_ed_mark_start = ttk.Button(
            mark_bar,
            text="[ Marcar Inicio en Cursor",
            command=lambda: self._on_editor_mark_here("start"),
        )
        self.btn_ed_mark_start.pack(side="left", padx=(0, 6))

        self.btn_ed_mark_end = ttk.Button(
            mark_bar,
            text="Marcar Fin en Cursor ]",
            command=lambda: self._on_editor_mark_here("end"),
        )
        self.btn_ed_mark_end.pack(side="left", padx=(0, 14))

        ttk.Label(mark_bar, text="Ir a tiempo:", style="Inner.TLabel").pack(side="left", padx=(0, 4))
        self.entry_ed_goto = ttk.Entry(mark_bar, textvariable=self.editor_goto_var, width=11)
        self.entry_ed_goto.pack(side="left", padx=(0, 4))
        self.entry_ed_goto.bind("<Return>", lambda _e: self._on_editor_goto_time())

        self.btn_ed_goto = ttk.Button(
            mark_bar, text="Mover Cursor", command=self._on_editor_goto_time
        )
        self.btn_ed_goto.pack(side="left")

        # Opciones de exportación y acciones de recorte / separación
        ed_opts = ttk.LabelFrame(
            self.tab_editor,
            text="2. Opciones del Nuevo Archivo y Acciones de Recorte / Separación",
            style="Card.TLabelframe",
            padding=8,
        )
        ed_opts.pack(fill="x", pady=(0, 6))
        ed_opts.columnconfigure(1, weight=1)
        ed_opts.columnconfigure(3, weight=1)
        ed_opts.columnconfigure(5, weight=1)

        ttk.Label(ed_opts, text="Formato salida:", style="Inner.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=3
        )
        ed_fmt_values = [str(spec["label"]) for spec in OUTPUT_FORMATS.values()] + [
            EDITOR_COPY_LABEL
        ]
        self.combo_ed_format = ttk.Combobox(
            ed_opts,
            textvariable=self.editor_format_var,
            values=ed_fmt_values,
            state="readonly",
        )
        self.combo_ed_format.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=3)
        self.combo_ed_format.bind("<<ComboboxSelected>>", self._on_editor_format_changed)

        ttk.Label(ed_opts, text="Calidad / Bitrate:", style="Inner.TLabel").grid(
            row=0, column=3, sticky="w", padx=(0, 6), pady=3
        )
        self.combo_ed_quality = ttk.Combobox(
            ed_opts, textvariable=self.editor_quality_var, state="readonly"
        )
        self.combo_ed_quality.grid(row=0, column=4, columnspan=2, sticky="ew", pady=3)

        ttk.Label(ed_opts, text="Volumen:", style="Inner.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=3
        )
        self.combo_ed_volume = ttk.Combobox(
            ed_opts,
            textvariable=self.editor_volume_var,
            values=[lbl for _, lbl in EDITOR_VOLUME_OPTIONS],
            state="readonly",
            width=18,
        )
        self.combo_ed_volume.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=3)

        ttk.Label(ed_opts, text="Fundido entrada:", style="Inner.TLabel").grid(
            row=1, column=2, sticky="w", padx=(0, 6), pady=3
        )
        self.combo_ed_fade_in = ttk.Combobox(
            ed_opts,
            textvariable=self.editor_fade_in_var,
            values=[lbl for _, lbl in EDITOR_FADE_OPTIONS],
            state="readonly",
            width=15,
        )
        self.combo_ed_fade_in.grid(row=1, column=3, sticky="ew", padx=(0, 10), pady=3)

        ttk.Label(ed_opts, text="Fundido salida:", style="Inner.TLabel").grid(
            row=1, column=4, sticky="w", padx=(0, 6), pady=3
        )
        self.combo_ed_fade_out = ttk.Combobox(
            ed_opts,
            textvariable=self.editor_fade_out_var,
            values=[lbl for _, lbl in EDITOR_FADE_OPTIONS],
            state="readonly",
            width=15,
        )
        self.combo_ed_fade_out.grid(row=1, column=5, sticky="ew", pady=3)

        ttk.Label(ed_opts, text="Nombre nuevo archivo:", style="Inner.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 6), pady=3
        )
        self.entry_ed_name = ttk.Entry(ed_opts, textvariable=self.editor_output_name_var)
        self.entry_ed_name.grid(row=2, column=1, columnspan=4, sticky="ew", padx=(0, 6), pady=3)
        self.entry_ed_name.bind("<KeyRelease>", self._on_editor_name_typed)

        self.btn_ed_reset_name = ttk.Button(
            ed_opts,
            text="Nombre sugerido",
            command=lambda: self._update_suggested_trim_name(force=True),
        )
        self.btn_ed_reset_name.grid(row=2, column=5, sticky="ew", pady=3)

        ed_btns = ttk.Frame(ed_opts, style="Inner.TFrame")
        ed_btns.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(6, 0))

        self.btn_ed_trim = ttk.Button(
            ed_btns,
            text="✂ Guardar Selección en Nuevo Archivo",
            style="Accent.TButton",
            command=self._on_editor_trim_single,
        )
        self.btn_ed_trim.pack(side="left", padx=(0, 6))

        self.btn_ed_split2 = ttk.Button(
            ed_btns,
            text="➗ Separar en 2 Partes (en Inicio)",
            command=lambda: self._on_editor_split("two_parts"),
        )
        self.btn_ed_split2.pack(side="left", padx=(0, 6))

        self.btn_ed_split3 = ttk.Button(
            ed_btns,
            text="➗ Separar en 3 Partes",
            command=lambda: self._on_editor_split("three_parts"),
        )
        self.btn_ed_split3.pack(side="left", padx=(0, 6))

        self.btn_ed_remove_seg = ttk.Button(
            ed_btns,
            text="🗑 Eliminar Selección y Unir Resto",
            command=self._on_editor_remove_segment,
        )
        self.btn_ed_remove_seg.pack(side="left")

        # Lista de múltiples fragmentos
        clips_frame = ttk.LabelFrame(
            self.tab_editor,
            text="3. Lista Rápida de Múltiples Recortes (Exportar varios fragmentos a MP3 / Audio)",
            style="Card.TLabelframe",
            padding=8,
        )
        clips_frame.pack(fill="both", expand=True)

        clips_top = ttk.Frame(clips_frame, style="Inner.TFrame")
        clips_top.pack(fill="x", pady=(0, 6))

        self.btn_ed_add_clip = ttk.Button(
            clips_top,
            text="+ Agregar selección a lista",
            command=self._on_editor_add_clip,
        )
        self.btn_ed_add_clip.pack(side="left", padx=(0, 6))

        self.btn_ed_rm_clip = ttk.Button(
            clips_top,
            text="Quitar fragmento",
            command=self._on_editor_remove_clip,
        )
        self.btn_ed_rm_clip.pack(side="left", padx=(0, 6))

        self.btn_ed_clear_clips = ttk.Button(
            clips_top,
            text="Limpiar fragmentos",
            command=self._on_editor_clear_clips,
        )
        self.btn_ed_clear_clips.pack(side="left")

        self.btn_ed_export_clips = ttk.Button(
            clips_top,
            text="Exportar todos los fragmentos",
            style="Accent.TButton",
            command=self._on_editor_export_clips,
        )
        self.btn_ed_export_clips.pack(side="right")

        clips_wrap = ttk.Frame(clips_frame, style="Inner.TFrame")
        clips_wrap.pack(fill="both", expand=True)

        self.clips_tree = ttk.Treeview(
            clips_wrap,
            columns=("inicio", "fin", "duracion", "nombre"),
            show="headings",
            height=3,
        )
        self.clips_tree.heading("inicio", text="Inicio")
        self.clips_tree.heading("fin", text="Fin")
        self.clips_tree.heading("duracion", text="Duración")
        self.clips_tree.heading("nombre", text="Archivo de Salida")
        self.clips_tree.column("inicio", width=110, anchor="center")
        self.clips_tree.column("fin", width=110, anchor="center")
        self.clips_tree.column("duracion", width=110, anchor="center")
        self.clips_tree.column("nombre", width=460)

        clips_scroll = ttk.Scrollbar(clips_wrap, orient="vertical", command=self.clips_tree.yview)
        self.clips_tree.configure(yscrollcommand=clips_scroll.set)
        self.clips_tree.pack(side="left", fill="both", expand=True)
        clips_scroll.pack(side="right", fill="y")

        # ==================== PANEL COMPARTIDO: DESTINO Y FFMPEG ====================
        dest_frame = ttk.LabelFrame(
            main,
            text="Carpeta de Destino y Configuración de FFmpeg",
            style="Card.TLabelframe",
            padding=8,
        )
        dest_frame.pack(fill="x", pady=(0, 6))
        dest_frame.columnconfigure(1, weight=1)

        row_same = ttk.Frame(dest_frame, style="Inner.TFrame")
        row_same.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))

        self.chk_same_folder = ttk.Checkbutton(
            row_same,
            text="Guardar en la misma carpeta del archivo original",
            variable=self.same_folder_var,
            style="Inner.TCheckbutton",
            command=self._toggle_dest_folder_state,
        )
        self.chk_same_folder.pack(side="left")

        ttk.Label(row_same, text="Si el archivo existe:", style="Inner.TLabel").pack(
            side="left", padx=(24, 6)
        )
        self.combo_overwrite = ttk.Combobox(
            row_same,
            textvariable=self.overwrite_label_var,
            values=[label for _, label in OVERWRITE_OPTIONS],
            state="readonly",
            width=38,
        )
        self.combo_overwrite.pack(side="left")

        ttk.Label(dest_frame, text="Carpeta personalizada:", style="Inner.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=2
        )
        self.entry_dest = ttk.Entry(dest_frame, textvariable=self.dest_folder_var)
        self.entry_dest.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=2)

        self.btn_choose_dest = ttk.Button(
            dest_frame, text="Elegir carpeta...", command=self._on_choose_dest
        )
        self.btn_choose_dest.grid(row=1, column=2, padx=(0, 6), pady=2)

        self.btn_open_dest = ttk.Button(
            dest_frame, text="Abrir carpeta", command=self._on_open_dest
        )
        self.btn_open_dest.grid(row=1, column=3, pady=2)

        ffmpeg_row = ttk.Frame(dest_frame, style="Inner.TFrame")
        ffmpeg_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        self.lbl_ffmpeg_status = ttk.Label(
            ffmpeg_row,
            textvariable=self.ffmpeg_status_var,
            style="Muted.TLabel",
        )
        self.lbl_ffmpeg_status.pack(side="left", fill="x", expand=True)

        self.btn_redetect_ffmpeg = ttk.Button(
            ffmpeg_row, text="Detectar de nuevo", command=self._on_redetect_ffmpeg
        )
        self.btn_redetect_ffmpeg.pack(side="right", padx=(6, 0))

        self.btn_config_ffmpeg = ttk.Button(
            ffmpeg_row, text="Configurar ruta de FFmpeg...", command=self._on_config_ffmpeg
        )
        self.btn_config_ffmpeg.pack(side="right")

        # ==================== PANEL COMPARTIDO: PROGRESO Y LOG ====================
        exec_frame = ttk.LabelFrame(
            main,
            text="Progreso y Registro de Actividad",
            style="Card.TLabelframe",
            padding=8,
        )
        exec_frame.pack(fill="both", expand=False)

        progress_grid = ttk.Frame(exec_frame, style="Inner.TFrame")
        progress_grid.pack(fill="x", pady=(0, 6))
        progress_grid.columnconfigure(1, weight=1)

        ttk.Label(progress_grid, text="Tarea actual:", style="Inner.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Label(progress_grid, textvariable=self.current_file_var, style="Inner.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(progress_grid, textvariable=self.file_pct_var, style="Inner.TLabel").grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )

        self.pb_file = ttk.Progressbar(
            progress_grid,
            variable=self.file_progress_var,
            maximum=100.0,
            mode="determinate",
        )
        self.pb_file.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 6))

        ttk.Label(progress_grid, text="Progreso global:", style="Inner.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Label(progress_grid, textvariable=self.batch_status_var, style="Inner.TLabel").grid(
            row=2, column=1, sticky="w"
        )
        self.pb_batch = ttk.Progressbar(
            progress_grid,
            variable=self.batch_progress_var,
            maximum=100.0,
            mode="determinate",
        )
        self.pb_batch.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        actions_row = ttk.Frame(exec_frame, style="Inner.TFrame")
        actions_row.pack(fill="x", pady=(0, 6))

        self.btn_cancel = ttk.Button(
            actions_row,
            text="Cancelar operación en curso",
            command=self._on_cancel,
            state="disabled",
        )
        self.btn_cancel.pack(side="left")

        self.btn_clear_log = ttk.Button(
            actions_row,
            text="Limpiar log",
            command=self._on_clear_log,
        )
        self.btn_clear_log.pack(side="right")

        self.log_text = scrolledtext.ScrolledText(
            exec_frame,
            height=6,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
            bg="#0F172A",
            fg="#E2E8F0",
            insertbackground="#FFFFFF",
            relief="flat",
            padx=8,
            pady=5,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("INFO", foreground="#93C5FD")
        self.log_text.tag_configure("AVISO", foreground="#FCD34D")
        self.log_text.tag_configure("EXITO", foreground="#6EE7B7")
        self.log_text.tag_configure("ERROR", foreground="#FCA5A5")
        self.log_text.tag_configure("FFMPEG", foreground="#CBD5E1")

    def _load_saved_preferences(self) -> None:
        cfg = load_config()
        fmt_key = str(cfg.get("default_format") or "mp3")
        if fmt_key not in OUTPUT_FORMATS:
            fmt_key = "mp3"
        self.format_label_var.set(str(OUTPUT_FORMATS[fmt_key]["label"]))

        ed_fmt_key = str(cfg.get("editor_format") or "mp3")
        if ed_fmt_key == "copy":
            self.editor_format_var.set(EDITOR_COPY_LABEL)
        elif ed_fmt_key in OUTPUT_FORMATS:
            self.editor_format_var.set(str(OUTPUT_FORMATS[ed_fmt_key]["label"]))
        else:
            self.editor_format_var.set(str(OUTPUT_FORMATS["mp3"]["label"]))

        codec_mode = str(cfg.get("codec_mode") or "auto")
        codec_label = next(
            (lbl for k, lbl in CODEC_MODE_OPTIONS if k == codec_mode),
            CODEC_MODE_OPTIONS[0][1],
        )
        self.codec_mode_label_var.set(codec_label)

        conflict_policy = str(cfg.get("conflict_policy") or "ask")
        conflict_label = next(
            (lbl for k, lbl in CONFLICT_POLICY_OPTIONS if k == conflict_policy),
            CONFLICT_POLICY_OPTIONS[0][1],
        )
        self.conflict_policy_label_var.set(conflict_label)

        overwrite_mode = str(cfg.get("overwrite_mode") or "rename")
        overwrite_label = next(
            (lbl for k, lbl in OVERWRITE_OPTIONS if k == overwrite_mode),
            OVERWRITE_OPTIONS[0][1],
        )
        self.overwrite_label_var.set(overwrite_label)

        if isinstance(cfg.get("same_folder"), bool):
            self.same_folder_var.set(bool(cfg["same_folder"]))
        if isinstance(cfg.get("custom_dest_folder"), str) and cfg["custom_dest_folder"].strip():
            self.dest_folder_var.set(cfg["custom_dest_folder"].strip())

    def _save_preferences(self) -> None:
        save_config(
            {
                "default_format": self._selected_format_key(),
                "editor_format": self._selected_editor_format_key(),
                "codec_mode": self._selected_codec_mode_key(),
                "conflict_policy": self._selected_conflict_policy_key(),
                "overwrite_mode": self._selected_overwrite_key(),
                "same_folder": bool(self.same_folder_var.get()),
                "custom_dest_folder": self.dest_folder_var.get().strip(),
            }
        )

    def _selected_format_key(self) -> str:
        return self._format_label_to_key.get(self.format_label_var.get(), "mp3")

    def _selected_editor_format_key(self) -> str:
        return self._editor_format_label_to_key.get(self.editor_format_var.get(), "mp3")

    def _selected_editor_quality_key(self) -> str:
        fmt_key = self._selected_editor_format_key()
        if fmt_key not in OUTPUT_FORMATS:
            return "copy"
        default_q = str(OUTPUT_FORMATS[fmt_key]["default_quality"])
        return self._editor_quality_label_to_key.get(self.editor_quality_var.get(), default_q)

    def _selected_quality_key(self) -> str:
        fmt_key = self._selected_format_key()
        default_q = str(OUTPUT_FORMATS[fmt_key]["default_quality"])
        return self._quality_label_to_key.get(self.quality_label_var.get(), default_q)

    def _selected_codec_mode_key(self) -> str:
        return self._codec_mode_label_to_key.get(self.codec_mode_label_var.get(), "auto")

    def _selected_conflict_policy_key(self) -> str:
        return self._conflict_label_to_key.get(self.conflict_policy_label_var.get(), "ask")

    def _selected_overwrite_key(self) -> str:
        return self._overwrite_label_to_key.get(self.overwrite_label_var.get(), "rename")

    def _refresh_ffmpeg_status(self, log_initial: bool = False) -> None:
        self.ffmpeg_loc = find_ffmpeg_binaries()
        if self.ffmpeg_loc.is_ready:
            origin_map = {
                "config": "ruta personalizada",
                "path": "PATH del sistema",
                "auto": "detección automática",
            }
            origin_txt = origin_map.get(self.ffmpeg_loc.source, "detectado")
            probe_note = (
                "ffprobe disponible"
                if self.ffmpeg_loc.has_ffprobe
                else "ffprobe no hallado (inspección limitada)"
            )
            self.ffmpeg_status_var.set(
                f"FFmpeg listo ({origin_txt}, {probe_note}): {self.ffmpeg_loc.ffmpeg_path}"
            )
            self.lbl_ffmpeg_status.configure(foreground="#15803D")
            if log_initial:
                self._append_log(
                    "INFO",
                    f"FFmpeg detectado ({origin_txt}): {self.ffmpeg_loc.ffmpeg_path}",
                )
        else:
            self.ffmpeg_status_var.set(
                "FFmpeg no detectado. Usa 'Configurar ruta de FFmpeg...' para elegir ffmpeg.exe."
            )
            self.lbl_ffmpeg_status.configure(foreground="#B91C1C")
            if log_initial:
                self._append_log(
                    "AVISO",
                    "No se detectó FFmpeg automáticamente. Configura la ruta manualmente o instálalo.",
                )

    def _update_quality_choices(self) -> None:
        fmt_key = self._selected_format_key()
        spec = OUTPUT_FORMATS[fmt_key]
        qualities: list[tuple[str, str]] = list(spec["qualities"])  # type: ignore[arg-type]
        self._quality_label_to_key = {label: key for key, label in qualities}
        labels = [label for _, label in qualities]
        self.combo_quality["values"] = labels

        default_key = str(spec["default_quality"])
        default_label = next((lbl for k, lbl in qualities if k == default_key), labels[0])
        self.quality_label_var.set(default_label)

    def _update_conflict_combo_state(self) -> None:
        mode = self._selected_codec_mode_key()
        if mode == "copy":
            self.combo_conflict_policy.configure(state="readonly")
        else:
            self.combo_conflict_policy.configure(state="disabled")

    def _toggle_dest_folder_state(self) -> None:
        same = bool(self.same_folder_var.get())
        state = "disabled" if same else "normal"
        btn_state = "disabled" if same else "normal"
        self.entry_dest.configure(state=state)
        self.btn_choose_dest.configure(state=btn_state)

    def _on_format_changed(self, _event: object = None) -> None:
        self._update_quality_choices()
        self._refresh_planned_outputs()

    def _on_codec_mode_changed(self, _event: object = None) -> None:
        self._update_conflict_combo_state()
        self._refresh_planned_outputs()

    def _get_picker_initial_dir(self) -> str:
        if not self.same_folder_var.get():
            custom = Path(self.dest_folder_var.get().strip())
            if custom.is_dir():
                return str(custom)
        return get_default_initial_dir()

    def _media_filetypes(self) -> list[tuple[str, str]]:
        all_pattern = " ".join(f"*{ext}" for ext in SUPPORTED_MEDIA_EXTENSIONS)
        audio_pattern = " ".join(f"*{ext}" for ext in SUPPORTED_AUDIO_EXTENSIONS)
        video_pattern = " ".join(f"*{ext}" for ext in SUPPORTED_VIDEO_EXTENSIONS)
        return [
            ("Audios y Videos compatibles", all_pattern),
            ("Archivos de Audio (MP3, WAV, Opus, Ogg, M4A, FLAC...)", audio_pattern),
            ("Archivos MP3", "*.mp3"),
            ("Archivos de Video (MP4, MKV, WebM, AVI, MOV)", video_pattern),
            ("Todos los archivos", "*.*"),
        ]

    def _on_add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title="Seleccionar archivo(s) de video o audio",
            initialdir=get_default_initial_dir(),
            filetypes=self._media_filetypes(),
        )
        if not selected:
            return
        self._enqueue_paths([Path(p) for p in selected])

    def _on_add_folder(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Seleccionar carpeta de videos o audios para procesar por lotes",
            initialdir=get_default_initial_dir(),
        )
        if not folder:
            return
        found = collect_video_files_from_folder(
            folder, recursive=bool(self.recursive_var.get()), include_audio=True
        )
        if not found:
            messagebox.showinfo(
                "Sin archivos compatibles",
                "No se encontraron archivos de video o audio compatibles en la carpeta seleccionada.",
                parent=self.root,
            )
            return
        self._append_log("INFO", f"Se encontraron {len(found)} archivo(s) compatible(s) en '{folder}'.")
        self._enqueue_paths(found)

    def _enqueue_paths(self, paths: list[Path]) -> None:
        existing_paths = {item.path.resolve() for item in self.items.values()}
        added_items: list[QueueItem] = []

        for path in paths:
            if path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
                self._append_log(
                    "AVISO", f"Se omitió '{path.name}': extensión no soportada."
                )
                continue
            resolved = path.resolve()
            if resolved in existing_paths:
                continue
            existing_paths.add(resolved)

            self._counter += 1
            item_id = f"item_{self._counter}"
            q_item = QueueItem(item_id=item_id, path=path)
            self.items[item_id] = q_item
            self.item_order.append(item_id)
            added_items.append(q_item)

            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    path.name,
                    path.suffix.lower(),
                    "Analizando...",
                    "--:--",
                    self._compute_preview_text(q_item),
                    "Analizando...",
                ),
            )

        if not added_items:
            return

        # Analizar archivos recién agregados en segundo plano con ffprobe
        probe_bin = self.ffmpeg_loc.ffprobe_path

        def _probe_worker() -> None:
            for q_item in added_items:
                info = probe_media_file(q_item.path, ffprobe_path=probe_bin)
                self.ui_queue.put(("probe_done", (q_item.item_id, info)))

        threading.Thread(target=_probe_worker, daemon=True).start()

    def _compute_preview_text(self, q_item: QueueItem) -> str:
        fmt_key = self._selected_format_key()
        quality_key = self._selected_quality_key()
        codec_mode = self._selected_codec_mode_key()
        policy = self._selected_conflict_policy_key()

        info = q_item.media_info
        if info is None:
            ext = str(OUTPUT_FORMATS[fmt_key]["extension"])
            return f"{q_item.path.stem}{ext}"
        if not info.has_audio:
            return "Sin pista de audio"

        plan = resolve_conversion_plan(
            source_codec=info.audio_codec,
            target_format_key=fmt_key,
            quality_key=quality_key,
            codec_mode=codec_mode,
            conflict_policy=policy,
        )
        tag = "Copia directa" if plan.is_stream_copy else "Recodificar"
        if plan.has_conflict and plan.resolution_applied == "conflict_pending":
            return f"Conflicto -> {plan.output_extension} (Preguntar)"
        if plan.has_conflict and plan.resolution_applied == "changed_container":
            tag = "Copia en cont. compatible"
        elif plan.has_conflict and plan.resolution_applied == "auto_recode":
            tag = "Recodif. automática"
        return f"{q_item.path.stem}{plan.output_extension} ({tag})"

    def _refresh_planned_outputs(self) -> None:
        for item_id in self.item_order:
            q_item = self.items.get(item_id)
            if not q_item or not self.tree.exists(item_id):
                continue
            vals = list(self.tree.item(item_id, "values"))
            vals[4] = self._compute_preview_text(q_item)
            self.tree.item(item_id, values=vals)

    def _on_remove_selected(self) -> None:
        if self.is_running:
            return
        selected = self.tree.selection()
        for item_id in selected:
            self.tree.delete(item_id)
            self.items.pop(item_id, None)
            if item_id in self.item_order:
                self.item_order.remove(item_id)

    def _on_clear_list(self) -> None:
        if self.is_running:
            return
        for item_id in list(self.item_order):
            if self.tree.exists(item_id):
                self.tree.delete(item_id)
        self.items.clear()
        self.item_order.clear()
        self.file_progress_var.set(0.0)
        self.batch_progress_var.set(0.0)
        self.file_pct_var.set("0%")
        self.batch_status_var.set("Lote: 0 / 0")
        self.current_file_var.set("Listo para iniciar.")

    def _on_choose_dest(self) -> None:
        initial = self._get_picker_initial_dir()
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Elegir carpeta de destino para los audios",
            initialdir=initial,
        )
        if folder:
            self.dest_folder_var.set(folder)

    def _on_open_dest(self) -> None:
        target: Optional[Path] = None
        if not self.same_folder_var.get():
            custom = Path(self.dest_folder_var.get().strip())
            if custom.is_dir():
                target = custom
        elif self.notebook.index(self.notebook.select()) == 1 and self.editor_file_path:
            if self.editor_file_path.parent.is_dir():
                target = self.editor_file_path.parent
        elif self.item_order:
            first = self.items.get(self.item_order[0])
            if first and first.path.parent.is_dir():
                target = first.path.parent

        if target is None:
            fallback = Path(get_default_initial_dir())
            target = fallback if fallback.is_dir() else Path.home()

        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(
                "Error al abrir carpeta",
                f"No se pudo abrir '{target}': {exc}",
                parent=self.root,
            )

    def _on_config_ffmpeg(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Seleccionar ejecutable ffmpeg.exe",
            filetypes=[("Ejecutable FFmpeg", "ffmpeg.exe ffmpeg"), ("Todos los archivos", "*.*")],
        )
        if not selected:
            return
        loc = find_ffmpeg_binaries(custom_ffmpeg_path=selected)
        if not loc.is_ready:
            messagebox.showerror(
                "Archivo inválido",
                "El archivo seleccionado no es un ejecutable válido de FFmpeg.",
                parent=self.root,
            )
            return
        save_config({"ffmpeg_path": loc.ffmpeg_path})
        self._refresh_ffmpeg_status()
        self._append_log("INFO", f"Ruta de FFmpeg guardada: {loc.ffmpeg_path}")

    def _on_redetect_ffmpeg(self) -> None:
        save_config({"ffmpeg_path": ""})
        self._refresh_ffmpeg_status()
        if self.ffmpeg_loc.is_ready:
            self._append_log("INFO", f"FFmpeg detectado en: {self.ffmpeg_loc.ffmpeg_path}")
        else:
            self._append_log("AVISO", "No se encontró FFmpeg en el sistema.")

    def _on_clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, level: str, message: str) -> None:
        tag = level if level in ("INFO", "AVISO", "EXITO", "ERROR", "FFMPEG") else "INFO"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{tag}] ", tag)
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_controls_running(self, running: bool) -> None:
        self.is_running = running
        state_idle = "disabled" if running else "normal"
        combo_idle = "disabled" if running else "readonly"

        self.btn_add_files.configure(state=state_idle)
        self.btn_add_folder.configure(state=state_idle)
        self.btn_remove.configure(state=state_idle)
        self.btn_clear.configure(state=state_idle)
        self.btn_send_to_editor.configure(state=state_idle)
        self.chk_recursive.configure(state=state_idle)
        self.combo_format.configure(state=combo_idle)
        self.combo_quality.configure(state=combo_idle)
        self.combo_codec_mode.configure(state=combo_idle)
        self.chk_same_folder.configure(state=state_idle)
        self.combo_overwrite.configure(state=combo_idle)
        self.btn_config_ffmpeg.configure(state=state_idle)
        self.btn_redetect_ffmpeg.configure(state=state_idle)

        # Controles del editor
        self.btn_ed_open.configure(state=state_idle)
        self.btn_ed_trim.configure(state=state_idle)
        self.btn_ed_split2.configure(state=state_idle)
        self.btn_ed_split3.configure(state=state_idle)
        self.btn_ed_remove_seg.configure(state=state_idle)
        self.btn_ed_add_clip.configure(state=state_idle)
        self.btn_ed_rm_clip.configure(state=state_idle)
        self.btn_ed_clear_clips.configure(state=state_idle)
        self.btn_ed_export_clips.configure(state=state_idle)
        self.combo_ed_format.configure(state=combo_idle)

        if running:
            self.combo_conflict_policy.configure(state="disabled")
            self.combo_ed_quality.configure(state="disabled")
            self.entry_dest.configure(state="disabled")
            self.btn_choose_dest.configure(state="disabled")
            self.btn_start.configure(state="disabled")
            self.btn_cancel.configure(state="normal")
        else:
            self._update_conflict_combo_state()
            self._update_editor_quality_choices(preserve_current=True)
            self._toggle_dest_folder_state()
            self.btn_start.configure(state="normal")
            self.btn_cancel.configure(state="disabled")

    def _ask_conflict_from_worker(
        self,
        filename: str,
        source_codec: str,
        target_ext: str,
        suggested_ext: str,
        suggested_desc: str,
    ) -> tuple[str, bool]:
        """Muestra el diálogo modal en el hilo principal de Tkinter y espera la decisión."""
        done_event = threading.Event()
        result_holder: dict[str, object] = {"decision": "skip", "apply_to_all": False}

        def _show_modal() -> None:
            dlg = ConflictDialog(
                parent=self.root,
                filename=filename,
                source_codec=source_codec,
                target_ext=target_ext,
                suggested_ext=suggested_ext,
                suggested_desc=suggested_desc,
            )
            self.root.wait_window(dlg)
            result_holder["decision"] = dlg.decision
            result_holder["apply_to_all"] = dlg.apply_to_all
            done_event.set()

        self.root.after(0, _show_modal)
        done_event.wait()
        return str(result_holder["decision"]), bool(result_holder["apply_to_all"])

    def _on_start(self) -> None:
        if self.is_running:
            return

        if not self.item_order:
            messagebox.showwarning(
                "Cola vacía",
                "Agrega al menos un archivo de video o una carpeta antes de iniciar.",
                parent=self.root,
            )
            return

        self._refresh_ffmpeg_status()
        if not self.ffmpeg_loc.is_ready or not self.ffmpeg_loc.ffmpeg_path:
            messagebox.showerror(
                "FFmpeg no disponible",
                "No se encontró el ejecutable de FFmpeg. Configura la ruta de ffmpeg.exe para continuar.",
                parent=self.root,
            )
            return

        use_same_folder = bool(self.same_folder_var.get())
        custom_dest = Path(self.dest_folder_var.get().strip()) if not use_same_folder else None
        if not use_same_folder:
            if not self.dest_folder_var.get().strip():
                messagebox.showwarning(
                    "Carpeta requerida",
                    "Indica una carpeta de destino o marca 'Guardar en la misma carpeta de cada video original'.",
                    parent=self.root,
                )
                return

        self._save_preferences()
        self.cancel_event.clear()
        self._set_controls_running(True)

        fmt_key = self._selected_format_key()
        quality_key = self._selected_quality_key()
        codec_mode = self._selected_codec_mode_key()
        conflict_policy = self._selected_conflict_policy_key()
        overwrite = self._selected_overwrite_key() == "overwrite"
        ffmpeg_bin = self.ffmpeg_loc.ffmpeg_path
        ffprobe_bin = self.ffmpeg_loc.ffprobe_path

        snapshot_ids = list(self.item_order)
        total = len(snapshot_ids)

        self.file_progress_var.set(0.0)
        self.batch_progress_var.set(0.0)
        self.file_pct_var.set("0%")
        self.batch_status_var.set(f"Lote: 0 / {total}")
        self._append_log("INFO", f"Iniciando lote de {total} archivo(s)...")

        def _batch_worker() -> None:
            ok_count = 0
            err_count = 0
            skip_count = 0
            sticky_policy: Optional[str] = None

            for idx, item_id in enumerate(snapshot_ids, start=1):
                if self.cancel_event.is_set():
                    break

                q_item = self.items.get(item_id)
                if q_item is None:
                    continue

                self.ui_queue.put(("current_file", (q_item.path.name, idx, total)))
                self.ui_queue.put(("file_progress", (0.0,)))
                self.ui_queue.put(("item_status", (item_id, "Procesando...")))

                # Asegurar MediaInfo si todavía no terminó de analizarse
                info = q_item.media_info
                if info is None:
                    info = probe_media_file(q_item.path, ffprobe_path=ffprobe_bin)
                    self.ui_queue.put(("probe_done", (item_id, info)))

                if not info.has_audio:
                    err_msg = info.error_message or "El video no contiene pista de audio."
                    self.ui_queue.put(("log", ("ERROR", f"{q_item.path.name}: {err_msg}")))
                    self.ui_queue.put(("item_status", (item_id, "Error (sin audio)")))
                    err_count += 1
                    self.ui_queue.put(("batch_progress", (idx, total)))
                    continue

                effective_policy = sticky_policy if sticky_policy else conflict_policy
                plan = resolve_conversion_plan(
                    source_codec=info.audio_codec,
                    target_format_key=fmt_key,
                    quality_key=quality_key,
                    codec_mode=codec_mode,
                    conflict_policy=effective_policy,
                )

                # Si quedó en conflicto pendiente ('ask'), preguntar al usuario
                if plan.has_conflict and plan.resolution_applied == "conflict_pending":
                    choice, apply_all = self._ask_conflict_from_worker(
                        filename=q_item.path.name,
                        source_codec=info.audio_codec or "desconocido",
                        target_ext=plan.output_extension,
                        suggested_ext=plan.suggested_extension or ".mka",
                        suggested_desc=plan.suggested_container_desc or "Contenedor compatible",
                    )
                    if apply_all and choice in ("recode", "change_container", "skip"):
                        sticky_policy = choice

                    if choice == "skip":
                        self.ui_queue.put(
                            (
                                "log",
                                (
                                    "AVISO",
                                    f"Omitido por incompatibilidad de códec/contenedor: {q_item.path.name}",
                                ),
                            )
                        )
                        self.ui_queue.put(("item_status", (item_id, "Omitido")))
                        skip_count += 1
                        self.ui_queue.put(("batch_progress", (idx, total)))
                        continue

                    plan = resolve_conversion_plan(
                        source_codec=info.audio_codec,
                        target_format_key=fmt_key,
                        quality_key=quality_key,
                        codec_mode=codec_mode,
                        conflict_policy=choice,
                    )

                def _on_pct(pct: float) -> None:
                    self.ui_queue.put(("file_progress", (pct,)))

                def _on_core_log(lvl: str, msg: str) -> None:
                    self.ui_queue.put(("log", (lvl, msg)))

                result = convert_audio(
                    input_path=q_item.path,
                    plan=plan,
                    ffmpeg_path=ffmpeg_bin,
                    output_dir=None if use_same_folder else custom_dest,
                    overwrite=overwrite,
                    duration_seconds=info.duration_seconds,
                    progress_callback=_on_pct,
                    log_callback=_on_core_log,
                    cancel_event=self.cancel_event,
                )

                if result.cancelled:
                    self.ui_queue.put(("item_status", (item_id, "Cancelado")))
                    self.ui_queue.put(("log", ("AVISO", f"Cancelado: {q_item.path.name}")))
                    break

                if result.success and result.output_path:
                    ok_count += 1
                    status_lbl = (
                        "Completado (Copia)" if plan.is_stream_copy else "Completado"
                    )
                    self.ui_queue.put(("item_status", (item_id, status_lbl)))
                    self.ui_queue.put(("item_output", (item_id, result.output_path.name)))
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                "EXITO",
                                f"Listo: {result.output_path}",
                            ),
                        )
                    )
                else:
                    err_count += 1
                    self.ui_queue.put(("item_status", (item_id, "Error")))
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                "ERROR",
                                f"Error en '{q_item.path.name}': {result.error_message}",
                            ),
                        )
                    )

                self.ui_queue.put(("batch_progress", (idx, total)))

            self.ui_queue.put(
                (
                    "batch_finished",
                    (ok_count, err_count, skip_count, self.cancel_event.is_set()),
                )
            )

        threading.Thread(target=_batch_worker, daemon=True).start()

    def _on_cancel(self) -> None:
        if not self.is_running:
            return
        self.cancel_event.set()
        self._append_log("AVISO", "Solicitando cancelación de la tarea actual...")
        self.btn_cancel.configure(state="disabled")

    def _process_ui_queue(self) -> None:
        try:
            while True:
                event_type, args = self.ui_queue.get_nowait()
                if event_type == "log":
                    level, msg = args
                    self._append_log(str(level), str(msg))

                elif event_type == "probe_done":
                    item_id, info = args
                    assert isinstance(item_id, str) and isinstance(info, MediaInfo)
                    q_item = self.items.get(item_id)
                    if q_item and self.tree.exists(item_id):
                        q_item.media_info = info
                        vals = list(self.tree.item(item_id, "values"))
                        if info.has_audio:
                            vals[2] = (info.audio_codec or "desconocido").upper()
                            vals[3] = info.formatted_duration
                            vals[4] = self._compute_preview_text(q_item)
                            if q_item.status == "Analizando...":
                                q_item.status = "En cola"
                                vals[5] = "En cola"
                        else:
                            vals[2] = "Sin audio"
                            vals[3] = info.formatted_duration
                            vals[4] = "No disponible"
                            q_item.status = "Sin pista de audio"
                            vals[5] = "Sin pista de audio"
                        self.tree.item(item_id, values=vals)

                elif event_type == "current_file":
                    name, idx, total = args
                    self.current_file_var.set(f"{name} ({idx} de {total})")

                elif event_type == "file_progress":
                    (pct,) = args
                    pct_float = float(pct)  # type: ignore[arg-type]
                    self.file_progress_var.set(pct_float)
                    self.file_pct_var.set(f"{pct_float:.0f}%")

                elif event_type == "batch_progress":
                    done, total = args
                    d_int = int(done)  # type: ignore[arg-type]
                    t_int = max(1, int(total))  # type: ignore[arg-type]
                    self.batch_progress_var.set((d_int / t_int) * 100.0)
                    self.batch_status_var.set(f"Lote: {d_int} / {t_int}")

                elif event_type == "item_status":
                    item_id, status_txt = args
                    assert isinstance(item_id, str)
                    q_item = self.items.get(item_id)
                    if q_item and self.tree.exists(item_id):
                        q_item.status = str(status_txt)
                        vals = list(self.tree.item(item_id, "values"))
                        vals[5] = str(status_txt)
                        self.tree.item(item_id, values=vals)

                elif event_type == "item_output":
                    item_id, out_name = args
                    assert isinstance(item_id, str)
                    if self.tree.exists(item_id):
                        vals = list(self.tree.item(item_id, "values"))
                        vals[4] = str(out_name)
                        self.tree.item(item_id, values=vals)

                elif event_type == "waveform_done":
                    req_seq, loaded_path, info, peaks, cache_wav = args
                    assert isinstance(loaded_path, Path) and isinstance(info, MediaInfo)
                    if req_seq != self._editor_load_seq:
                        if isinstance(cache_wav, Path) and cache_wav.exists():
                            try:
                                cache_wav.unlink()
                            except OSError:
                                pass
                        continue

                    if self.editor_file_path and self.editor_file_path.resolve() == loaded_path.resolve():
                        self.editor_media_info = info
                        self._editor_cache_wav = cache_wav if isinstance(cache_wav, Path) else None
                        for old_f in self._editor_temp_dir.glob("*.wav"):
                            if self._editor_cache_wav is None or old_f.resolve() != self._editor_cache_wav.resolve():
                                try:
                                    old_f.unlink()
                                except OSError:
                                    pass

                        if not info.has_audio or not info.duration_seconds:
                            self.editor_file_info_var.set(
                                f"{loaded_path.name} — Error: {info.error_message or 'Sin audio o duración desconocida'}"
                            )
                            self.waveform.set_audio_data([], 0.0)
                            self.seek_scale.configure(to=100.0)
                            self._update_playhead_display(0.0)
                            self._append_log(
                                "ERROR",
                                f"No se pudo cargar en el editor '{loaded_path.name}': {info.error_message}",
                            )
                        else:
                            codec_lbl = (info.audio_codec or "desconocido").upper()
                            self.editor_file_info_var.set(
                                f"{loaded_path.name}  |  Códec: {codec_lbl}  |  Duración exacta: {format_timestamp(info.duration_seconds)}"
                            )
                            self.waveform.set_audio_data(list(peaks), info.duration_seconds)  # type: ignore[arg-type]
                            self._on_waveform_range_changed(0.0, info.duration_seconds)
                            self.seek_scale.configure(to=info.duration_seconds)
                            self._is_playing = False
                            self._is_paused = False
                            self._update_playhead_display(0.0)
                            self.btn_ed_play.configure(text="▶ Escuchar")
                            self.btn_ed_stop.configure(state="disabled")
                            self._append_log(
                                "INFO",
                                f"Audio cargado en el editor (alta precisión): {loaded_path.name} ({codec_lbl}, {format_timestamp(info.duration_seconds)}, {len(peaks)} muestras de onda)",
                            )

                elif event_type == "batch_finished":
                    ok_c, err_c, skip_c, was_cancelled = args
                    self._set_controls_running(False)
                    if was_cancelled:
                        self.current_file_var.set("Proceso cancelado.")
                        self._append_log(
                            "AVISO",
                            f"Proceso cancelado. Completados: {ok_c}, Omitidos: {skip_c}, Errores: {err_c}.",
                        )
                    else:
                        self.current_file_var.set("Lote finalizado.")
                        self._append_log(
                            "INFO",
                            f"Lote terminado. Exitosos: {ok_c}, Omitidos: {skip_c}, Errores: {err_c}.",
                        )

                elif event_type == "editor_finished":
                    summary_msg, was_cancelled = args
                    self._set_controls_running(False)
                    if was_cancelled:
                        self.current_file_var.set("Edición cancelada.")
                        self._append_log("AVISO", str(summary_msg))
                    else:
                        self.current_file_var.set("Edición finalizada.")
                        self._append_log("INFO", str(summary_msg))
        except queue.Empty:
            pass
        finally:
            self.root.after(60, self._process_ui_queue)

    # ==================== MÉTODOS DEL EDITOR DE AUDIO ====================

    def _on_edit_selected_from_queue(self) -> None:
        if self.is_running:
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo(
                "Sin selección",
                "Selecciona primero un archivo de la tabla para abrirlo en el Editor de Audio.",
                parent=self.root,
            )
            return
        q_item = self.items.get(selected[0])
        if q_item:
            self.notebook.select(self.tab_editor)
            self._load_editor_file(q_item.path)

    def _on_editor_open_file(self) -> None:
        if self.is_running:
            return
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Abrir archivo de audio o video para editar",
            initialdir=get_default_initial_dir(),
            filetypes=self._media_filetypes(),
        )
        if not selected:
            return
        self._load_editor_file(Path(selected))

    def _load_editor_file(self, path: Path) -> None:
        self._stop_playback()
        self._editor_load_seq += 1
        req_seq = self._editor_load_seq
        self.editor_file_path = path
        self.editor_media_info = None
        self._editor_cache_wav = None
        self._custom_name_edited = False
        self._on_editor_clear_clips()
        self.editor_file_info_var.set(f"Decodificando y generando forma de onda exacta de '{path.name}'...")
        self.waveform.set_audio_data([], 0.0)

        self._refresh_ffmpeg_status()
        ffmpeg_bin = self.ffmpeg_loc.ffmpeg_path
        ffprobe_bin = self.ffmpeg_loc.ffprobe_path
        target_wav = self._editor_temp_dir / f"editor_{req_seq}.wav"

        def _load_worker() -> None:
            info = probe_media_file(path, ffprobe_path=ffprobe_bin)
            peaks: list[float] = []
            cache_wav: Optional[Path] = None
            if info.has_audio and ffmpeg_bin:
                if prepare_editor_wav_cache(path, ffmpeg_bin, target_wav):
                    wav_peaks, exact_dur = extract_waveform_from_wav(target_wav)
                    if exact_dur > 0.0 and wav_peaks:
                        peaks = wav_peaks
                        info.duration_seconds = exact_dur
                        cache_wav = target_wav
                if not peaks:
                    peaks = extract_waveform_peaks(path, ffmpeg_path=ffmpeg_bin, num_buckets=4000)
            self.ui_queue.put(("waveform_done", (req_seq, path, info, peaks, cache_wav)))

        threading.Thread(target=_load_worker, daemon=True).start()

    def _on_waveform_range_changed(self, start_sec: float, end_sec: float) -> None:
        self.editor_start_var.set(format_timestamp(start_sec))
        self.editor_end_var.set(format_timestamp(end_sec))
        self.editor_sel_dur_var.set(f"Duración sel.: {format_timestamp(max(0.0, end_sec - start_sec))}")
        self._update_suggested_trim_name(force=False)

    def _on_editor_name_typed(self, _event: object = None) -> None:
        self._custom_name_edited = True

    def _update_suggested_trim_name(self, force: bool = False) -> None:
        if not self.editor_file_path:
            return
        if self._custom_name_edited and not force:
            return
        if force:
            self._custom_name_edited = False
        s_tag = format_timestamp_for_filename(self.waveform.start_sec)
        e_tag = format_timestamp_for_filename(self.waveform.end_sec)
        self.editor_output_name_var.set(f"{self.editor_file_path.stem}_recorte_{s_tag}_a_{e_tag}")

    def _nudge_editor_time(self, target: str, delta: float) -> None:
        if self.waveform.duration <= 0.05:
            return
        if target == "start":
            self.waveform.set_range(
                self.waveform.start_sec + delta, self.waveform.end_sec, notify=True
            )
        else:
            self.waveform.set_range(
                self.waveform.start_sec, self.waveform.end_sec + delta, notify=True
            )

    def _apply_editor_time_entries(self) -> bool:
        if self.waveform.duration <= 0.05:
            return False
        try:
            s = parse_timestamp(self.editor_start_var.get())
            e = parse_timestamp(self.editor_end_var.get())
        except ValueError as exc:
            messagebox.showwarning("Tiempo inválido", str(exc), parent=self.root)
            return False

        if e > self.waveform.duration:
            e = self.waveform.duration
        if s >= e - 0.05:
            messagebox.showwarning(
                "Rango inválido",
                "El tiempo de Inicio debe ser menor que el de Fin (al menos 0.05 s de diferencia).",
                parent=self.root,
            )
            return False

        self.waveform.set_range(s, e, notify=True)
        return True

    def _on_editor_select_all(self) -> None:
        if self.waveform.duration <= 0.05:
            return
        self.waveform.set_range(0.0, self.waveform.duration, notify=True)

    def _on_editor_format_changed(self, _event: object = None) -> None:
        self._update_editor_quality_choices(preserve_current=False)

    def _update_editor_quality_choices(self, preserve_current: bool = False) -> None:
        fmt_key = self._selected_editor_format_key()
        if fmt_key == "copy":
            self._editor_quality_label_to_key = {"Original (sin recodificar)": "copy"}
            self.combo_ed_quality["values"] = ["Original (sin recodificar)"]
            self.editor_quality_var.set("Original (sin recodificar)")
            self.combo_ed_quality.configure(state="disabled")
            return

        spec = OUTPUT_FORMATS.get(fmt_key, OUTPUT_FORMATS["mp3"])
        qualities: list[tuple[str, str]] = list(spec["qualities"])  # type: ignore[arg-type]
        self._editor_quality_label_to_key = {lbl: k for k, lbl in qualities}
        labels = [lbl for _, lbl in qualities]
        self.combo_ed_quality["values"] = labels
        if not self.is_running:
            self.combo_ed_quality.configure(state="readonly")

        if preserve_current and self.editor_quality_var.get() in labels:
            return
        default_key = str(spec["default_quality"])
        default_lbl = next((lbl for k, lbl in qualities if k == default_key), labels[0])
        self.editor_quality_var.set(default_lbl)

    def _on_waveform_view_changed(
        self, view_start: float, view_end: float, zoom_factor: float
    ) -> None:
        self.editor_zoom_var.set(f"Zoom: {zoom_factor:.1f}x")
        dur = self.waveform.duration
        if dur > 0.0 and zoom_factor > 1.001:
            self.waveform_hscroll.set(view_start / dur, view_end / dur)
        else:
            self.waveform_hscroll.set(0.0, 1.0)

    def _on_waveform_hscroll(self, *args: str) -> None:
        if not args:
            return
        if args[0] == "moveto" and len(args) >= 2:
            self.waveform.pan_to_fraction(float(args[1]))
        elif args[0] == "scroll" and len(args) >= 3:
            count = int(args[1])
            unit = args[2]
            step = 0.15 if unit == "units" else 0.75
            self.waveform.scroll_view(count * step)

    def _on_editor_zoom_in(self) -> None:
        self.waveform.set_zoom(self.waveform.zoom_factor * 1.5)

    def _on_editor_zoom_out(self) -> None:
        self.waveform.set_zoom(self.waveform.zoom_factor / 1.5)

    def _on_editor_zoom_selection(self) -> None:
        self.waveform.zoom_to_selection()

    def _on_editor_zoom_reset(self) -> None:
        self.waveform.set_zoom(1.0)

    def _current_playback_position(self) -> float:
        if self._is_playing and self._ffplay_proc is not None:
            if self._hw_clock_ready:
                elapsed = (time.monotonic() - self._hw_clock_mono) * self._play_speed
                return min(self.waveform.duration, max(0.0, self._hw_audio_pos + elapsed))
            return self._play_base_sec
        if self.waveform.playhead_sec is not None:
            return self.waveform.playhead_sec
        return self._current_play_pos

    def _update_playhead_display(self, pos_sec: float) -> None:
        dur = self.waveform.duration
        pos = max(0.0, min(dur, float(pos_sec))) if dur > 0.0 else 0.0
        self._current_play_pos = pos
        self.waveform.set_playhead(pos if dur > 0.0 else None)
        if dur > 0.0:
            self.waveform.ensure_time_visible(pos)

        if not self._user_dragging_seek:
            self._updating_seek_scale = True
            self.editor_seek_var.set(pos)
            self._updating_seek_scale = False

        if dur > 0.0:
            self.editor_play_pos_var.set(
                f"Posición: {format_timestamp(pos)} / {format_timestamp(dur)}"
            )
            if self.root.focus_get() != getattr(self, "entry_ed_goto", None):
                self.editor_goto_var.set(format_timestamp(pos))
        else:
            self.editor_play_pos_var.set("Posición: --:--")

    def _kill_ffplay_only(self) -> None:
        self._ffplay_gen += 1
        self._hw_clock_ready = False
        if self._play_after_id is not None:
            try:
                self.root.after_cancel(self._play_after_id)
            except tk.TclError:
                pass
            self._play_after_id = None

        if self._ffplay_proc is not None:
            try:
                if self._ffplay_proc.poll() is None:
                    self._ffplay_proc.terminate()
                    self._ffplay_proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._ffplay_proc.kill()
                except OSError:
                    pass
            try:
                if self._ffplay_proc.stderr is not None:
                    self._ffplay_proc.stderr.close()
            except OSError:
                pass
            self._ffplay_proc = None

    def _read_ffplay_clock(self, proc: subprocess.Popen[bytes], my_gen: int) -> None:
        """Lee en tiempo real el reloj maestro de ffplay -stats para sincronizar el cursor."""
        if proc.stderr is None:
            return
        buf = ""
        clock_re = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+M-A:")
        while True:
            try:
                ch = proc.stderr.read(1)
            except (OSError, ValueError):
                break
            if not ch:
                break
            c = ch.decode("utf-8", errors="replace")
            if c in ("\r", "\n"):
                line = buf.strip()
                buf = ""
                if line:
                    m = clock_re.match(line)
                    if m and self._ffplay_gen == my_gen:
                        try:
                            val = float(m.group(1))
                            delta_real = max(0.0, val - self._play_base_sec)
                            self._hw_audio_pos = self._play_base_sec + delta_real * self._play_speed
                            self._hw_clock_mono = time.monotonic()
                            self._hw_clock_ready = True
                        except ValueError:
                            pass
            else:
                buf += c

    def _start_playback_from(self, pos_sec: float, free_limit: bool = False) -> None:
        if not self.editor_file_path or not self.ffmpeg_loc.ffplay_path:
            return
        dur_total = self.waveform.duration
        if dur_total <= 0.05:
            return

        self._kill_ffplay_only()
        my_gen = self._ffplay_gen
        start_pos = max(0.0, min(max(0.0, dur_total - 0.05), float(pos_sec)))
        if free_limit or start_pos >= self.waveform.end_sec - 0.05:
            self._play_limit_sec = dur_total
        else:
            self._play_limit_sec = self.waveform.end_sec

        self._play_speed = self._speed_label_to_val.get(self.editor_speed_var.get(), 1.0)
        playback_target = (
            self._editor_cache_wav
            if self._editor_cache_wav is not None and self._editor_cache_wav.is_file()
            else self.editor_file_path
        )
        cmd = [
            self.ffmpeg_loc.ffplay_path,
            "-nodisp",
            "-autoexit",
            "-stats",
            "-loglevel",
            "quiet",
            "-ss",
            f"{start_pos:.4f}",
        ]
        atempo = build_atempo_filter(self._play_speed)
        if atempo:
            cmd.extend(["-af", atempo])
        cmd.append(str(playback_target))

        try:
            self._ffplay_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_SUBPROCESS_FLAGS,
            )
        except OSError as exc:
            self._is_playing = False
            messagebox.showerror(
                "Error al reproducir",
                f"No se pudo iniciar la reproducción con ffplay: {exc}",
                parent=self.root,
            )
            return

        self._is_playing = True
        self._is_paused = False
        self._hw_clock_ready = False
        self._play_started_mono = time.monotonic()
        self._play_base_sec = start_pos
        self._hw_audio_pos = start_pos
        self._hw_clock_mono = self._play_started_mono

        threading.Thread(
            target=self._read_ffplay_clock,
            args=(self._ffplay_proc, my_gen),
            daemon=True,
        ).start()

        self.btn_ed_play.configure(text="⏸ Pausar")
        self.btn_ed_stop.configure(state="normal")
        self._update_playhead_display(start_pos)
        self._playback_tick()

    def _on_editor_toggle_play_pause(self) -> None:
        if not self.editor_file_path or not self.editor_media_info or not self.editor_media_info.has_audio:
            messagebox.showinfo(
                "Sin audio cargado",
                "Carga primero un archivo de video o audio en el editor.",
                parent=self.root,
            )
            return

        if self._is_playing:
            pos = self._current_playback_position()
            self._kill_ffplay_only()
            self._is_playing = False
            self._is_paused = True
            self._update_playhead_display(pos)
            self.btn_ed_play.configure(text="▶ Reanudar")
            self.btn_ed_stop.configure(state="normal")
            return

        self._refresh_ffmpeg_status()
        if not self.ffmpeg_loc.has_ffplay or not self.ffmpeg_loc.ffplay_path:
            messagebox.showwarning(
                "ffplay no disponible",
                "No se encontró ffplay.exe junto a ffmpeg.exe ni en el PATH para reproducir la vista previa.",
                parent=self.root,
            )
            return

        if not self._apply_editor_time_entries():
            return

        if self._is_paused and self._current_play_pos < self.waveform.duration - 0.05:
            start_from = self._current_play_pos
        elif (
            abs(self._current_play_pos - self.waveform.start_sec) > 0.05
            and self._current_play_pos < self.waveform.duration - 0.05
        ):
            start_from = self._current_play_pos
        else:
            start_from = self.waveform.start_sec

        self._start_playback_from(start_from, free_limit=False)

    def _playback_tick(self) -> None:
        if not self._is_playing or self._ffplay_proc is None:
            return
        if self._ffplay_proc.poll() is not None:
            self._stop_playback()
            return

        pos = self._current_playback_position()
        if pos >= self._play_limit_sec:
            self._stop_playback()
            return

        self._update_playhead_display(pos)
        self._play_after_id = self.root.after(50, self._playback_tick)

    def _stop_playback(self) -> None:
        self._kill_ffplay_only()
        self._is_playing = False
        self._is_paused = False
        reset_pos = self.waveform.start_sec if self.waveform.duration > 0.0 else 0.0
        self._update_playhead_display(reset_pos)
        if hasattr(self, "btn_ed_play"):
            self.btn_ed_play.configure(text="▶ Escuchar")
        if hasattr(self, "btn_ed_stop"):
            self.btn_ed_stop.configure(state="disabled")

    def _seek_playback(self, target_sec: float) -> None:
        if self.waveform.duration <= 0.05:
            return
        pos = max(0.0, min(self.waveform.duration, float(target_sec)))
        self._update_playhead_display(pos)
        if self._is_playing:
            self._start_playback_from(pos, free_limit=True)
        else:
            self._is_paused = True
            self.btn_ed_play.configure(text="▶ Reanudar")
            self.btn_ed_stop.configure(state="normal")

    def _skip_playback(self, delta_sec: float) -> None:
        if self.waveform.duration <= 0.05:
            return
        base = self._current_playback_position()
        self._seek_playback(base + float(delta_sec))

    def _on_editor_speed_changed(self, _event: object = None) -> None:
        self._play_speed = self._speed_label_to_val.get(self.editor_speed_var.get(), 1.0)
        if self._is_playing:
            pos = self._current_playback_position()
            self._start_playback_from(pos, free_limit=True)

    def _on_seek_scale_press(self, _event: object = None) -> None:
        self._user_dragging_seek = True

    def _on_seek_scale_changed(self, value_str: str) -> None:
        if self._updating_seek_scale or self.waveform.duration <= 0.05:
            return
        if self._user_dragging_seek:
            try:
                pos = max(0.0, min(self.waveform.duration, float(value_str)))
            except ValueError:
                return
            self._current_play_pos = pos
            self.waveform.set_playhead(pos)
            self.waveform.ensure_time_visible(pos)
            self.editor_play_pos_var.set(
                f"Posición: {format_timestamp(pos)} / {format_timestamp(self.waveform.duration)}"
            )

    def _on_seek_scale_release(self, _event: object = None) -> None:
        self._user_dragging_seek = False
        if self.waveform.duration > 0.05:
            self._seek_playback(float(self.editor_seek_var.get()))

    def _on_editor_goto_time(self) -> None:
        if self.waveform.duration <= 0.05:
            return
        try:
            t = parse_timestamp(self.editor_goto_var.get())
        except ValueError as exc:
            messagebox.showwarning("Tiempo inválido", str(exc), parent=self.root)
            return
        self._seek_playback(t)

    def _on_editor_arrow_key(self, _event: object, delta_sec: float) -> None:
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, ttk.Entry, tk.Text)):
            return
        if self.notebook.index(self.notebook.select()) != 1:
            return
        self._skip_playback(delta_sec)

    def _on_editor_mark_here(self, target: str) -> None:
        if self.waveform.duration <= 0.05:
            messagebox.showinfo(
                "Sin audio cargado",
                "Carga primero un archivo en el editor para mover el cursor y marcar el rango.",
                parent=self.root,
            )
            return
        pos = self._current_playback_position()
        if target == "start":
            new_end = self.waveform.end_sec
            if pos >= new_end - 0.05:
                new_end = self.waveform.duration
            new_start = max(0.0, min(pos, new_end - 0.05))
            self.waveform.set_range(new_start, new_end, notify=True)
        else:
            new_start = self.waveform.start_sec
            if pos <= new_start + 0.05:
                new_start = 0.0
            new_end = min(self.waveform.duration, max(pos, new_start + 0.05))
            self.waveform.set_range(new_start, new_end, notify=True)

    def _validate_editor_ready(self) -> Optional[tuple[Path, MediaInfo, str, Optional[Path], bool]]:
        if self.is_running:
            return None
        if not self.editor_file_path or not self.editor_media_info or not self.editor_media_info.has_audio:
            messagebox.showwarning(
                "Sin archivo cargado",
                "Abre primero un archivo de video o audio válido en el Editor.",
                parent=self.root,
            )
            return None

        self._refresh_ffmpeg_status()
        if not self.ffmpeg_loc.is_ready or not self.ffmpeg_loc.ffmpeg_path:
            messagebox.showerror(
                "FFmpeg no disponible",
                "No se encontró el ejecutable de FFmpeg. Configura la ruta de ffmpeg.exe para continuar.",
                parent=self.root,
            )
            return None

        use_same = bool(self.same_folder_var.get())
        custom_dest = Path(self.dest_folder_var.get().strip()) if not use_same else None
        if not use_same and not self.dest_folder_var.get().strip():
            messagebox.showwarning(
                "Carpeta requerida",
                "Indica una carpeta de destino o marca 'Guardar en la misma carpeta del archivo original'.",
                parent=self.root,
            )
            return None

        if not self._apply_editor_time_entries():
            return None

        self._save_preferences()
        overwrite = self._selected_overwrite_key() == "overwrite"
        return (
            self.editor_file_path,
            self.editor_media_info,
            self.ffmpeg_loc.ffmpeg_path,
            custom_dest,
            overwrite,
        )

    def _collect_editor_audio_options(self) -> tuple[str, str, float, float, float]:
        fmt_key = self._selected_editor_format_key()
        q_key = self._selected_editor_quality_key()
        vol = self._volume_label_to_val.get(self.editor_volume_var.get(), 1.0)
        fade_in = self._fade_label_to_val.get(self.editor_fade_in_var.get(), 0.0)
        fade_out = self._fade_label_to_val.get(self.editor_fade_out_var.get(), 0.0)
        return fmt_key, q_key, vol, fade_in, fade_out

    def _run_editor_task(self, task_title: str, worker_fn: Callable[[], str]) -> None:
        self._stop_playback()
        self.cancel_event.clear()
        self._set_controls_running(True)
        self.file_progress_var.set(0.0)
        self.batch_progress_var.set(0.0)
        self.file_pct_var.set("0%")
        self.batch_status_var.set("Editor: en progreso")
        self.current_file_var.set(task_title)

        def _runner() -> None:
            summary = worker_fn()
            self.ui_queue.put(("editor_finished", (summary, self.cancel_event.is_set())))

        threading.Thread(target=_runner, daemon=True).start()

    def _on_editor_trim_single(self) -> None:
        ctx = self._validate_editor_ready()
        if ctx is None:
            return
        src, info, ffmpeg_bin, custom_dest, overwrite = ctx
        fmt_key, q_key, vol, fade_in, fade_out = self._collect_editor_audio_options()
        start_s = self.waveform.start_sec
        end_s = self.waveform.end_sec
        custom_stem = self.editor_output_name_var.get().strip() or None
        pcm_cache = self._editor_cache_wav

        def _worker() -> str:
            res = trim_and_export_segment(
                input_path=src,
                ffmpeg_path=ffmpeg_bin,
                start_sec=start_s,
                end_sec=end_s,
                target_format_key=fmt_key,
                quality_key=q_key,
                output_dir=custom_dest,
                custom_stem=custom_stem,
                overwrite=overwrite,
                source_codec=info.audio_codec,
                volume_factor=vol,
                fade_in_sec=fade_in,
                fade_out_sec=fade_out,
                pcm_source_path=pcm_cache,
                progress_callback=lambda p: self.ui_queue.put(("file_progress", (p,))),
                log_callback=lambda lvl, m: self.ui_queue.put(("log", (lvl, m))),
                cancel_event=self.cancel_event,
            )
            self.ui_queue.put(("batch_progress", (1, 1)))
            if res.cancelled:
                return "Recorte cancelado por el usuario."
            if res.success and res.output_path:
                self.ui_queue.put(("log", ("EXITO", f"Recorte guardado en: {res.output_path}")))
                return f"Recorte completado: {res.output_path.name}"
            self.ui_queue.put(("log", ("ERROR", f"Error al recortar: {res.error_message}")))
            return "El recorte finalizó con errores."

        self._run_editor_task(f"Recortando {src.name}...", _worker)

    def _on_editor_split(self, mode: str) -> None:
        ctx = self._validate_editor_ready()
        if ctx is None:
            return
        src, info, ffmpeg_bin, custom_dest, overwrite = ctx
        total_dur = info.duration_seconds or self.waveform.duration
        fmt_key, q_key, vol, fade_in, fade_out = self._collect_editor_audio_options()
        start_s = self.waveform.start_sec
        end_s = self.waveform.end_sec
        pcm_cache = self._editor_cache_wav

        def _worker() -> str:
            results = split_audio_track(
                input_path=src,
                ffmpeg_path=ffmpeg_bin,
                total_duration=total_dur,
                mode=mode,
                cut_sec=start_s,
                start_sec=start_s,
                end_sec=end_s,
                target_format_key=fmt_key,
                quality_key=q_key,
                output_dir=custom_dest,
                overwrite=overwrite,
                source_codec=info.audio_codec,
                volume_factor=vol,
                fade_in_sec=fade_in,
                fade_out_sec=fade_out,
                pcm_source_path=pcm_cache,
                progress_callback=lambda p: (
                    self.ui_queue.put(("file_progress", (p,))),
                    self.ui_queue.put(("batch_progress", (int(p), 100))),
                ),
                log_callback=lambda lvl, m: self.ui_queue.put(("log", (lvl, m))),
                cancel_event=self.cancel_event,
            )
            ok_paths = [r.output_path for r in results if r.success and r.output_path]
            for out_p in ok_paths:
                self.ui_queue.put(("log", ("EXITO", f"Parte generada: {out_p}")))
            errors = [r.error_message for r in results if not r.success and not r.cancelled]
            for err in errors:
                self.ui_queue.put(("log", ("ERROR", f"Error al separar: {err}")))
            if any(r.cancelled for r in results):
                return "Separación cancelada."
            return f"Separación finalizada: {len(ok_paths)} archivo(s) creados."

        desc = "en 2 partes" if mode == "two_parts" else "en 3 partes"
        self._run_editor_task(f"Separando {src.name} {desc}...", _worker)

    def _on_editor_remove_segment(self) -> None:
        ctx = self._validate_editor_ready()
        if ctx is None:
            return
        src, info, ffmpeg_bin, custom_dest, overwrite = ctx
        total_dur = info.duration_seconds or self.waveform.duration
        fmt_key, q_key, vol, fade_in, fade_out = self._collect_editor_audio_options()
        start_s = self.waveform.start_sec
        end_s = self.waveform.end_sec
        custom_stem = self.editor_output_name_var.get().strip()
        if not custom_stem or "_recorte_" in custom_stem:
            custom_stem = f"{src.stem}_sin_fragmento"
        pcm_cache = self._editor_cache_wav

        def _worker() -> str:
            res = remove_segment_and_join(
                input_path=src,
                ffmpeg_path=ffmpeg_bin,
                total_duration=total_dur,
                start_sec=start_s,
                end_sec=end_s,
                target_format_key=fmt_key,
                quality_key=q_key,
                output_dir=custom_dest,
                custom_stem=custom_stem,
                overwrite=overwrite,
                source_codec=info.audio_codec,
                volume_factor=vol,
                fade_in_sec=fade_in,
                fade_out_sec=fade_out,
                pcm_source_path=pcm_cache,
                progress_callback=lambda p: self.ui_queue.put(("file_progress", (p,))),
                log_callback=lambda lvl, m: self.ui_queue.put(("log", (lvl, m))),
                cancel_event=self.cancel_event,
            )
            self.ui_queue.put(("batch_progress", (1, 1)))
            if res.cancelled:
                return "Operación cancelada."
            if res.success and res.output_path:
                self.ui_queue.put(("log", ("EXITO", f"Archivo unido guardado en: {res.output_path}")))
                return f"Tramo eliminado y audio unido: {res.output_path.name}"
            self.ui_queue.put(("log", ("ERROR", f"Error al unir el resto: {res.error_message}")))
            return "No se pudo completar la unión del audio."

        self._run_editor_task(f"Eliminando tramo de {src.name}...", _worker)

    def _on_editor_add_clip(self) -> None:
        if not self.editor_file_path or self.waveform.duration <= 0.05:
            messagebox.showinfo(
                "Sin archivo cargado",
                "Carga un archivo y selecciona un tramo para agregarlo a la lista.",
                parent=self.root,
            )
            return
        if not self._apply_editor_time_entries():
            return

        start_s = self.waveform.start_sec
        end_s = self.waveform.end_sec
        self._clip_counter += 1
        clip_id = f"clip_{self._clip_counter}"
        stem = self.editor_output_name_var.get().strip()
        if not stem:
            stem = f"{self.editor_file_path.stem}_fragmento_{self._clip_counter}"
        # Si ya existe un fragmento con ese nombre en la lista, enumerar
        existing_stems = {c.output_stem for c in self.editor_clips.values()}
        if stem in existing_stems:
            stem = f"{stem}_parte{self._clip_counter}"

        clip = EditorClipItem(
            clip_id=clip_id,
            start_sec=start_s,
            end_sec=end_s,
            output_stem=stem,
        )
        self.editor_clips[clip_id] = clip
        self.editor_clip_order.append(clip_id)
        self.clips_tree.insert(
            "",
            "end",
            iid=clip_id,
            values=(
                format_timestamp(start_s),
                format_timestamp(end_s),
                format_timestamp(end_s - start_s),
                stem,
            ),
        )

    def _on_editor_remove_clip(self) -> None:
        for cid in self.clips_tree.selection():
            self.clips_tree.delete(cid)
            self.editor_clips.pop(cid, None)
            if cid in self.editor_clip_order:
                self.editor_clip_order.remove(cid)

    def _on_editor_clear_clips(self) -> None:
        for cid in list(self.editor_clip_order):
            if self.clips_tree.exists(cid):
                self.clips_tree.delete(cid)
        self.editor_clips.clear()
        self.editor_clip_order.clear()

    def _on_editor_export_clips(self) -> None:
        if not self.editor_clip_order:
            messagebox.showinfo(
                "Lista vacía",
                "Agrega al menos un fragmento con '+ Agregar selección a lista'.",
                parent=self.root,
            )
            return
        ctx = self._validate_editor_ready()
        if ctx is None:
            return
        src, info, ffmpeg_bin, custom_dest, overwrite = ctx
        fmt_key, q_key, vol, fade_in, fade_out = self._collect_editor_audio_options()
        clips_snapshot = [self.editor_clips[cid] for cid in self.editor_clip_order if cid in self.editor_clips]
        total = len(clips_snapshot)
        pcm_cache = self._editor_cache_wav

        def _worker() -> str:
            ok_count = 0
            err_count = 0
            for idx, clip in enumerate(clips_snapshot, start=1):
                if self.cancel_event.is_set():
                    break
                self.ui_queue.put(("current_file", (clip.output_stem, idx, total)))
                res = trim_and_export_segment(
                    input_path=src,
                    ffmpeg_path=ffmpeg_bin,
                    start_sec=clip.start_sec,
                    end_sec=clip.end_sec,
                    target_format_key=fmt_key,
                    quality_key=q_key,
                    output_dir=custom_dest,
                    custom_stem=clip.output_stem,
                    overwrite=overwrite,
                    source_codec=info.audio_codec,
                    volume_factor=vol,
                    fade_in_sec=fade_in,
                    fade_out_sec=fade_out,
                    pcm_source_path=pcm_cache,
                    progress_callback=lambda p: self.ui_queue.put(("file_progress", (p,))),
                    log_callback=lambda lvl, m: self.ui_queue.put(("log", (lvl, m))),
                    cancel_event=self.cancel_event,
                )
                if res.cancelled:
                    break
                if res.success and res.output_path:
                    ok_count += 1
                    self.ui_queue.put(("log", ("EXITO", f"Fragmento guardado: {res.output_path}")))
                else:
                    err_count += 1
                    self.ui_queue.put(("log", ("ERROR", f"Error en '{clip.output_stem}': {res.error_message}")))
                self.ui_queue.put(("batch_progress", (idx, total)))

            if self.cancel_event.is_set():
                return f"Exportación de fragmentos cancelada. Completados: {ok_count}/{total}."
            return f"Fragmentos exportados: {ok_count} exitosos, {err_count} errores."

        self._run_editor_task(f"Exportando {total} fragmento(s)...", _worker)

    def _on_close(self) -> None:
        if self.is_running:
            if not messagebox.askyesno(
                "Conversión en curso",
                "Hay una operación en progreso. ¿Deseas cancelar y cerrar la aplicación?",
                parent=self.root,
            ):
                return
            self.cancel_event.set()
        self._stop_playback()
        self._save_preferences()
        self.root.destroy()
        shutil.rmtree(self._editor_temp_dir, ignore_errors=True)


def main() -> None:
    """Punto de entrada principal de la aplicación gráfica."""
    root = tk.Tk()
    AudioExtractorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
