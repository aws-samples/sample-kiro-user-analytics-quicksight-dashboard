#!/usr/bin/env python3
"""
Create (or update) the Kiro QuickSight Analysis and published Dashboard
via boto3. AWS CloudFormation provisions the DataSource and DataSets
(see cfn/02-quicksight.yaml); this script assembles the QuickSight
Definition payload in Python, which keeps the visual configuration
easy to edit and review.

Usage:
    python scripts/create_dashboard.py \\
        --account-id 123456789012 \\
        --region us-east-1 \\
        --principal-arn arn:aws:quicksight:us-east-1:123456789012:user/default/me
"""
from __future__ import annotations

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

DEFAULT_ASSET_ID = "kiro-user-analytics"
NAME = "Kiro User Analytics"

# Dataset identifier suffixes. Combined with --resource-prefix to form the
# full DataSetId at build time. Must match the IDs minted by cfn/02-quicksight.yaml.
DATASET_SUFFIXES = {
    "base":         "base-user-activity",
    "trends":       "daily-trends",
    "users":        "user-totals",
    "tiers":        "tier-breakdown",
    "engagement":   "engagement",
    "movers":       "wow-movers",
    "models":       "model-usage",
    "heatmap":      "activity-heatmap",
    "cohort":       "cohort-retention",
    "period":       "period-comparison",
}
# NOTE: the People engagement funnel was reworked from the native funnel chart
# (which read the `engagement-funnel` SPICE dataset) to three date-range-driven
# KPI tiles computed from `base` (see funnel_geN_user calc fields). The
# `engagement-funnel` dataset, its refresh schedule, and the underlying Athena
# view (formerly athena/08_engagement_funnel.sql) have all been removed; the
# funnel now depends only on base-user-activity.


def dataset_arn(account_id: str, region: str, dataset_id: str) -> str:
    return f"arn:aws:quicksight:{region}:{account_id}:dataset/{dataset_id}"


# Auto-abbreviated number format (12345678 -> 12.3M, 12345 -> 12.3K). Applied
# to KPI tile values so customers with millions of messages don't see raw
# digits. QS picks the appropriate scale automatically with NumberScale: AUTO.
_AUTO_NUMBER_FORMAT = {
    "FormatConfiguration": {
        "NumberDisplayFormatConfiguration": {
            "NumberScale": "AUTO",
            "DecimalPlacesConfiguration": {"DecimalPlaces": 1},
        },
    },
}

# Fixed colors for the two recurring categorical dimensions, so a given tier /
# client is the SAME color on every sheet (otherwise QuickSight assigns palette
# slots per-visual by alphabetical category order, and e.g. "Power" can be one
# color on Activity and another on Economics). Colors are drawn from the theme
# DataColorPalette (cfn/02-quicksight.yaml). Values must match what the views
# emit: tier is normalized to Pro/Pro+/Power/Unknown (00_base_user_activity),
# client_type is upper()'d (KIRO_IDE/KIRO_CLI/PLUGIN). A value not in the map
# falls back to normal palette assignment, so new client types still render.
_TIER_COLORS = {
    "Pro":     "#0972D3",   # AWS blue
    "Pro+":    "#E7157B",   # magenta
    "Power":   "#9046FF",   # Kiro purple (the headline / highest tier)
    "Unknown": "#8C8C8C",   # grey - de-emphasized
}
_CLIENT_COLORS = {
    "KIRO_IDE": "#9046FF",  # Kiro purple (the dominant surface)
    "KIRO_CLI": "#FF8C00",  # warm orange
    "PLUGIN":   "#0972D3",  # AWS blue
}
# Map a color dimension's column name -> its fixed value/color dict, so helpers
# can auto-apply the right ColorMap when they color/stack by that column.
_FIXED_COLOR_COLUMNS = {
    "subscription_tier": _TIER_COLORS,
    "client_type":       _CLIENT_COLORS,
}


def _color_map(field_id: str, color_col: str) -> list[dict]:
    """VisualPalette.ColorMap entries pinning each known value of `color_col`
    to its fixed color. Returns [] for columns without a fixed mapping (the
    visual then uses normal theme-palette assignment)."""
    colors = _FIXED_COLOR_COLUMNS.get(color_col)
    if not colors:
        return []
    return [
        {"Element": {"FieldId": field_id, "FieldValue": value}, "Color": color}
        for value, color in colors.items()
    ]


def _kpi(visual_id: str, title: str, dataset: str, column: str, agg: str = "SUM"):
    return {
        "KPIVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "Values": [{
                        "NumericalMeasureField": {
                            "FieldId": f"{visual_id}-v",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": column},
                            "AggregationFunction": {"SimpleNumericalAggregation": agg},
                            "FormatConfiguration": _AUTO_NUMBER_FORMAT,
                        },
                    }],
                },
            },
        },
    }


def _kpi_percent(visual_id: str, title: str, dataset: str, column: str, agg: str = "AVERAGE"):
    """KPI that renders a 0-1 measure as a percentage. Used for seat
    utilization: AVERAGE(is_active) over the provisioned-user roster =
    fraction of users active in the trailing-30d window."""
    return {
        "KPIVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "Values": [{
                        "NumericalMeasureField": {
                            "FieldId": f"{visual_id}-v",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": column},
                            "AggregationFunction": {"SimpleNumericalAggregation": agg},
                            "FormatConfiguration": {
                                "FormatConfiguration": {
                                    "PercentageDisplayFormatConfiguration": {
                                        "DecimalPlacesConfiguration": {"DecimalPlaces": 0},
                                    },
                                },
                            },
                        },
                    }],
                },
            },
        },
    }


def _kpi_distinct_count(visual_id: str, title: str, dataset: str, column: str):
    return {
        "KPIVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "Values": [{
                        "CategoricalMeasureField": {
                            "FieldId": f"{visual_id}-v",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": column},
                            "AggregationFunction": "DISTINCT_COUNT",
                        },
                    }],
                },
            },
        },
    }


def _line(visual_id: str, title: str, dataset: str, date_col: str, value_col: str,
          color_col: str | None = None, agg: str = "SUM"):
    field_wells = {
        "Category": [{
            "DateDimensionField": {
                "FieldId": f"{visual_id}-d",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                "DateGranularity": "DAY",
            },
        }],
        "Values": [{
            "NumericalMeasureField": {
                "FieldId": f"{visual_id}-v",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                "AggregationFunction": {"SimpleNumericalAggregation": agg},
            },
        }],
    }
    chart_config = {"FieldWells": {"LineChartAggregatedFieldWells": field_wells}}
    if color_col:
        field_wells["Colors"] = [{
            "CategoricalDimensionField": {
                "FieldId": f"{visual_id}-c",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": color_col},
            },
        }]
        cmap = _color_map(f"{visual_id}-c", color_col)
        if cmap:
            chart_config["VisualPalette"] = {"ColorMap": cmap}
    return {
        "LineChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": chart_config,
        },
    }


def _line_multi(visual_id: str, title: str, dataset: str, date_col: str,
                value_cols: list[tuple[str, str]], agg: str = "SUM"):
    """Line chart with multiple value series (one line per (column, label)).
    Used for new-vs-returning where the two series are separate columns, not a
    pivot dimension. Each series is a NumericalMeasureField; QuickSight renders
    one line per value field and labels it with the column's display name."""
    values = [
        {
            "NumericalMeasureField": {
                "FieldId": f"{visual_id}-v{i}",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": col},
                "AggregationFunction": {"SimpleNumericalAggregation": agg},
            },
        }
        for i, (col, _label) in enumerate(value_cols)
    ]
    return {
        "LineChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {"LineChartAggregatedFieldWells": {
                    "Category": [{
                        "DateDimensionField": {
                            "FieldId": f"{visual_id}-d",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                            "DateGranularity": "DAY",
                        },
                    }],
                    "Values": values,
                }},
            },
        },
    }


def _bar(visual_id: str, title: str, dataset: str, category_col: str, value_col: str,
         items_limit: int | None = None):
    chart = {
        "FieldWells": {
            "BarChartAggregatedFieldWells": {
                "Category": [{
                    "CategoricalDimensionField": {
                        "FieldId": f"{visual_id}-c",
                        "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                    },
                }],
                "Values": [{
                    "NumericalMeasureField": {
                        "FieldId": f"{visual_id}-v",
                        "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                        "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                    },
                }],
            },
        },
        "Orientation": "HORIZONTAL",
        "SortConfiguration": {
            "CategorySort": [{
                "FieldSort": {"FieldId": f"{visual_id}-v", "Direction": "DESC"},
            }],
        },
    }
    if items_limit:
        chart["SortConfiguration"]["CategoryItemsLimit"] = {
            "ItemsLimit": items_limit,
        }
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": chart,
        },
    }


def _bar_calc(visual_id: str, title: str, dataset: str, category_col: str,
              calc_field: str, percent: bool = False):
    """Horizontal bar whose value is a pre-aggregated calculated field (e.g.
    credits_per_user computed as sum/distinctCount). Calculated aggregate
    fields take NO AggregationFunction - QS evaluates them at the category
    grouping level. `percent` renders a 0-1 calc field as XX.X%."""
    fmt = (
        {"FormatConfiguration": {"PercentageDisplayFormatConfiguration": {
            "DecimalPlacesConfiguration": {"DecimalPlaces": 1}}}}
        if percent else
        {"FormatConfiguration": _AUTO_NUMBER_FORMAT}
    )
    value_field = {
        "FieldId": f"{visual_id}-v",
        "Column": {"DataSetIdentifier": dataset, "ColumnName": calc_field},
    }
    value_field.update(fmt)
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                            },
                        }],
                        "Values": [{"NumericalMeasureField": value_field}],
                    },
                },
                "Orientation": "HORIZONTAL",
                "SortConfiguration": {
                    "CategorySort": [{
                        "FieldSort": {"FieldId": f"{visual_id}-v", "Direction": "DESC"},
                    }],
                },
            },
        },
    }


