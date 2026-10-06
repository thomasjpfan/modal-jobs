"""A read-only web dashboard of the jobs tracked by the backend, styled after modal.com.

The dashboard reads jobs through the `Registry`, so the registry stays the only writer
of the jobs volume and running jobs are reconciled whenever the dashboard shows them.
"""

import re
import time
from collections.abc import Callable

import dash_mantine_components as dmc
from dash import Dash, Input, Output, dcc, html

from modal_jobs._format import (
    format_bytes,
    format_duration,
    format_exit_code,
    format_timestamp,
    record_duration,
    record_fields,
)
from modal_jobs._store import (
    FAILED,
    RUNNING,
    STATUSES,
    SUCCEEDED,
    TIMED_OUT,
    parse_label_filter,
)

# Show at most this many jobs, newest first.
MAX_JOBS = 200
# Show only the end of each log, to keep pages small.
LOG_TAIL_BYTES = 200 * 1024
REFRESH_MS = 10_000

# Colors from modal.com's dark palette. Mantine needs ten shades per color.
THEME = {
    "primaryColor": "modal-green",
    "primaryShade": {"light": 9, "dark": 9},
    "autoContrast": True,
    "fontFamily": "Inter, system-ui, sans-serif",
    "fontFamilyMonospace": "'Fira Mono', ui-monospace, monospace",
    "headings": {"fontFamily": "Inter, system-ui, sans-serif", "fontWeight": "500"},
    "defaultRadius": "sm",
    "colors": {
        # dark[0] is text, dark[2] dimmed text, dark[4] borders, and dark[7] the page.
        "dark": [
            "#d1d1d1",
            "#bababa",
            "#a3a3a3",
            "#747474",
            "#3b3b3b",
            "#2f2f2f",
            "#232323",
            "#181818",
            "#141414",
            "#101010",
        ],
        "modal-green": [
            "#1d231c",
            "#222d20",
            "#273823",
            "#2d4327",
            "#37582f",
            "#4c833e",
            "#569846",
            "#28c700",
            "#6ac345",
            "#7fee64",
        ],
        "modal-pink": [
            "#3b2a37",
            "#5d3b56",
            "#8b537f",
            "#a35e94",
            "#9d0c7d",
            "#d176bd",
            "#e66dcb",
            "#f0a7e0",
            "#f5c4ea",
            "#fae2f5",
        ],
        "modal-red": [
            "#3a2525",
            "#452a2a",
            "#723c3c",
            "#b72121",
            "#dc3636",
            "#f87171",
            "#f09797",
            "#f4b2b2",
            "#fad9d9",
            "#fbe5e5",
        ],
    },
}

# Like modal.com, running is pink, success green, and failure red.
STATUS_COLORS = {
    RUNNING: "modal-pink",
    SUCCEEDED: "modal-green",
    FAILED: "modal-red",
    TIMED_OUT: "modal-red",
}

# Status colors as CSS colors, for the dots drawn outside Mantine components.
DOT_COLORS = {
    RUNNING: "var(--mantine-color-modal-pink-6)",
    SUCCEEDED: "var(--mantine-color-modal-green-9)",
    FAILED: "var(--mantine-color-modal-red-5)",
    TIMED_OUT: "var(--mantine-color-modal-red-5)",
}

FONTS = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600"
    "&family=Fira+Mono:wght@400;500&display=swap"
)

# Fields shown elsewhere on the job page: in its header, error, command, or timeline.
HEADER_FIELDS = {
    "ID",
    "Name",
    "Status",
    "Error",
    "Command",
    "Exit code",
    "Label",
    "Submitted",
    "Started",
    "Finished",
    "Duration",
    "GPU",
}
# Fields shown under resources on the job page. The rest are shown under environment.
RESOURCE_FIELDS = {"CPU", "Memory", "Timeout", "Retries", "CPU time", "Container"}

