"""
GUI Osciloscopio - Graficador de archivos CSV (osciloscopio / FRA) y LTspice (.raw)
Soporta superposicion de multiples archivos.

Requisitos:
    pip install matplotlib pandas numpy ltspice tkinterdnd2

Uso:
    python osciloscopio_gui.py
"""

import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
import numpy as np
import pandas as pd

import matplotlib
matplotlib.rcParams['font.family'] = 'DejaVu Sans'
matplotlib.rcParams['font.size'] = 9
matplotlib.use("TkAgg")

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.widgets import SubplotTool
import matplotlib.ticker as ticker

# ─────────────────────────────────────────────
#  Drag & Drop — backend segun plataforma
#  Windows : ctypes nativo (WM_DROPFILES)
#  Linux   : tkinterdnd2
# ─────────────────────────────────────────────
import sys
import urllib.parse

DND_BACKEND = None   # "windows" | "tkinterdnd2" | None

if sys.platform == "win32":
    try:
        import ctypes, ctypes.wintypes
        DND_BACKEND = "windows"
    except Exception:
        DND_BACKEND = None
else:
    try:
        from tkinterdnd2 import TkDnD, DND_FILES
        DND_BACKEND = "tkinterdnd2"
    except Exception:
        DND_BACKEND = None

DND_AVAILABLE = DND_BACKEND is not None

