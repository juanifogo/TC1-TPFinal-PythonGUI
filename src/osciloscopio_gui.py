"""
GUI Osciloscopio - Graficador de archivos CSV de osciloscopio
Cumple todos los puntos obligatorios y opcionales de la consigna.

Requisitos:
    pip install matplotlib pandas numpy tkinterdnd2

Uso:
    python osciloscopio_gui.py
"""

import os
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
import matplotlib.ticker as ticker

try:
    from tkinterdnd2 import TkDnD, DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

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
]

# ─────────────────────────────────────────────
#  Lectura del CSV
# ─────────────────────────────────────────────

def load_csv(path):
    if not path.lower().endswith(".csv"):
        raise ValueError("El archivo no es un .csv")
    try:
        header_df = pd.read_csv(path, header=None, nrows=2)
        col_names = header_df.iloc[0].tolist()
        col_units = header_df.iloc[1].tolist()
        data_df   = pd.read_csv(path, header=None, skiprows=2)
        if data_df.shape[1] < 2:
            raise ValueError("El CSV no tiene suficientes columnas.")
        new_cols = [f"{n} [{u}]" for n, u in zip(col_names, col_units)]
        data_df.columns = new_cols[:data_df.shape[1]]
        data_df = data_df.apply(pd.to_numeric, errors='coerce').dropna()
        x_unit = str(col_units[0]).strip().lower()
        y_unit = str(col_units[1]).strip().lower()
        return data_df, x_unit, y_unit
    except Exception as e:
        raise ValueError(f"No se pudo leer el archivo: {e}")

# ─────────────────────────────────────────────
#  Panel izquierdo con scroll
# ─────────────────────────────────────────────