# Styles that Mantine's props cannot express: the page glow, the sticky header, the
# pulsing dots, and the table and log chrome.
STYLES = """
body {
  background-image: radial-gradient(1000px 420px at 50% -180px, rgba(127, 238, 100, 0.09), transparent 70%);
  background-attachment: fixed;
  min-height: 100vh;
}
.mj-header {
  position: sticky; top: 0; z-index: 10;
  background: rgba(20, 20, 20, 0.72);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--mantine-color-dark-5);
}
.mj-logo {
  width: 22px; height: 22px; border-radius: 6px;
  background: linear-gradient(135deg, #7fee64, #28c700);
  box-shadow: 0 0 18px rgba(127, 238, 100, 0.35);
}
.mj-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex: none; }
.mj-pulse { animation: mj-pulse 1.8s ease-out infinite; }
@keyframes mj-pulse {
  0% { box-shadow: 0 0 0 0 currentColor; }
  70%, 100% { box-shadow: 0 0 0 6px transparent; }
}
.mj-table thead th {
  background: var(--mantine-color-dark-8);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.mj-table tbody tr:last-child td { border-bottom: none; }
.mj-id { color: var(--mantine-color-modal-green-9); white-space: nowrap; }
.mj-id:hover { color: #a6f590; }
.mj-log {
  background: #0d0d0d; border: 1px solid var(--mantine-color-dark-5);
  max-height: 640px; overflow: auto; line-height: 1.6;
}
"""

INDEX_STRING = f"""<!DOCTYPE html>
<html>
  <head>
    {{%metas%}}
    <title>{{%title%}}</title>
    {{%favicon%}}
    {{%css%}}
    <style>{STYLES}</style>
  </head>
  <body>
    {{%app_entry%}}
    <footer>{{%config%}}{{%scripts%}}{{%renderer%}}</footer>
  </body>
</html>
"""


def status_dot(status: str):
    color = DOT_COLORS.get(status, "var(--mantine-color-dark-2)")
    return html.Span(
        className="mj-dot mj-pulse" if status == RUNNING else "mj-dot",
        style={"background": color, "color": color},
    )


def status_badge(status: str, size: str = "md"):
    # Mantine's light badges dim the text too much on the dark palette.
    text_color = DOT_COLORS.get(status, "var(--mantine-color-dark-0)")
    return dmc.Badge(
        status.replace("_", " "),
        leftSection=status_dot(status),
        color=STATUS_COLORS.get(status, "gray"),
        variant="light",
        radius="xl",
        size=size,
        tt="none",
        fw=500,
        styles={"label": {"color": text_color}},
    )


def label_pills(record: dict):
    labels = record.get("labels") or {}
    if not labels:
        return mono("-", c="dimmed")
    return dmc.Group(
        [
            dmc.Badge(
                f"{key}={value}" if value else key,
                variant="outline",
                color="dark.2",
                radius="sm",
                tt="none",
                fw=400,
                ff="monospace",
            )
            for key, value in labels.items()
        ],
        gap=4,
    )


def mono(text: str, **kwargs):
    return dmc.Text(text, ff="monospace", size="sm", **kwargs)


def stat_card(title: str, count: int, status: str | None = None):
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        *([status_dot(status)] if status else []),
                        dmc.Text(title, size="xs", c="dimmed", tt="uppercase", lts="0.06em"),
                    ],
                    gap=8,
                ),
                dmc.Text(str(count), fz=28, fw=500, lh=1),
            ],
            gap=10,
        ),
        withBorder=True,
        p="md",
        bg="dark.8",
    )


def stats_view(records: list[dict]):
    """Return a row of cards counting the jobs `records` by status."""
    counts = {status: 0 for status in STATUSES}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    return dmc.SimpleGrid(
        [
            stat_card("Jobs", len(records)),
            stat_card("Running", counts[RUNNING], RUNNING),
            stat_card("Succeeded", counts[SUCCEEDED], SUCCEEDED),
            stat_card("Failed", counts[FAILED] + counts[TIMED_OUT], FAILED),
        ],
        cols={"base": 2, "sm": 4},
        spacing="md",
    )


