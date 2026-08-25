"""The semantic layer: what a schema dump cannot say.

Column names and types are not enough to draft a correct query, and the gap is
where most text-to-SQL errors actually live. Four things a model cannot infer
from `information_schema`:

**What a column means.** `order_status` is `text`. That tells you nothing about
which values exist, so a model guesses `'complete'` when the data says
`'shipped'` — and returns zero rows with no error and no warning.

**Which join is correct.** Two views both having `order_id` does not say they
should be joined on it, nor which direction preserves rows. A model that guesses
produces plausible SQL and wrong numbers.

**What a metric means.** "Revenue" is a business definition, not a column.
Whether it includes cancelled orders is a decision someone made once, and it must
live here rather than being re-decided per question.

**What a good answer looks like.** Few-shot examples are the highest-leverage
part of this file: showing three correct queries for this schema beats any amount
of prose about it.

This is authored, not generated. That is the point — it encodes decisions, and a
decision cannot be introspected out of a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnDoc:
    name: str
    description: str
    # Enumerated values where the set is small and closed. Without these a model
    # invents plausible-looking values and silently returns nothing.
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ViewDoc:
    name: str
    description: str
    columns: tuple[ColumnDoc, ...]
    grain: str = ""


@dataclass(frozen=True)
class JoinPath:
    """A join someone has confirmed is correct."""

    left: str
    right: str
    on: str
    note: str = ""


@dataclass(frozen=True)
class Metric:
    """A business definition, pinned to one expression."""

    name: str
    description: str
    expression: str
    caveat: str = ""


@dataclass(frozen=True)
class Example:
    question: str
    sql: str


@dataclass(frozen=True)
class IoTMetricTemplate:
    """A reviewed, parameter-free mapping for a common IoT aggregate."""

    aliases: tuple[str, ...]
    device_name: str
    metric: str
    devices_view: str
    metrics_view: str


@dataclass(frozen=True)
class SemanticLayer:
    views: tuple[ViewDoc, ...] = ()
    joins: tuple[JoinPath, ...] = ()
    metrics: tuple[Metric, ...] = ()
    examples: tuple[Example, ...] = ()
    iot_metric_templates: tuple[IoTMetricTemplate, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_prompt(self) -> str:
        """Render for an LLM prompt.

        Ordering is deliberate: schema, then joins, then metrics, then examples.
        Examples come last because they are what the model imitates most
        strongly, and imitation is the most reliable lever available.
        """
        parts: list[str] = ["## Available views", ""]

        for view in self.views:
            parts.append(f"### {view.name}")
            parts.append(view.description)
            if view.grain:
                parts.append(f"Grain: {view.grain}")
            parts.append("")
            for column in view.columns:
                line = f"- `{column.name}` — {column.description}"
                if column.values:
                    line += f" Values: {', '.join(repr(v) for v in column.values)}."
                parts.append(line)
            parts.append("")

        if self.joins:
            parts.append("## Join paths")
            parts.append("")
            for join in self.joins:
                line = f"- `{join.left}` to `{join.right}` ON {join.on}"
                if join.note:
                    line += f" ({join.note})"
                parts.append(line)
            parts.append("")

        if self.metrics:
            parts.append("## Metric definitions")
            parts.append("")
            for metric in self.metrics:
                parts.append(f"- **{metric.name}**: {metric.description}")
                parts.append(f"  Expression: `{metric.expression}`")
                if metric.caveat:
                    parts.append(f"  Caveat: {metric.caveat}")
            parts.append("")

        if self.notes:
            parts.append("## Notes")
            parts.append("")
            parts.extend(f"- {note}" for note in self.notes)
            parts.append("")

        if self.examples:
            parts.append("## Example queries")
            parts.append("")
            for example in self.examples:
                parts.append(f"Q: {example.question}")
                parts.append(f"```sql\n{example.sql}\n```")
                parts.append("")

        return "\n".join(parts).strip()


def from_discovered_schema(
    discovered: list[dict[str, Any]],
    *,
    connector_name: str,
    reviewed_training: dict[str, Any] | None = None,
) -> SemanticLayer:
    """Build a safe baseline semantic layer from approved database metadata.

    A connector's reader role and configured schema decide what arrives here;
    this function never inspects base tables or sample rows. Column names and
    types are enough to make a newly connected system useful immediately, while
    a data owner can later add richer metrics and join definitions where their
    business meaning matters.
    """
    views: list[ViewDoc] = []
    for item in discovered:
        name = item.get("name")
        columns = item.get("columns")
        if not isinstance(name, str) or not isinstance(columns, list):
            continue

        documented_columns = tuple(
            ColumnDoc(
                name=str(column["name"]),
                description=f"Source data type: {column.get('type', 'unknown')}.",
            )
            for column in columns
            if isinstance(column, dict) and isinstance(column.get("name"), str)
        )
        if documented_columns:
            views.append(
                ViewDoc(
                    name=name,
                    description=f"Approved read-only view from {connector_name}.",
                    columns=documented_columns,
                )
            )

    notes = [
        "Use only the approved views listed above. Do not guess at base tables or columns.",
        "This schema was discovered from metadata only; it contains no sampled business rows.",
    ]
    # A repository assistant's response is never used directly.  This is only
    # reached for an administrator-reviewed, activated training record, and we
    # project a few bounded factual fields rather than injecting its JSON blob.
    notes.extend(_reviewed_training_notes(reviewed_training))
    return SemanticLayer(
        views=tuple(views),
        notes=tuple(notes),
        iot_metric_templates=_reviewed_iot_templates(reviewed_training, views),
    )


def _reviewed_training_notes(profile: dict[str, Any] | None) -> list[str]:
    """Project reviewed domain facts without treating repository text as instructions."""
    if not isinstance(profile, dict):
        return []
    notes: list[str] = []
    summary = profile.get("system_summary")
    if isinstance(summary, str) and summary.strip():
        notes.append(f"Administrator-reviewed system context: {_bounded_text(summary, 600)}")
    terms = profile.get("business_terms")
    if isinstance(terms, list):
        for term in terms[:20]:
            if not isinstance(term, dict):
                continue
            name, definition = term.get("term"), term.get("definition")
            if (
                isinstance(name, str)
                and isinstance(definition, str)
                and name.strip()
                and definition.strip()
            ):
                synonyms = term.get("synonyms")
                synonym_text = ""
                if isinstance(synonyms, list):
                    safe_synonyms = [
                        _bounded_text(value, 60)
                        for value in synonyms[:10]
                        if isinstance(value, str) and value.strip()
                    ]
                    if safe_synonyms:
                        synonym_text = f" (synonyms: {', '.join(safe_synonyms)})"
                notes.append(
                    "Administrator-reviewed term: "
                    f"{_bounded_text(name, 80)}{synonym_text} = "
                    f"{_bounded_text(definition, 240)}"
                )
    metrics = profile.get("metrics")
    if isinstance(metrics, list):
        for metric in metrics[:20]:
            if not isinstance(metric, dict):
                continue
            name, definition = metric.get("name"), metric.get("definition")
            if (
                isinstance(name, str)
                and isinstance(definition, str)
                and name.strip()
                and definition.strip()
            ):
                calculation_note = metric.get("calculation_note")
                calculation_text = (
                    f" Calculation: {_bounded_text(calculation_note, 240)}"
                    if isinstance(calculation_note, str) and calculation_note.strip()
                    else ""
                )
                notes.append(
                    "Administrator-reviewed metric: "
                    f"{_bounded_text(name, 80)} = {_bounded_text(definition, 240)}"
                    f"{calculation_text}"
                )
    return notes


def _bounded_text(value: str, limit: int) -> str:
    """Keep prompt context compact and visibly data-like, even after review."""
    return " ".join(value.replace("`", "'").split())[:limit]


def _reviewed_iot_templates(
    profile: dict[str, Any] | None, views: list[ViewDoc]
) -> tuple[IoTMetricTemplate, ...]:
    """Turn an explicit reviewed term into a safe, deterministic IoT mapping.

    Nothing is inferred from database rows. The device and metric must both be
    named in the admin-reviewed profile, and the views must have been discovered
    through the connector's own curated reader role.
    """
    if not isinstance(profile, dict):
        return ()
    devices_view = next(
        (view.name for view in views if view.name.endswith(".v_devices")),
        None,
    )
    metrics_view = next(
        (view.name for view in views if view.name.endswith(".v_device_metrics")),
        None,
    )
    if not devices_view or not metrics_view:
        return ()
    metrics = {
        str(item.get("name")).lower(): str(item.get("name"))
        for item in profile.get("metrics", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    templates: list[IoTMetricTemplate] = []
    for term in profile.get("business_terms", []):
        if not isinstance(term, dict):
            continue
        name, definition = term.get("term"), term.get("definition")
        if not isinstance(name, str) or not isinstance(definition, str):
            continue
        device_match = re.search(r"device named\s+([^\.]+)", definition, re.IGNORECASE)
        if not device_match:
            continue
        metric = next(
            (
                value
                for key, value in metrics.items()
                if re.search(rf"\b{re.escape(key)}\b", definition, re.IGNORECASE)
            ),
            None,
        )
        if metric is None:
            continue
        synonyms = term.get("synonyms")
        aliases = [name]
        if isinstance(synonyms, list):
            aliases.extend(value for value in synonyms if isinstance(value, str))
        templates.append(
            IoTMetricTemplate(
                aliases=tuple(alias.lower().strip() for alias in aliases if alias.strip()),
                device_name=device_match.group(1).strip(),
                metric=metric,
                devices_view=devices_view,
                metrics_view=metrics_view,
            )
        )
    return tuple(templates)


# The semantic layer for the demo analytics warehouse.
#
# In a real deployment this is configuration a data owner writes, not code —
# which is what Phase 6's console is for. It lives here now because there is no
# console yet, and hardcoding it makes the shape obvious.
ANALYTICS_SEMANTICS = SemanticLayer(
    views=(
        ViewDoc(
            name="curated.v_orders",
            description="One row per order, with the customer already joined in.",
            grain="order",
            columns=(
                ColumnDoc("order_id", "Unique order identifier."),
                ColumnDoc(
                    "order_status",
                    "Current state of the order.",
                    values=("pending", "shipped", "cancelled"),
                ),
                ColumnDoc("ordered_at", "Date the order was placed."),
                ColumnDoc("shipped_at", "Date the order shipped; NULL if not yet shipped."),
                ColumnDoc("customer_id", "Identifier of the ordering customer."),
                ColumnDoc("customer_name", "Customer's display name."),
                ColumnDoc(
                    "customer_country",
                    "Two-letter country code.",
                    values=("UK", "US", "DE", "SG"),
                ),
                ColumnDoc(
                    "customer_segment",
                    "Commercial segment.",
                    values=("enterprise", "midmarket", "smb"),
                ),
                ColumnDoc(
                    "days_to_ship",
                    "shipped_at minus ordered_at, in days. NULL until the order ships.",
                ),
            ),
        ),
        ViewDoc(
            name="curated.v_order_lines",
            description="One row per line item, with product and order details joined in.",
            grain="order line",
            columns=(
                ColumnDoc("order_line_id", "Unique line identifier."),
                ColumnDoc("order_id", "The order this line belongs to."),
                ColumnDoc("order_status", "Status of the parent order."),
                ColumnDoc("ordered_at", "Date the parent order was placed."),
                ColumnDoc("product_sku", "Product code, e.g. 'QN-1183-A'."),
                ColumnDoc("product_name", "Product display name."),
                ColumnDoc(
                    "product_category",
                    "Product grouping.",
                    values=("parts", "sensors", "electronics"),
                ),
                ColumnDoc("quantity", "Units ordered on this line."),
                ColumnDoc("unit_price", "Price per unit at the time of order."),
                ColumnDoc("line_total", "quantity * unit_price. Use this rather than recomputing."),
            ),
        ),
        ViewDoc(
            name="curated.v_customers",
            description="One row per customer.",
            grain="customer",
            columns=(
                ColumnDoc("customer_id", "Unique customer identifier."),
                ColumnDoc("customer_name", "Display name."),
                ColumnDoc("country", "Two-letter country code."),
                ColumnDoc("segment", "Commercial segment."),
                ColumnDoc("created_at", "Date the customer account was created."),
            ),
        ),
    ),
    joins=(
        JoinPath(
            left="curated.v_orders",
            right="curated.v_order_lines",
            on="v_orders.order_id = v_order_lines.order_id",
            note="one order to many lines",
        ),
        JoinPath(
            left="curated.v_customers",
            right="curated.v_orders",
            on="v_customers.customer_id = v_orders.customer_id",
            note=(
                "only needed for customers with no orders; v_orders already carries customer fields"
            ),
        ),
    ),
    metrics=(
        Metric(
            name="revenue",
            description="Total value of goods ordered.",
            expression="sum(line_total) FROM curated.v_order_lines",
            caveat=(
                "Cancelled orders are INCLUDED unless filtered out. For net revenue add "
                "WHERE order_status <> 'cancelled'."
            ),
        ),
        Metric(
            name="average time to ship",
            description="Mean days between ordering and shipping.",
            expression="avg(days_to_ship) FROM curated.v_orders",
            caveat="Unshipped orders have NULL days_to_ship and avg() skips them silently.",
        ),
        Metric(
            name="order count",
            description="Number of orders.",
            expression="count(*) FROM curated.v_orders",
        ),
    ),
    notes=(
        "Query the curated.* views only. Base tables are not accessible and a query "
        "naming one will be refused by the database.",
        "v_orders already includes customer name, country and segment — joining "
        "v_customers is usually unnecessary.",
        "Dates are DATE, not timestamp. Compare with date literals like '2026-01-01'.",
    ),
    examples=(
        Example(
            question="How many orders shipped last month?",
            sql=(
                "SELECT count(*) AS shipped_orders\n"
                "FROM curated.v_orders\n"
                "WHERE order_status = 'shipped'\n"
                "  AND shipped_at >= date_trunc('month', current_date - interval '1 month')\n"
                "  AND shipped_at < date_trunc('month', current_date)"
            ),
        ),
        Example(
            question="Which customers spent the most?",
            sql=(
                "SELECT o.customer_name, sum(l.line_total) AS revenue\n"
                "FROM curated.v_orders o\n"
                "JOIN curated.v_order_lines l ON l.order_id = o.order_id\n"
                "WHERE o.order_status <> 'cancelled'\n"
                "GROUP BY o.customer_name\n"
                "ORDER BY revenue DESC"
            ),
        ),
        Example(
            question="What is the average time to ship by country?",
            sql=(
                "SELECT customer_country, round(avg(days_to_ship), 1) AS avg_days\n"
                "FROM curated.v_orders\n"
                "WHERE shipped_at IS NOT NULL\n"
                "GROUP BY customer_country\n"
                "ORDER BY avg_days"
            ),
        ),
    ),
)
