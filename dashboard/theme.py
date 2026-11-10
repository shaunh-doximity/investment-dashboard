"""Shared typography and Plotly defaults for the dark Streamlit dashboard."""
from __future__ import annotations

PLOTLY_FONT_FAMILY = "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
PLOTLY_TEXT_COLOR = "#DDE3EC"
PLOTLY_MUTED_COLOR = "#AEB8C6"
PLOTLY_GRID_COLOR = "rgba(160, 174, 192, 0.16)"

PLOTLY_FONT_SIZE = 14
PLOTLY_LEGEND_FONT_SIZE = 14
PLOTLY_HOVER_FONT_SIZE = 14

CALENDAR_CARD_WIDTH = 304
CALENDAR_CARD_MIN_HEIGHT = 218
CALENDAR_CARD_PADDING = 16
CALENDAR_META_FONT_SIZE = 14
CALENDAR_DATE_FONT_SIZE = 16
CALENDAR_TITLE_FONT_SIZE = 19
CALENDAR_BODY_FONT_SIZE = 16
CALENDAR_SECONDARY_FONT_SIZE = 13


def apply_plotly_theme(figure, *, height: int | None = None):
    """Apply consistent readable chart typography without changing chart data."""
    layout = {
        "template": "plotly_dark",
        "font": {"family": PLOTLY_FONT_FAMILY, "size": PLOTLY_FONT_SIZE, "color": PLOTLY_TEXT_COLOR},
        "hoverlabel": {
            "font": {"family": PLOTLY_FONT_FAMILY, "size": PLOTLY_HOVER_FONT_SIZE, "color": PLOTLY_TEXT_COLOR},
            "bgcolor": "#202833",
            "bordercolor": "#495566",
        },
        "legend": {"font": {"family": PLOTLY_FONT_FAMILY, "size": PLOTLY_LEGEND_FONT_SIZE, "color": PLOTLY_TEXT_COLOR}},
    }
    if height is not None:
        layout["height"] = height
    figure.update_layout(**layout)
    axis_font = {"family": PLOTLY_FONT_FAMILY, "size": PLOTLY_FONT_SIZE, "color": PLOTLY_MUTED_COLOR}
    figure.update_xaxes(tickfont=axis_font, title_font=axis_font)
    figure.update_yaxes(tickfont=axis_font, title_font=axis_font)
    return figure