def _bar_grouped(visual_id: str, title: str, dataset: str,
                 category_col: str, value_col: str, group_col: str,
                 orientation: str = "VERTICAL"):
    """Clustered/grouped bar - category on one axis, group_col splits each
    category into side-by-side bars. Color (group) ordering follows
    alphabetical of group_col."""
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "BarsArrangement": "CLUSTERED",
                "Orientation": orientation,
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                            },
                        }],
                        "Values": [{
                            "NumericalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                            },
                        }],
                        "Colors": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-g",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": group_col},
                            },
                        }],
                    },
                },
                "DataLabels": {"Visibility": "VISIBLE"},
            },
        },
    }


def _period_compare_bar(visual_id: str, title: str):
    """Specialized clustered bar for period_comparison: prior 30d vs
    current 30d, by tier.

    Two QuickSight knobs make this a non-default visual:

    - SortConfiguration.ColorSort with Direction: DESC. QS rejects sorting
      the color dimension by any other column ('Color can only be sorted
      by itself'), so we sort the period values by themselves. P > C
      alphabetically; DESC puts 'Prior' before 'Current' in the cluster
      ordering, which renders left-to-right Prior → Current.
    - VisualPalette.ColorMap pins 'Current' to kiro purple and 'Prior' to
      warm orange explicitly. Without this, palette assignment is
      alphabetical and the 'headline' period gets the secondary color.
    """
    field_g = f"{visual_id}-g"
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "BarsArrangement": "CLUSTERED",
                "Orientation": "VERTICAL",
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": "period", "ColumnName": "subscription_tier"},
                            },
                        }],
                        "Values": [{
                            "NumericalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": "period", "ColumnName": "messages"},
                                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                            },
                        }],
                        "Colors": [{
                            "CategoricalDimensionField": {
                                "FieldId": field_g,
                                "Column": {"DataSetIdentifier": "period", "ColumnName": "period"},
                            },
                        }],
                    },
                },
                "SortConfiguration": {
                    "ColorSort": [{
                        "FieldSort": {"FieldId": field_g, "Direction": "DESC"},
                    }],
                },
                "DataLabels": {"Visibility": "VISIBLE"},
                "VisualPalette": {
                    "ColorMap": [
                        {"Element": {"FieldId": field_g, "FieldValue": "Current"}, "Color": "#9046FF"},
                        {"Element": {"FieldId": field_g, "FieldValue": "Prior"},   "Color": "#FF8C00"},
                    ],
                },
            },
        },
    }


def _bar_stacked(visual_id: str, title: str, dataset: str,
                 category_col: str, value_cols: list[tuple[str, str]]):
    """Vertical stacked bar with multiple measures stacked per category.
    `value_cols` is a list of (column_name, display_label) pairs."""
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "BarsArrangement": "STACKED",
                "Orientation": "VERTICAL",
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                            },
                        }],
                        "Values": [{
                            "NumericalMeasureField": {
                                "FieldId": f"{visual_id}-v{i}",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": col},
                                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                            },
                        } for i, (col, _label) in enumerate(value_cols)],
                    },
                },
                "DataLabels": {"Visibility": "VISIBLE"},
            },
        },
    }


def _pie(visual_id: str, title: str, dataset: str, category_col: str, value_col: str):
    return {
        "PieChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "PieChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                            },
                        }],
                        "Values": [{
                            "NumericalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                            },
                        }],
                    },
                },
                "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
            },
        },
    }


def _pie_count(visual_id: str, title: str, dataset: str, category_col: str, count_col: str):
    return {
        "PieChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "PieChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                            },
                        }],
                        "Values": [{
                            "CategoricalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": count_col},
                                "AggregationFunction": "DISTINCT_COUNT",
                            },
                        }],
                    },
                },
                # Default to value-DESC so larger wedges come first instead
                # of alphabetical category order.
                "SortConfiguration": {
                    "CategorySort": [{
                        "FieldSort": {"FieldId": f"{visual_id}-v", "Direction": "DESC"},
                    }],
                },
                "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
                # Pin fixed colors when the category is a known dimension
                # (subscription_tier); segment_calc and other categories fall
                # back to the theme palette (empty ColorMap is omitted).
                **({"VisualPalette": {"ColorMap": _color_map(f"{visual_id}-c", category_col)}}
                   if _color_map(f"{visual_id}-c", category_col) else {}),
            },
        },
    }


def _sub(visual: dict, subtitle: str) -> dict:
    """Attach a one-liner subtitle to any visual produced by the helpers below.
    Subtitle sits next to Title inside the inner visual dict (KPIVisual /
    BarChartVisual / etc.) and renders as a small caption under the title."""
    inner_key = next(iter(visual))
    visual[inner_key]["Subtitle"] = {
        "Visibility": "VISIBLE",
        "FormatText": {"PlainText": subtitle},
    }
    return visual


# --- Executive-page helpers ---------------------------------------------------

def _kpi_sparkline(visual_id: str, title: str, dataset: str, value_col: str,
                   date_col: str, agg: str = "SUM"):
    """KPI tile showing the windowed total with a sparkline trend
    (TrendGroups gives the sparkline its daily series). No period-over-period
    comparison: see the KPIOptions note below for why the comparison/date
    label is intentionally omitted.
    """
    return {
        "KPIVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "Values": [{
                        "NumericalMeasureField": {
                            "FieldId": f"{visual_id}-v",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                            "AggregationFunction": {"SimpleNumericalAggregation": agg},
                            "FormatConfiguration": _AUTO_NUMBER_FORMAT,
                        },
                    }],
                    "TrendGroups": [{
                        "DateDimensionField": {
                            "FieldId": f"{visual_id}-t",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                            "DateGranularity": "DAY",
                        },
                    }],
                },
                "KPIOptions": {
                    # KPI shows the windowed total + a sparkline trend only.
                    # A KPI with a date TrendGroup auto-renders a "secondary
                    # value" - the latest trend point's DATE (e.g. "Jun 2,
                    # 2026") next to a period-over-period difference. That read
                    # as "data ends June 2" and as a number that didn't match
                    # the windowed total. SecondaryValue: HIDDEN suppresses that
                    # date/difference while keeping the sparkline. (Omitting the
                    # Comparison/TrendArrows blocks alone does NOT remove it -
                    # the secondary value is on by default for a TrendGroup KPI.)
                    "Sparkline": {"Type": "LINE", "Visibility": "VISIBLE"},
                    "SecondaryValue": {"Visibility": "HIDDEN"},
                    "PrimaryValueDisplayType": "ACTUAL",
                },
            },
        },
    }


def _kpi_sparkline_calc(visual_id: str, title: str, dataset: str,
                        value_col: str, date_col: str):
    """KPI for an already-aggregated calculated field (e.g. sum(a)/sum(b)).
    QS rejects AggregationFunction on aggregated calc fields - we use a
    NumericalMeasureField with the field omitted."""
    return {
        "KPIVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "Values": [{
                        "NumericalMeasureField": {
                            "FieldId": f"{visual_id}-v",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                            "FormatConfiguration": _AUTO_NUMBER_FORMAT,
                        },
                    }],
                    "TrendGroups": [{
                        "DateDimensionField": {
                            "FieldId": f"{visual_id}-t",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                            "DateGranularity": "DAY",
                        },
                    }],
                },
                "KPIOptions": {
                    # KPI shows the windowed total + a sparkline trend only.
                    # A KPI with a date TrendGroup auto-renders a "secondary
                    # value" - the latest trend point's DATE (e.g. "Jun 2,
                    # 2026") next to a period-over-period difference. That read
                    # as "data ends June 2" and as a number that didn't match
                    # the windowed total. SecondaryValue: HIDDEN suppresses that
                    # date/difference while keeping the sparkline. (Omitting the
                    # Comparison/TrendArrows blocks alone does NOT remove it -
                    # the secondary value is on by default for a TrendGroup KPI.)
                    "Sparkline": {"Type": "LINE", "Visibility": "VISIBLE"},
                    "SecondaryValue": {"Visibility": "HIDDEN"},
                    "PrimaryValueDisplayType": "ACTUAL",
                },
            },
        },
    }


def _kpi_sparkline_distinct(visual_id: str, title: str, dataset: str,
                            value_col: str, date_col: str):
    """Same shape as _kpi_sparkline but for a DISTINCT_COUNT (e.g. active users)."""
    return {
        "KPIVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "Values": [{
                        "CategoricalMeasureField": {
                            "FieldId": f"{visual_id}-v",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                            "AggregationFunction": "DISTINCT_COUNT",
                            # CategoricalMeasureField FormatConfiguration uses a
                            # different shape than NumericalMeasureField - it
                            # nests under NumericFormatConfiguration directly.
                            "FormatConfiguration": {
                                "NumericFormatConfiguration": {
                                    "NumberDisplayFormatConfiguration": {
                                        "NumberScale": "AUTO",
                                        "DecimalPlacesConfiguration": {"DecimalPlaces": 1},
                                    },
                                },
                            },
                        },
                    }],
                    "TrendGroups": [{
                        "DateDimensionField": {
                            "FieldId": f"{visual_id}-t",
                            "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                            "DateGranularity": "DAY",
                        },
                    }],
                },
                "KPIOptions": {
                    # KPI shows the windowed total + a sparkline trend only.
                    # A KPI with a date TrendGroup auto-renders a "secondary
                    # value" - the latest trend point's DATE (e.g. "Jun 2,
                    # 2026") next to a period-over-period difference. That read
                    # as "data ends June 2" and as a number that didn't match
                    # the windowed total. SecondaryValue: HIDDEN suppresses that
                    # date/difference while keeping the sparkline. (Omitting the
                    # Comparison/TrendArrows blocks alone does NOT remove it -
                    # the secondary value is on by default for a TrendGroup KPI.)
                    "Sparkline": {"Type": "LINE", "Visibility": "VISIBLE"},
                    "SecondaryValue": {"Visibility": "HIDDEN"},
                    "PrimaryValueDisplayType": "ACTUAL",
                },
            },
        },
    }


def _area(visual_id: str, title: str, dataset: str, date_col: str, value_col: str,
          stack_col: str | None = None):
    field_wells = {
        "Category": [{
            "DateDimensionField": {
                "FieldId": f"{visual_id}-d",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                "DateGranularity": "DAY",
            },
        }],
        "Values": [{
            "NumericalMeasureField": {
                "FieldId": f"{visual_id}-v",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
            },
        }],
    }
    area_config = {
        "Type": "STACKED_AREA",
        "FieldWells": {"LineChartAggregatedFieldWells": field_wells},
    }
    if stack_col:
        field_wells["Colors"] = [{
            "CategoricalDimensionField": {
                "FieldId": f"{visual_id}-c",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": stack_col},
            },
        }]
        cmap = _color_map(f"{visual_id}-c", stack_col)
        if cmap:
            area_config["VisualPalette"] = {"ColorMap": cmap}
    return {
        "LineChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": area_config,
        },
    }