class ScrollableFrame(ttk.Frame):
    """Frame con scrollbar vertical interno."""

    def __init__(self, parent, width=290, **kwargs):
        super().__init__(parent, **kwargs)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(self, width=width, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=sb.set)

        # Frame interior donde van los widgets
        self.inner = ttk.Frame(self._canvas)
        self._win_id = self._canvas.create_window(
            (0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Scroll con rueda del mouse
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

    def _on_inner_configure(self, event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._win_id, width=event.width)

    def _bind_mousewheel(self, event=None):
        self._canvas.bind_all("<MouseWheel>",      self._on_mousewheel)
        self._canvas.bind_all("<Button-4>",         self._on_mousewheel)
        self._canvas.bind_all("<Button-5>",         self._on_mousewheel)

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
        self.root.minsize(1100, 700)

        # Fijar tamanio de ventana para que el filedialog no lo modifique
        self.root.resizable(True, True)
        self._fixed_geometry = None

        self.df = None
        self.x_col = None
        self.y_cols = []
        self.channel_colors = list(COLORS)
        self.channel_vars = {}
        self.channel_offset_vars = {}
        self.channel_scale_vars = {}
        self.cursor_mode = False
        self.cursor_clicks = []
        self.cursor_lines = []

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

        # Panel izquierdo scrollable, ancho fijo 290px
        self._scroll_panel = ScrollableFrame(self.root, width=290)
        self._scroll_panel.grid(row=0, column=0, sticky="ns", padx=(8, 0), pady=8)
        self._scroll_panel.grid_propagate(False)
        self._scroll_panel.configure(width=290)

        # El frame interior es donde van todos los controles
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
        # ── Archivo ──
        f = self._lf(p, "ARCHIVO", 0)
        self.lbl_file = ttk.Label(f, text="Sin archivo cargado", wraplength=230)
        self.lbl_file.grid(row=0, column=0, columnspan=2, padx=4, pady=2)
        ttk.Button(f, text="Abrir CSV", command=self._open_file).grid(
            row=1, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(f, text="Recargar", command=self._replot).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew")
        if DND_AVAILABLE:
            ttk.Label(f, text="(o arrastra el .csv aqui)").grid(
                row=2, column=0, columnspan=2, pady=(0, 4))

        # ── Titulos ──
        f = self._lf(p, "TITULOS", 1)
        for i, (txt, attr) in enumerate([("Titulo", "ent_title"),
                                          ("Eje X",  "ent_xlabel"),
                                          ("Eje Y",  "ent_ylabel")]):
            ttk.Label(f, text=txt).grid(row=i, column=0, padx=4, pady=1, sticky="w")
            e = ttk.Entry(f)
            e.grid(row=i, column=1, padx=4, pady=1, sticky="ew")
            setattr(self, attr, e)
        self.ent_title.insert(0, "Senales de Osciloscopio")
        self.ent_xlabel.insert(0, "Tiempo")
        self.ent_ylabel.insert(0, "Voltaje")

        # ── Unidades ──
        f = self._lf(p, "UNIDADES", 2)
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

        # ── Escala logaritmica ──
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

        # ── Canales (dinamico) ──
        self.frm_channels = ttk.LabelFrame(p, text="CANALES")
        self.frm_channels.grid(row=5, column=0, sticky="ew", padx=4, pady=3)
        ttk.Label(self.frm_channels,
                  text="(cargar archivo primero)").pack(padx=4, pady=4)

        # ── Modo XY ──
        f = self._lf(p, "MODO XY - LISSAJOUS", 6)
        self.var_xy = tk.BooleanVar()
        ttk.Checkbutton(f, text="Activar modo XY", variable=self.var_xy).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Label(f, text="Canal X").grid(row=1, column=0, padx=4, pady=1, sticky="w")
        self.cmb_xy_x = ttk.Combobox(f, state="readonly", width=10)
        self.cmb_xy_x.grid(row=1, column=1, padx=4, pady=1, sticky="ew")
        ttk.Label(f, text="Canal Y").grid(row=2, column=0, padx=4, pady=1, sticky="w")
        self.cmb_xy_y = ttk.Combobox(f, state="readonly", width=10)
        self.cmb_xy_y.grid(row=2, column=1, padx=4, pady=1, sticky="ew")

        # ── Puntos importantes ──
        f = self._lf(p, "PUNTOS IMPORTANTES", 7)
        self.var_show_max = tk.BooleanVar()
        self.var_show_min = tk.BooleanVar()
        ttk.Checkbutton(f, text="Marcar maximo", variable=self.var_show_max).grid(
            row=0, column=0, columnspan=2, padx=4, pady=2, sticky="w")
        ttk.Checkbutton(f, text="Marcar minimo", variable=self.var_show_min).grid(
            row=1, column=0, columnspan=2, padx=4, pady=2, sticky="w")

        # ── Cursores ──
        f = self._lf(p, "CURSORES", 8)
        self.btn_cursor = ttk.Button(f, text="Activar cursores",
                                     command=self._toggle_cursors)
        self.btn_cursor.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(f, text="Limpiar", command=self._clear_cursors).grid(
            row=0, column=1, padx=4, pady=4, sticky="ew")
        self.lbl_cursor_info = ttk.Label(f, text="", wraplength=230)
        self.lbl_cursor_info.grid(row=1, column=0, columnspan=2, padx=4, pady=2)

        # ── Boton graficar ──
        ttk.Button(p, text="GRAFICAR", command=self._replot).grid(
            row=9, column=0, padx=4, pady=8, sticky="ew")

    def _build_plot(self, parent):
        self.fig = Figure(figsize=(8, 5))
        self.fig.patch.set_facecolor("#f5f5f5")
        self.ax = self.fig.add_subplot(111)

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        tb_frame = ttk.Frame(parent)
        tb_frame.grid(row=1, column=0, sticky="ew")
        self.toolbar = NavigationToolbar2Tk(self.canvas, tb_frame)
        self.toolbar.update()

        self._draw_welcome()

    def _draw_welcome(self):
        self.ax.clear()
        self.ax.set_facecolor("#f0f0f0")
        self.ax.text(0.5, 0.5,
                     "Abri un archivo .csv\nde osciloscopio para comenzar",
                     ha="center", va="center", transform=self.ax.transAxes,
                     color="#555555", fontsize=12)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.draw()

    # ─── Drag & drop ───────────────────────────

    def _setup_dnd(self):
        if not DND_AVAILABLE:
            return
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event):
        path = event.data.strip("{}")
        self._load_file(path)

    # ─── Abrir archivo ─────────────────────────

    def _open_file(self):
        # Guardar y fijar la geometria actual para que el filedialog no la cambie
        self.root.update_idletasks()
        geom = self.root.geometry()

        path = filedialog.askopenfilename(
            title="Seleccionar archivo CSV",
            filetypes=[("Archivos CSV", "*.csv"), ("Todos", "*.*")]
        )

        # Restaurar geometria exacta tras cerrar el dialogo
        self.root.geometry(geom)
        self.root.update_idletasks()

        if path:
            self._load_file(path)

    def _load_file(self, path):
        if not path.lower().endswith(".csv"):
            messagebox.showerror(
                "Archivo no valido",
                f"El archivo NO es un .csv:\n\n{os.path.basename(path)}\n\n"
                "Por favor selecciona un archivo con extension .csv"
            )
            return
        try:
            df, x_unit, y_unit = load_csv(path)
        except ValueError as e:
            messagebox.showerror("Error al leer el archivo", str(e))
            return

        self.df = df
        self.x_col = df.columns[0]
        self.y_cols = list(df.columns[1:])
        self.lbl_file.config(text=os.path.basename(path))

        if "second" in x_unit:
            self.cmb_time.set("us")
        if "volt" in y_unit:
            self.cmb_volt.set("V")

        self.channel_colors = [COLORS[i % len(COLORS)] for i in range(len(self.y_cols))]
        self._build_channel_controls()
        self._update_xy_combos()
        self._replot()

    # ─── Controles de canales ──────────────────

    def _build_channel_controls(self):
        for w in self.frm_channels.winfo_children():
            w.destroy()
        self.channel_vars = {}
        self.channel_offset_vars = {}
        self.channel_scale_vars = {}

        for i, col in enumerate(self.y_cols):
            short = col.split("[")[0].strip()
            frm = ttk.Frame(self.frm_channels)
            frm.pack(fill="x", padx=4, pady=2)
            frm.columnconfigure(1, weight=1)

            var_vis = tk.BooleanVar(value=True)
            self.channel_vars[col] = var_vis
            ttk.Checkbutton(frm, text=short, variable=var_vis).grid(
                row=0, column=0, sticky="w")

            btn = tk.Button(frm, bg=self.channel_colors[i], width=2, height=1,
                            relief="raised",
                            command=lambda c=col, idx=i: self._pick_color(c, idx))
            btn.grid(row=0, column=1, padx=4, sticky="e")
            setattr(self, f"_color_btn_{i}", btn)

            ttk.Label(frm, text="Offset:").grid(row=1, column=0, sticky="w", padx=4)
            var_off = tk.DoubleVar(value=0.0)
            self.channel_offset_vars[col] = var_off
            ttk.Entry(frm, textvariable=var_off, width=7).grid(
                row=1, column=1, sticky="ew", padx=4)

            ttk.Label(frm, text="Escala:").grid(row=2, column=0, sticky="w", padx=4)
            var_sc = tk.DoubleVar(value=1.0)
            self.channel_scale_vars[col] = var_sc
            ttk.Entry(frm, textvariable=var_sc, width=7).grid(
                row=2, column=1, sticky="ew", padx=4)

            ttk.Separator(self.frm_channels, orient="horizontal").pack(
                fill="x", padx=4)

    def _pick_color(self, col, idx):
        result = colorchooser.askcolor(color=self.channel_colors[idx],
                                       title=f"Color para {col}")
        if result and result[1]:
            self.channel_colors[idx] = result[1]
            btn = getattr(self, f"_color_btn_{idx}", None)
            if btn:
                btn.config(bg=result[1])

    def _update_xy_combos(self):
        self.cmb_xy_x["values"] = self.y_cols
        self.cmb_xy_y["values"] = self.y_cols
        if len(self.y_cols) >= 1:
            self.cmb_xy_x.set(self.y_cols[0])
        if len(self.y_cols) >= 2:
            self.cmb_xy_y.set(self.y_cols[1])

    # ─── Graficado ─────────────────────────────

    def _replot(self):
        if self.df is None:
            return
        self.ax.clear()
        self.ax.set_facecolor("#0d0d1a")
        self.fig.patch.set_facecolor("#1a1a2e")
        for spine in self.ax.spines.values():
            spine.set_color("#333366")
        self.ax.tick_params(colors="#aaaacc", labelsize=8)

        t_factor, t_label = TIME_UNITS[self.cmb_time.get()]
        v_factor, v_label = VOLT_UNITS[self.cmb_volt.get()]
        x_data = self.df[self.x_col].values * t_factor

        if self.var_xy.get() and len(self.y_cols) >= 2:
            self._plot_xy(v_factor)
        else:
            self._plot_normal(x_data, v_factor, t_label, v_label)

        self.canvas.draw()

    def _plot_normal(self, x_data, v_factor, t_label, v_label):
        title  = self.ent_title.get()  or "Senales de Osciloscopio"
        xlabel = self.ent_xlabel.get() or t_label
        ylabel = self.ent_ylabel.get() or v_label

        for i, col in enumerate(self.y_cols):
            if not self.channel_vars.get(col, tk.BooleanVar(value=True)).get():
                continue
            offset = self.channel_offset_vars[col].get()
            scale  = self.channel_scale_vars[col].get()
            y_data = self.df[col].values * v_factor * scale + offset
            label  = col.split("[")[0].strip()

            self.ax.plot(x_data, y_data, color=self.channel_colors[i],
                         linewidth=1.2, label=label)

            if self.var_show_max.get():
                idx_max = int(np.argmax(y_data))
                self.ax.annotate(
                    f"MAX {y_data[idx_max]:.3g}",
                    xy=(x_data[idx_max], y_data[idx_max]),
                    xytext=(8, 8), textcoords="offset points",
                    color=self.channel_colors[i], fontsize=7,
                    arrowprops=dict(arrowstyle="->",
                                   color=self.channel_colors[i], lw=0.8))
            if self.var_show_min.get():
                idx_min = int(np.argmin(y_data))
                self.ax.annotate(
                    f"MIN {y_data[idx_min]:.3g}",
                    xy=(x_data[idx_min], y_data[idx_min]),
                    xytext=(8, -14), textcoords="offset points",
                    color=self.channel_colors[i], fontsize=7,
                    arrowprops=dict(arrowstyle="->",
                                   color=self.channel_colors[i], lw=0.8))

        self.ax.set_title(title,  color="#00bfff", fontsize=11)
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

        if self.y_cols:
            self.ax.legend(loc="upper right", facecolor="#111130",
                           edgecolor="#3333aa", labelcolor="#ddddff",
                           fontsize=8)

        self._redraw_cursors()

    def _plot_xy(self, v_factor):
        cx = self.cmb_xy_x.get()
        cy = self.cmb_xy_y.get()
        if cx not in self.df.columns or cy not in self.df.columns:
            return
        xd = self.df[cx].values * v_factor
        yd = self.df[cy].values * v_factor
        self.ax.plot(xd, yd, color="#00bfff", linewidth=0.8)
        self.ax.set_title("Figura de Lissajous (Modo XY)", color="#00bfff")
        self.ax.set_xlabel(cx.split("[")[0].strip(), color="#8888aa")
        self.ax.set_ylabel(cy.split("[")[0].strip(), color="#8888aa")
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
        self.cursor_lines  = []
        self.lbl_cursor_info.config(text="")
        self._replot()


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    if DND_AVAILABLE:
        root = TkDnD()
    else:
        root = tk.Tk()
    app = OsciloscopioApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
