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
    format_duration,
    format_labels,
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

FONTS = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600"
    "&family=Fira+Mono:wght@400;500&display=swap"
)

# Fields shown elsewhere on the job page.
HEADER_FIELDS = {"ID", "Name", "Status", "Error", "Command"}


def status_badge(status: str):
    return dmc.Badge(
        status.replace("_", " "),
        color=STATUS_COLORS.get(status, "gray"),
        variant="light",
        radius="sm",
        tt="none",
        ff="monospace",
    )


def mono(text: str, **kwargs):
    return dmc.Text(text, ff="monospace", size="sm", **kwargs)


def jobs_view(records: list[dict], now: float | None = None):
    """Return the table of the jobs `records`."""
    if not records:
        return dmc.Text("No jobs found.", c="dimmed", py="xl", ta="center")
    now = time.time() if now is None else now
    headers = ["ID", "Name", "Status", "Submitted", "Duration", "GPU", "Labels"]
    rows = [
        dmc.TableTr(
            [
                dmc.TableTd(
                    dmc.Anchor(
                        record["id"], href=f"/jobs/{record['id']}", ff="monospace", size="sm"
                    )
                ),
                dmc.TableTd(dmc.Text(record["name"], size="sm", truncate="end", maw=320)),
                dmc.TableTd(status_badge(record["status"])),
                dmc.TableTd(
                    mono(f"{format_duration(now - record['submitted_at'])} ago", c="dimmed")
                ),
                dmc.TableTd(mono(record_duration(record, now), c="dimmed")),
                dmc.TableTd(mono(record.get("gpu") or "-", c="dimmed")),
                dmc.TableTd(mono(format_labels(record), c="dimmed", truncate="end", maw=240)),
            ]
        )
        for record in records
    ]
    return dmc.TableScrollContainer(
        dmc.Table(
            [
                dmc.TableThead(
                    dmc.TableTr(
                        [dmc.TableTh(dmc.Text(h, size="xs", c="dimmed", fw=500)) for h in headers]
                    )
                ),
                dmc.TableTbody(rows),
            ],
            highlightOnHover=True,
            verticalSpacing="sm",
            horizontalSpacing="md",
        ),
        minWidth=880,
    )


def tail_log(log: bytes) -> str:
    """Decode the end of `log`, noting when the start was cut off."""
    if len(log) <= LOG_TAIL_BYTES:
        return log.decode(errors="replace")
    tail = log[-LOG_TAIL_BYTES:].decode(errors="replace")
    return f"... showing the last {LOG_TAIL_BYTES // 1024} KiB ...\n{tail}"


def job_view(record: dict, log: bytes | None):
    """Return the page of the job `record`, whose saved output is `log`, if any."""
    all_fields = record_fields(record)
    fields = [(label, value) for label, value in all_fields if label not in HEADER_FIELDS]
    if log is not None:
        log_body = dmc.Code(tail_log(log) or "(no output)", block=True, fz="xs")
    elif record["status"] == RUNNING:
        log_body = dmc.Text(
            [
                "The output is saved when the job finishes. Stream it with ",
                dmc.Code(f"modal-jobs logs --follow {record['id']}"),
            ],
            c="dimmed",
            size="sm",
        )
    else:
        log_body = dmc.Text("No saved output.", c="dimmed", size="sm")
    return dmc.Stack(
        [
            dmc.Anchor("← All jobs", href="/", size="sm"),
            dmc.Group(
                [dmc.Title(record["name"], order=2), status_badge(record["status"])],
                gap="sm",
            ),
            mono(record["id"], c="dimmed"),
            *(
                [dmc.Alert(record["error"], color="modal-red", variant="light", title="Error")]
                if record.get("error")
                else []
            ),
            section("Command", dmc.Code(dict(all_fields)["Command"], block=True)),
            section(
                "Details",
                dmc.SimpleGrid(
                    [
                        dmc.Stack(
                            [dmc.Text(label, size="xs", c="dimmed"), mono(value)],
                            gap=2,
                        )
                        for label, value in fields
                    ],
                    cols={"base": 1, "sm": 2, "md": 3},
                    spacing="lg",
                    verticalSpacing="md",
                ),
            ),
            section("Output", log_body),
        ],
        gap="md",
    )


def section(title: str, body):
    return dmc.Paper(
        dmc.Stack([dmc.Text(title, size="sm", fw=500), body], gap="sm"),
        withBorder=True,
        p="lg",
        bg="dark.8",
    )


def not_found(message: str):
    return dmc.Stack(
        [dmc.Anchor("← All jobs", href="/", size="sm"), dmc.Text(message, c="dimmed")],
        gap="md",
    )


def jobs_page():
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Select(
                        id="status-filter",
                        value="all",
                        data=[{"value": "all", "label": "all statuses"}]
                        + [{"value": s, "label": s.replace("_", " ")} for s in STATUSES],
                        # Keep a status selected, so the filter is never empty.
                        allowDeselect=False,
                        size="xs",
                        w=160,
                    ),
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id="label-filter",
                                placeholder="Filter by label, e.g. team=ml exp",
                                size="xs",
                                w=240,
                                debounce=300,
                            ),
                            dmc.TextInput(
                                id="name-filter",
                                placeholder="Filter by name",
                                size="xs",
                                w=240,
                                debounce=300,
                            ),
                        ],
                        gap="sm",
                    ),
                ],
                justify="space-between",
            ),
            dmc.Paper(html.Div(id="jobs-table"), withBorder=True, bg="dark.8"),
        ],
        gap="md",
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
    app.layout = dmc.MantineProvider(
        [
            dcc.Location(id="url"),
            dcc.Interval(id="refresh", interval=REFRESH_MS),
            dmc.Box(
                dmc.Container(
                    dmc.Group(
                        [
                            dmc.Anchor(
                                dmc.Text("modal-jobs", ff="monospace", fw=500, c="dark.0"),
                                href="/",
                                underline="never",
                            ),
                            dmc.Text("Jobs", size="sm", c="dimmed"),
                        ],
                        gap="lg",
                        h=56,
                    ),
                    size="xl",
                ),
                style={"borderBottom": "1px solid var(--mantine-color-dark-5)"},
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
        return jobs_view(records[:MAX_JOBS])

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
