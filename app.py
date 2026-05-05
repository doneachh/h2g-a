import math
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import json
from pathlib import Path

from scripts.plot_helpers import (
    load_plot_config,
    read_stats_dict,
    read_csv_nafix,
    prepare_dataframe,
    rename_electricity,
    rename_h2,
    rename_gas,
    rename_h2o,
    rename_oil,
    rename_co2,
    rename_co2_stored,
    rename_costs,
    rename_stores,
    colors,
    get_scen_col_function,
    apply_standard_styling,
    update_layout,
    plot_energy_balance,
    plot_capacity,
    plot_gwkm,
    h2o_cost_bar_fig,
    register_template,
    export_plotly_figure,
)

register_template()

st.set_page_config(page_title="H2Global meets Africa", layout="wide")
st.markdown(
    "<style>.block-container { padding-top: 1.5rem; }</style>",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data
def _load_yaml_config():
    return load_plot_config()


@st.cache_data
def _load_energy_data(sdir_str, balance_keys, capacity_keys, cost_keys):
    sdir = Path(sdir_str)
    balance_dict = read_stats_dict("balance_dict", sdir, keys=balance_keys)
    capacity_dict = read_stats_dict("optimal_capacity_dict", sdir, keys=capacity_keys)
    costs_dict = read_stats_dict("costs_dict", sdir, keys=cost_keys)
    return balance_dict, capacity_dict, costs_dict


@st.cache_data
def _load_marginal_prices(sdir_str):
    return read_csv_nafix(Path(sdir_str) / "marginal_prices_prepared.csv")


@st.cache_data
def _load_gwkm(sdir_str, filename):
    return read_csv_nafix(Path(sdir_str) / filename)


@st.cache_data
def _load_stores(sdir_str):
    df = read_csv_nafix(Path(sdir_str) / "stores.csv")
    df = df.set_index(INDEX_COLS)
    return df


@st.cache_data
def _load_h2o_cost_data(sdir_str, countries_tuple, years_tuple, index_levels_to_drop_tuple):
    """Load capex/opex/H2 CSVs and compute H2O cost per MWh_H2 by component."""
    sdir = Path(sdir_str)
    countries = list(countries_tuple)
    years = list(years_tuple)
    index_levels_to_drop = list(index_levels_to_drop_tuple)

    _IDX = [
        "run_name_prefix", "run_name", "country", "year",
        "simpl", "clusters", "ll", "opts", "sopts",
        "discountrate", "demand", "h2export",
    ]
    H2O_COMPONENTS = [
        "desalination", "seawater", "H2O pipeline",
        "H2O store", "H2O store charger", "H2O store discharger",
        "H2O generator",
    ]

    _capex = read_csv_nafix(sdir / "costs_dict_capex.csv")
    _opex  = read_csv_nafix(sdir / "costs_dict_opex.csv")
    _h2    = read_csv_nafix(sdir / "balance_dict_H2.csv")

    def _filt(df):
        return df[df["country"].isin(countries) & df["year"].isin(years)].copy()

    capex_f, opex_f, h2_f = _filt(_capex), _filt(_opex), _filt(_h2)

    present_comps = [
        c for c in H2O_COMPONENTS
        if c in capex_f.columns or c in opex_f.columns
    ]

    cap_sub = capex_f[_IDX + [c for c in present_comps if c in capex_f.columns]].rename(
        columns={c: f"{c}__cap" for c in present_comps if c in capex_f.columns}
    )
    opx_sub = opex_f[_IDX + [c for c in present_comps if c in opex_f.columns]].rename(
        columns={c: f"{c}__opx" for c in present_comps if c in opex_f.columns}
    )
    combined = cap_sub.merge(opx_sub, on=_IDX, how="inner")

    h2o_costs = combined[_IDX].copy()
    for comp in present_comps:
        cap_v = combined[f"{comp}__cap"].fillna(0) if f"{comp}__cap" in combined.columns else 0.0
        opx_v = combined[f"{comp}__opx"].fillna(0) if f"{comp}__opx" in combined.columns else 0.0
        h2o_costs[comp] = cap_v + opx_v

    h2_elec = h2_f[_IDX + ["H2 Electrolysis"]].copy()
    h2_elec["H2 Electrolysis"] = h2_elec["H2 Electrolysis"].abs()

    df_h2o = h2o_costs.merge(h2_elec, on=_IDX, how="inner")
    for comp in present_comps:
        df_h2o[comp] = df_h2o[comp] / df_h2o["H2 Electrolysis"] * 1000  # bn€/TWh→€/MWh

    # Use set_scen_col to derive scen labels (same mapping as all other charts)
    df_h2o = set_scen_col(df_h2o, index_levels_to_drop=index_levels_to_drop)

    df_scen = df_h2o[["country", "year", "scen"] + present_comps].copy()
    df_scen["total"] = df_scen[present_comps].sum(axis=1)

    nonzero_comps = [c for c in present_comps if df_scen[c].abs().max() > 0.01]
    return df_scen, nonzero_comps


@st.cache_data
def _load_geojson(path_str):
    with open(path_str) as f:
        return json.load(f)


@st.cache_data
def _load_merged_country_geojson(countries_tuple):
    """Load and merge country-shape GeoJSON features for all given countries (cached)."""
    all_features = []
    missing_files = []
    for country in countries_tuple:
        geojson_filename = _COUNTRY_SHAPE_TEMPLATE.format(country=country)
        geojson_path = _COUNTRY_SHAPES_DIR / geojson_filename
        if not geojson_path.exists():
            missing_files.append(geojson_filename)
            continue
        gj = _load_geojson(str(geojson_path))
        all_features.extend(gj["features"])
    return all_features, missing_files


@st.cache_data
def _build_country_map_layer_data(countries_tuple, year, sdir_str, map_level):
    """Build coloured country-shape GeoJSON and view-state for the export-bus price map."""
    all_features, missing_files = _load_merged_country_geojson(countries_tuple)
    if not all_features:
        return None, None, None, None, None, None, 0, 0, missing_files

    df_prices = _load_marginal_prices(sdir_str)
    country_scens = {f"{c}-{map_level}" for c in countries_tuple}
    mask = (
        df_prices["scen"].isin(country_scens)
        & (df_prices["year"] == year)
        & (df_prices["variable"] == "H2 export bus")
    )
    df_sel = df_prices[mask].copy()
    # One row per country — keyed on ISO2 country code
    price_lookup = df_sel.set_index("country")["value"].to_dict()

    values = [price_lookup.get(f["properties"]["name"]) for f in all_features]
    non_null = [v for v in values if v is not None]
    vmin = min(non_null) if non_null else 0
    vmax = max(non_null) if non_null else 1

    # Build coloured features with dict unpacking — avoids an expensive JSON round-trip
    features_colored = [
        {
            **feat,
            "properties": {
                **feat["properties"],
                "value": round(val, 2) if val is not None else None,
                "fill_color": _value_to_color(val, vmin, vmax, no_data=(val is None)),
            },
        }
        for feat, val in zip(all_features, values)
    ]
    geojson_colored = {"type": "FeatureCollection", "features": features_colored}

    # View-state — derived from geometry bounding box (country shapes have no stored x/y)
    all_lons, all_lats = [], []
    for feat in all_features:
        for lon, lat in _iter_coords(feat["geometry"]):
            all_lons.append(lon)
            all_lats.append(lat)
    view_lon = (min(all_lons) + max(all_lons)) / 2
    view_lat = (min(all_lats) + max(all_lats)) / 2
    span = max(max(all_lons) - min(all_lons), max(all_lats) - min(all_lats))
    zoom = max(1.5, min(6.0, math.log2(360 / max(span, 1))))

    return (
        geojson_colored, vmin, vmax,
        view_lon, view_lat, zoom,
        len(all_features), len(non_null),
        missing_files,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

yaml_config = _load_yaml_config()

RUN_NAME_PREFIX = yaml_config["run"]["name_prefix"]

_results_root = Path(__file__).parent / "results"
_summary_dirs = sorted(
    d for d in _results_root.iterdir()
    if d.is_dir() and d.name.startswith(f"{RUN_NAME_PREFIX}_summary_")
)
VERSION_OPTIONS = {
    f"13 countries {d.name.split('_summary_')[-1]}": d
    for d in _summary_dirs
}

ALL_COUNTRIES = yaml_config["data"]["countries"]
DEFAULT_COUNTRIES = ALL_COUNTRIES[5:7]
ALL_YEARS = yaml_config["data"]["years"]
INDEX_LEVELS_TO_DROP = yaml_config["data"]["index_levels_to_drop"]
SCEN_FILTER = yaml_config["data"]["scen_filter"]
SCEN_ORDER = yaml_config["data"]["scen_order"]

set_scen_col = get_scen_col_function(yaml_config["data"]["scen_col_function"])

ISO2_TO_NAME = yaml_config["data"]["iso2_to_name"]

INDEX_COLS = [
    "run_name_prefix", "run_name", "country", "year",
    "simpl", "clusters", "ll", "opts", "sopts",
    "discountrate", "demand", "h2export",
]

# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------

DATASETS = {
    "Marginal prices": {"type": "marginal"},
    "Electricity supply": {
        "type": "balance", "carrier": "AC", "chart_key": "electricity_supply",
        "rename": rename_electricity, "show_supply": True,
    },
    "Electricity demand": {
        "type": "balance", "carrier": "AC", "chart_key": "electricity_demand",
        "rename": rename_electricity, "show_supply": False,
    },
    "Electricity capacity": {
        "type": "capacity", "carrier": "AC", "chart_key": "electricity_capacity",
        "rename": rename_electricity,
    },
    "Hydrogen balance": {
        "type": "balance", "carrier": "H2", "chart_key": "hydrogen_balance",
        "rename": rename_h2,
    },
    "Hydrogen capacity": {
        "type": "capacity", "carrier": "H2", "chart_key": "hydrogen_capacity",
        "rename": rename_h2,
    },
    "Storage capacity": {
        "type": "stores", "chart_key": "stores_capacity",
    },
    "Liquid fuel balance": {
        "type": "balance", "carrier": "oil", "chart_key": "liquid_fuel_balance",
        "rename": rename_oil,
    },
    "CH4 balance": {
        "type": "balance", "carrier": "gas", "chart_key": "ch4_balance",
        "rename": rename_gas,
    },
    "CO2 capture and usage": {
        "type": "balance", "carrier": "co2 stored", "chart_key": "co2_stored_balance",
        "rename": rename_co2_stored,
    },
    "CO2 emissions": {
        "type": "balance", "carrier": "co2", "chart_key": "co2_emissions",
        "rename": rename_co2,
    },
    "Water balance": {
        "type": "balance", "carrier": "H2O", "chart_key": "water_balance",
        "rename": rename_h2o,
    },
    "System costs": {"type": "costs", "chart_key": "system_costs"},
    "Grid km (AC)": {"type": "gwkm", "filename": "gwkm_dict_AC.csv"},
    "Grid km (H2 pipeline)": {"type": "gwkm", "filename": "gwkm_dict_H2 pipeline.csv"},
    "Water cost breakdown": {"type": "h2o_cost", "chart_key": "h2o_cost_breakdown", "rename": rename_h2o},
}

# ---------------------------------------------------------------------------
# Marginal-price dumbbell chart (one per year)
# ---------------------------------------------------------------------------

def _marginal_price_dumbbell(df, year, data_start, data_mid, data_end,
                             use_iso2=True,
                             xaxis_title="Marginal price (\u20ac/MWh<sub>H2</sub>)",
                             conversion_factor=0.03333,
                             xaxis2_title="Marginal price (\u20ac/kg H2)",
                             xaxis2_color="#a8508c"):

    df = df[(df.year == year)].copy()
    countries = df["country"].unique()

    # Build legend names with volume suffixes from config
    yr_volumes = VOLUME_LABELS.get(year, {})
    label_start = f"{data_start} ({yr_volumes[data_start]})" if data_start in yr_volumes else data_start
    label_mid   = f"{data_mid} ({yr_volumes[data_mid]})"     if data_mid   in yr_volumes else data_mid
    label_end   = f"{data_end} ({yr_volumes[data_end]})"     if data_end   in yr_volumes else data_end

    vals_start, vals_mid, vals_end, valid = [], [], [], []
    skipped = []

    for country in countries:
        s = df.loc[(df.scen == f"{country}-{data_start}") & (df.country == country), "value"]
        m = df.loc[(df.scen == f"{country}-{data_mid}")   & (df.country == country), "value"]
        e = df.loc[(df.scen == f"{country}-{data_end}")   & (df.country == country), "value"]
        if len(s) > 0 and len(e) > 0:
            vals_start.append(s.values[0])
            vals_end.append(e.values[0])
            vals_mid.append(m.values[0] if len(m) > 0 else None)
            valid.append(country)
        else:
            missing = []
            if len(s) == 0:
                missing.append(data_start)
            if len(e) == 0:
                missing.append(data_end)
            skipped.append(f"{country} (missing: {', '.join(missing)})")

    # Sort descending by data_start value (highest at bottom, lowest at top)
    order = sorted(range(len(vals_start)), key=lambda i: vals_start[i], reverse=True)
    valid      = [valid[i]      for i in order]
    vals_start = [vals_start[i] for i in order]
    vals_end   = [vals_end[i]   for i in order]
    vals_mid   = [vals_mid[i]   for i in order]

    # Map ISO2 codes to full country names (or keep codes)
    valid_names = valid if use_iso2 else [ISO2_TO_NAME.get(c, c) for c in valid]

    # Compute max value for axis range
    all_vals = vals_start + vals_end + [v for v in vals_mid if v is not None]
    max_val = max(all_vals) if all_vals else 1

    # Connecting lines spanning full range (including mid)
    line_x, line_y = [], []
    for vs, vm, ve, name in zip(vals_start, vals_mid, vals_end, valid_names):
        span = [v for v in [vs, vm, ve] if v is not None]
        line_x.extend([min(span), max(span), None])
        line_y.extend([name, name, None])

    fig = go.Figure(data=[
        go.Scatter(x=line_x, y=line_y, mode="lines", showlegend=False,
                   marker=dict(color="grey")),
        go.Scatter(
            x=vals_start, y=valid_names, mode="markers+text", name=label_start,
            text=[f"{v:.0f}" for v in vals_start], textposition="middle left",
            textfont=dict(size=11), marker=dict(color="#99bdcc", size=13),
        ),
        go.Scatter(
            x=[v for v in vals_mid if v is not None],
            y=[n for n, v in zip(valid_names, vals_mid) if v is not None],
            mode="markers", name=label_mid,
            marker=dict(color="#669db2", size=13),
        ),
        go.Scatter(
            x=vals_end, y=valid_names, mode="markers+text", name=label_end,
            text=[f"{v:.0f}" for v in vals_end], textposition="middle right",
            textfont=dict(size=11), marker=dict(color="#005b7f", size=13),
        ),
        # Invisible trace to anchor the secondary x-axis
        go.Scatter(
            x=[80 * conversion_factor, max_val * 1.1 * conversion_factor],
            y=[valid_names[0], valid_names[0]],
            xaxis="x2", mode="markers",
            marker=dict(opacity=0), showlegend=False, hoverinfo="skip",
        ),
    ])

    fig.update_layout(
        height=max(500, len(valid_names) * 45),
        legend=dict(x=0.95, y=1, xanchor="left", yanchor="top"),
        legend_itemclick=False,
        xaxis_title=xaxis_title,
        xaxis=dict(range=[80, max_val * 1.1]),
        yaxis=dict(title="", categoryorder="array", categoryarray=valid_names),
        xaxis2=dict(
            title=dict(text=xaxis2_title, font=dict(color=xaxis2_color)),
            overlaying="x", side="top",
            range=[80 * conversion_factor, max_val * 1.1 * conversion_factor],
            tickfont=dict(color=xaxis2_color),
            linecolor=xaxis2_color,
            tickcolor=xaxis2_color,
        ),
    )
    return fig, skipped


# ---------------------------------------------------------------------------
# Map helpers
# ---------------------------------------------------------------------------

_COUNTRY_SHAPES_DIR = Path(__file__).parent / "resources" / "country_shapes"
_COUNTRY_SHAPE_TEMPLATE = "H2G_A_{country}_country_shape.geojson"


def _iter_coords(geometry):
    """Yield all (lon, lat) pairs from a GeoJSON geometry (Polygon or MultiPolygon)."""
    gtype = geometry["type"]
    coords = geometry["coordinates"]
    if gtype == "Polygon":
        for ring in coords:
            yield from ring
    elif gtype == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                yield from ring


def _value_to_color(value, vmin, vmax, no_data=False):
    """Map a scalar value to an RGBA list using a yellow→teal gradient."""
    if no_data or value is None:
        return [180, 180, 180, 160]  # grey for missing
    if vmax == vmin:
        t = 0.5
    else:
        t = max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
    # low: #005b7f (0, 91, 127), high: #fce356 (252, 227, 86)
    r = int(0 + t * (252 - 0))
    g = int(91 + t * (227 - 91))
    b = int(127 + t * (86 - 127))
    return [r, g, b, 200]


def _render_marginal_map(df_prices, unit_label,
                         selected_countries, selected_levels, year, sdir_str):
    """Render a PyDeck choropleth map of H2 export bus marginal prices per country."""
    col_controls, col_map = st.columns([1, 3])
    map_year = year

    with col_controls:
        # Use a set for O(1) membership checks
        scens_set = set(df_prices["scen"])
        available_levels = [
            lvl for lvl in selected_levels
            if any(f"{c}-{lvl}" in scens_set for c in selected_countries)
        ]
        if not available_levels:
            st.warning("No scenario data available for the selected countries.")
            return
        map_level = st.selectbox("Scenario level", available_levels, key="map_level")

    (
        geojson_colored, vmin, vmax,
        view_lon, view_lat, zoom,
        n_features, n_non_null,
        missing_files,
    ) = _build_country_map_layer_data(
        tuple(sorted(selected_countries)), map_year,
        sdir_str, map_level,
    )

    with col_controls:
        if missing_files:
            st.warning(f"GeoJSON not found for: {', '.join(missing_files)}")

    with col_map:
        if geojson_colored is None:
            st.warning("No country shape GeoJSON files found for the selected countries.")
            return

        view_state = pdk.ViewState(
            longitude=view_lon,
            latitude=view_lat,
            zoom=zoom,
            pitch=0,
        )

        layer = pdk.Layer(
            "GeoJsonLayer",
            data=geojson_colored,
            get_fill_color="properties.fill_color",
            get_line_color=[80, 80, 80, 200],
            line_width_min_pixels=1,
            pickable=True,
            auto_highlight=True,
        )

        tooltip = {
            "html": "<b>{name}</b><br/>" + unit_label + ": {value}",
            "style": {"backgroundColor": "white", "color": "black", "fontSize": "13px"},
        }

        st.pydeck_chart(
            pdk.Deck(
                layers=[layer],
                initial_view_state=view_state,
                tooltip=tooltip,
                map_style="light",
            ),
            use_container_width=True,
            height=700,
        )
        _unit = unit_label.split("(")[-1].rstrip(")")
        st.markdown(
            f"""
            <div style="display:flex; align-items:center; gap:8px; margin-bottom:2px;">
              <div style="font-size:12px; white-space:nowrap;">{vmin:.0f} {_unit}</div>
              <div style="flex:1; height:12px; border-radius:4px;
                          background: linear-gradient(to right, #005b7f, #fce356);"></div>
              <div style="font-size:12px; white-space:nowrap;">{vmax:.0f} {_unit}</div>
            </div>
            <div style="font-size:11px; color:grey; margin-bottom:4px;">
              {len(selected_countries)} countr{'y' if len(selected_countries) == 1 else 'ies'} &nbsp;·&nbsp;
              {n_non_null}/{n_features} countries with data
            </div>
            """,
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------

def _render_export_controls(fig, key: str) -> None:
    """Render width/height inputs and a PNG download button below a chart.

    Parameters
    ----------
    fig : plotly.graph_objects.Figure
        The figure to export.
    key : str
        Human-readable identifier (e.g. the dataset name) used to build
        unique Streamlit widget keys.
    """
    key = key + "_CC BY 4.0_h2export_paper_" 
    key_safe = (
        key.lower()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("(", "")
        .replace(")", "")
    )
    export_w = int(st.session_state.get("exp_w_global", 800))
    export_h = int(st.session_state.get("exp_h_global", 500))
    _, col_dl = st.columns([10, 2])
    with col_dl:
        img_bytes = export_plotly_figure(fig, width=export_w, height=export_h)
        if img_bytes is not None:
            st.download_button(
                label="⬇ Download figure (.png)",
                data=img_bytes,
                file_name=f"{key_safe}.png",
                mime="image/png",
                key=f"exp_dl_{key_safe}",
            )
        else:
            st.caption("⚠ PNG export unavailable (kaleido not installed?)")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("H2Global meets Africa Project")
    st.subheader("Comparative assessment of hydrogen production costs using PyPSA-Earth")
    _default_version = f"13 countries {yaml_config['run']['summary_version']}"
    _default_idx = list(VERSION_OPTIONS).index(_default_version) if _default_version in VERSION_OPTIONS else 0
    selected_version = st.selectbox("Select experiment version", list(VERSION_OPTIONS), index=_default_idx)
    SDIR = VERSION_OPTIONS[selected_version]
    st.markdown("Display options")
    volume_in_mt = st.checkbox("H2 volume unit in Mt", value=False)
    flip_axes = st.checkbox("Flip axes", value=False)
    show_totals = st.checkbox("Show totals on bars", value=True)
    show_map = st.checkbox("Show map tab", value=False)
    use_rename = st.checkbox("Rename variables", value=True)
    use_iso2 = st.checkbox("Use 2-letter country code", value=True)
    st.markdown("Download options")
    st.number_input(
        "Width (px)", min_value=100, max_value=4000, value=800, step=50, key="exp_w_global",
    )
    st.number_input(
        "Height (px)", min_value=100, max_value=4000, value=500, step=50, key="exp_h_global",
    )

_vl_key = "volume_labels_mt" if volume_in_mt else "volume_labels_twh"
VOLUME_LABELS = {int(k): v for k, v in yaml_config["data"][_vl_key].items()}

# ---------------------------------------------------------------------------
# URL query-parameter pre-filtering
# ---------------------------------------------------------------------------
# Supported parameters (all optional):
#   ?country=MA            – single ISO2 country code, pre-selects that country
#   ?country=MA,KE,NG      – comma-separated list, pre-selects multiple countries
#   ?year=2035             – pre-selects the year (integer)
#   ?dataset=Marginal+prices – pre-selects the dataset tab
#   ?level=low,mid         – comma-separated scenario levels
#   ?embed=true            – hides sidebar/header/footer for clean iframe embedding
#                            (Streamlit Community Cloud hides toolbar/header/footer
#                             automatically; this additionally hides the sidebar)
#
# Example iframe embed:
#   <iframe src="https://your-app.com/?country=MA&year=2035&embed=true" ...></iframe>
# The parent website can update the iframe src dynamically via JavaScript to
# react to map-click events:
#   iframe.src = `https://your-app.com/?country=${isoCode}&year=${year}&embed=true`;
# ---------------------------------------------------------------------------

_qp = st.query_params

# Country
_qp_country_raw = _qp.get("country", "")
_qp_countries = (
    [c.strip().upper() for c in _qp_country_raw.split(",") if c.strip()]
    if _qp_country_raw else []
)
_qp_countries = [c for c in _qp_countries if c in ALL_COUNTRIES]  # validate

# Year
_qp_year_raw = _qp.get("year", "")
try:
    _qp_year = int(_qp_year_raw) if _qp_year_raw else None
except ValueError:
    _qp_year = None
_qp_year = _qp_year if _qp_year in ALL_YEARS else None

# Dataset
_qp_dataset_raw = _qp.get("dataset", "")
_qp_dataset = _qp_dataset_raw if _qp_dataset_raw in DATASETS else None

# Scenario levels
_qp_level_raw = _qp.get("level", "")
_qp_levels = (
    [lvl.strip().lower() for lvl in _qp_level_raw.split(",") if lvl.strip()]
    if _qp_level_raw else []
)
_qp_levels = [lvl for lvl in _qp_levels if lvl in ["low", "mid", "high"]]

# Embed mode — hide sidebar (Streamlit Cloud handles header/footer/toolbar)
_qp_embed = _qp.get("embed", "").strip().lower() == "true"
if _qp_embed:
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"] { display: none !important; }
        [data-testid="collapsedControl"]  { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Main area — controls
# ---------------------------------------------------------------------------

LEVEL_OPTIONS = ["low", "mid", "high"]
col_dataset, col_scen_level, col_year = st.columns([2, 1, 1])
with col_dataset:
    _ds_index = list(DATASETS.keys()).index(_qp_dataset) if _qp_dataset else 0
    dataset = st.selectbox("Select dataset", list(DATASETS.keys()), index=_ds_index)
with col_scen_level:
    if dataset not in ("Marginal prices",):
        _lvl_default = _qp_levels if _qp_levels else LEVEL_OPTIONS
        selected_levels = st.multiselect(
            "Scenario levels", LEVEL_OPTIONS, default=_lvl_default
        )
        if not selected_levels:
            selected_levels = LEVEL_OPTIONS
    else:
        selected_levels = LEVEL_OPTIONS
with col_year:
    if dataset in ("Marginal prices", "Water cost breakdown"):
        _yr_index = ALL_YEARS.index(_qp_year) if _qp_year else 0
        selected_year = st.selectbox("Select year", ALL_YEARS, index=_yr_index)

default_all = dataset in ("Marginal prices", "Water cost breakdown")
col_check, col_select = st.columns([1, 3])
with col_check:
    select_all = st.checkbox("Select all countries", value=default_all)
with col_select:
    _country_default = (
        ALL_COUNTRIES if select_all
        else (_qp_countries if _qp_countries else DEFAULT_COUNTRIES)
    )
    selected_countries = st.multiselect(
        "Select countries", options=ALL_COUNTRIES,
        default=_country_default,
        disabled=select_all,
    )
    if select_all:
        selected_countries = ALL_COUNTRIES

if not selected_countries:
    st.warning("Select at least one country.")
    st.stop()

spec = DATASETS[dataset]
_is_map_type = spec["type"] == "marginal"
_show_map_tab = _is_map_type and show_map
_tab_labels = ["Chart", "Table", "Map", "Info"] if _show_map_tab else ["Chart", "Table", "Info"]
_tabs = st.tabs(_tab_labels)
tab_chart, tab_table = _tabs[0], _tabs[1]
tab_map = _tabs[2] if _show_map_tab else None
tab_info = _tabs[3] if _show_map_tab else _tabs[2]

# ---------------------------------------------------------------------------
# Build plot config for energy-system charts
# ---------------------------------------------------------------------------

idx_slice = pd.IndexSlice
IDX_GROUP = idx_slice[[RUN_NAME_PREFIX], :, selected_countries, ALL_YEARS]

width = yaml_config["plot"]["width"]
_heights_key = "flipped_heights" if flip_axes else "heights"
heights = yaml_config["plot"][_heights_key]
_kwargs_key = "flipped_kwargs" if flip_axes else "default_kwargs"
fig_kwargs = yaml_config["plot"][_kwargs_key].copy()
fig_kwargs["width"] = width
fig_kwargs["height"] = heights["large"] if len(selected_countries) > 8 else heights["medium"]

# Derive scenario-level filter, order and rename function
single_level = len(selected_levels) == 1

def _rename_scen(s):
    if single_level:
        s = s.rsplit("-", 1)[0]
    if not use_iso2:
        parts = s.split("-", 1)
        s = ISO2_TO_NAME.get(parts[0], parts[0]) + ("-" + parts[1] if len(parts) > 1 else "")
    return s

rename_scen_func = _rename_scen if (single_level or not use_iso2) else None

effective_scen_filter = [f"{c}-{lvl}" for c in ALL_COUNTRIES for lvl in selected_levels]
if SCEN_FILTER:
    effective_scen_filter = [s for s in effective_scen_filter if s in SCEN_FILTER]
effective_scen_order = [
    s for s in SCEN_ORDER
    if any(s.endswith(f"-{lvl}") for lvl in selected_levels)
    and any(s.startswith(f"{c}-") for c in selected_countries)
]
display_scen_order = (
    [rename_scen_func(s) for s in effective_scen_order]
    if rename_scen_func else effective_scen_order
)

plot_config = {
    "idx_group": IDX_GROUP,
    "idx_group_name": "".join(selected_countries) + "_MAIN",
    "index_levels_to_drop": INDEX_LEVELS_TO_DROP,
    "set_scen_col_func": set_scen_col,
    "scen_filter": effective_scen_filter,
    "scen_order": display_scen_order,
    "fig_kwargs": fig_kwargs,
    "heights": heights,
    "flip_axes": flip_axes,
    "show_totals": show_totals,
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

if spec["type"] == "marginal":
    df = _load_marginal_prices(str(SDIR))
    df = df[df["country"].isin(selected_countries)].copy()
    df = df.query("variable == 'H2 export bus'").copy()
    if SCEN_FILTER:
        df = df.query("scen in @SCEN_FILTER")

    ds, dm, de = "low", "mid", "high"

    with tab_chart:
        fig, skipped = _marginal_price_dumbbell(df, selected_year, ds, dm, de, use_iso2=use_iso2)
        if skipped:
            st.caption(f"Skipped: {', '.join(skipped)}")
        st.plotly_chart(fig, use_container_width=True)
        _render_export_controls(fig, dataset)

    with tab_table:
        _tbl = df[["country", "year", "scen", "value"]].copy()
        if not use_iso2:
            _tbl["country"] = _tbl["country"].map(lambda c: ISO2_TO_NAME.get(c, c))
        st.dataframe(
            _tbl.sort_values(["year", "country", "scen"]),
            use_container_width=True,
        )

    if tab_map is not None:
        with tab_map:
            _df_map = _load_marginal_prices(str(SDIR))
            _df_map = _df_map[_df_map["country"].isin(selected_countries)].copy()
            _render_marginal_map(
                _df_map, "Price (€/MWh<sub>H2</sub>)",
                selected_countries, LEVEL_OPTIONS, selected_year, str(SDIR),
            )

elif spec["type"] in ("balance", "capacity"):
    balance_dict, capacity_dict, costs_dict = _load_energy_data(
        str(SDIR),
        yaml_config["carriers"]["balance"],
        yaml_config["carriers"]["capacity"],
        yaml_config["carriers"]["costs"],
    )
    plot_config["chart_config"] = yaml_config["charts"][spec["chart_key"]]

    try:
        rename_func = spec.get("rename") if use_rename else None
        if spec["type"] == "balance":
            fig, df = plot_energy_balance(
                carrier=spec["carrier"],
                balance_dict=balance_dict,
                config=plot_config,
                rename_function=rename_func,
                rename_scen_function=rename_scen_func,
                show_supply=spec.get("show_supply"),
            )
        else:
            fig, df = plot_capacity(
                carrier=spec["carrier"],
                capacity_dict=capacity_dict,
                config=plot_config,
                rename_function=rename_func,
                rename_scen_function=rename_scen_func,
            )

        with tab_chart:
            st.plotly_chart(fig, use_container_width=True)
            _render_export_controls(fig, dataset)
        with tab_table:
            if not use_iso2:
                df = df.copy()
                df["country"] = df["country"].map(lambda c: ISO2_TO_NAME.get(c, c))
            st.dataframe(df, use_container_width=True)

    except (ValueError, KeyError) as e:
        st.error(f"No data available for this selection: {e}")

elif spec["type"] == "costs":
    balance_dict, capacity_dict, costs_dict = _load_energy_data(
        str(SDIR),
        yaml_config["carriers"]["balance"],
        yaml_config["carriers"]["capacity"],
        yaml_config["carriers"]["costs"],
    )
    chart_config = yaml_config["charts"].get("system_costs", {
        "title": "System Costs",
        "save_name": "system_costs_billion_eur",
        "chart_type": "bar_with_negatives",
    })
    plot_config["chart_config"] = chart_config

    try:
        stats_df = pd.concat([costs_dict["capex"], costs_dict["opex"]], axis=0)
        df = prepare_dataframe(
            stats_df, IDX_GROUP, INDEX_LEVELS_TO_DROP, set_scen_col,
            rename_function=rename_costs if use_rename else None,
        )
        df = df[df["value"] != 0]
        if effective_scen_filter:
            df = df.query("scen in @effective_scen_filter")
        if rename_scen_func:
            df["scen"] = df["scen"].map(rename_scen_func)

        fig_kw = fig_kwargs.copy()
        fig_kw["height"] = heights["large"]

        fig = px.bar(
            df, **fig_kw,
            color_discrete_map=colors["costs"],
            category_orders={
                "variable": list(colors["costs"].keys()),
                "scen": display_scen_order,
            },
            labels={"value": "System costs (billion EUR/year)", "year": "", "scen": ""},
        )
        fig = apply_standard_styling(fig, chart_config.get("chart_type", "bar"),
                                     chart_config)
        update_layout(fig, flip_axes=flip_axes)

        with tab_chart:
            st.plotly_chart(fig, use_container_width=True)
            _render_export_controls(fig, dataset)
        with tab_table:
            if not use_iso2:
                df = df.copy()
                df["country"] = df["country"].map(lambda c: ISO2_TO_NAME.get(c, c))
            st.dataframe(df, use_container_width=True)

    except (ValueError, KeyError) as e:
        st.error(f"No data available for this selection: {e}")

elif spec["type"] == "gwkm":
    gwkm_df = _load_gwkm(str(SDIR), spec["filename"])
    gwkm_df = gwkm_df[gwkm_df["country"].isin(selected_countries)].copy()

    try:
        fig, gwkm_plot_df = plot_gwkm(
            gwkm_df,
            config=plot_config,
            rename_scen_function=rename_scen_func,
        )
        with tab_chart:
            st.plotly_chart(fig, use_container_width=True)
            _render_export_controls(fig, dataset)
        with tab_table:
            _gwkm_tbl = gwkm_plot_df[["country", "year", "scen", "variable", "value"]].copy()
            if not use_iso2:
                _gwkm_tbl["country"] = _gwkm_tbl["country"].map(lambda c: ISO2_TO_NAME.get(c, c))
            st.dataframe(
                _gwkm_tbl.sort_values(["year", "country"]),
                use_container_width=True,
            )
    except (ValueError, KeyError) as e:
        st.error(f"No data available for this selection: {e}")

elif spec["type"] == "stores":
    stores_df = _load_stores(str(SDIR))
    plot_config["chart_config"] = yaml_config["charts"][spec["chart_key"]]

    try:
        fig, df = plot_capacity(
            carrier="stores",
            capacity_dict={"stores": stores_df},
            config=plot_config,
            rename_function=rename_stores if use_rename else None,
            rename_scen_function=rename_scen_func,
        )

        with tab_chart:
            st.plotly_chart(fig, use_container_width=True)
            _render_export_controls(fig, dataset)
        with tab_table:
            if not use_iso2:
                df = df.copy()
                df["country"] = df["country"].map(lambda c: ISO2_TO_NAME.get(c, c))
            st.dataframe(df, use_container_width=True)

    except (ValueError, KeyError) as e:
        st.error(f"No data available for this selection: {e}")

elif spec["type"] == "h2o_cost":
    try:
        df_scen, nonzero_comps = _load_h2o_cost_data(
            str(SDIR),
            tuple(ALL_COUNTRIES),
            tuple(ALL_YEARS),
            tuple(INDEX_LEVELS_TO_DROP),
        )
        df_scen = df_scen[df_scen["country"].isin(selected_countries)].copy()
        if effective_scen_filter:
            df_scen = df_scen[df_scen["scen"].isin(effective_scen_filter)]
        if not use_iso2:
            df_scen["country"] = df_scen["country"].map(lambda c: ISO2_TO_NAME.get(c, c))
        if rename_scen_func:
            df_scen["scen"] = df_scen["scen"].map(rename_scen_func)

        comp_colors = yaml_config["charts"]["h2o_cost_breakdown"]["color_discrete_map"]

        with tab_chart:
            fig = h2o_cost_bar_fig(
                df_scen,
                year=selected_year,
                components=nonzero_comps,
                scen_order_list=display_scen_order,
                component_colors=comp_colors,
                show_totals=show_totals,
                flip_axes=flip_axes,
            )
            st.plotly_chart(fig, use_container_width=True)
            _render_export_controls(fig, dataset)

        with tab_table:
            st.dataframe(
                df_scen[df_scen["year"] == selected_year]
                .sort_values(["scen"])
                .round(2),
                use_container_width=True,
            )

    except (ValueError, KeyError, FileNotFoundError) as e:
        st.error(f"No H2O cost data available for this selection: {e}")

# ---------------------------------------------------------------------------
# Info tab
# ---------------------------------------------------------------------------

_INFO_CONTENT = """
## Scenario Export Levels

The scenarios represent different levels of annual green hydrogen (GH2) export demand
modelled per country.
Each country is simulated independently at three export ambition levels —
**low**, **mid**, and **high** — which scale between the two planning horizons.

### 2035 — Early ramp-up phase

| Level | Annual H2 export | Approx. mass |
|-------|:----------------:|:------------:|
| **Low**  | 3.33 TWh/year   | ≈ 100 kt H₂/year |
| **Mid**  | 13.33 TWh/year  | ≈ 400 kt H₂/year |
| **High** | 23.33 TWh/year  | ≈ 700 kt H₂/year |

### 2050 — Mature market phase

| Level | Annual H2 export | Approx. mass |
|-------|:----------------:|:------------:|
| **Low**  | 23.33 TWh/year  | ≈ 700 kt H₂/year   |
| **Mid**  | 78.33 TWh/year  | ≈ 2,350 kt H₂/year |
| **High** | 133.32 TWh/year | ≈ 4,000 kt H₂/year |

> **Note:** The 2035 *high* level (23.33 TWh/year) equals the 2050 *low* level,
> reflecting a step-up in export ambitions from early deployment to full-scale operations.
> Mass values are derived using a lower heating value of 33.33 kWh/kg H₂.
"""

with tab_info:
    st.markdown(_INFO_CONTENT)

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
col1, col2, col3 = st.columns(3)

with col1:
    st.caption(
        'Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)',
    )
    st.caption(
        "Paper: Achhammer, A., Jansen, L., Schumm, L., Meisinger, A., "
        "Duque Pérez, E., Schamel, M., Pilsl, A., Nguyen, H. H., & Sterner, M., "
        "2026, "
        "Comparative assessment of hydrogen production costs in thirteen African "
        "countries using PyPSA-Earth, submitted to SDEWES 2026."
    )

with col2:
    st.caption("OTH Regensburg (FENES), Fraunhofer IEE, 2026")
    st.caption("App: Jansen, L., Achhammer, A., Huy Hoang, M.:" \
    " [Github Repository](https://github.com/ljansen-iee/h2g-vis)")

with col3:
    st.caption("[Imprint](https://www.iee.fraunhofer.de/en/publishing-notes.html)")
    st.caption("[Data Protection](https://www.iee.fraunhofer.de/en/data_protection.html)")