def _bar_time_stacked(visual_id: str, title: str, dataset: str, date_col: str,
                      value_col: str, stack_col: str | None = None):
    """Vertical stacked bar over a daily date axis, stacked by `stack_col`.
    Drop-in replacement for _area(): sparse data reads as discrete bars per
    day instead of a filled area that exaggerates a few scattered points."""
    field_wells = {
        "Category": [{
            "DateDimensionField": {
                "FieldId": f"{visual_id}-d",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": date_col},
                "DateGranularity": "DAY",
            },
        }],
        "Values": [{
            "NumericalMeasureField": {
                "FieldId": f"{visual_id}-v",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
            },
        }],
    }
    bar_config = {
        "BarsArrangement": "STACKED",
        "Orientation": "VERTICAL",
        "FieldWells": {"BarChartAggregatedFieldWells": field_wells},
    }
    if stack_col:
        field_wells["Colors"] = [{
            "CategoricalDimensionField": {
                "FieldId": f"{visual_id}-c",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": stack_col},
            },
        }]
        cmap = _color_map(f"{visual_id}-c", stack_col)
        if cmap:
            bar_config["VisualPalette"] = {"ColorMap": cmap}
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": bar_config,
        },
    }


def _donut(visual_id: str, title: str, dataset: str, category_col: str, count_col: str):
    return {
        "PieChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "PieChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": category_col},
                            },
                        }],
                        "Values": [{
                            "CategoricalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": count_col},
                                "AggregationFunction": "DISTINCT_COUNT",
                            },
                        }],
                    },
                },
                "DonutOptions": {
                    "ArcOptions": {"ArcThickness": "MEDIUM"},
                },
            },
        },
    }


def _heatmap(visual_id: str, title: str, dataset: str, rows_col: str, cols_col: str,
             value_col: str):
    return {
        "HeatMapVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "HeatMapAggregatedFieldWells": {
                        "Rows": [{
                            "DateDimensionField": {
                                "FieldId": f"{visual_id}-r",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": rows_col},
                                "DateGranularity": "DAY",
                            },
                        }],
                        "Columns": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": cols_col},
                            },
                        }],
                        "Values": [{
                            "NumericalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                            },
                        }],
                    },
                },
            },
        },
    }


def _table(visual_id: str, title: str, dataset: str,
           dimensions: list[str], values: list[tuple[str, str]] | None = None,
           sort_by: tuple[str, str] | None = None):
    """Table visual. `dimensions` are group-by columns. Each entry is either a
    plain string (a STRING column → CategoricalDimensionField) or a
    `(column_name, "date")` tuple (a DATETIME column → DateDimensionField).
    QuickSight rejects a DATETIME or numeric/boolean column placed in a
    CategoricalDimensionField, so date columns MUST use the tuple form and
    non-string non-date columns should go in `values` (or be cast in the view)
    rather than `dimensions`. `values` is a list of (column_name, agg) for
    numeric columns - agg is one of SUM / AVERAGE / COUNT / MIN / MAX.
    `sort_by` is an optional (column_name, direction) pair - direction is
    "ASC" or "DESC". The sort column may be unaggregated (we use MAX as the
    SortBy aggregation since QS requires one).

    NOTE: dimension order matters. FieldId is set to `{visual_id}-d{i}` where
    i is the index in `dimensions`. Visual-level Actions (e.g. click-through
    setting a parameter to a row's value) reference these FieldIds, so
    reordering the `dimensions` list silently breaks any Action that points
    at a positional FieldId. See the p-all-users click-through Action
    (set_parameters_operation) for an example."""
    values = values or []
    # Each value tuple is (col, agg) or (col, agg, fmt). fmt is one of:
    #   None / omitted - default number display
    #   "percent"      - field is a 0-1 fraction; render as XX.X% (QS
    #                    PercentageDisplayFormatConfiguration multiplies by
    #                    100 automatically)
    # agg=None marks a pre-aggregated calculated field: QS rejects an
    # AggregationFunction on aggregate calc fields, so we omit it.
    def _value_field(visual_id: str, i: int, val: tuple) -> dict:
        col, agg = val[0], val[1]
        fmt = val[2] if len(val) > 2 else None
        field = {
            "FieldId": f"{visual_id}-v{i}",
            "Column": {"DataSetIdentifier": dataset, "ColumnName": col},
        }
        if agg is not None:
            field["AggregationFunction"] = {"SimpleNumericalAggregation": agg}
        if fmt == "percent":
            field["FormatConfiguration"] = {
                "FormatConfiguration": {
                    "PercentageDisplayFormatConfiguration": {
                        "DecimalPlacesConfiguration": {"DecimalPlaces": 1},
                    },
                },
            }
        else:
            field["FormatConfiguration"] = _AUTO_NUMBER_FORMAT
        return {"NumericalMeasureField": field}

    def _group_field(i: int, dim) -> dict:
        # dim is either "col" (STRING) or ("col", "date") for a DATETIME column.
        if isinstance(dim, tuple):
            col, kind = dim
        else:
            col, kind = dim, "string"
        if kind == "date":
            return {
                "DateDimensionField": {
                    "FieldId": f"{visual_id}-d{i}",
                    "Column": {"DataSetIdentifier": dataset, "ColumnName": col},
                    "DateGranularity": "DAY",
                },
            }
        return {
            "CategoricalDimensionField": {
                "FieldId": f"{visual_id}-d{i}",
                "Column": {"DataSetIdentifier": dataset, "ColumnName": col},
            },
        }

    config = {
        "FieldWells": {
            "TableAggregatedFieldWells": {
                "GroupBy": [_group_field(i, dim) for i, dim in enumerate(dimensions)],
                "Values": [_value_field(visual_id, i, val) for i, val in enumerate(values)],
            },
        },
    }
    if sort_by:
        col, direction = sort_by
        config["SortConfiguration"] = {
            "RowSort": [{
                "ColumnSort": {
                    "SortBy": {"DataSetIdentifier": dataset, "ColumnName": col},
                    "Direction": direction,
                    "AggregationFunction": {"NumericalAggregationFunction": {"SimpleNumericalAggregation": "MAX"}},
                },
            }],
        }
    return {
        "TableVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": config,
        },
    }


def _funnel(visual_id: str, title: str, dataset: str, stage_col: str, value_col: str,
            sort_col: str = "sort_key"):
    """Native FunnelChartVisual. Stages are ordered by `sort_col` ascending
    so the visual reads top-to-bottom (e.g. New -> Active -> Power), not by
    value descending (which would scramble the funnel on healthy populations
    where Active > New > Power)."""
    return {
        "FunnelChartVisual": {
            "VisualId": visual_id,
            "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": title}},
            "ChartConfiguration": {
                "FieldWells": {
                    "FunnelChartAggregatedFieldWells": {
                        "Category": [{
                            "CategoricalDimensionField": {
                                "FieldId": f"{visual_id}-c",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": stage_col},
                            },
                        }],
                        "Values": [{
                            "NumericalMeasureField": {
                                "FieldId": f"{visual_id}-v",
                                "Column": {"DataSetIdentifier": dataset, "ColumnName": value_col},
                                "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                            },
                        }],
                    },
                },
                "SortConfiguration": {
                    "CategorySort": [{
                        "ColumnSort": {
                            "SortBy": {"DataSetIdentifier": dataset, "ColumnName": sort_col},
                            "Direction": "ASC",
                            "AggregationFunction": {"NumericalAggregationFunction": {"SimpleNumericalAggregation": "MIN"}},
                        },
                    }],
                },
                "DataLabelOptions": {"Visibility": "VISIBLE"},
            },
        },
    }


def _grid(elements: list[tuple[str, int, int, int, int]]):
    """Build a GridLayoutConfiguration. Each element is
    (visual_id, column_index, row_index, column_span, row_span). Grid is 36
    columns wide by QS convention.
    """
    return {
        "GridLayout": {
            "Elements": [
                {
                    "ElementId": vid,
                    "ElementType": "VISUAL",
                    "ColumnIndex": ci,
                    "ColumnSpan": cs,
                    "RowIndex": ri,
                    "RowSpan": rs,
                }
                for vid, ci, ri, cs, rs in elements
            ],
            "CanvasSizeOptions": {
                "ScreenCanvasSizeOptions": {"ResizeOption": "FIXED", "OptimizedViewPortWidth": "1600px"},
            },
        },
    }