# ── Implementacion Windows: WM_DROPFILES via ctypes ──────────────────────────
if sys.platform == "win32":
    _WM_DROPFILES = 0x0233
    _GWL_WNDPROC  = -4

    # En 32-bit los punteros son 4 bytes; en 64-bit son 8 bytes.
    # WNDPROC siempre usa c_long como retorno en ambas arquitecturas.
    _WNDPROCTYPE = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.wintypes.HWND,
        ctypes.c_uint,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    )

    # Setear tipos de retorno correctos para las funciones que usamos
    ctypes.windll.shell32.DragQueryFileW.restype  = ctypes.c_uint
    ctypes.windll.shell32.DragAcceptFiles.restype = None
    ctypes.windll.shell32.DragFinish.restype      = None

    _dnd_cb      = None   # referencia global para evitar GC
    _old_wndproc = None

    def _win_enable_drop(hwnd, callback):
        global _dnd_cb, _old_wndproc

        # Habilitar drops en la ventana
        ctypes.windll.shell32.DragAcceptFiles(hwnd, True)

        # En Windows Vista+ con UAC, necesitamos esto para recibir
        # WM_DROPFILES desde procesos de menor privilegio (Explorer)
        try:
            # MSGFLT_ALLOW = 1
            ctypes.windll.user32.ChangeWindowMessageFilterEx(
                hwnd, _WM_DROPFILES, 1, None)
            # Tambien necesitamos habilitar WM_COPYDATA (0x004A)
            # que a veces viene junto
            ctypes.windll.user32.ChangeWindowMessageFilterEx(
                hwnd, 0x0049, 1, None)  # WM_COPYGLOBALDATA
        except Exception:
            pass

        def _wndproc(h, msg, wparam, lparam):
            if msg == _WM_DROPFILES:
                hdrop = ctypes.wintypes.HANDLE(wparam)
                # Obtener cantidad de archivos
                count = ctypes.windll.shell32.DragQueryFileW(
                    hdrop, 0xFFFFFFFF, None, 0)
                paths = []
                buf = ctypes.create_unicode_buffer(4096)
                for i in range(count):
                    ctypes.windll.shell32.DragQueryFileW(
                        hdrop, i, buf, ctypes.sizeof(buf) // 2)
                    paths.append(buf.value)
                ctypes.windll.shell32.DragFinish(hdrop)
                # Llamar callback en el hilo principal de Tk
                callback(paths)
                return 0
            # Pasar al wndproc original
            return ctypes.windll.user32.CallWindowProcW(
                _old_wndproc, h, msg, wparam, lparam)

        _dnd_cb = _WNDPROCTYPE(_wndproc)

        # Seleccionar SetWindowLong segun arquitectura
        try:
            _SetWL = ctypes.windll.user32.SetWindowLongPtrW
        except AttributeError:
            _SetWL = ctypes.windll.user32.SetWindowLongW

        _SetWL.restype = ctypes.c_long
        _old_wndproc = _SetWL(hwnd, _GWL_WNDPROC, _dnd_cb)

try:
    import ltspice
    LTSPICE_AVAILABLE = True
except ImportError:
    LTSPICE_AVAILABLE = False

# ─────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────
TIME_UNITS = {
    "s":  (1,    "Tiempo (s)"),
    "ms": (1e3,  "Tiempo (ms)"),
    "us": (1e6,  "Tiempo (us)"),
    "ns": (1e9,  "Tiempo (ns)"),
}
VOLT_UNITS = {
    "V":  (1,    "Voltaje (V)"),
    "mV": (1e3,  "Voltaje (mV)"),
}
COLORS = [
    "#00BFFF", "#FF6347", "#7CFC00", "#FFD700",
    "#FF69B4", "#BA55D3", "#FF8C00", "#00CED1",
    "#FF4500", "#ADFF2F", "#1E90FF", "#FF1493",
    "#00FA9A", "#FF8C69", "#9370DB", "#20B2AA",
]

LEGEND_LOCS = [
    "upper right", "upper left", "lower left", "lower right",
    "center left", "center right", "upper center", "lower center", "center",
]

# ─────────────────────────────────────────────
#  Helpers de lectura
# ─────────────────────────────────────────────

def _open_text(path):
    for enc in ("utf-8", "latin-1", "cp1252", "utf-8-sig"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read(), enc
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, "r", encoding="latin-1", errors="replace") as f:
        return f.read(), "latin-1"


def detect_csv_type(path):
    content, _ = _open_text(path)
    first = content.splitlines()[0].strip() if content else ""
    return "fra" if first.startswith("#") else "osc"


# ─────────────────────────────────────────────
#  Parsers CSV
# ─────────────────────────────────────────────

def load_csv_osc(path):
    _, enc = _open_text(path)
    header_df = pd.read_csv(path, header=None, nrows=2, encoding=enc)
    col_names = header_df.iloc[0].tolist()
    col_units = header_df.iloc[1].tolist()
    data_df   = pd.read_csv(path, header=None, skiprows=2, encoding=enc)
    if data_df.shape[1] < 2:
        raise ValueError("El CSV no tiene suficientes columnas.")
    new_cols = [f"{n} [{u}]" for n, u in zip(col_names, col_units)]
    data_df.columns = new_cols[:data_df.shape[1]]
    data_df = data_df.apply(pd.to_numeric, errors='coerce').dropna()
    x_unit = str(col_units[0]).strip().lower()
    y_unit = str(col_units[1]).strip().lower()
    return data_df, x_unit, y_unit


def load_csv_fra(path):
    _, enc = _open_text(path)
    df = pd.read_csv(path, header=0, encoding=enc)
    df.columns = [c.strip() for c in df.columns]
    cols = list(df.columns)
    freq_col = next((c for c in cols if "freq" in c.lower() or "hz" in c.lower()), None)
    if freq_col is None:
        freq_col = cols[1] if len(cols) > 1 else cols[0]
    data_cols = [c for c in cols if c != cols[0] and c != freq_col]
    df_out = df[[freq_col] + data_cols].copy()
    df_out = df_out.apply(pd.to_numeric, errors='coerce').dropna()
    df_out.rename(columns={freq_col: "frequency [hz]"}, inplace=True)
    return df_out, "hz", "db"


def load_csv(path):
    if not path.lower().endswith(".csv"):
        raise ValueError("El archivo no es un .csv")
    try:
        kind = detect_csv_type(path)
        return load_csv_fra(path) if kind == "fra" else load_csv_osc(path)
    except Exception as e:
        raise ValueError(f"No se pudo leer el archivo CSV: {e}")


# ─────────────────────────────────────────────
#  Parser LTspice .raw
# ─────────────────────────────────────────────

def load_ltspice(path):
    if not LTSPICE_AVAILABLE:
        raise ValueError("La libreria 'ltspice' no esta instalada.\npip install ltspice")
    l = ltspice.Ltspice(path)
    l.parse()

    # get_time() lanza InvalidPhysicalValueRequestedException en simulaciones AC
    # get_frequency() lanza lo mismo en simulaciones transient → usar try/except en ambos
    time = None
    try:
        t = l.get_time()
        if t is not None and len(t) > 0:
            time = t
    except Exception:
        pass

    freq = None
    try:
        f = l.get_frequency()
        if f is not None and len(f) > 0:
            freq = f
    except Exception:
        pass

    if time is not None:
        x_data, x_name, x_unit, is_ac = np.array(time, dtype=float), "time [second]", "second", False
    elif freq is not None:
        x_data, x_name, x_unit, is_ac = np.array(freq, dtype=float), "frequency [hz]", "hz", True
    else:
        raise ValueError("No se encontro eje temporal ni de frecuencia.")

    variables = None
    for attr in ("variables", "_variables", "get_variable_names"):
        candidate = getattr(l, attr, None)
        if candidate is not None:
            variables = candidate() if callable(candidate) else list(candidate)
            if variables:
                break
    if not variables:
        raise ValueError("No se pudieron obtener los nombres de variables del .raw.")

    signal_vars = [v for v in variables if str(v).lower() not in {"time", "frequency"}]
    if not signal_vars:
        raise ValueError("No se encontraron senales en el archivo LTspice.")

    df = pd.DataFrame({x_name: x_data})
    for var in signal_vars:
        try:
            raw = l.get_data(var)
            if raw is None or len(raw) == 0:
                continue
            data = np.array(raw, dtype=complex)
            if is_ac:
                mag = np.abs(data)
                arr = np.where(mag > 0, 20 * np.log10(mag), -200.0)
                col_name = f"{var} [dB]"
            else:
                arr, col_name = data.real, f"{var} [V]"
            if len(arr) == len(x_data):
                df[col_name] = arr
        except Exception:
            continue

    if df.shape[1] < 2:
        raise ValueError("No se pudieron extraer senales del archivo .raw.")
    return df, x_unit, "db" if is_ac else "volt"


# ─────────────────────────────────────────────
#  Clase Layer
# ─────────────────────────────────────────────

class Layer:
    def __init__(self, name, df, x_unit, y_unit, color_offset=0):
        self.name    = name
        self.df      = df
        self.x_col   = df.columns[0]
        self.y_cols  = list(df.columns[1:])
        self.x_unit  = x_unit
        self.y_unit  = y_unit
        self.is_freq = ("hz" in x_unit)
        self.visible = True

        n = len(self.y_cols)
        self.colors           = [COLORS[(color_offset + i) % len(COLORS)] for i in range(n)]
        self.offsets_y        = [0.0] * n   # offset eje Y por canal
        self.offsets_x        = [0.0] * n   # offset eje X por canal
        self.scales           = [1.0] * n
        self.visible_channels = [True] * n
        # Label editable por canal (por defecto: nombre corto de la columna)
        self.labels = [col.split("[")[0].strip() for col in self.y_cols]
        self.label_visible = [True] * n     # si aparece en la leyenda




# ─────────────────────────────────────────────
#  Clase FuncLayer — capa de funcion teorica
# ─────────────────────────────────────────────

class FuncLayer:
    """
    Capa que representa una funcion matematica introducida por el usuario.
    El eje X se define por rango y numero de puntos.
    Las constantes son variables editables que aparecen como sliders/entries.
    """
    def __init__(self, name, color_offset=0):
        self.name          = name
        self.visible       = True
        self.is_freq       = False   # puede cambiarse con el checkbox
        self.kind          = "func"  # distingue de Layer normal

        # Expresion y rango
        self.expr          = ""           # ej: "A * sin(2*pi*f*x)"
        self.x_start       = "0"
        self.x_end         = "1e-3"
        self.n_points      = "1000"
        self.x_log         = False        # generar puntos en escala log

        # Constantes definidas por el usuario: [{name, value, min, max}]
        self.constants     = []

        # Propiedades de trazo (una sola curva por FuncLayer)
        self.color         = COLORS[color_offset % len(COLORS)]
        self.label         = name
        self.label_visible = True
        self.offset_y      = 0.0
        self.offset_x      = 0.0
        self.scale         = 1.0
        self.linestyle     = "-"   # -, --, -., :

    def evaluate(self):
        """
        Evalua la expresion y devuelve (x_data, y_data) o lanza ValueError.
        El namespace incluye numpy y las constantes del usuario.
        """
        try:
            n   = max(2, int(float(self.n_points)))
            x0  = float(self.x_start)
            x1  = float(self.x_end)
            if x0 >= x1:
                raise ValueError("X inicio debe ser menor que X fin.")
            if self.x_log:
                if x0 <= 0:
                    raise ValueError("Para escala log X, X inicio debe ser > 0.")
                x = np.logspace(np.log10(x0), np.log10(x1), n)
            else:
                x = np.linspace(x0, x1, n)
        except Exception as e:
            raise ValueError(f"Rango X invalido: {e}")

        # Namespace con numpy y constantes del usuario
        ns = {
            # numpy
            "np": np, "pi": np.pi, "e": np.e, "inf": np.inf,
            "sin": np.sin, "cos": np.cos, "tan": np.tan,
            "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
            "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
            "exp": np.exp, "log": np.log, "log10": np.log10, "log2": np.log2,
            "sqrt": np.sqrt, "abs": np.abs, "sign": np.sign,
            "floor": np.floor, "ceil": np.ceil,
            "linspace": np.linspace, "logspace": np.logspace,
            # variable independiente
            "x": x,
        }
        # Constantes del usuario
        for c in self.constants:
            try:
                ns[c["name"]] = float(c["value"])
            except Exception:
                pass

        try:
            y = eval(self.expr, {"__builtins__": {}}, ns)
            y = np.asarray(y, dtype=float)
            if y.shape == ():         # escalar -> broadcast
                y = np.full_like(x, float(y))
        except Exception as e:
            raise ValueError(f"Error en la expresion: {e}")

        y = y * self.scale + self.offset_y
        x = x + self.offset_x
        return x, y

# ─────────────────────────────────────────────
#  Toolbar personalizada con boton Home corregido
# ─────────────────────────────────────────────

class FixedToolbar(NavigationToolbar2Tk):
    """
    Toolbar que:
    - Sobreescribe 'home' para restaurar limites exactos del ultimo replot.
    - Sobreescribe 'configure_subplots' para abrir nuestra ventana con entradas numericas.
    """
    def __init__(self, canvas, parent, app):
        self._app = app
        super().__init__(canvas, parent)

    def home(self, *args):
        lims = getattr(self._app, "_home_limits", None)
        if lims:
            xmin, xmax, ymin, ymax = lims
            self._app.ax.set_xlim(xmin, xmax)
            self._app.ax.set_ylim(ymin, ymax)
            self._app.canvas.draw()
        else:
            super().home(*args)

    def configure_subplots(self, *args):
        self._app._open_subplot_config()


# ─────────────────────────────────────────────
#  Ventana "Configure subplots" con entradas numericas
# ─────────────────────────────────────────────

class SubplotConfigWindow(tk.Toplevel):
    """
    Ventana de configuracion de margenes del grafico.
    Tiene sliders Y campos numericos editables.
    """
    PARAMS = [
        ("left",   "Izquierda", 0.0, 1.0, 0.125),
        ("right",  "Derecha",   0.0, 1.0, 0.9),
        ("top",    "Arriba",    0.0, 1.0, 0.9),
        ("bottom", "Abajo",     0.0, 1.0, 0.11),
        ("hspace", "H-espacio", 0.0, 1.0, 0.2),
        ("wspace", "W-espacio", 0.0, 1.0, 0.2),
    ]

    def __init__(self, parent, fig, canvas):
        super().__init__(parent)
        self.title("Configurar margenes")
        self.resizable(False, False)
        self.fig    = fig
        self.canvas = canvas
        self._vars  = {}
        self._sliders = {}
        self._entries = {}
        self._build()

    def _build(self):
        sp = self.fig.subplotpars
        for row, (key, label, lo, hi, default) in enumerate(self.PARAMS):
            current = getattr(sp, key, default)

            ttk.Label(self, text=label, width=12).grid(
                row=row, column=0, padx=8, pady=4, sticky="w")

            var = tk.DoubleVar(value=round(current, 4))
            self._vars[key] = var

            sl = ttk.Scale(self, from_=lo, to=hi, variable=var,
                           orient="horizontal", length=200,
                           command=lambda val, k=key: self._on_slider(k))
            sl.grid(row=row, column=1, padx=4, pady=4)
            self._sliders[key] = sl

            ent = ttk.Entry(self, textvariable=var, width=7)
            ent.grid(row=row, column=2, padx=8, pady=4)
            ent.bind("<Return>",   lambda e, k=key: self._on_entry(k))
            ent.bind("<FocusOut>", lambda e, k=key: self._on_entry(k))
            self._entries[key] = ent

        ttk.Button(self, text="Aplicar", command=self._apply).grid(
            row=len(self.PARAMS), column=0, columnspan=3,
            pady=8, padx=8, sticky="ew")

    def _on_slider(self, key):
        self._apply()

    def _on_entry(self, key):
        try:
            val = float(self._entries[key].get())
            lo = self.PARAMS[[p[0] for p in self.PARAMS].index(key)][2]
            hi = self.PARAMS[[p[0] for p in self.PARAMS].index(key)][3]
            val = max(lo, min(hi, val))
            self._vars[key].set(val)
        except ValueError:
            pass
        self._apply()

    def _apply(self):
        kw = {k: v.get() for k, v in self._vars.items()}
        try:
            self.fig.subplots_adjust(**kw)
            self.canvas.draw()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  ScrollableFrame
# ─────────────────────────────────────────────

class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, width=300, **kwargs):
        super().__init__(parent, **kwargs)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(self, width=width, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=sb.set)

        self.inner = ttk.Frame(self._canvas)
        self._win_id = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

    def _on_inner_configure(self, event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._win_id, width=event.width)

    def _bind_mousewheel(self, event=None):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind_all("<Button-4>",   self._on_mousewheel)
        self._canvas.bind_all("<Button-5>",   self._on_mousewheel)

    def _unbind_mousewheel(self, event=None):
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


