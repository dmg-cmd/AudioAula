<p align="center">
  <img src="../assets/AudioAula.png" alt="Logo oficial de AudioAula" width="112" />
</p>

# Documentación de AudioAula

Bienvenido al centro de documentación de **AudioAula**, una herramienta de escritorio libre y de código abierto (Licencia MIT) pensada para que docentes, formadores y estudiantes puedan **extraer, convertir, recortar y separar audio** a partir de videos o pistas de audio utilizando **FFmpeg**, sin suscripciones ni dependencias externas de `pip`.

---

## Índice de documentos

| Documento | Propósito |
| :--- | :--- |
| **[MANUAL_USO.md](MANUAL_USO.md)** | Manual paso a paso para docentes: instalación de FFmpeg, extracción por lotes, compatibilidad de códecs, editor visual de forma de onda, atajos y solución de problemas. |
| **[HISTORIAL.md](HISTORIAL.md)** | Registro cronológico del desarrollo, arquitectura interna de `app.py` y `converter_core.py`, decisiones de diseño y guía técnica para futuras reformas. |
| **[README principal](../README.md)** | Presentación general del repositorio, características y puesta en marcha rápida. |
| **[LICENSE](../LICENSE)** | Texto legal de la Licencia MIT bajo la que se distribuye este proyecto. |

---

## Estructura del repositorio

```text
AudioAula/
├── app.py                        # Interfaz gráfica Tkinter (pestañas Lotes y Editor + Canvas de onda)
├── converter_core.py             # Núcleo FFmpeg/ffprobe, caché WAV, recorte, división y configuración
├── compilar_exe.ps1              # Script PowerShell para compilar AudioAula.exe con PyInstaller
├── assets/
│   ├── AudioAula.png             # Icono oficial en PNG (512x512 RGBA)
│   └── AudioAula.ico             # Icono multi-resolución para Windows (.exe y ventana)
├── docs/
│   ├── README.md                 # Este índice de documentación
│   ├── MANUAL_USO.md             # Manual completo de usuario
│   └── HISTORIAL.md              # Historial del proyecto y arquitectura para mantenimiento
├── .github/
│   └── workflows/
│       └── release.yml           # Compilación automática de AudioAula.exe y publicación de Releases
├── .gitignore                    # Exclusiones (config.json, build/, dist/, *.exe, docs/iconos/)
├── LICENSE                       # Licencia MIT
└── README.md                     # Portada del repositorio
```

> **Nota sobre archivos locales excluidos de Git**: `config.json` (preferencias locales del equipo), `docs/iconos/` (borradores de diseño en alta resolución) y los artefactos generados al compilar (`build/`, `dist/`, `*.spec`, `*.exe`) están excluidos mediante [`.gitignore`](../.gitignore).

---

## Publicar una nueva versión (*Release*)

El flujo automático configurado en [`.github/workflows/release.yml`](../.github/workflows/release.yml) compila `AudioAula.exe` en GitHub Actions, calcula su suma de verificación `SHA-256` y crea el lanzamiento en la pestaña **Releases** de GitHub cuando se sube una etiqueta con prefijo `v`:

```powershell
git tag -a v1.0.1 -m "AudioAula v1.0.1: descripción breve de los cambios"
git push origin v1.0.1
```

También puede lanzarse manualmente desde la pestaña **Actions** de GitHub mediante el botón **Run workflow** (`workflow_dispatch`).