def build_definition(account_id: str, region: str, resource_prefix: str) -> dict:
    decls = [
        {"Identifier": ident,
         "DataSetArn": dataset_arn(account_id, region, f"{resource_prefix}-{suffix}")}
        for ident, suffix in DATASET_SUFFIXES.items()
    ]

    # ----- Executive sheet (front page) -----------------------------------
    # 36-column grid. KPI tiles are 12-wide × 4-tall.
    exec_visuals = [
        # Active users MUST be a DISTINCT_COUNT of user_id over the window -
        # NOT a SUM of daily-distinct counts (which would total user-days,
        # e.g. 2164 instead of ~50). daily_trends is pre-aggregated so the
        # raw user_id is gone; read `base` (one row per user/day) and let
        # QS compute the true distinct.
        _sub(_kpi_sparkline_distinct("xv-users", "Active users", "base", "user_id", "activity_date"),
             "Distinct users with at least one active day in the selected window."),
        _sub(_kpi_sparkline("xv-messages", "Messages",     "trends", "total_messages","activity_date"),
             "Total user->Kiro messages (volume of usage, not cost)."),
        _sub(_kpi_sparkline("xv-credits",  "Credits used", "trends", "credits_used",  "activity_date"),
             "Total credits consumed (the billing unit)."),
        # Seat utilization: fraction of provisioned users active in the
        # trailing-30d window. AVERAGE(is_active) over engagement_segmentation
        # (one row per provisioned user). The #1 question for a paid-tool
        # admin: how much of what we pay for is actually being used. Fixed
        # trailing-30d window - not driven by the date picker.
        _sub(_kpi_percent("xv-utilization", "Seat utilization (30d)", "engagement", "is_active"),
             "Share of provisioned users active at least once in the trailing 30 days. Trailing-30d window - not affected by the date-range picker."),
        # Provisioned seats = the raw denominator behind Seat utilization:
        # DISTINCT_COUNT(user_id) over engagement_segmentation (one row per
        # ever-seen / provisioned user). Pairs with the utilization % so an
        # admin sees both "how many seats" and "what fraction are active".
        # Same dataset as utilization, so likewise NOT date-filtered.
        _sub(_kpi_distinct_count("xv-seats", "Provisioned seats", "engagement", "user_id"),
             "Total provisioned users (the denominator behind Seat utilization). All-time roster - not affected by the date-range picker."),
    ]
    # Five KPI tiles across the 36-col grid (width 7 each: 0/7/14/21/28).
    # KPI tiles are height 6 (not 4): the sparkline only renders when the tile
    # has enough vertical room for title + value + the trend line. At height 4
    # QuickSight drops the sparkline.
    exec_grid = [
        ("xv-users",          0, 0,  7, 6),
        ("xv-messages",       7, 0,  7, 6),
        ("xv-credits",       14, 0,  7, 6),
        ("xv-utilization",   21, 0,  7, 6),
        ("xv-seats",         28, 0,  8, 6),
    ]
    next_row = 6

    # Executive is a 30-second status page. KPI tiles cover the headline
    # "active users / messages / credits"; one trend line for the
    # finance-call signal (daily overage credits) and one tier-level
    # comparison (current vs prior 30d). The day-by-day detail charts
    # ("daily messages by client", etc.) live on the Activity sheet so
    # Executive stays a single-screen summary.
    exec_visuals += [
        _sub(_line("xv-overage", "Daily overage credits", "trends", "activity_date", "overage_credits_used"),
             "Total overage credits consumed per day. Sustained increases suggest customers approaching or exceeding plan capacity."),
        _sub(_period_compare_bar("xv-period",
                                 "Messages by tier - prior 30d vs current 30d"),
             "Each tier shows two bars: the prior 30-day window on the left, the current 30-day window on the right. Anchored on the latest export date so the comparison stays meaningful as new data lands."),
    ]
    exec_grid += [
        ("xv-overage",  0, next_row,      36, 8),
        ("xv-period",   0, next_row + 8,  36, 8),
    ]

    sheets = [
        {
            "SheetId": "executive",
            "Name": "Executive",
            "ContentType": "INTERACTIVE",
            "Visuals": exec_visuals,
            "Layouts": [{"Configuration": _grid(exec_grid)}],
        },
        # ----- Activity & Trends ------------------------------------------
        {
            "SheetId": "activity",
            "Name": "Activity & Trends",
            "ContentType": "INTERACTIVE",
            "Visuals": [
                # Top row: two compact line charts for the volume signals.
                # These three read tier_breakdown (which carries
                # subscription_tier) so the Tier picker actually filters them.
                # The `users` column there is per-(date, tier, client) - SUMming
                # across clients gives daily distinct-by-tier; SUMming across
                # tiers loses the distinct-user property but matches the
                # by-client view we want here.
                # Reads daily_trends (grouped date+client_type), NOT
                # tier_breakdown: tier_breakdown is grouped date+tier+client, so
                # summing its per-tier distinct-user counts across the tier
                # dimension double-counts users active in >1 tier and inflates
                # DAU. daily_trends.active_users is COUNT(DISTINCT user_id) per
                # (date, client) - the correct per-client daily active count.
                _sub(_line("a-active",      "Daily active users (by client)", "trends", "activity_date", "active_users", color_col="client_type"),
                     "Daily active users (distinct), by client - see if engagement is concentrated in IDE, CLI, or Plugin."),
                _sub(_line("a-messages",    "Daily messages (by client)",     "tiers", "activity_date", "messages",      color_col="client_type"),
                     "Daily message volume by client. The mix shifts as users change which surfaces they prefer."),
                # Single large stacked area for the headline cost story.
                _sub(_bar_time_stacked("a-credits",     "Daily credits used (by client)", "tiers", "activity_date", "credits_used", stack_col="client_type"),
                     "Daily credit consumption stacked by client - the cost story. Which surface drives most spend?"),
                # Daily active users stacked by tier - replaces the earlier
                # tier × day heatmap which read poorly with only 3 tiers.
                _sub(_bar_time_stacked("a-tier-trend", "Daily active users by tier", "heatmap", "activity_date", "active_users", stack_col="subscription_tier"),
                     "Daily distinct users by tier (each counted once per day). POWER share rising = good adoption."),
                # New vs returning active users per day - the adoption/onboarding
                # signal. new_users / returning_users are separate columns on
                # daily_trends, so this is a two-series line (not a pivot-stacked
                # area). Aggregated across clients by the date grouping.
                _sub(_line_multi("a-new-returning", "Daily new vs returning users", "trends",
                                 "activity_date",
                                 [("new_users", "New"), ("returning_users", "Returning")]),
                     "Daily new (first-ever-seen) vs returning active users. A healthy adoption curve shows returning users growing while new users stay steady or rise."),
                # One stacked bar for model usage - replaces the bar+line pair.
                _sub(_bar_time_stacked("a-models",      "Daily messages by model",        "models", "activity_date", "messages", stack_col="model_name"),
                     "Daily messages stacked by model. New models appear automatically; the relative bar height shows adoption. Responds to both the date range and the Tier picker."),
            ],
            # Reading flow: who's using -> what they're sending -> what it costs.
            "Layouts": [{"Configuration": _grid([
                # Row 1: WHO - active users by client + by tier, side by side.
                ("a-active",      0,  0, 18, 6),
                ("a-tier-trend", 18,  0, 18, 6),
                # Row 2: WHO (cont.) - new vs returning adoption trend.
                ("a-new-returning", 0,  6, 36, 6),
                # Row 3: WHAT - messages, two views.
                ("a-messages",    0, 12, 36, 7),
                ("a-models",      0, 19, 36, 7),
                # Row 4: COST - credits.
                ("a-credits",     0, 26, 36, 8),
            ])}],
        },

        # ----- People ----------------------------------------------------
        # Cohort-level analytics: top-N tables, segmentation, funnel, cohort
        # retention, week-over-week movers. Per-user drilling lives on the
        # separate User-detail sheet via the DrillUser parameter.
        {
            "SheetId": "people",
            "Name": "People",
            "ContentType": "INTERACTIVE",
            "Visuals": [
                # The all-users table is the workhorse and what admins come
                # to People for, so it leads the sheet. It's sortable by any
                # column, which makes separate "top 10 by X" bars redundant -
                # those were removed.
                # Group by user_label + user_tier (NOT subscription_tier):
                # subscription_tier is per-row in base, so a user who changed
                # tier mid-window (e.g. Pro -> Pro+) would split into two rows.
                # user_tier is a per-user CONSTANT (MAX over the window in the
                # base view, so 'PRO_PLUS' > 'PRO' -> highest/most-recent tier
                # wins) - grouping on it adds no extra rows, so each user is one
                # row with usage metrics summed across whatever tier(s) they
                # held in the selected range. A categorical MAX in the visual
                # layer isn't possible (CategoricalMeasureField only allows
                # DISTINCT_COUNT/COUNT), hence the data-layer constant column.
                _sub(_table("p-all-users", "All users",
                           "base",
                           dimensions=["user_label", "user_tier"],
                           values=[
                               ("total_messages",       "SUM"),
                               ("chat_conversations",   "SUM"),
                               ("credits_used",         "SUM"),
                               ("overage_credits_used", "SUM"),
                               ("active_days_calc",     None),
                           ],
                           sort_by=("credits_used", "DESC")),
                     "Every user (one row per user; usage summed across the period even if their tier changed), sortable by any column. Scoped to the date range selected above. Click any row to open that user on the User detail sheet."),
                _sub(_pie_count("p-segments", "Engagement segments (selected range)", "base", "segment_calc", "user_id"),
                     "Active users in the selected date range split by intensity: Power (≥20 active days or ≥1000 messages) / Active (≥8 days) / Light (≥1 day). Recomputes as you change the date range above - pick a month to segment that period's users."),
                # Engagement funnel rendered as three KPI tiles over the
                # selected range (the native funnel chart can't be driven by
                # the date picker - see the funnel_geN_user calc fields). Titles
                # are just the threshold; the engagement-funnel framing lives in
                # the captions to avoid repeating "Funnel:" on every tile.
                _sub(_kpi_distinct_count("p-funnel-1", "≥1 active day", "base", "funnel_ge1_user"),
                     "Engagement funnel: users active ≥1 day in the selected range. Cumulative - includes everyone in the ≥8 and ≥20 tiles. Recomputes with the date range above."),
                _sub(_kpi_distinct_count("p-funnel-8", "≥8 active days", "base", "funnel_ge8_user"),
                     "Engagement funnel: users active ≥8 days in the selected range."),
                _sub(_kpi_distinct_count("p-funnel-20", "≥20 active days", "base", "funnel_ge20_user"),
                     "Engagement funnel: users active ≥20 days in the selected range."),
                _sub(_bar("p-by-model", "Top users (by model)", "models", "user_label", "messages", items_limit=15),
                     "Per-user message count across all models, scoped to the selected date range and Tier picker."),
                _sub({
                    "LineChartVisual": {
                        "VisualId": "p-cohort",
                        "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": "Cohort retention (all-time · 12 most recent cohorts)"}},
                        "ChartConfiguration": {
                            "FieldWells": {
                                "LineChartAggregatedFieldWells": {
                                    "Category": [{
                                        "NumericalDimensionField": {
                                            "FieldId": "p-cohort-d",
                                            "Column": {"DataSetIdentifier": "cohort", "ColumnName": "months_since"},
                                        },
                                    }],
                                    "Values": [{
                                        "NumericalMeasureField": {
                                            "FieldId": "p-cohort-v",
                                            "Column": {"DataSetIdentifier": "cohort", "ColumnName": "retention_rate"},
                                            "AggregationFunction": {"SimpleNumericalAggregation": "AVERAGE"},
                                            # retention_rate is a 0-1 fraction; render as XX.X% on the y-axis.
                                            "FormatConfiguration": {
                                                "FormatConfiguration": {
                                                    "PercentageDisplayFormatConfiguration": {
                                                        "DecimalPlacesConfiguration": {"DecimalPlaces": 1},
                                                    },
                                                },
                                            },
                                        },
                                    }],
                                    "Colors": [{
                                        "DateDimensionField": {
                                            "FieldId": "p-cohort-c",
                                            "Column": {"DataSetIdentifier": "cohort", "ColumnName": "cohort_month"},
                                            "DateGranularity": "MONTH",
                                        },
                                    }],
                                },
                            },
                            # Limit color series to the 12 most-recent cohort
                            # months. QS picks based on color order, which for
                            # a DateDimensionField is chronological; combined
                            # with the limit this yields the most-recent 12.
                            "SortConfiguration": {
                                "ColorItemsLimitConfiguration": {"ItemsLimit": 12},
                            },
                        },
                    },
                }, "Retention curve per monthly cohort (12 most recent shown). X = months since first active. Y = % of cohort still active. Cohorts span the full export window - not affected by the date-range picker. Tier is sticky to user_dim; users keep their cohort line even if they upgrade tier."),
                # Single movers table with at_risk flag column - replaces the
                # earlier separate movers + at-risk tables.
                _sub(_table("p-movers", "Week-over-week movers (fixed 7d vs 7d)",
                           "movers",
                           dimensions=["user_label", "subscription_tier", "at_risk"],
                           values=[
                               ("prior_messages",  "SUM"),
                               ("recent_messages", "SUM"),
                               ("message_delta",   "SUM"),
                               ("pct_change",      "AVERAGE", "percent"),
                           ],
                           # Sort by abs_delta (biggest movers in either
                           # direction) without displaying it - it's a sort
                           # key, not a number the admin needs to read.
                           sort_by=("abs_delta", "DESC")),
                     "Users with the largest absolute change in messages (last 7d vs prior 7d). at_risk = Yes when drop > 50%. Window is fixed at trailing 7d vs prior 7d - not affected by the date-range picker. Tier picker filters which users are listed; the 7d/7d window is computed before the filter applies."),
            ],
            "Layouts": [{"Configuration": _grid([
                # All-users table leads (what admins come here for).
                ("p-all-users",    0,  0, 36, 12),
                # Engagement: segment mix (donut) on the left; the conversion
                # funnel as three stacked KPI tiles on the right half.
                ("p-segments",     0, 12, 18, 10),
                ("p-funnel-1",    18, 12, 18,  4),
                ("p-funnel-8",    18, 16, 18,  3),
                ("p-funnel-20",   18, 19, 18,  3),
                # Per-model top users, then cohort retention, then movers.
                ("p-by-model",     0, 22, 36, 10),
                ("p-cohort",       0, 32, 36, 10),
                ("p-movers",       0, 42, 36, 12),
            ])}],
        },

        # ----- Economics -------------------------------------------------
        # All Economics visuals read `base` so the date-range picker scopes
        # them to a window. Ratios (credits/user, credits/message, overage %)
        # are analysis-level calculated fields evaluated per tier over the
        # filtered rows - see calculated_fields below.
        {
            "SheetId": "economics",
            "Name": "Economics",
            "ContentType": "INTERACTIVE",
            "Visuals": [
                _sub(_pie_count("e-users-by-tier", "Users by tier", "base", "subscription_tier", "user_id"),
                     "Distinct users active in each tier (Pro / Pro+ / Power) within the selected window."),
                # Stacked: in-plan (calc, no agg) + overage (summed) per tier.
                _sub({
                    "BarChartVisual": {
                        "VisualId": "e-credits-by-tier",
                        "Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": "Credit usage by tier (in-plan + overage)"}},
                        "ChartConfiguration": {
                            "BarsArrangement": "STACKED",
                            "Orientation": "VERTICAL",
                            "FieldWells": {
                                "BarChartAggregatedFieldWells": {
                                    "Category": [{
                                        "CategoricalDimensionField": {
                                            "FieldId": "e-credits-by-tier-c",
                                            "Column": {"DataSetIdentifier": "base", "ColumnName": "subscription_tier"},
                                        },
                                    }],
                                    "Values": [
                                        {"NumericalMeasureField": {
                                            "FieldId": "e-credits-by-tier-base",
                                            "Column": {"DataSetIdentifier": "base", "ColumnName": "base_credits_calc"},
                                            "FormatConfiguration": _AUTO_NUMBER_FORMAT,
                                        }},
                                        {"NumericalMeasureField": {
                                            "FieldId": "e-credits-by-tier-over",
                                            "Column": {"DataSetIdentifier": "base", "ColumnName": "overage_credits_used"},
                                            "AggregationFunction": {"SimpleNumericalAggregation": "SUM"},
                                            "FormatConfiguration": _AUTO_NUMBER_FORMAT,
                                        }},
                                    ],
                                },
                            },
                            "DataLabels": {"Visibility": "VISIBLE"},
                        },
                    },
                }, "Credits per tier in the selected window, split into in-plan vs overage. A tall overage section = candidates for a tier upgrade."),
                _sub(_bar_calc("e-credits-per-user",    "Credits per user (by tier)",    "base", "subscription_tier", "credits_per_user_calc"),
                     "Credits consumed per active user in the window, by tier. Shows whether each tier's users are using their plan fully."),
                _sub(_bar_calc("e-credits-per-message", "Credits per message (by tier)", "base", "subscription_tier", "credits_per_message_calc"),
                     "Credits per message - proxy for cost-per-interaction. Higher = users picking heavier models or longer chats."),
                _sub(_table("e-unit-econ-table", "Unit economics by tier",
                       "base",
                       dimensions=["subscription_tier"],
                       values=[
                           ("active_users_calc",        None),
                           ("total_messages",           "SUM"),
                           ("credits_per_user_calc",    None),
                           ("credits_per_message_calc", None),
                           ("overage_pct_calc",         None, "percent"),
                       ]),
                     "Tier-level economics for the selected window: active users, messages, credits/user, credits/message, and overage % of total credits."),
            ],
            "Layouts": [{"Configuration": _grid([
                ("e-users-by-tier",        0,  0, 12, 10),
                ("e-credits-by-tier",     12,  0, 24, 10),
                ("e-credits-per-user",     0, 10, 18,  8),
                ("e-credits-per-message", 18, 10, 18,  8),
                ("e-unit-econ-table",      0, 18, 36,  8),
            ])}],
        },

        # ----- User detail (drill) ---------------------------------------
        # Pick one user from the dropdown above; every visual on this sheet
        # filters to just that user via the DrillUser parameter.
        {
            "SheetId": "user-detail",
            "Name": "User detail",
            "ContentType": "INTERACTIVE",
            "Visuals": [
                # Lifetime profile strip (reads `users`/user_totals, drill-
                # filtered to the selected user via fg-drill-users). This is
                # the ONLY visual on the sheet that reads `users`; it gives the
                # lifetime/identity context the windowed KPIs below lack (tier,
                # first/last active, overage). NOT date-filtered by design - it
                # is whole-history context, so it stays populated even when the
                # selected window has no activity for the user. The dimensions
                # (tier / first-active / last-active / overage-enabled) are the
                # descriptive fields; the values are lifetime totals (MAX over
                # the single user_totals row).
                _sub(_table("u-profile", "User profile (lifetime)",
                           "users",
                           dimensions=["subscription_tier",
                                       ("first_active_date", "date"),
                                       ("last_active_date", "date")],
                           values=[
                               ("active_days",          "MAX"),
                               ("total_messages",       "MAX"),
                               ("credits_used",         "MAX"),
                               ("overage_credits_used", "MAX"),
                           ]),
                     "Lifetime context for the selected user (all-time, not affected by the date picker): plan tier, first/last active date, and lifetime active days / messages / credits (incl. overage credits, which are non-zero only if the user has exceeded plan capacity). The KPIs and charts below are scoped to the selected date window."),
                # KPIs read `base` (per-day fact table) so the date picker
                # filters them. The sparkline series uses activity_date.
                _sub(_kpi_sparkline("u-msgs",   "Messages",      "base", "total_messages", "activity_date"),
                     "Messages this user sent in the selected date window. Sparkline shows day-by-day."),
                _sub(_kpi_sparkline("u-credits","Credits",       "base", "credits_used",   "activity_date"),
                     "Credit consumption for this user in the selected window. Sparkline shows daily credits."),
                # Active days = distinct_count(activity_date) via the
                # active_days_calc calculated field. base has one row per
                # (user, day, client_type), so a plain COUNT would double
                # count a user active on two clients the same day; the
                # distinct-count calc field avoids that.
                _sub(_kpi_sparkline_calc("u-days", "Active days", "base", "active_days_calc", "activity_date"),
                     "Distinct days this user was active in the selected window."),
                _sub(_line("u-daily",   "Daily messages",          "base",  "activity_date", "total_messages"),
                     "Per-day message volume for the selected user."),
                _sub(_line("u-credits-line", "Daily credits used", "base",  "activity_date", "credits_used"),
                     "Per-day credit consumption for the selected user."),
                _sub(_pie("u-models", "Model split",               "models", "model_name", "messages"),
                     "How this user's messages are distributed across models."),
            ],
            "Layouts": [{"Configuration": _grid([
                # Lifetime profile strip leads (whole-history context), then
                # the windowed KPIs, daily trends, and model split below it.
                ("u-profile",     0,  0, 36, 4),
                ("u-msgs",        0,  4, 12, 5),
                ("u-credits",    12,  4, 12, 5),
                ("u-days",       24,  4, 12, 5),
                ("u-daily",       0,  9, 36, 8),
                ("u-credits-line",0, 17, 36, 8),
                ("u-models",      0, 25, 36, 9),
            ])}],
        },
    ]

    # Dashboard-level parameters. SelectedUser was previously a multi-select
    # picker on the People sheet, but the User detail sheet covers the
    # single-user drill case more cleanly via DrillUser; the picker on People
    # only collapsed top-N visuals into top-1 displays which were redundant.
    parameter_declarations = [
        # Date range default = "last 30 days", so the picker-driven headline
        # KPIs align out of the box with the fixed-30d visuals (Seat
        # utilization, prior-vs-current 30d). Change -30 to e.g. -90 for a
        # wider default window; users can always widen via the picker.
        {
            "DateTimeParameterDeclaration": {
                "Name": "DateRangeStart",
                "DefaultValues": {
                    "RollingDate": {"Expression": "addDateTime(-30, 'DD', truncDate('DD', now()))"},
                },
            },
        },
        {
            "DateTimeParameterDeclaration": {
                "Name": "DateRangeEnd",
                "DefaultValues": {
                    "RollingDate": {"Expression": "now()"},
                },
            },
        },
        # Model picker on People. MULTI_VALUED so the dropdown's "Select All"
        # option = every model. Default populates from the live model_usage
        # dataset so all models are selected on first load.
        {
            "StringParameterDeclaration": {
                "ParameterValueType": "MULTI_VALUED",
                "Name": "SelectedModel",
                "DefaultValues": {
                    "DynamicValue": {
                        "DefaultValueColumn": {
                            "DataSetIdentifier": "models",
                            "ColumnName": "model_name",
                        },
                        "UserNameColumn": {
                            "DataSetIdentifier": "models",
                            "ColumnName": "model_name",
                        },
                    },
                },
            },
        },
        # Single-user drill picker on the User detail sheet. SINGLE_VALUED
        # so the visuals can confidently render a single user's history.
        # Defaults to the lexically-first user_label so the page isn't blank.
        {
            "StringParameterDeclaration": {
                "ParameterValueType": "SINGLE_VALUED",
                "Name": "DrillUser",
                "DefaultValues": {
                    "DynamicValue": {
                        "DefaultValueColumn": {
                            "DataSetIdentifier": "users",
                            "ColumnName": "user_label",
                        },
                        "UserNameColumn": {
                            "DataSetIdentifier": "users",
                            "ColumnName": "user_label",
                        },
                    },
                },
            },
        },
        # Tier picker - drives Activity, People, and Economics. Default
        # populates from the live tiers dataset so all tiers are selected.
        {
            "StringParameterDeclaration": {
                "ParameterValueType": "MULTI_VALUED",
                "Name": "SelectedTier",
                "DefaultValues": {
                    "DynamicValue": {
                        "DefaultValueColumn": {
                            "DataSetIdentifier": "tiers",
                            "ColumnName": "subscription_tier",
                        },
                        "UserNameColumn": {
                            "DataSetIdentifier": "tiers",
                            "ColumnName": "subscription_tier",
                        },
                    },
                },
            },
        },
    ]

    # Tab order shown in the dashboard = order of this list. The sheet blocks
    # above are authored in a convenient order; reorder them here into the
    # intended narrative without moving the (large) literals around:
    #   Executive -> Activity & Trends -> Economics -> People -> User detail
    # This keeps the two user-centric sheets (People and the User detail it
    # drills into) adjacent, with the aggregate/trend sheets (Activity,
    # Economics) grouped before them.
    _SHEET_ORDER = ["executive", "activity", "economics", "people", "user-detail"]
    sheets.sort(key=lambda s: _SHEET_ORDER.index(s["SheetId"]))

    people_sheet = next(s for s in sheets if s["SheetId"] == "people")
    # The People sheet's per-sheet controls (Model + Tier) are added later
    # alongside the other sheets' picker controls so they all share helper
    # builders (_model_control, _tier_control).
    people_sheet["ParameterControls"] = []

    # Date-range controls - duplicated on Executive and Activity, each with
    # a unique ParameterControlId. They drive the same DateRange parameters
    # so the picker on either sheet filters both.
    def _date_controls(suffix: str) -> list[dict]:
        return [
            {"DateTimePicker": {
                "ParameterControlId": f"date-start-{suffix}",
                "Title": "From",
                "SourceParameterName": "DateRangeStart",
                "DisplayOptions": {
                    "DateTimeFormat": "YYYY-MM-DD",
                    "TitleOptions": {"Visibility": "VISIBLE"},
                },
            }},
            {"DateTimePicker": {
                "ParameterControlId": f"date-end-{suffix}",
                "Title": "To",
                "SourceParameterName": "DateRangeEnd",
                "DisplayOptions": {
                    "DateTimeFormat": "YYYY-MM-DD",
                    "TitleOptions": {"Visibility": "VISIBLE"},
                },
            }},
        ]

    def _tier_control(suffix: str) -> dict:
        return {
            "Dropdown": {
                "ParameterControlId": f"tier-picker-{suffix}",
                "Title": "Tier",
                "SourceParameterName": "SelectedTier",
                "DisplayOptions": {"SelectAllOptions": {"Visibility": "VISIBLE"}},
                "Type": "MULTI_SELECT",
                "SelectableValues": {
                    "LinkToDataSetColumn": {
                        "DataSetIdentifier": "tiers",
                        "ColumnName": "subscription_tier",
                    },
                },
            },
        }

    def _model_control(suffix: str) -> dict:
        return {
            "Dropdown": {
                "ParameterControlId": f"model-picker-{suffix}",
                "Title": "Model",
                "SourceParameterName": "SelectedModel",
                "DisplayOptions": {"SelectAllOptions": {"Visibility": "VISIBLE"}},
                "Type": "MULTI_SELECT",
                "SelectableValues": {
                    "LinkToDataSetColumn": {
                        "DataSetIdentifier": "models",
                        "ColumnName": "model_name",
                    },
                },
            },
        }

    exec_sheet = next(s for s in sheets if s["SheetId"] == "executive")
    exec_sheet["ParameterControls"] = _date_controls("exec")
    activity_sheet = next(s for s in sheets if s["SheetId"] == "activity")
    activity_sheet["ParameterControls"] = _date_controls("activity") + [_tier_control("activity"), _model_control("activity")]
    # People sheet gets the date-range picker (so per-user metrics can be
    # scoped to a month/period - usage resets monthly, so lifetime sums blur
    # periods together) plus the Tier picker. The date+tier controls drive the
    # base-backed visuals (All Users table, segments donut, the three funnel
    # tiles) and the models-backed Top-users-by-model bar - see
    # people_base_visuals and the fg-date/tier-people-base groups below. Only
    # two People visuals are intentionally NOT date-filtered: cohort retention
    # (spans all history by definition) and week-over-week movers (fixed
    # trailing 7d-vs-7d) - both are labeled as fixed-window. The Model picker is
    # intentionally omitted - it was misleading on People (only p-by-model reads
    # `models`, so it appeared global but affected one visual); slice by model
    # on Activity (a-models) or User-detail (u-models) instead.
    people_sheet["ParameterControls"].extend(
        _date_controls("people") + [_tier_control("people")]
    )
    economics_sheet = next(s for s in sheets if s["SheetId"] == "economics")
    economics_sheet["ParameterControls"] = _date_controls("economics") + [_tier_control("economics")]

    # Click-through Action on the All users table: clicking a row sets
    # DrillUser to that row's user_label and navigates to the User-detail
    # sheet. Saves the customer from manually copy-pasting a user_label out
    # of People and into User-detail's dropdown.
    p_all_users = next(
        v for v in people_sheet["Visuals"]
        if v.get("TableVisual", {}).get("VisualId") == "p-all-users"
    )
    p_all_users["TableVisual"]["Actions"] = [{
        "CustomActionId": "p-all-users-drill",
        "Name": "Open in User detail",
        "Status": "ENABLED",
        "Trigger": "DATA_POINT_CLICK",
        "ActionOperations": [
            # QuickSight requires NavigationOperation to come before
            # SetParametersOperation in the action chain.
            {
                "NavigationOperation": {
                    "LocalNavigationConfiguration": {"TargetSheetId": "user-detail"},
                },
            },
            {
                "SetParametersOperation": {
                    "ParameterValueConfigurations": [{
                        "DestinationParameterName": "DrillUser",
                        "Value": {
                            "SourceField": "p-all-users-d0",  # user_label dimension
                        },
                    }],
                },
            },
        ],
    }]

    user_detail_sheet = next(s for s in sheets if s["SheetId"] == "user-detail")
    # User-detail KPIs read `base` so the date picker actually filters them
    # (Messages / Credits / Active days in the selected window). Prior
    # iteration kept lifetime KPIs from `users` and dropped the date picker
    # to avoid a UX trap; the `base` formulation is the cleaner finish.
    user_detail_sheet["ParameterControls"] = [
        {
            "Dropdown": {
                "ParameterControlId": "drill-user-picker",
                "Title": "User",
                "SourceParameterName": "DrillUser",
                "Type": "SINGLE_SELECT",
                "SelectableValues": {
                    "LinkToDataSetColumn": {
                        "DataSetIdentifier": "users",
                        "ColumnName": "user_label",
                    },
                },
            },
        },
        _model_control("user-detail"),
    ] + _date_controls("user-detail")

    # FilterGroups: each entry below filters its target visuals by a
    # parameter (SelectedTier, SelectedModel, DateRangeStart/End, DrillUser).
    filter_groups = []

    # Drill filter on the User-detail sheet. Filters the three datasets
    # that page reads from to the single user picked in the dropdown.
    for dataset_id in ("users", "base", "models"):
        filter_groups.append({
            "FilterGroupId": f"fg-drill-{dataset_id}",
            "Filters": [{
                "CategoryFilter": {
                    "FilterId": f"f-drill-{dataset_id}",
                    "Column": {"DataSetIdentifier": dataset_id, "ColumnName": "user_label"},
                    "Configuration": {
                        "CustomFilterConfiguration": {
                            "MatchOperator": "EQUALS",
                            "NullOption": "NON_NULLS_ONLY",
                            "ParameterName": "DrillUser",
                        },
                    },
                },
            }],
            "ScopeConfiguration": {
                "SelectedSheets": {
                    "SheetVisualScopingConfigurations": [{
                        "SheetId": "user-detail",
                        "Scope": "ALL_VISUALS",
                    }],
                },
            },
            "CrossDataset": "SINGLE_DATASET",
            "Status": "ENABLED",
        })

    # Tier filter - datasets that carry subscription_tier, scoped to the
    # sheets where those datasets are visualized. Executive doesn't get a
    # tier filter because its KPIs come from `trends` (no tier column) and
    # the sheet is meant as a 30-second status summary.
    tier_filtered_datasets = [
        # base now backs the Economics sheet (all 5 visuals) in addition to
        # being read elsewhere; scope its tier filter to economics.
        ("base",       ["economics"]),
        ("heatmap",    ["activity"]),
        ("users",      ["people"]),
        ("movers",     ["people"]),
        # cohort carries subscription_tier and lives on the People sheet. The
        # segments donut and funnel tiles moved to `base` (date-range driven),
        # so their tier filtering is handled by fg-tier-people-base instead -
        # `engagement` and `funnel` are no longer tier-filtered here (the
        # engagement dataset is still used by the Executive seat-utilization
        # KPI, which is not tier-filtered by design).
        ("cohort",     ["people"]),
        # tiers (tier_breakdown view) is read on Activity for the daily
        # by-client lines/area.
        ("tiers",      ["activity"]),
        # models (model_usage) now carries subscription_tier, so the Tier
        # picker filters the model visuals too (a-models on Activity,
        # p-by-model on People, u-models on User detail). CrossDataset is
        # SINGLE_DATASET so this only touches models-backed visuals on those
        # sheets, not their other visuals.
        ("models",     ["activity", "people", "user-detail"]),
    ]
    for dataset_id, sheet_ids in tier_filtered_datasets:
        filter_groups.append({
            "FilterGroupId": f"fg-tier-{dataset_id}",
            "Filters": [{
                "CategoryFilter": {
                    "FilterId": f"f-tier-{dataset_id}",
                    "Column": {"DataSetIdentifier": dataset_id, "ColumnName": "subscription_tier"},
                    "Configuration": {
                        "CustomFilterConfiguration": {
                            "MatchOperator": "EQUALS",
                            "NullOption": "NON_NULLS_ONLY",
                            "ParameterName": "SelectedTier",
                        },
                    },
                },
            }],
            "ScopeConfiguration": {
                "SelectedSheets": {
                    "SheetVisualScopingConfigurations": [
                        {"SheetId": s, "Scope": "ALL_VISUALS"} for s in sheet_ids
                    ],
                },
            },
            "CrossDataset": "SINGLE_DATASET",
            "Status": "ENABLED",
        })

    # Date-range filter, scoped per dataset to the sheets where that dataset
    # is visualized. On People, only the All Users table (p-all-users, which
    # reads `base`) is date-scoped - it's added as a dedicated SELECTED_VISUALS
    # group below. The other People visuals (segments / funnel / cohort /
    # movers) keep their own trailing-window logic and are NOT date-filtered.
    date_filtered_datasets = [
        ("trends",  "activity_date", ["executive", "activity"]),
        # base feeds the Executive "Active users" distinct-count KPI, the
        # Activity-sheet visuals, User-detail (KPIs + daily lines), and the
        # Economics sheet (all Economics visuals now read base).
        ("base",    "activity_date", ["executive", "activity", "user-detail", "economics"]),
        # models feeds Activity (a-models) and User-detail (u-models).
        # People is included so the "Top users (by model)" bar honors the
        # People date picker. People's only `models`-backed visual is
        # p-by-model, so ALL_VISUALS on the People sheet scopes to just it.
        ("models",  "activity_date", ["executive", "activity", "user-detail", "people"]),
        ("heatmap", "activity_date", ["executive", "activity"]),
        # tier_breakdown carries activity_date and is read by Activity-sheet
        # visuals (a-active / a-messages / a-credits).
        ("tiers",   "activity_date", ["activity"]),
    ]
    for dataset_id, column, sheet_ids in date_filtered_datasets:
        filter_groups.append({
            "FilterGroupId": f"fg-date-{dataset_id}",
            "Filters": [{
                "TimeRangeFilter": {
                    "FilterId": f"f-date-{dataset_id}",
                    "Column": {"DataSetIdentifier": dataset_id, "ColumnName": column},
                    "IncludeMinimum": True,
                    "IncludeMaximum": True,
                    "RangeMinimumValue": {"Parameter": "DateRangeStart"},
                    "RangeMaximumValue": {"Parameter": "DateRangeEnd"},
                    "NullOption": "NON_NULLS_ONLY",
                    "TimeGranularity": "DAY",
                },
            }],
            "ScopeConfiguration": {
                "SelectedSheets": {
                    "SheetVisualScopingConfigurations": [
                        {"SheetId": s, "Scope": "ALL_VISUALS"}
                        for s in sheet_ids
                    ],
                },
            },
            "CrossDataset": "SINGLE_DATASET",
            "Status": "ENABLED",
        })

    # People base-backed visuals: date-scope the visuals that read `base` (the
    # All Users table and the engagement-segments donut), leaving the remaining
    # trailing-window visuals untouched. Separate group with a SELECTED_VISUALS
    # scope so the People date picker drives these without affecting
    # funnel/cohort/movers.
    people_base_visuals = ["p-all-users", "p-segments",
                           "p-funnel-1", "p-funnel-8", "p-funnel-20"]
    filter_groups.append({
        "FilterGroupId": "fg-date-people-base",
        "Filters": [{
            "TimeRangeFilter": {
                "FilterId": "f-date-people-base",
                "Column": {"DataSetIdentifier": "base", "ColumnName": "activity_date"},
                "IncludeMinimum": True,
                "IncludeMaximum": True,
                "RangeMinimumValue": {"Parameter": "DateRangeStart"},
                "RangeMaximumValue": {"Parameter": "DateRangeEnd"},
                "NullOption": "NON_NULLS_ONLY",
                "TimeGranularity": "DAY",
            },
        }],
        "ScopeConfiguration": {
            "SelectedSheets": {
                "SheetVisualScopingConfigurations": [{
                    "SheetId": "people",
                    "Scope": "SELECTED_VISUALS",
                    "VisualIds": people_base_visuals,
                }],
            },
        },
        "CrossDataset": "SINGLE_DATASET",
        "Status": "ENABLED",
    })

    # Same base-backed People visuals also honor the Tier picker. The tier
    # filter group for `base` (above) is scoped to economics only; add these
    # explicitly so the Tier control works against the base-backed table/donut.
    filter_groups.append({
        "FilterGroupId": "fg-tier-people-base",
        "Filters": [{
            "CategoryFilter": {
                "FilterId": "f-tier-people-base",
                "Column": {"DataSetIdentifier": "base", "ColumnName": "subscription_tier"},
                "Configuration": {
                    "CustomFilterConfiguration": {
                        "MatchOperator": "EQUALS",
                        "NullOption": "NON_NULLS_ONLY",
                        "ParameterName": "SelectedTier",
                    },
                },
            },
        }],
        "ScopeConfiguration": {
            "SelectedSheets": {
                "SheetVisualScopingConfigurations": [{
                    "SheetId": "people",
                    "Scope": "SELECTED_VISUALS",
                    "VisualIds": people_base_visuals,
                }],
            },
        },
        "CrossDataset": "SINGLE_DATASET",
        "Status": "ENABLED",
    })

    # Model picker filter: applied to every visual that reads the `models`
    # dataset across Activity, People, and User-detail. QuickSight requires
    # filter groups scoped to multiple sheets to use ALL_VISUALS on each
    # sheet, so for SELECTED_VISUALS scopes we emit one filter group per
    # sheet.
    # Scope the model filter ONLY to the visuals on sheets that actually have a
    # Model picker: a-models (Activity) and u-models (User detail). People's
    # p-by-model is deliberately NOT included - so SelectedModel, set on
    # Activity or User detail, does NOT carry over and silently filter
    # p-by-model when the user navigates to People (People has no Model control
    # to reveal or reset such a filter). p-by-model therefore always shows all
    # models, scoped only by the People date + tier pickers. (People used to
    # have a Model picker; it was removed because it filtered only 1 of the
    # sheet's visuals and read as global.)
    model_filter_visuals = {
        "activity":    ["a-models"],
        "user-detail": ["u-models"],
    }
    for sheet_id, visual_ids in model_filter_visuals.items():
        filter_groups.append({
            "FilterGroupId": f"fg-model-{sheet_id}",
            "Filters": [{
                "CategoryFilter": {
                    "FilterId": f"f-model-{sheet_id}",
                    "Column": {"DataSetIdentifier": "models", "ColumnName": "model_name"},
                    "Configuration": {
                        "CustomFilterConfiguration": {
                            "MatchOperator": "EQUALS",
                            "NullOption": "NON_NULLS_ONLY",
                            "ParameterName": "SelectedModel",
                        },
                    },
                },
            }],
            "ScopeConfiguration": {
                "SelectedSheets": {
                    "SheetVisualScopingConfigurations": [{
                        "SheetId": sheet_id,
                        "Scope": "SELECTED_VISUALS",
                        "VisualIds": visual_ids,
                    }],
                },
            },
            "CrossDataset": "SINGLE_DATASET",
            "Status": "ENABLED",
        })

    # (No explicit "exclude Idle" filter on the segments donut any more: it now
    # reads `base` over the selected date range, where every visible user has
    # >=1 active day in range, so the Idle bucket can't appear. The old filter
    # targeted the fixed-window engagement view, which the donut no longer uses.
    # Filtering on the level-aware segment_calc field is also unsafe, so it is
    # intentionally omitted.)

    # Analysis-level calculated fields. These let the Economics sheet and the
    # User-detail "Active days" KPI compute correct aggregates over `base`
    # *after* the date-range filter is applied - something a pre-aggregated
    # Athena view (e.g. user_totals) can't do because its numbers are frozen
    # at full-history totals. This is why the Economics sheet was moved off a
    # dedicated unit-economics view and onto `base` + these calc fields.
    #
    # Ratio fields use aggregate functions directly (sum/distinctCount) so
    # QuickSight evaluates them at the visual's grouping level (e.g. per
    # tier) over the filtered rows, giving a true windowed ratio rather than
    # an average-of-daily-ratios.
    calculated_fields = [
        # User-detail: true active-day count. base has one row per
        # (user, day, client_type); a user active on IDE+CLI the same day
        # is two rows, so a plain COUNT over-reports days. distinct_count of
        # the date is correct.
        {
            "DataSetIdentifier": "base",
            "Name": "active_days_calc",
            "Expression": "distinct_count({activity_date})",
        },
        # --- People "engagement segments" donut, computed over the selected
        # date range (not a fixed trailing 30d). The donut groups by
        # `segment_calc` and counts distinct users per bucket. Because these
        # are PRE_AGG level-aware aggregations partitioned by user_id, each
        # user's active-days / messages are evaluated over exactly the rows the
        # People date+tier picker leaves visible, so the segment recomputes as
        # the range changes. "Idle" cannot occur within a window (every visible
        # user has >=1 active day in range), so the bucket naturally drops out
        # and no explicit exclude filter is needed.
        {
            "DataSetIdentifier": "base",
            "Name": "user_active_days_in_range",
            "Expression": "distinctCountOver({activity_date}, [{user_id}], PRE_AGG)",
        },
        {
            "DataSetIdentifier": "base",
            "Name": "user_messages_in_range",
            "Expression": "sumOver({total_messages}, [{user_id}], PRE_AGG)",
        },
        {
            "DataSetIdentifier": "base",
            "Name": "segment_calc",
            "Expression": (
                "ifelse("
                "{user_active_days_in_range} >= 20 OR {user_messages_in_range} >= 1000, 'Power',"
                "{user_active_days_in_range} >= 8, 'Active',"
                "{user_active_days_in_range} >= 1, 'Light',"
                "'Idle')"
            ),
        },
        # --- People engagement funnel, computed over the selected date range.
        # The native funnel chart needs one pre-pivoted row per stage, which we
        # can't produce from daily-grain `base` in a single visual; instead the
        # funnel is rendered as three KPI tiles (>=1 / >=8 / >=20 active days),
        # each a DISTINCT_COUNT of the users meeting that threshold IN RANGE.
        # Each field returns the user_id when the user qualifies, else NULL, so
        # distinct_count tallies only qualifying users. Cumulative by
        # construction: every >=20 user is also >=8 and >=1, so the three
        # counts read as a descending conversion narrative. Reuses
        # user_active_days_in_range (PRE_AGG over user_id), so the tiles
        # recompute as the People date/tier picker changes.
        {
            "DataSetIdentifier": "base",
            "Name": "funnel_ge1_user",
            "Expression": "ifelse({user_active_days_in_range} >= 1, {user_id}, NULL)",
        },
        {
            "DataSetIdentifier": "base",
            "Name": "funnel_ge8_user",
            "Expression": "ifelse({user_active_days_in_range} >= 8, {user_id}, NULL)",
        },
        {
            "DataSetIdentifier": "base",
            "Name": "funnel_ge20_user",
            "Expression": "ifelse({user_active_days_in_range} >= 20, {user_id}, NULL)",
        },
        # Distinct active users in the window, as a numeric calc field so it
        # can sit in a table value well (a raw STRING user_id can't take
        # DISTINCT_COUNT in a NumericalMeasureField).
        {
            "DataSetIdentifier": "base",
            "Name": "active_users_calc",
            "Expression": "distinct_count({user_id})",
        },
        # Economics ratios over the filtered window, evaluated per tier.
        # Denominators are guarded with ifelse(<d>=0, NULL, <d>): a tier/window
        # can legitimately have a zero denominator (e.g. credit-only rows with
        # zero messages, or a window where a tier has no users), and an
        # unguarded /0 renders as a broken/blank bar with no explanation.
        # Returning NULL makes the cell explicitly empty instead.
        {
            "DataSetIdentifier": "base",
            "Name": "credits_per_user_calc",
            "Expression": "sum({credits_used}) / ifelse(distinct_count({user_id}) = 0, NULL, distinct_count({user_id}))",
        },
        {
            "DataSetIdentifier": "base",
            "Name": "credits_per_message_calc",
            "Expression": "sum({credits_used}) / ifelse(sum({total_messages}) = 0, NULL, sum({total_messages}))",
        },
        {
            "DataSetIdentifier": "base",
            "Name": "overage_pct_calc",
            "Expression": "sum({overage_credits_used}) / ifelse(sum({credits_used}) = 0, NULL, sum({credits_used}))",
        },
        # In-plan (non-overage) credits, for the stacked credits-by-tier bar.
        # overage_credits_used <= credits_used by construction, so the
        # difference is non-negative without a guard (QS calc fields don't
        # support Athena's greatest()).
        {
            "DataSetIdentifier": "base",
            "Name": "base_credits_calc",
            "Expression": "sum({credits_used}) - sum({overage_credits_used})",
        },
    ]

    return {
        "DataSetIdentifierDeclarations": decls,
        "Sheets": sheets,
        "ParameterDeclarations": parameter_declarations,
        "FilterGroups": filter_groups,
        "CalculatedFields": calculated_fields,
    }