# ─────────────────────────────────────────────
#  App principal
# ─────────────────────────────────────────────

class OsciloscopioApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Osciloscopio GUI")
        self.root.minsize(1150, 700)
        self.root.resizable(True, True)

        self.layers        = []
        self.cursor_mode   = False
        self.cursor_clicks = []
        self._home_limits  = None   # (xmin, xmax, ymin, ymax) del ultimo replot
        self._subplot_win  = None   # ventana de configuracion de subplots

        self._build_ui()
        self._setup_dnd()

    # ─── UI ────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._scroll_panel = ScrollableFrame(self.root, width=300)
        self._scroll_panel.grid(row=0, column=0, sticky="ns", padx=(8, 0), pady=8)
        self._scroll_panel.grid_propagate(False)
        self._scroll_panel.configure(width=300)

        left = self._scroll_panel.inner
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(self.root)
        right.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_controls(left)
        self._build_plot(right)

    def _lf(self, parent, text, row):
        f = ttk.LabelFrame(parent, text=text)
        f.grid(row=row, column=0, sticky="ew", padx=4, pady=3)
        f.columnconfigure(1, weight=1)
        return f

    def _build_controls(self, p):
        # ── Archivos ──
        f = self._lf(p, "ARCHIVOS", 0)
        ttk.Button(f, text="+ Agregar CSV",
                   command=self._open_csv).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        lt_txt = "+ Agregar LTspice (.raw)" if LTSPICE_AVAILABLE else "+ Agregar LTspice (*)"
        ttk.Button(f, text=lt_txt,
                   command=self._open_ltspice).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        if not LTSPICE_AVAILABLE:
            ttk.Label(f, text="(*) pip install ltspice",
                      foreground="#ff8800").grid(row=1, column=0, columnspan=2, padx=4, pady=(0,2))
        ttk.Button(f, text="+ Funcion teorica",
                   command=self._add_func_layer).grid(row=1, column=0, columnspan=2,
                                                       padx=4, pady=(0,4), sticky="ew")
        ttk.Button(f, text="Borrar todos",
                   command=self._clear_all).grid(row=2, column=0, columnspan=2,
                                                  padx=4, pady=(0,6), sticky="ew")

        # ── Titulos ──
        f = self._lf(p, "TITULOS", 1)
        for i, (txt, attr) in enumerate([("Titulo", "ent_title"),
                                          ("Eje X",  "ent_xlabel"),
                                          ("Eje Y",  "ent_ylabel")]):
            ttk.Label(f, text=txt).grid(row=i, column=0, padx=4, pady=1, sticky="w")
            e = ttk.Entry(f)
            e.grid(row=i, column=1, padx=4, pady=1, sticky="ew")
            setattr(self, attr, e)
        self.ent_title.insert(0, "Senales")

        # ── Unidades ──
        f = self._lf(p, "UNIDADES (CSV osciloscopio)", 2)
        ttk.Label(f, text="Tiempo").grid(row=0, column=0, padx=4, pady=2, sticky="w")
        self.cmb_time = ttk.Combobox(f, values=list(TIME_UNITS.keys()),
                                      state="readonly", width=6)
        self.cmb_time.set("us")
        self.cmb_time.grid(row=0, column=1, padx=4, pady=2, sticky="ew")
        ttk.Label(f, text="Voltaje").grid(row=1, column=0, padx=4, pady=2, sticky="w")
        self.cmb_volt = ttk.Combobox(f, values=list(VOLT_UNITS.keys()),
                                      state="readonly", width=6)
        self.cmb_volt.set("V")
        self.cmb_volt.grid(row=1, column=1, padx=4, pady=2, sticky="ew")

        # ── Escala log ──
        f = self._lf(p, "ESCALA LOGARITMICA", 3)
        self.var_logx = tk.BooleanVar()
        self.var_logy = tk.BooleanVar()
        ttk.Checkbutton(f, text="Log X", variable=self.var_logx).grid(
            row=0, column=0, padx=4, pady=2)
        ttk.Checkbutton(f, text="Log Y", variable=self.var_logy).grid(
            row=0, column=1, padx=4, pady=2)

        # ── Grilla ──
        f = self._lf(p, "GRILLA", 4)
        self.var_grid = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Mostrar grilla", variable=self.var_grid).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Label(f, text="Paso X").grid(row=1, column=0, padx=4, pady=1, sticky="w")
        self.ent_gridx = ttk.Entry(f, width=8)
        self.ent_gridx.insert(0, "auto")
        self.ent_gridx.grid(row=1, column=1, padx=4, pady=1, sticky="ew")
        ttk.Label(f, text="Paso Y").grid(row=2, column=0, padx=4, pady=1, sticky="w")
        self.ent_gridy = ttk.Entry(f, width=8)
        self.ent_gridy.insert(0, "auto")
        self.ent_gridy.grid(row=2, column=1, padx=4, pady=1, sticky="ew")

        # ── Leyenda ──
        f = self._lf(p, "LEYENDA", 5)
        self.var_legend = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Mostrar leyenda", variable=self.var_legend).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Label(f, text="Posicion").grid(row=1, column=0, padx=4, pady=2, sticky="w")
        self.cmb_legend_loc = ttk.Combobox(f, values=LEGEND_LOCS,
                                            state="readonly", width=14)
        self.cmb_legend_loc.set("upper right")
        self.cmb_legend_loc.grid(row=1, column=1, padx=4, pady=2, sticky="ew")

        # ── Capas y canales ──
        self.frm_layers = ttk.LabelFrame(p, text="CAPAS Y CANALES")
        self.frm_layers.grid(row=6, column=0, sticky="ew", padx=4, pady=3)
        self.frm_layers.columnconfigure(0, weight=1)
        ttk.Label(self.frm_layers, text="(agregar archivos primero)").pack(padx=4, pady=4)

        # ── Modo XY ──
        f = self._lf(p, "MODO XY - LISSAJOUS", 7)
        self.var_xy = tk.BooleanVar()
        ttk.Checkbutton(f, text="Activar modo XY", variable=self.var_xy).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Label(f, text="Canal X").grid(row=1, column=0, padx=4, pady=1, sticky="w")
        self.cmb_xy_x = ttk.Combobox(f, state="readonly", width=12)
        self.cmb_xy_x.grid(row=1, column=1, padx=4, pady=1, sticky="ew")
        ttk.Label(f, text="Canal Y").grid(row=2, column=0, padx=4, pady=1, sticky="w")
        self.cmb_xy_y = ttk.Combobox(f, state="readonly", width=12)
        self.cmb_xy_y.grid(row=2, column=1, padx=4, pady=1, sticky="ew")

        # ── Puntos importantes ──
        f = self._lf(p, "PUNTOS IMPORTANTES", 8)
        self.var_show_max = tk.BooleanVar()
        self.var_show_min = tk.BooleanVar()
        ttk.Checkbutton(f, text="Marcar maximo", variable=self.var_show_max).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Checkbutton(f, text="Marcar minimo", variable=self.var_show_min).grid(
            row=1, column=0, columnspan=2, padx=4, pady=2, sticky="w")

        # ── Cursores ──
        f = self._lf(p, "CURSORES", 9)
        self.btn_cursor = ttk.Button(f, text="Activar cursores",
                                     command=self._toggle_cursors)
        self.btn_cursor.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(f, text="Limpiar", command=self._clear_cursors).grid(
            row=0, column=1, padx=4, pady=4, sticky="ew")
        self.lbl_cursor_info = ttk.Label(f, text="", wraplength=250)
        self.lbl_cursor_info.grid(row=1, column=0, columnspan=2, padx=4, pady=2)

        # ── Boton graficar ──
        ttk.Button(p, text="GRAFICAR", command=self._replot).grid(
            row=10, column=0, padx=4, pady=8, sticky="ew")

    def _build_plot(self, parent):
        self.fig = Figure(figsize=(8, 5))
        self.fig.patch.set_facecolor("#f5f5f5")
        self.ax = self.fig.add_subplot(111)

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        tb_frame = ttk.Frame(parent)
        tb_frame.grid(row=1, column=0, sticky="ew")

        # Toolbar personalizada: home corregido + boton configure subplots propio
        self.toolbar = FixedToolbar(self.canvas, tb_frame, app=self)
        self.toolbar.update()

        self._draw_welcome()

    def _draw_welcome(self):
        self.ax.clear()
        self.ax.set_facecolor("#f0f0f0")
        self.ax.text(0.5, 0.5,
                     "Abri uno o varios archivos\n.csv  o  .raw (LTspice)\npara comenzar",
                     ha="center", va="center", transform=self.ax.transAxes,
                     color="#555555", fontsize=12)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.draw()

    def _open_subplot_config(self):
        if self._subplot_win and self._subplot_win.winfo_exists():
            self._subplot_win.lift()
            return
        self._subplot_win = SubplotConfigWindow(self.root, self.fig, self.canvas)

    # ─── Drag & drop ───────────────────────────

    def _setup_dnd(self):
        if not DND_AVAILABLE:
            return
        if DND_BACKEND == "windows":
            # Esperar a que la ventana tenga HWND asignado
            self.root.after(300, self._setup_dnd_windows)
        else:
            # Linux: tkinterdnd2 — registrar recursivamente tras render
            self.root.after(200, self._register_dnd_tkinterdnd2)

    def _setup_dnd_windows(self):
        """Habilita WM_DROPFILES nativo en Windows via ctypes."""
        try:
            # Buscar el HWND real de la ventana por su titulo.
            # winfo_id() en Python 32-bit no devuelve el HWND correcto,
            # FindWindowW es la forma mas robusta.
            title = self.root.title()
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if not hwnd:
                # fallback: intentar con winfo_id directo
                hwnd = self.root.winfo_id()
            if hwnd:
                _win_enable_drop(hwnd, self._on_drop_paths)
            else:
                print("[DnD Windows] No se pudo obtener el HWND de la ventana.")
        except Exception as e:
            print(f"[DnD Windows] Error: {e}")

    def _register_dnd_tkinterdnd2(self):
        """Registra DnD en todos los widgets (Linux via tkinterdnd2)."""
        def register(widget):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop_event)
            except Exception:
                pass
            for child in widget.winfo_children():
                register(child)
        register(self.root)

    def _on_drop_paths(self, paths):
        """Callback para Windows: recibe lista de paths ya limpios."""
        for path in paths:
            self._dispatch_file(path)

    def _on_drop_event(self, event):
        """Callback para Linux/tkinterdnd2: parsea event.data."""
        raw = event.data.strip()
        # Linux puede entregar: file:///ruta  o  {ruta con espacios}
        if raw.startswith("file://"):
            parts = re.split(r'\s+', raw)
        else:
            parts_raw = re.findall(r'\{([^}]+)\}|(\S+)', raw)
            parts = [a or b for a, b in parts_raw]
        for part in parts:
            path = self._clean_drop_path(part)
            if path:
                self._dispatch_file(path)

    @staticmethod
    def _clean_drop_path(path):
        """Normaliza un path de drag & drop en Linux."""
        path = path.strip()
        if not path:
            return None
        if path.startswith("file:///"):
            path = path[7:]
        elif path.startswith("file://"):
            path = path[7:]
        path = urllib.parse.unquote(path)
        return os.path.normpath(path)

    def _dispatch_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            self._load_csv_file(path)
        elif ext == ".raw":
            self._load_ltspice_file(path)
        else:
            messagebox.showerror("Archivo no valido",
                f"Extension no soportada: '{ext}'\nSe aceptan: .csv  .raw")

    # ─── Abrir archivos ────────────────────────

    def _open_csv(self):
        self.root.update_idletasks()
        geom = self.root.geometry()
        path = filedialog.askopenfilename(
            title="Agregar archivo CSV",
            filetypes=[("Archivos CSV", "*.csv"), ("Todos", "*.*")]
        )
        self.root.geometry(geom)
        self.root.update_idletasks()
        if path:
            self._load_csv_file(path)

    def _open_ltspice(self):
        if not LTSPICE_AVAILABLE:
            messagebox.showerror("Libreria no instalada", "pip install ltspice")
            return
        self.root.update_idletasks()
        geom = self.root.geometry()
        path = filedialog.askopenfilename(
            title="Agregar archivo LTspice (.raw)",
            filetypes=[("LTspice raw", "*.raw"), ("Todos", "*.*")]
        )
        self.root.geometry(geom)
        self.root.update_idletasks()
        if path:
            self._load_ltspice_file(path)

    # ─── Carga ─────────────────────────────────

    def _load_csv_file(self, path):
        if not path.lower().endswith(".csv"):
            messagebox.showerror("Archivo no valido",
                f"El archivo NO es un .csv:\n{os.path.basename(path)}")
            return
        try:
            df, x_unit, y_unit = load_csv(path)
        except ValueError as e:
            messagebox.showerror("Error al leer el CSV", str(e))
            return
        self._add_layer(df, x_unit, y_unit, path)

    def _load_ltspice_file(self, path):
        try:
            df, x_unit, y_unit = load_ltspice(path)
        except ValueError as e:
            messagebox.showerror("Error al leer archivo LTspice", str(e))
            return
        self._add_layer(df, x_unit, y_unit, path)

    def _add_layer(self, df, x_unit, y_unit, path):
        color_offset = sum(len(l.y_cols) for l in self.layers)
        layer = Layer(os.path.basename(path), df, x_unit, y_unit,
                      color_offset=color_offset)
        self.layers.append(layer)
        if len(self.layers) == 1:
            self.var_logx.set(layer.is_freq)
        self._rebuild_layers_panel()
        self._update_xy_combos()
        self._replot()
        # Re-registrar DnD en nuevos widgets (solo Linux/tkinterdnd2)
        if DND_BACKEND == "tkinterdnd2":
            self.root.after(50, self._register_dnd_tkinterdnd2)


    # ─── Funcion teorica ───────────────────────

    def _add_func_layer(self):
        """Crea una nueva capa de funcion teorica y la agrega."""
        color_offset = sum(
            (len(l.y_cols) if hasattr(l, "y_cols") else 1)
            for l in self.layers
        )
        n = sum(1 for l in self.layers if hasattr(l, "kind") and l.kind == "func")
        fl = FuncLayer(f"Funcion {n+1}", color_offset=color_offset)
        self.layers.append(fl)
        self._rebuild_layers_panel()
        if DND_BACKEND == "tkinterdnd2":
            self.root.after(50, self._register_dnd_tkinterdnd2)

    def _build_func_channel_ui(self, fl, li):
        """Construye los controles de una FuncLayer en el panel de capas."""

        # ── Expresion ──
        frm_expr = ttk.Frame(self.frm_layers)
        frm_expr.pack(fill="x", padx=8, pady=2)
        frm_expr.columnconfigure(1, weight=1)
        ttk.Label(frm_expr, text="f(x) =").grid(row=0, column=0, sticky="w", padx=2)
        var_expr = tk.StringVar(value=fl.expr)
        def _set_expr(*a, flr=fl, v=var_expr):
            flr.expr = v.get()
        var_expr.trace_add("write", _set_expr)
        ent_expr = ttk.Entry(frm_expr, textvariable=var_expr)
        ent_expr.grid(row=0, column=1, sticky="ew", padx=2)
        ent_expr.bind("<Return>",   lambda e: self._eval_and_replot(fl))
        ent_expr.bind("<FocusOut>", lambda e: self._eval_and_replot(fl))

        # ── Rango X ──
        frm_range = ttk.Frame(self.frm_layers)
        frm_range.pack(fill="x", padx=8, pady=1)
        frm_range.columnconfigure(1, weight=1)
        frm_range.columnconfigure(3, weight=1)
        ttk.Label(frm_range, text="X:").grid(row=0, column=0, sticky="w", padx=2)
        var_x0 = tk.StringVar(value=fl.x_start)
        def _set_x0(*a, flr=fl, v=var_x0):
            flr.x_start = v.get()
        var_x0.trace_add("write", _set_x0)
        ttk.Entry(frm_range, textvariable=var_x0, width=8).grid(
            row=0, column=1, sticky="ew", padx=2)
        ttk.Label(frm_range, text="a").grid(row=0, column=2, padx=2)
        var_x1 = tk.StringVar(value=fl.x_end)
        def _set_x1(*a, flr=fl, v=var_x1):
            flr.x_end = v.get()
        var_x1.trace_add("write", _set_x1)
        ttk.Entry(frm_range, textvariable=var_x1, width=8).grid(
            row=0, column=3, sticky="ew", padx=2)

        # ── Puntos y log X ──
        frm_pts = ttk.Frame(self.frm_layers)
        frm_pts.pack(fill="x", padx=8, pady=1)
        frm_pts.columnconfigure(1, weight=1)
        ttk.Label(frm_pts, text="Puntos:").grid(row=0, column=0, sticky="w", padx=2)
        var_pts = tk.StringVar(value=fl.n_points)
        def _set_pts(*a, flr=fl, v=var_pts):
            flr.n_points = v.get()
        var_pts.trace_add("write", _set_pts)
        ttk.Entry(frm_pts, textvariable=var_pts, width=7).grid(
            row=0, column=1, sticky="ew", padx=2)
        var_xlog = tk.BooleanVar(value=fl.x_log)
        def _set_xlog(flr=fl, v=var_xlog):
            flr.x_log = v.get()
        ttk.Checkbutton(frm_pts, text="Log X", variable=var_xlog,
                        command=_set_xlog).grid(row=0, column=2, padx=4)

        # ── Estilo de linea ──
        frm_style = ttk.Frame(self.frm_layers)
        frm_style.pack(fill="x", padx=8, pady=1)
        frm_style.columnconfigure(1, weight=1)
        ttk.Label(frm_style, text="Linea:").grid(row=0, column=0, sticky="w", padx=2)
        cmb_ls = ttk.Combobox(frm_style,
                              values=["solida  (-)", "guiones  (--)",
                                      "punto-guion  (-.)","punteada  (:)"],
                              state="readonly", width=16)
        ls_map = {"solida  (-)": "-", "guiones  (--)": "--",
                  "punto-guion  (-.)": "-.", "punteada  (:)": ":"}
        ls_inv = {v: k for k, v in ls_map.items()}
        cmb_ls.set(ls_inv.get(fl.linestyle, "solida  (-)"))
        def _set_ls(e, flr=fl, cmb=cmb_ls):
            flr.linestyle = ls_map.get(cmb.get(), "-")
        cmb_ls.bind("<<ComboboxSelected>>", _set_ls)
        cmb_ls.grid(row=0, column=1, sticky="ew", padx=2)

        # ── Color + label ──
        frm_clr = ttk.Frame(self.frm_layers)
        frm_clr.pack(fill="x", padx=8, pady=1)
        frm_clr.columnconfigure(3, weight=1)

        btn_clr = tk.Button(frm_clr, bg=fl.color, width=2, height=1,
                            relief="raised",
                            command=lambda flr=fl: self._pick_func_color(flr))
        btn_clr.grid(row=0, column=0, padx=2)
        setattr(fl, "_color_btn", btn_clr)

        var_lbl_vis = tk.BooleanVar(value=fl.label_visible)
        def _toggle_lbl(flr=fl, v=var_lbl_vis):
            flr.label_visible = v.get(); self._replot()
        ttk.Checkbutton(frm_clr, text="", variable=var_lbl_vis,
                        command=_toggle_lbl).grid(row=0, column=1)

        var_lbl = tk.StringVar(value=fl.label)
        def _set_lbl(*a, flr=fl, v=var_lbl):
            flr.label = v.get()
        var_lbl.trace_add("write", _set_lbl)
        ent_lbl = ttk.Entry(frm_clr, textvariable=var_lbl, width=12)
        ent_lbl.grid(row=0, column=3, sticky="ew", padx=2)
        ent_lbl.bind("<Return>",   lambda e: self._replot())
        ent_lbl.bind("<FocusOut>", lambda e: self._replot())

        # ── Offset y escala ──
        frm_off = ttk.Frame(self.frm_layers)
        frm_off.pack(fill="x", padx=8, pady=1)
        frm_off.columnconfigure(1, weight=1)
        frm_off.columnconfigure(3, weight=1)
        ttk.Label(frm_off, text="Off Y:").grid(row=0, column=0, sticky="w", padx=2)
        var_offy = tk.DoubleVar(value=fl.offset_y)
        def _set_offy(*a, flr=fl, v=var_offy):
            try: flr.offset_y = v.get()
            except Exception: pass
        var_offy.trace_add("write", _set_offy)
        ttk.Entry(frm_off, textvariable=var_offy, width=7).grid(
            row=0, column=1, sticky="ew", padx=2)
        ttk.Label(frm_off, text="Off X:").grid(row=0, column=2, sticky="w", padx=2)
        var_offx = tk.DoubleVar(value=fl.offset_x)
        def _set_offx(*a, flr=fl, v=var_offx):
            try: flr.offset_x = v.get()
            except Exception: pass
        var_offx.trace_add("write", _set_offx)
        ttk.Entry(frm_off, textvariable=var_offx, width=7).grid(
            row=0, column=3, sticky="ew", padx=2)

        frm_sc = ttk.Frame(self.frm_layers)
        frm_sc.pack(fill="x", padx=8, pady=1)
        frm_sc.columnconfigure(1, weight=1)
        ttk.Label(frm_sc, text="Escala:").grid(row=0, column=0, sticky="w", padx=2)
        var_sc = tk.DoubleVar(value=fl.scale)
        def _set_sc(*a, flr=fl, v=var_sc):
            try: flr.scale = v.get()
            except Exception: pass
        var_sc.trace_add("write", _set_sc)
        ttk.Entry(frm_sc, textvariable=var_sc, width=7).grid(
            row=0, column=1, sticky="ew", padx=2)

        # ── Constantes ──
        frm_cst_hdr = ttk.Frame(self.frm_layers)
        frm_cst_hdr.pack(fill="x", padx=8, pady=(4, 0))
        frm_cst_hdr.columnconfigure(0, weight=1)
        ttk.Label(frm_cst_hdr, text="Constantes:",
                  font=("TkDefaultFont", 8, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(frm_cst_hdr, text="+ Agregar", width=8,
                   command=lambda flr=fl: self._add_constant(flr)).grid(
            row=0, column=1, padx=2)

        # Frame contenedor de constantes (se rellena/actualiza dinamicamente)
        frm_cst = ttk.Frame(self.frm_layers)
        frm_cst.pack(fill="x", padx=8, pady=1)
        frm_cst.columnconfigure(1, weight=1)
        setattr(fl, "_frm_cst", frm_cst)
        self._rebuild_constants_ui(fl)

        # ── Boton graficar funcion ──
        ttk.Button(self.frm_layers, text="Graficar funcion",
                   command=lambda flr=fl: self._eval_and_replot(flr)).pack(
            fill="x", padx=8, pady=(2, 4))

    def _rebuild_constants_ui(self, fl):
        """Reconstruye el subpanel de constantes de una FuncLayer."""
        frm = fl._frm_cst
        for w in frm.winfo_children():
            w.destroy()
        for ci, const in enumerate(fl.constants):
            row_frm = ttk.Frame(frm)
            row_frm.pack(fill="x", pady=1)
            row_frm.columnconfigure(1, weight=1)

            # Nombre
            var_name = tk.StringVar(value=const["name"])
            def _set_name(*a, c=const, v=var_name):
                c["name"] = v.get()
            var_name.trace_add("write", _set_name)
            ttk.Entry(row_frm, textvariable=var_name, width=6).grid(
                row=0, column=0, padx=2)

            # Valor con slider
            var_val = tk.DoubleVar(value=const["value"])
            def _set_val(*a, c=const, v=var_val, flr=fl):
                try:
                    c["value"] = v.get()
                except Exception:
                    pass
            var_val.trace_add("write", _set_val)

            ttk.Entry(row_frm, textvariable=var_val, width=9).grid(
                row=0, column=1, padx=2, sticky="ew")

            # Slider entre min y max
            var_min = tk.DoubleVar(value=const["min"])
            var_max = tk.DoubleVar(value=const["max"])

            def _slider_cb(val, c=const, v=var_val, flr=fl):
                try:
                    c["value"] = v.get()
                except Exception:
                    pass
                self.root.after_idle(lambda flr=flr: self._eval_and_replot(flr))

            sl = ttk.Scale(row_frm, from_=const["min"], to=const["max"],
                           variable=var_val, orient="horizontal", length=80,
                           command=_slider_cb)
            sl.grid(row=0, column=2, padx=2)

            # Min / Max editables
            def _set_min(e, s=sl, vmin=var_min, vmax=var_max):
                try:
                    s.configure(from_=float(vmin.get()))
                except Exception:
                    pass
            def _set_max(e, s=sl, vmin=var_min, vmax=var_max):
                try:
                    s.configure(to=float(vmax.get()))
                except Exception:
                    pass
            ent_min = ttk.Entry(row_frm, textvariable=var_min, width=5)
            ent_min.grid(row=0, column=3, padx=1)
            ent_min.bind("<Return>",   _set_min)
            ent_min.bind("<FocusOut>", _set_min)
            ent_max = ttk.Entry(row_frm, textvariable=var_max, width=5)
            ent_max.grid(row=0, column=4, padx=1)
            ent_max.bind("<Return>",   _set_max)
            ent_max.bind("<FocusOut>", _set_max)

            # Boton eliminar
            ttk.Button(row_frm, text="x", width=2,
                       command=lambda idx=ci, flr=fl: self._remove_constant(flr, idx)
                       ).grid(row=0, column=5, padx=2)

    def _add_constant(self, fl):
        fl.constants.append({"name": f"k{len(fl.constants)+1}",
                             "value": 1.0, "min": 0.0, "max": 10.0})
        self._rebuild_constants_ui(fl)
        if DND_BACKEND == "tkinterdnd2":
            self.root.after(50, self._register_dnd_tkinterdnd2)

    def _remove_constant(self, fl, idx):
        if 0 <= idx < len(fl.constants):
            fl.constants.pop(idx)
        self._rebuild_constants_ui(fl)

    def _pick_func_color(self, fl):
        result = colorchooser.askcolor(color=fl.color, title="Color de la funcion")
        if result and result[1]:
            fl.color = result[1]
            btn = getattr(fl, "_color_btn", None)
            if btn:
                btn.config(bg=result[1])
            self._replot()

    def _eval_and_replot(self, fl):
        """Evalua la funcion y redibuja. Muestra error inline si falla."""
        self._replot()

    def _clear_all(self):
        self.layers.clear()
        self.cursor_clicks.clear()
        self._home_limits = None
        self._rebuild_layers_panel()
        self._draw_welcome()

    # ─── Panel de capas ────────────────────────

    def _rebuild_layers_panel(self):
        for w in self.frm_layers.winfo_children():
            w.destroy()

        if not self.layers:
            ttk.Label(self.frm_layers,
                      text="(agregar archivos primero)").pack(padx=4, pady=4)
            return

        for li, layer in enumerate(self.layers):
            # Cabecera comun
            hdr = ttk.Frame(self.frm_layers)
            hdr.pack(fill="x", padx=2, pady=(6, 0))
            hdr.columnconfigure(1, weight=1)

            var_vis = tk.BooleanVar(value=layer.visible)
            def _toggle_layer(lyr=layer, v=var_vis):
                lyr.visible = v.get(); self._replot()

            ttk.Checkbutton(hdr, text="", variable=var_vis,
                            command=_toggle_layer).grid(row=0, column=0)

            # Icono segun tipo
            kind_tag = "[F]" if (hasattr(layer, "kind") and layer.kind == "func") else ""
            ttk.Label(hdr, text=f"{kind_tag}[{li+1}] {layer.name}",
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=1, sticky="w", padx=2)
            ttk.Button(hdr, text="X", width=2,
                       command=lambda idx=li: self._remove_layer(idx)).grid(
                row=0, column=2, padx=2)

            # Controles especificos segun tipo
            if hasattr(layer, "kind") and layer.kind == "func":
                self._build_func_channel_ui(layer, li)
                ttk.Separator(self.frm_layers, orient="horizontal").pack(
                    fill="x", padx=4, pady=2)
                continue

            # Canales
            for ci, col in enumerate(layer.y_cols):
                frm = ttk.Frame(self.frm_layers)
                frm.pack(fill="x", padx=8, pady=1)
                frm.columnconfigure(1, weight=1)

                # Fila 0: checkbox visible + boton color
                var_ch = tk.BooleanVar(value=layer.visible_channels[ci])
                def _toggle_ch(lyr=layer, idx=ci, v=var_ch):
                    lyr.visible_channels[idx] = v.get(); self._replot()
                ttk.Checkbutton(frm, text="", variable=var_ch,
                                command=_toggle_ch).grid(row=0, column=0)

                btn = tk.Button(frm, bg=layer.colors[ci], width=2, height=1,
                                relief="raised",
                                command=lambda lyr=layer, idx=ci: self._pick_color(lyr, idx))
                btn.grid(row=0, column=1, padx=2, sticky="w")
                setattr(layer, f"_btn_{ci}", btn)

                # Label editable + checkbox leyenda
                var_lbl_vis = tk.BooleanVar(value=layer.label_visible[ci])
                def _toggle_lbl(lyr=layer, idx=ci, v=var_lbl_vis):
                    lyr.label_visible[idx] = v.get(); self._replot()
                ttk.Checkbutton(frm, text="", variable=var_lbl_vis,
                                command=_toggle_lbl).grid(row=0, column=2)

                var_lbl = tk.StringVar(value=layer.labels[ci])
                def _set_lbl(name, idx_tr, mode, lyr=layer, idx=ci, v=var_lbl):
                    lyr.labels[idx] = v.get()
                var_lbl.trace_add("write", _set_lbl)
                ent_lbl = ttk.Entry(frm, textvariable=var_lbl, width=12)
                ent_lbl.grid(row=0, column=3, padx=2, sticky="ew")
                ent_lbl.bind("<Return>", lambda e: self._replot())
                ent_lbl.bind("<FocusOut>", lambda e: self._replot())
                frm.columnconfigure(3, weight=1)

                # Fila 1: offset Y
                ttk.Label(frm, text="Offset Y:").grid(row=1, column=0, columnspan=2,
                                                       sticky="w", padx=2)
                var_offy = tk.DoubleVar(value=layer.offsets_y[ci])
                def _set_offy(n, it, m, lyr=layer, idx=ci, v=var_offy):
                    try: lyr.offsets_y[idx] = v.get()
                    except Exception: pass
                var_offy.trace_add("write", _set_offy)
                ttk.Entry(frm, textvariable=var_offy, width=8).grid(
                    row=1, column=2, columnspan=2, sticky="ew", padx=2)

                # Fila 2: offset X
                ttk.Label(frm, text="Offset X:").grid(row=2, column=0, columnspan=2,
                                                       sticky="w", padx=2)
                var_offx = tk.DoubleVar(value=layer.offsets_x[ci])
                def _set_offx(n, it, m, lyr=layer, idx=ci, v=var_offx):
                    try: lyr.offsets_x[idx] = v.get()
                    except Exception: pass
                var_offx.trace_add("write", _set_offx)
                ttk.Entry(frm, textvariable=var_offx, width=8).grid(
                    row=2, column=2, columnspan=2, sticky="ew", padx=2)

                # Fila 3: escala
                ttk.Label(frm, text="Escala:").grid(row=3, column=0, columnspan=2,
                                                     sticky="w", padx=2)
                var_sc = tk.DoubleVar(value=layer.scales[ci])
                def _set_sc(n, it, m, lyr=layer, idx=ci, v=var_sc):
                    try: lyr.scales[idx] = v.get()
                    except Exception: pass
                var_sc.trace_add("write", _set_sc)
                ttk.Entry(frm, textvariable=var_sc, width=8).grid(
                    row=3, column=2, columnspan=2, sticky="ew", padx=2)

            ttk.Separator(self.frm_layers, orient="horizontal").pack(
                fill="x", padx=4, pady=2)

    def _remove_layer(self, idx):
        if 0 <= idx < len(self.layers):
            self.layers.pop(idx)
        self._rebuild_layers_panel()
        self._update_xy_combos()
        if self.layers:
            self._replot()
        else:
            self._draw_welcome()

    def _pick_color(self, layer, ci):
        result = colorchooser.askcolor(color=layer.colors[ci],
                                       title=f"Color para {layer.y_cols[ci]}")
        if result and result[1]:
            layer.colors[ci] = result[1]
            btn = getattr(layer, f"_btn_{ci}", None)
            if btn:
                btn.config(bg=result[1])
            self._replot()

    def _update_xy_combos(self):
        all_cols = [col for layer in self.layers
                    if not (hasattr(layer, "kind") and layer.kind == "func")
                    for col in layer.y_cols]
        self.cmb_xy_x["values"] = all_cols
        self.cmb_xy_y["values"] = all_cols
        if len(all_cols) >= 1: self.cmb_xy_x.set(all_cols[0])
        if len(all_cols) >= 2: self.cmb_xy_y.set(all_cols[1])

    # ─── Graficado ─────────────────────────────

    def _get_x_data(self, layer, ci):
        x_raw = layer.df[layer.x_col].values
        if layer.is_freq:
            x = x_raw
        else:
            t_factor, _ = TIME_UNITS[self.cmb_time.get()]
            x = x_raw * t_factor
        return x + layer.offsets_x[ci]

    def _get_y_data(self, layer, ci):
        col = layer.y_cols[ci]
        raw = layer.df[col].values
        col_lower = col.lower()
        if any(k in col_lower for k in ("db", "mag", "gain", "phase", "amp", "°")):
            y = raw
        else:
            v_factor, _ = VOLT_UNITS[self.cmb_volt.get()]
            y = raw * v_factor
        return y * layer.scales[ci] + layer.offsets_y[ci]

    def _replot(self):
        if not self.layers:
            return

        self.ax.clear()
        self.ax.set_facecolor("#0d0d1a")
        self.fig.patch.set_facecolor("#1a1a2e")
        for spine in self.ax.spines.values():
            spine.set_color("#333366")
        # Forzar color gris en todos los tick labels (fix para Linux donde
        # algunos ticks quedan negros tras ax.clear())
        self.ax.tick_params(axis='both', colors="#aaaacc", labelsize=8,
                            which='both', labelcolor="#aaaacc")
        self.ax.xaxis.label.set_color("#8888aa")
        self.ax.yaxis.label.set_color("#8888aa")

        if self.var_xy.get():
            self._plot_xy()
        else:
            self._plot_normal()

        # Guardar limites reales para el boton Home
        self.fig.canvas.draw()   # necesario para que los limites se calculen
        self._home_limits = (
            *self.ax.get_xlim(),
            *self.ax.get_ylim()
        )
        self.canvas.draw()

    def _plot_normal(self):
        title  = self.ent_title.get()
        xlabel = self.ent_xlabel.get()
        ylabel = self.ent_ylabel.get()
        has_data = False

        for layer in self.layers:
            if not layer.visible:
                continue

            # ── FuncLayer ──────────────────────────────────────────────────
            if hasattr(layer, "kind") and layer.kind == "func":
                if not layer.expr.strip():
                    continue
                try:
                    x_data, y_data = layer.evaluate()
                except Exception as err:
                    # Dibujar mensaje de error en el grafico
                    self.ax.text(0.02, 0.05 + 0.06 * self.layers.index(layer),
                                 f"[{layer.name}] Error: {err}",
                                 transform=self.ax.transAxes,
                                 color="#ff4444", fontsize=7, va="bottom")
                    continue
                lbl = layer.label if layer.label_visible else "_nolegend_"
                self.ax.plot(x_data, y_data, color=layer.color,
                             linewidth=1.4, linestyle=layer.linestyle,
                             label=lbl)
                has_data = True
                if self.var_show_max.get() and len(y_data):
                    idx_max = int(np.argmax(y_data))
                    self.ax.annotate(f"MAX {y_data[idx_max]:.3g}",
                        xy=(x_data[idx_max], y_data[idx_max]),
                        xytext=(8, 8), textcoords="offset points",
                        color=layer.color, fontsize=7,
                        arrowprops=dict(arrowstyle="->", color=layer.color, lw=0.8))
                if self.var_show_min.get() and len(y_data):
                    idx_min = int(np.argmin(y_data))
                    self.ax.annotate(f"MIN {y_data[idx_min]:.3g}",
                        xy=(x_data[idx_min], y_data[idx_min]),
                        xytext=(8, -14), textcoords="offset points",
                        color=layer.color, fontsize=7,
                        arrowprops=dict(arrowstyle="->", color=layer.color, lw=0.8))
                continue

            # ── Layer normal (CSV / .raw) ───────────────────────────────────
            for ci, col in enumerate(layer.y_cols):
                if not layer.visible_channels[ci]:
                    continue
                x_data = self._get_x_data(layer, ci)
                y_data = self._get_y_data(layer, ci)

                # Usar label editable solo si esta visible en leyenda
                lbl = layer.labels[ci] if layer.label_visible[ci] else "_nolegend_"

                self.ax.plot(x_data, y_data, color=layer.colors[ci],
                             linewidth=1.2, label=lbl)
                has_data = True

                if self.var_show_max.get():
                    idx_max = int(np.argmax(y_data))
                    self.ax.annotate(
                        f"MAX {y_data[idx_max]:.3g}",
                        xy=(x_data[idx_max], y_data[idx_max]),
                        xytext=(8, 8), textcoords="offset points",
                        color=layer.colors[ci], fontsize=7,
                        arrowprops=dict(arrowstyle="->",
                                        color=layer.colors[ci], lw=0.8))
                if self.var_show_min.get():
                    idx_min = int(np.argmin(y_data))
                    self.ax.annotate(
                        f"MIN {y_data[idx_min]:.3g}",
                        xy=(x_data[idx_min], y_data[idx_min]),
                        xytext=(8, -14), textcoords="offset points",
                        color=layer.colors[ci], fontsize=7,
                        arrowprops=dict(arrowstyle="->",
                                        color=layer.colors[ci], lw=0.8))

        # Eje X automatico
        if not xlabel and self.layers:
            first = next((l for l in self.layers if l.visible), None)
            if first:
                xlabel = "Frecuencia (Hz)" if first.is_freq else \
                         f"Tiempo ({self.cmb_time.get()})"

        self.ax.set_title(title or "Senales", color="#00bfff", fontsize=11)
        self.ax.set_xlabel(xlabel, color="#8888aa")
        self.ax.set_ylabel(ylabel, color="#8888aa")

        if self.var_logx.get():
            self.ax.set_xscale("log")
        if self.var_logy.get():
            self.ax.set_yscale("log")

        if self.var_grid.get():
            try:
                gx = float(self.ent_gridx.get())
                self.ax.xaxis.set_major_locator(ticker.MultipleLocator(gx))
            except ValueError:
                pass
            try:
                gy = float(self.ent_gridy.get())
                self.ax.yaxis.set_major_locator(ticker.MultipleLocator(gy))
            except ValueError:
                pass
            self.ax.grid(True, color="#1e1e4f", linestyle="--",
                         linewidth=0.5, alpha=0.7)
        else:
            self.ax.grid(False)

        # Leyenda: solo si hay al menos un label visible
        if has_data and self.var_legend.get():
            handles, labels = self.ax.get_legend_handles_labels()
            # filtrar entradas "_nolegend_"
            pairs = [(h, l) for h, l in zip(handles, labels) if not l.startswith("_")]
            if pairs:
                hs, ls = zip(*pairs)
                leg = self.ax.legend(hs, ls,
                                     loc=self.cmb_legend_loc.get(),
                                     facecolor="#111130", edgecolor="#3333aa",
                                     fontsize=7)
                # labelcolor se agrego en matplotlib 3.3 — colorear manualmente
                # para compatibilidad con versiones anteriores
                for text in leg.get_texts():
                    text.set_color("#ddddff")

        self._redraw_cursors()

    def _plot_xy(self):
        cx_name = self.cmb_xy_x.get()
        cy_name = self.cmb_xy_y.get()

        def find_col(name):
            for layer in self.layers:
                if name in layer.y_cols:
                    ci = layer.y_cols.index(name)
                    return self._get_y_data(layer, ci)
            return None

        xd, yd = find_col(cx_name), find_col(cy_name)
        if xd is None or yd is None:
            return
        n = min(len(xd), len(yd))
        self.ax.plot(xd[:n], yd[:n], color="#00bfff", linewidth=0.8)
        self.ax.set_title("Figura de Lissajous (Modo XY)", color="#00bfff")
        self.ax.set_xlabel(cx_name.split("[")[0].strip(), color="#8888aa")
        self.ax.set_ylabel(cy_name.split("[")[0].strip(), color="#8888aa")
        if self.var_grid.get():
            self.ax.grid(True, color="#1e1e4f", linestyle="--",
                         linewidth=0.5, alpha=0.7)

    # ─── Cursores ──────────────────────────────

    def _toggle_cursors(self):
        self.cursor_mode = not self.cursor_mode
        if self.cursor_mode:
            self.btn_cursor.config(text="Cursores: ON")
            self.cursor_clicks = []
            self._cid = self.canvas.mpl_connect(
                "button_press_event", self._on_cursor_click)
        else:
            self.btn_cursor.config(text="Activar cursores")
            if hasattr(self, "_cid"):
                self.canvas.mpl_disconnect(self._cid)

    def _on_cursor_click(self, event):
        if event.inaxes != self.ax or event.xdata is None:
            return
        self.cursor_clicks.append(event.xdata)
        color = "#ffff00" if len(self.cursor_clicks) % 2 == 1 else "#ff6600"
        self.ax.axvline(x=event.xdata, color=color,
                        linestyle="--", linewidth=1, alpha=0.9)
        self.canvas.draw()
        if len(self.cursor_clicks) >= 2:
            dx   = abs(self.cursor_clicks[-1] - self.cursor_clicks[-2])
            unit = self.cmb_time.get()
            dx_s = dx / TIME_UNITS[unit][0]
            freq_str = f"{1/dx_s:.4g} Hz" if dx_s != 0 else "---"
            self.lbl_cursor_info.config(
                text=f"dX = {dx:.4g} {unit}   |   freq ~ {freq_str}")

    def _redraw_cursors(self):
        for i, x in enumerate(self.cursor_clicks):
            color = "#ffff00" if i % 2 == 0 else "#ff6600"
            self.ax.axvline(x=x, color=color, linestyle="--",
                            linewidth=1, alpha=0.9)

    def _clear_cursors(self):
        self.cursor_clicks = []
        self.lbl_cursor_info.config(text="")
        self._replot()


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    # Windows: usar tk.Tk() normal; el DnD lo maneja ctypes directamente
    # Linux:   usar TkDnD() si tkinterdnd2 esta disponible
    if DND_BACKEND == "tkinterdnd2":
        root = TkDnD()
    else:
        root = tk.Tk()
    app = OsciloscopioApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
