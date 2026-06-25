"""
GUI Osciloscopio - Graficador de archivos CSV (osciloscopio / FRA)
Soporta superposicion de multiples archivos.

Requisitos:
    pip install matplotlib pandas numpy

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

        self.layers       = []
        self._home_limits = None   # (xmin, xmax, ymin, ymax) del ultimo replot
        self._subplot_win = None   # ventana de configuracion de subplots

        self._build_ui()

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
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        return f

    def _build_controls(self, p):
        # ── Archivos ──
        f = self._lf(p, "ARCHIVOS", 0)
        ttk.Button(f, text="+ Agregar CSV",
                   command=self._open_csv).grid(row=0, column=0, padx=4, pady=8, sticky="ew")

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

        # ── Puntos importantes ──
        f = self._lf(p, "PUNTOS IMPORTANTES", 8)
        self.var_show_max = tk.BooleanVar()
        self.var_show_min = tk.BooleanVar()
        ttk.Checkbutton(f, text="Marcar maximo", variable=self.var_show_max).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Checkbutton(f, text="Marcar minimo", variable=self.var_show_min).grid(
            row=1, column=0, columnspan=2, padx=4, pady=2, sticky="w")

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
                     "Abri uno o varios archivos\n.csv\npara comenzar",
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

    def _open_csv(self):
        path = filedialog.askopenfilename(
            title="Abrir CSV",
            filetypes=[("CSV files", "*.csv")]
        )
        if path:
            self._load_csv_file(path)

    def _add_layer(self, df, x_unit, y_unit, path):
        color_offset = sum(len(l.y_cols) for l in self.layers)
        layer = Layer(os.path.basename(path), df, x_unit, y_unit,
                      color_offset=color_offset)
        self.layers.append(layer)
        if len(self.layers) == 1:
            self.var_logx.set(layer.is_freq)
        self._rebuild_layers_panel()


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
                lyr.visible = v.get()

            ttk.Checkbutton(hdr, text="", variable=var_vis,
                            command=_toggle_layer).grid(row=0, column=0)

            # Icono segun tipo
            ttk.Label(hdr, text=f"[{li+1}] {layer.name}",
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=1, sticky="w", padx=2)
            ttk.Button(hdr, text="X", width=2,
                       command=lambda idx=li: self._remove_layer(idx)).grid(
                row=0, column=2, padx=2)

            # Canales
            for ci, col in enumerate(layer.y_cols):
                frm = ttk.Frame(self.frm_layers)
                frm.pack(fill="x", padx=8, pady=1)
                frm.columnconfigure(1, weight=1)

                # Fila 0: checkbox visible + boton color
                var_ch = tk.BooleanVar(value=layer.visible_channels[ci])
                def _toggle_ch(lyr=layer, idx=ci, v=var_ch):
                    lyr.visible_channels[idx] = v.get()
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
                    lyr.label_visible[idx] = v.get()
                ttk.Checkbutton(frm, text="", variable=var_lbl_vis,
                                command=_toggle_lbl).grid(row=0, column=2)

                var_lbl = tk.StringVar(value=layer.labels[ci])
                def _set_lbl(name, idx_tr, mode, lyr=layer, idx=ci, v=var_lbl):
                    lyr.labels[idx] = v.get()
                var_lbl.trace_add("write", _set_lbl)
                ent_lbl = ttk.Entry(frm, textvariable=var_lbl, width=12)
                ent_lbl.grid(row=0, column=3, padx=2, sticky="ew")
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

    def _pick_color(self, layer, idx):
        result = colorchooser.askcolor(color=layer.colors[idx],
                                       title=f"Color para {layer.y_cols[idx]}")
        if result and result[1]:
            layer.colors[idx] = result[1]
            btn = getattr(layer, f"_btn_{idx}", None)
            if btn:
                btn.config(bg=result[1])

    def _remove_layer(self, idx):
        if 0 <= idx < len(self.layers):
            self.layers.pop(idx)
        self._rebuild_layers_panel()
        if not self.layers:
            self._draw_welcome()

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

            # ── Layer normal (CSV) ───────────────────────────────────
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


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    root = tk.Tk()
    app = OsciloscopioApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
