import streamlit as st
import pandas as pd
import geopandas as gpd
import plotly.express as px
import numpy as np
import plotly.graph_objects as go


st.set_page_config(page_title="H2Global meets Africa", layout="wide")

@st.cache_data
def load_marginal_prices(year):
    """Load marginal prices data for specified year with caching."""
    if year == "2035":
        filepath = "results/marginal_prices_2035.csv"
    else:
        filepath = "results/marginal_prices_2050.csv"
    
    return pd.read_csv(filepath, keep_default_na=False)

# Define vertical sidebar
with st.sidebar:
    title=st.title("Marginal Prices for Hydrogen export ports")
    topic= st.selectbox("Select Metric", ["Marginal prices"])
    
    year = st.selectbox(
        "Select Year",
        ["2035","2050"],)
    

#Plot for marginal prices figures
data = {"line_x": [], "line_y": [], "data_start": [], "data_end": [], "valid_countries": []}
skipped_countries = []

if year=="2035":
    data_start="0.1MtH2export"
    data_end="0.7MtH2export"
else:
    data_start="0.7MtH2export"
    data_end="4.0MtH2export"

df = load_marginal_prices(year)
countries = df["country"].unique()

for country in countries:
    scen_start = f"{country}-{data_start}"
    scen_end = f"{country}-{data_end}"
    
    start_data = df.loc[(df.scen == scen_start) & (df.country == country), "value"]
    end_data = df.loc[(df.scen == scen_end) & (df.country == country), "value"]
    
    if len(start_data) > 0 and len(end_data) > 0:
        val_start = start_data.values[0]
        val_end = end_data.values[0]
        
        data["data_start"].append(val_start)
        data["data_end"].append(val_end)
        data["line_x"].extend([val_start, val_end, None])
        data["line_y"].extend([country, country, None])
        data["valid_countries"].append(country)
    else:
        missing = []
        if len(start_data) == 0:
            missing.append(data_start)
        if len(end_data) == 0:
            missing.append(data_end)
        skipped_countries.append(f"{country} (missing: {', '.join(missing)})")

fig = go.Figure(
        data=[
            go.Scatter(
                x=data["line_x"],
                y=data["line_y"],
                mode="lines",
                showlegend=False,
                marker=dict(
                    color="grey"
                )
            ),
            go.Scatter(
                x=data["data_start"],
                y=data["valid_countries"],
                mode="markers+text",
                name=data_start,
                text=[f"{v:.1f}" for v in data["data_start"]],           
                textposition="top center",
                textfont=dict(size=15),
                marker=dict(
                    color="#A6BCC9",
                    size=20
                )

            ),
            go.Scatter(
                x=data["data_end"],
                y=data["valid_countries"],
                mode="markers+text",
                name=data_end,
                text=[f"{v:.1f}" for v in data["data_end"]],
                textposition="top center",
                textfont=dict(size= 15),
                marker=dict(
                    color="#179c7d",
                    size=20
                )
            ),
        ]
    )

fig.update_layout(
        title= f"Marginal price for H2 at export port in {year} <br><sub>Per country and H2 export volume in €/MWh_H2_LHV <sub>",
        title_font_size=30,
        width=400,
        height=max(750, len(data["valid_countries"]) * 50),
        legend_itemclick=False,
    )

if topic == "Marginal prices":
    if skipped_countries:
        st.warning(f"⚠️ Skipped {len(skipped_countries)} country(ies) due to missing scenario data: " + ", ".join(skipped_countries))
    st.plotly_chart(fig)