def upsert(qs, *, account_id: str, principal_arn: str, theme_arn: str | None,
           asset_id: str, is_dashboard: bool, definition: dict) -> str:
    create_kwargs = {
        "AwsAccountId": account_id,
        "Name": NAME,
        "Definition": definition,
        "Permissions": [{
            "Principal": principal_arn,
            "Actions": (
                [
                    "quicksight:DescribeDashboard",
                    "quicksight:ListDashboardVersions",
                    "quicksight:UpdateDashboardPermissions",
                    "quicksight:QueryDashboard",
                    "quicksight:UpdateDashboard",
                    "quicksight:DeleteDashboard",
                    "quicksight:DescribeDashboardPermissions",
                    "quicksight:UpdateDashboardPublishedVersion",
                ] if is_dashboard else [
                    "quicksight:RestoreAnalysis",
                    "quicksight:UpdateAnalysisPermissions",
                    "quicksight:DeleteAnalysis",
                    "quicksight:DescribeAnalysisPermissions",
                    "quicksight:QueryAnalysis",
                    "quicksight:DescribeAnalysis",
                    "quicksight:UpdateAnalysis",
                ]
            ),
        }],
    }

    if is_dashboard:
        create_kwargs["DashboardId"] = asset_id
        create_fn = qs.create_dashboard
        update_fn = qs.update_dashboard
        update_kwargs = {
            "AwsAccountId": account_id,
            "DashboardId": asset_id,
            "Name": NAME,
            "Definition": definition,
        }
    else:
        create_kwargs["AnalysisId"] = asset_id
        create_fn = qs.create_analysis
        update_fn = qs.update_analysis
        update_kwargs = {
            "AwsAccountId": account_id,
            "AnalysisId": asset_id,
            "Name": NAME,
            "Definition": definition,
        }

    if theme_arn:
        create_kwargs["ThemeArn"] = theme_arn
        update_kwargs["ThemeArn"] = theme_arn

    try:
        resp = create_fn(**create_kwargs)
        action = "created"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceExistsException":
            raise
        resp = update_fn(**update_kwargs)
        action = "updated"

    # Wait for the build to finish and assert success on BOTH paths. Without
    # this, a malformed Definition produces a CREATION_FAILED version
    # while the script returns 0, which is hard to detect in CI.
    _wait_and_assert(qs, account_id, asset_id, resp, is_dashboard)

    # update_dashboard creates a new version but does NOT publish it.
    # Promote so the UI shows the latest. (create_dashboard auto-publishes
    # v1 - only update needs explicit promotion.)
    if is_dashboard and action == "updated":
        version = resp.get("VersionArn", "").rsplit("/", 1)[-1]
        if version.isdigit():
            qs.update_dashboard_published_version(
                AwsAccountId=account_id,
                DashboardId=asset_id,
                VersionNumber=int(version),
            )
            print(f"  Dashboard  published: v{version}", file=sys.stderr)

    print(f"  {('Dashboard' if is_dashboard else 'Analysis'):9s} {action}: {resp.get('Arn', asset_id)}",
          file=sys.stderr)
    return asset_id