def jobs_view(records: list[dict], now: float | None = None):
    """Return the table of the jobs `records`."""
    if not records:
        return dmc.Stack(
            [
                dmc.Text("No jobs found.", fw=500),
                dmc.Text(
                    ["Submit one with ", dmc.Code("modal-jobs run script.py"), "."],
                    c="dimmed",
                    size="sm",
                ),
            ],
            align="center",
            gap=6,
            py=48,
        )
    now = time.time() if now is None else now
    headers = ["ID", "Name", "Status", "Submitted", "Duration", "GPU", "Labels"]
    rows = [
        dmc.TableTr(
            [
                dmc.TableTd(
                    dmc.Anchor(
                        record["id"],
                        href=f"/jobs/{record['id']}",
                        ff="monospace",
                        size="sm",
                        className="mj-id",
                    )
                ),
                dmc.TableTd(dmc.Text(record["name"], size="sm", fw=500, truncate="end", maw=320)),
                dmc.TableTd(status_badge(record["status"])),
                dmc.TableTd(
                    mono(f"{format_duration(now - record['submitted_at'])} ago", c="dimmed")
                ),
                dmc.TableTd(mono(record_duration(record, now), c="dimmed")),
                dmc.TableTd(mono(record.get("gpu") or "-", c="dimmed")),
                dmc.TableTd(label_pills(record)),
            ]
        )
        for record in records
    ]
    return dmc.TableScrollContainer(
        dmc.Table(
            [
                dmc.TableThead(
                    dmc.TableTr(
                        [dmc.TableTh(dmc.Text(h, fz=11, c="dimmed", fw=500)) for h in headers]
                    )
                ),
                dmc.TableTbody(rows),
            ],
            highlightOnHover=True,
            verticalSpacing="sm",
            horizontalSpacing="md",
            className="mj-table",
        ),
        minWidth=880,
    )


def tail_log(log: bytes) -> str:
    """Decode the end of `log`, noting when the start was cut off."""
    if len(log) <= LOG_TAIL_BYTES:
        return log.decode(errors="replace")
    tail = log[-LOG_TAIL_BYTES:].decode(errors="replace")
    return f"... showing the last {LOG_TAIL_BYTES // 1024} KiB ...\n{tail}"


def copy_button(value: str):
    return dmc.CopyButton(
        "Copy",
        value=value,
        copiedChildren="Copied",
        variant="subtle",
        color="gray",
        copiedColor="modal-green.9",
        size="compact-xs",
    )


def key_stat(label: str, value: str):
    return dmc.Stack(
        [dmc.Text(label, fz=11, c="dimmed", tt="uppercase", lts="0.06em"), mono(value, fz="md")],
        gap=4,
    )


def timeline(record: dict):
    """Return the timeline of when the job of `record` was submitted, started, and finished."""
    events = [
        ("Submitted", record["submitted_at"]),
        ("Started", record.get("started_at")),
        ("Finished", record.get("finished_at")),
    ]
    reached = sum(timestamp is not None for _, timestamp in events)
    color = DOT_COLORS.get(record["status"], "var(--mantine-color-dark-2)")
    return dmc.Timeline(
        [
            dmc.TimelineItem(
                mono(format_timestamp(timestamp), c="dimmed", fz="xs"),
                title=dmc.Text(title, size="sm"),
            )
            for title, timestamp in events
        ],
        active=reached - 1,
        color=color,
        bulletSize=12,
        lineWidth=2,
    )


def field_list(fields: list[tuple[str, str]]):
    """Return a list of the `fields`, joining the values of fields with the same label."""
    grouped: dict[str, list[str]] = {}
    for label, value in fields:
        grouped.setdefault(label, []).append(value)
    return dmc.Stack(
        [
            dmc.Stack(
                [
                    dmc.Text(label, fz=11, c="dimmed", tt="uppercase", lts="0.06em"),
                    *(mono(value, style={"wordBreak": "break-all"}) for value in values),
                ],
                gap=4,
            )
            for label, values in grouped.items()
        ],
        gap="md",
    )


def log_view(record: dict, log: bytes | None):
    if log is None:
        if record["status"] == RUNNING:
            message = [
                "The output is saved when the job finishes. Stream it with ",
                dmc.Code(f"modal-jobs logs --follow {record['id']}"),
            ]
        else:
            message = "No saved output."
        return section("Output", dmc.Text(message, c="dimmed", size="sm"))
    lines = log.count(b"\n") + (not log.endswith(b"\n") and bool(log))
    return section(
        "Output",
        dmc.Code(tail_log(log) or "(no output)", block=True, fz="xs", p="md", className="mj-log"),
        aside=dmc.Text(f"{lines:,} lines · {format_bytes(len(log))}", size="xs", c="dimmed"),
    )


def job_view(record: dict, log: bytes | None, now: float | None = None):
    """Return the page of the job `record`, whose saved output is `log`, if any."""
    now = time.time() if now is None else now
    fields = record_fields(record)
    command = dict(fields)["Command"]
    resources = [(label, value) for label, value in fields if label in RESOURCE_FIELDS]
    environment = [
        (label, value)
        for label, value in fields
        if label not in HEADER_FIELDS and label not in RESOURCE_FIELDS
    ]
    running = record["status"] == RUNNING
    header = dmc.Paper(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Title(record["name"], order=2, fw=500),
                        status_badge(record["status"], size="lg"),
                    ],
                    gap="sm",
                ),
                dmc.Group(
                    [mono(record["id"], c="dimmed"), copy_button(record["id"])],
                    gap=6,
                ),
                *([label_pills(record)] if record.get("labels") else []),
                dmc.Divider(color="dark.5", my=4),
                dmc.SimpleGrid(
                    [
                        key_stat(
                            "Running for" if running else "Duration", record_duration(record, now)
                        ),
                        key_stat(
                            "Submitted", f"{format_duration(now - record['submitted_at'])} ago"
                        ),
                        key_stat("GPU", record.get("gpu") or "none"),
                        key_stat(
                            "Exit code",
                            format_exit_code(record)
                            if record.get("exit_code") is not None
                            else "-",
                        ),
                    ],
                    cols={"base": 2, "sm": 4},
                    spacing="lg",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="lg",
        bg="dark.8",
    )
    error = (
        [
            dmc.Alert(
                record["error"],
                color="modal-red",
                variant="outline",
                title="Error",
                styles={"root": {"background": "rgba(220, 54, 54, 0.08)"}},
            )
        ]
        if record.get("error")
        else []
    )
    main = dmc.Stack(
        [
            section(
                "Command",
                dmc.Code(f"$ {command}", block=True, p="md", className="mj-log"),
                aside=copy_button(command),
            ),
            log_view(record, log),
        ],
        gap="md",
    )
    sidebar = dmc.Stack(
        [
            section("Timeline", timeline(record)),
            *([section("Resources", field_list(resources))] if resources else []),
            *([section("Environment", field_list(environment))] if environment else []),
        ],
        gap="md",
    )
    return dmc.Stack(
        [
            back_link(),
            header,
            *error,
            dmc.Grid(
                [
                    dmc.GridCol(main, span={"base": 12, "md": 8}),
                    dmc.GridCol(sidebar, span={"base": 12, "md": 4}),
                ],
                gutter="md",
            ),
        ],
        gap="md",
    )


def section(title: str, body, aside=None):
    heading = dmc.Text(title, size="sm", fw=500)
    if aside is not None:
        heading = dmc.Group([heading, aside], justify="space-between", h=22)
    return dmc.Paper(
        dmc.Stack([heading, body], gap="md"),
        withBorder=True,
        p="lg",
        bg="dark.8",
    )


def back_link():
    return dmc.Anchor("← All jobs", href="/", size="sm", c="dimmed", w="fit-content")


def not_found(message: str):
    return dmc.Stack([back_link(), dmc.Text(message, c="dimmed")], gap="md")


def jobs_page():
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Stack(
                        [
                            dmc.Title("Jobs", order=2, fw=500),
                            dmc.Text(
                                "Newest first, refreshed every 10 seconds.", size="sm", c="dimmed"
                            ),
                        ],
                        gap=4,
                    ),
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id="name-filter",
                                placeholder="Filter by name",
                                size="sm",
                                w=200,
                                debounce=300,
                            ),
                            dmc.TextInput(
                                id="label-filter",
                                placeholder="Labels, e.g. team=ml exp",
                                size="sm",
                                w=220,
                                debounce=300,
                            ),
                            dmc.Select(
                                id="status-filter",
                                value="all",
                                data=[{"value": "all", "label": "All statuses"}]
                                + [{"value": s, "label": s.replace("_", " ")} for s in STATUSES],
                                # Keep a status selected, so the filter is never empty.
                                allowDeselect=False,
                                size="sm",
                                w=150,
                            ),
                        ],
                        gap="sm",
                    ),
                ],
                justify="space-between",
                align="flex-end",
            ),
            html.Div(id="jobs-table"),
        ],
        gap="lg",
    )