def _wait_and_assert(qs, account_id: str, asset_id: str, resp: dict, is_dashboard: bool):
    """Poll the asset until it reaches a terminal state. Raise SystemExit
    with the QS-reported errors if it failed."""
    version_arn = resp.get("VersionArn", "")
    version = version_arn.rsplit("/", 1)[-1] if version_arn else ""

    label = "Dashboard" if is_dashboard else "Analysis"
    terminal_ok   = {"CREATION_SUCCESSFUL", "UPDATE_SUCCESSFUL"}
    terminal_bad  = {"CREATION_FAILED", "UPDATE_FAILED"}

    for _ in range(120):
        if is_dashboard:
            kwargs = {"AwsAccountId": account_id, "DashboardId": asset_id}
            if version.isdigit():
                kwargs["VersionNumber"] = int(version)
            desc = qs.describe_dashboard(**kwargs)["Dashboard"]["Version"]
        else:
            desc = qs.describe_analysis(
                AwsAccountId=account_id, AnalysisId=asset_id,
            )["Analysis"]
        status = desc["Status"]
        if status in terminal_ok | terminal_bad:
            break
        time.sleep(2)
    else:
        raise SystemExit(f"{label} {asset_id} did not reach terminal state in 240s")

    if status in terminal_bad:
        errors = desc.get("Errors", [])
        msg = f"{label} {asset_id} ended in {status}"
        if errors:
            msg += f"\n  Errors: {errors}"
        raise SystemExit(msg)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--principal-arn", required=True)
    p.add_argument("--theme-arn", default=None,
                   help="Optional QuickSight Theme ARN to apply to the "
                        "Analysis and Dashboard.")
    p.add_argument("--asset-id", default=DEFAULT_ASSET_ID,
                   help="Identifier used for both the Analysis and Dashboard. "
                        "Override when running multiple parallel deployments "
                        "in the same account.")
    p.add_argument("--resource-prefix", default="kiro-analytics",
                   help="Prefix used for QuickSight DataSet IDs. Must match "
                        "the ResourcePrefix passed to the QS CFN stack.")
    args = p.parse_args()

    qs = boto3.client("quicksight", region_name=args.region)
    definition = build_definition(args.account_id, args.region,
                                  args.resource_prefix)

    common = dict(account_id=args.account_id, principal_arn=args.principal_arn,
                  theme_arn=args.theme_arn, asset_id=args.asset_id)
    print("Upserting Analysis", file=sys.stderr)
    upsert(qs, **common, is_dashboard=False, definition=definition)
    print("Upserting Dashboard", file=sys.stderr)
    upsert(qs, **common, is_dashboard=True, definition=definition)
    return 0


if __name__ == "__main__":
    sys.exit(main())