def parse_label_filters(value: str | None) -> dict[str, str | None]:
    """Parse the label filter box: `KEY[=VALUE]` terms separated by spaces or commas.

    Raises `ValueError` if a term has an empty key.
    """
    terms = re.split(r"[\s,]+", (value or "").strip())
    return dict(parse_label_filter(term) for term in terms if term)


def create_app(get_registry: Callable, read_log: Callable[[str], bytes]) -> Dash:
    """Create the dashboard app.

    `get_registry` returns a handle to the `Registry`, and `read_log` returns the saved
    output of a job, raising `FileNotFoundError` if it has none.
    """
    app = Dash(
        __name__,
        title="modal-jobs",
        external_stylesheets=[FONTS],
        suppress_callback_exceptions=True,
        update_title=None,
    )
    app.index_string = INDEX_STRING
    app.layout = dmc.MantineProvider(
        [
            dcc.Location(id="url"),
            dcc.Interval(id="refresh", interval=REFRESH_MS),
            dmc.Box(
                dmc.Container(
                    dmc.Group(
                        [
                            dmc.Anchor(
                                dmc.Group(
                                    [
                                        html.Div(className="mj-logo"),
                                        dmc.Text("modal-jobs", ff="monospace", fw=500, c="dark.0"),
                                    ],
                                    gap=10,
                                ),
                                href="/",
                                underline="never",
                            ),
                            dmc.Group(
                                [
                                    html.Span(
                                        className="mj-dot mj-pulse",
                                        style={
                                            "background": DOT_COLORS[SUCCEEDED],
                                            "color": DOT_COLORS[SUCCEEDED],
                                        },
                                    ),
                                    dmc.Text("Live", size="xs", c="dimmed"),
                                ],
                                gap=8,
                            ),
                        ],
                        justify="space-between",
                        h=60,
                    ),
                    size="xl",
                ),
                className="mj-header",
            ),
            dmc.Container(html.Div(id="page"), size="xl", py="xl"),
        ],
        theme=THEME,
        forceColorScheme="dark",
    )

    @app.callback(Output("page", "children"), Input("url", "pathname"))
    def route(pathname):
        if pathname and pathname.startswith("/jobs/"):
            return html.Div(id="job-detail")
        return jobs_page()

    @app.callback(
        Output("jobs-table", "children"),
        Input("refresh", "n_intervals"),
        Input("status-filter", "value"),
        Input("name-filter", "value"),
        Input("label-filter", "value"),
    )
    def update_jobs(_, status, name, labels):
        try:
            labels = parse_label_filters(labels)
        except ValueError as e:
            return dmc.Text(str(e), c="modal-red.5", py="xl", ta="center")
        records = get_registry().list_jobs.remote(
            None, None if status == "all" else status, labels=labels or None
        )
        if name:
            records = [record for record in records if name.lower() in record["name"].lower()]
        return dmc.Stack(
            [
                stats_view(records),
                dmc.Paper(jobs_view(records[:MAX_JOBS]), withBorder=True, bg="dark.8"),
            ],
            gap="md",
        )

    @app.callback(
        Output("job-detail", "children"),
        Input("refresh", "n_intervals"),
        Input("url", "pathname"),
    )
    def update_job(_, pathname):
        job_id = pathname.removeprefix("/jobs/")
        try:
            record = get_registry().get_job.remote(job_id)
        except KeyError:
            return not_found(f"No job found with ID {job_id!r}.")
        except ValueError as e:
            return not_found(str(e))
        try:
            log = read_log(record["id"])
        except FileNotFoundError:
            log = None
        return job_view(record, log)

    return app
