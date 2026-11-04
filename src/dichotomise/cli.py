"""The `dichotomise` command: reads the flags, runs the pipeline, and reports the result."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any, TypeVar

import rich_click as click
import rich_click.rich_click as rich_click_config
from rich.console import Group
from rich.live import Live
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich_click.rich_command import RichCommand

from dichotomise.errors import DichotomiseError
from dichotomise.pipeline import run_pipeline
from dichotomise.run import Run
from dichotomise.stages.audit import (
    AuditedFile,
    AuditResult,
    _folder_summaries,
    _FolderSummary,
    audit_directory,
    folder_notes,
)
from dichotomise.utils.archive import pigz_path
from dichotomise.utils.console import console, error, success

_T = TypeVar("_T")

rich_click_config.OPTION_GROUPS = {
    "dichotomise": [
        {
            "name": "Input (required)",
            "options": ["--source-dir"],
            "title_style": "bold red",
        },
        {
            "name": "Output (standard runs)",
            "options": ["--out-dir"],
            "title_style": "bold red",
        },
        {
            "name": "QC mode",
            "options": ["--qc"],
            "title_style": "bold yellow",
        },
        {
            "name": "Optional flags",
            "options": [
                "--sanitise",
                "--sanitise-policy",
                "--subj-id",
                "--new-id",
                "--random-name",
                "--mapping",
                "--mapping-file",
                "--keep-working-files",
                "--keep-unzipped",
                "--help",
            ],
            "title_style": "bold",
        },
    ]
}
rich_click_config.OPTIONS_TABLE_COLUMN_TYPES = ["opt_short", "opt_long", "metavar", "help"]
rich_click_config.OPTIONS_TABLE_HELP_SECTIONS = ["help", "deprecated", "envvar", "default"]
rich_click_config.TEXT_MARKUP = "rich"

_EXAMPLE_COMMAND = (
    "[bold white]dichotomise[/] "
    "[bold cyan]--source-dir[/] [green]/path/to/scanner_export[/] "
    "[bold cyan]--out-dir[/] [green]/path/to/fixed_export[/]"
)


@dataclass(frozen=True)
class ProcessingOptions:
    """Validated pipeline options assembled from the command-line flags."""

    sanitise: bool
    sanitise_policy: str
    subject_id: str | None
    new_id: str | None
    label_mode: str
    subject_mappings: dict[str, str] | None


class DichotomiseCommand(RichCommand):
    """Render the command's usage notes in the same Rich style as its options."""

    def format_epilog(self, context: Any, formatter: Any) -> None:
        if self.epilog is None:
            return
        single_subject = Panel(
            Text.from_markup(
                f"[bold cyan]Numerical:[/] {_EXAMPLE_COMMAND} "
                "[bold cyan]--sanitise[/] [bold cyan]--subj-id[/] [magenta]6[/]\n"
                "           Creates [green]sub-0006[/].\n\n"
                f"[bold cyan]Random:[/] {_EXAMPLE_COMMAND} [bold cyan]--random-name[/]\n\n"
                f"[bold cyan]Exact:[/] {_EXAMPLE_COMMAND} "
                "[bold cyan]--sanitise[/] [bold cyan]--new-id[/] [magenta]ADNC0751[/]\n"
                "       Creates [green]sub-ADNC0751[/]."
            ),
            title=Text.from_markup("[bold yellow]Single-Subject mode[/]"),
            title_align="left",
            border_style="yellow",
            padding=(0, 1),
        )
        multi_subject = Panel(
            Text.from_markup(
                f"[bold cyan]Numerical:[/] {_EXAMPLE_COMMAND} "
                "[bold cyan]--sanitise[/] [bold cyan]--subj-id[/] [magenta]6[/]\n"
                "           Creates [green]sub-0006[/], [green]sub-0007[/], [green]sub-0008[/], …\n"
                "           [yellow]Warning:[/] enumeration is enabled automatically.\n\n"
                f"[bold cyan]Random:[/] {_EXAMPLE_COMMAND} [bold cyan]--random-name[/]\n\n"
                f"[bold cyan]Mapped:[/] {_EXAMPLE_COMMAND} "
                "[bold cyan]--sanitise[/] [bold cyan]--mapping-file[/] "
                "[green]/path/to/mapping_file.json[/]"
            ),
            title=Text.from_markup("[bold yellow]Multi-Subject mode[/]"),
            title_align="left",
            border_style="yellow",
            padding=(0, 1),
        )
        qc_mode = Panel(
            Text.from_markup(
                "[bold white]dichotomise[/] [bold cyan]--qc[/] "
                "[bold cyan]--source-dir[/] [green]/path/to/scanner_export[/]\n"
                "Prints the DICOM audit table only; no files or folders are created."
            ),
            title=Text.from_markup("[bold yellow]QC mode[/]"),
            title_align="left",
            border_style="yellow",
            padding=(0, 1),
        )
        content = Group(
            Text.from_markup("[bold orange1]Usage Guide:[/]"),
            Padding(single_subject, (1, 0, 0, 0)),
            Padding(multi_subject, (1, 0, 0, 0)),
            Padding(qc_mode, (1, 0, 0, 0)),
        )
        formatter.write(Padding(content, formatter.config.padding_epilog))


def _package_version() -> str:
    """Return the installed dichotomise version, or "dev" outside an installed package."""
    try:
        return _installed_version("dichotomise")
    except PackageNotFoundError:
        return "dev"


def _print_banner(source_dir: Path) -> None:
    """Print the run's opening banner: version, then the source path.

    The output line is printed separately, once it is known: either
    `_print_qc_mode_line()` immediately for a `--qc` run (which creates no
    output directory), or `_print_output_line()` once `run_pipeline()`
    reports the actual timestamped run folder it created.
    """
    title = f"dichotomise v{_package_version()}"
    width = max(len(title) + 4, 45)
    border = "%" + "-" * (width - 2) + "%"
    console.print(f"[bold cyan]{border}[/bold cyan]")
    console.print(f"[bold cyan]{title.center(width)}[/bold cyan]")
    console.print(f"[bold cyan]{border}[/bold cyan]")
    # soft_wrap: a long path must never be broken across lines, on a narrow
    # terminal or Rich's 80-column default for non-interactive output alike.
    console.print(f"[bold]Source:[/bold] {source_dir}", soft_wrap=True)


def _print_qc_mode_line() -> None:
    """Print the banner's second line for a `--qc` run, which writes nothing."""
    console.print("[bold yellow]QC Mode[/bold yellow]")
    console.print()


def _print_output_line(run: Run) -> None:
    """Print the banner's second line once the run's actual output folder exists."""
    console.print(f"[bold]Output:[/bold] {run.root}", soft_wrap=True)
    console.print()


def _format_elapsed(seconds: float) -> str:
    """Format a stage duration for a concise CLI progress message."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(int(seconds), 60)
    return f"{minutes}m {remaining_seconds}s"


def _run_with_elapsed_ticker(run_started: float, work: Callable[[], _T]) -> _T:
    """Run `work()`, showing a continuously updating "Time elapsed" line meanwhile.

    Only draws the ticker on an interactive terminal, for the same reason
    as `_log_archive_progress`: a redirected/CI log has no use for a line
    that rewrites itself, and `work()` still runs normally without it.
    `work` runs on the main thread so keyboard interrupts reach the pipeline.
    A background thread updates the elapsed-time display.
    """
    if not console.is_terminal:
        return work()

    stopped = Event()
    with Live(console=console, refresh_per_second=4, transient=True) as live:

        def update() -> None:
            while not stopped.is_set():
                elapsed = _format_elapsed(monotonic() - run_started)
                live.update(Text(f"Time elapsed: {elapsed}", style="dim"))
                stopped.wait(0.2)

        thread = Thread(target=update)
        thread.start()
        try:
            return work()
        finally:
            stopped.set()
            thread.join()


def _log_stage(status: str, description: str, elapsed_seconds: float) -> None:
    """Render the pipeline's timestamped stage progress in the v1 CLI style."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if status == "started":
        message = f"Started: {description}"
    elif status == "completed":
        message = f"Completed: {description} in {_format_elapsed(elapsed_seconds)}"
    else:
        message = f"Failed: {description} after {_format_elapsed(elapsed_seconds)}"
    console.print(f"[dim]{timestamp}[/] {message}")


def _log_warning(message: str) -> None:
    """Render a timestamped warning alongside verbose pipeline progress."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    console.print(f"[dim]{timestamp}[/] [yellow]Warning:[/] {message}")


def _log_info(message: str) -> None:
    """Render a timestamped informational note alongside verbose pipeline progress."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    console.print(f"[dim]{timestamp}[/] {message}")


def _format_kb(num_bytes: int) -> str:
    """Format a byte count as whole kilobytes, for a compact progress readout."""
    return f"{num_bytes / 1024:,.0f}KB"


def _log_archive_progress(description: str, done: int, total: int) -> None:
    """Render an updating, single-line byte progress readout for one archive.

    Only drawn on an interactive terminal: a redirected/CI log would just
    accumulate every update as its own line, which is noise, not progress.
    """
    if not console.is_terminal or total <= 0:
        return
    line = f"  {description}: {_format_kb(done)}/{_format_kb(total)}"
    end = "\n" if done >= total else ""
    console.file.write(f"\r{line}{end}")
    console.file.flush()


def _audit_reasons(summary: _FolderSummary) -> list[str]:
    """Return the per-folder data-quality and structural findings."""
    reasons = [f"{field} varies" for field in summary.inconsistencies]
    duplicate_count = summary.duplicate_count
    misfiled_count = summary.misfiled_count
    conflict_count = summary.conflict_count
    if duplicate_count:
        suffix = "file" if duplicate_count == 1 else "files"
        reasons.append(f"{duplicate_count} duplicate {suffix}")
    if conflict_count:
        suffix = "file" if conflict_count == 1 else "files"
        reasons.append(f"{conflict_count} SOP-UID conflict {suffix}")
    if misfiled_count:
        suffix = "file" if misfiled_count == 1 else "files"
        reasons.append(f"{misfiled_count} misfiled {suffix}")
    return reasons


def _latest_modified(files: list[AuditedFile]) -> str:
    """Return the most recent filesystem timestamp in a readable UTC form."""
    modified_time = max(file.metadata.path.stat().st_mtime for file in files)
    return datetime.fromtimestamp(modified_time, UTC).strftime("%Y-%m-%d %H:%MZ")


def _print_subject_audits(source_dir: Path) -> None:
    """Audit the source directory in place and print each subject's table and NOTES in turn."""
    results = audit_directory(source_dir)
    for result in results:
        subject = result.subject
        console.print(
            f"\n[bold]Subject {escape(subject.subject_id or '(no PatientID)')}[/] "
            f"(study {escape(subject.scan_date or 'date unknown')}, {len(result.files)} DICOMs)"
        )
        _print_audit_table(result)


def _print_audit_table(audit_result: AuditResult) -> None:
    """Render every captured series with its structural and metadata findings."""
    table = Table(title="DICOM inventory", show_lines=False)
    table.add_column("Series folder", overflow="fold")
    table.add_column("DICOMs", justify="right")
    table.add_column("Modified")
    table.add_column("Check")
    table.add_column("Why", overflow="fold")
    for summary in _folder_summaries(audit_result):
        reasons = _audit_reasons(summary)
        table.add_row(
            summary.folder.name,
            str(len(summary.files)),
            _latest_modified(summary.files),
            "[green]Pass[/]" if not reasons else "[red]Failed[/]",
            "; ".join(reasons) if reasons else "—",
        )
    console.print(table)
    _print_audit_notes(audit_result)


def _print_audit_notes(audit_result: AuditResult) -> None:
    """Explain the table's findings in plain language, series folder by series folder."""
    notes = folder_notes(audit_result)
    if not notes:
        return
    console.print("\n[bold]NOTES[/]")
    for note in notes:
        console.print(f"[bold]{escape(note.folder)}[/]")
        for text in note.texts:
            console.print(f"  - {escape(text)}")


def _report_value(report: dict[str, object], key: str) -> str:
    """Return a display value from one of the pipeline's JSON reports."""
    value = report.get(key, "—")
    return str(value)


def _print_run_summary(reports_dir: Path) -> None:
    """Render a compact per-subject audit and archive summary."""
    table = Table(title="Audit and archive summary")
    table.add_column("Subject")
    table.add_column("Files", justify="right")
    table.add_column("Duplicates", justify="right")
    table.add_column("Misfiled", justify="right")
    table.add_column("Retained", justify="right")
    table.add_column("Review", justify="right")
    table.add_column("Archive")
    table.add_column("Review archive")

    for report_dir in sorted(path for path in reports_dir.iterdir() if path.is_dir()):
        audit_report = json.loads((report_dir / "stage-01-report.json").read_text())
        sift_report = json.loads((report_dir / "stage-02-report.json").read_text())
        finalise_report = json.loads((report_dir / "stage-03-report.json").read_text())
        review_archive = sift_report.get("review_archive")
        review_archive_display = "—"
        if review_archive:
            review_archive_display = (
                f"{review_archive} (sanitised)"
                if sift_report.get("review_archive_sanitised")
                else str(review_archive)
            )
        table.add_row(
            _report_value(finalise_report, "subject_label"),
            _report_value(audit_report, "total_files"),
            _report_value(audit_report, "duplicate_count"),
            _report_value(audit_report, "misfiled_count"),
            _report_value(sift_report, "retained_count"),
            _report_value(sift_report, "review_count"),
            _report_value(finalise_report, "archive"),
            review_archive_display,
        )

    console.print(Panel(table, title="Dichotomise complete", border_style="green"))


def _infer_label_mode(subject_id: str | None, new_id: str | None) -> str:
    """Work out the replacement-label mode from the selected label source."""
    if new_id:
        return "custom"
    if subject_id:
        return "numerical"
    return "default"


def _add_mapping(mapping: dict[str, str], source_id: str, replacement_id: str) -> None:
    if not source_id or not replacement_id:
        raise click.UsageError("Each mapping must contain both a source ID and a replacement ID.")
    if source_id in mapping:
        raise click.UsageError(f"A replacement ID was supplied more than once for {source_id!r}.")
    mapping[source_id] = replacement_id


def _parse_inline_mappings(entries: tuple[str, ...]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in entries:
        for pair in entry.split(","):
            source_id, separator, replacement_id = pair.partition(":")
            if not separator:
                raise click.UsageError(
                    "Each --mapping value must be written as patient_id:replacement_id."
                )
            _add_mapping(mapping, source_id, replacement_id)
    return mapping


def _load_mapping_file(path: Path) -> dict[str, str]:
    resolved_path = path if path.is_file() else path.with_suffix(".json")
    if not resolved_path.is_file():
        raise click.UsageError(f"No JSON mapping file found at {path} or {resolved_path}.")
    try:
        contents = json.loads(resolved_path.read_text())
    except json.JSONDecodeError as error:
        raise click.UsageError(f"{resolved_path} is not valid JSON.") from error
    if not isinstance(contents, Mapping):
        raise click.UsageError(
            "A JSON mapping file must be an object of PatientID to replacement ID."
        )
    loaded_mapping: dict[str, str] = {}
    for source_id, replacement_id in contents.items():
        if source_id == "instructions":
            if not isinstance(replacement_id, list) or not all(
                isinstance(instruction, str) for instruction in replacement_id
            ):
                raise click.UsageError(
                    'The optional "instructions" entry must be a list of strings.'
                )
            continue
        if not isinstance(source_id, str) or not isinstance(replacement_id, str):
            raise click.UsageError("A JSON mapping file must contain string IDs only.")
        _add_mapping(loaded_mapping, source_id, replacement_id)
    return loaded_mapping


def _run_qc(
    source_dir: Path,
    out_dir: Path | None,
    sanitise: bool,
    sanitise_policy: str | None,
    subject_id: str | None,
    new_id: str | None,
    random_name: bool,
    mapping: tuple[str, ...],
    mapping_file: Path | None,
    keep_working_files: bool,
    keep_unzipped: bool,
) -> None:
    """Print an in-place audit after ensuring no processing option was selected."""
    incompatible_options = (
        out_dir is not None,
        sanitise,
        sanitise_policy is not None,
        subject_id is not None,
        new_id is not None,
        random_name,
        bool(mapping),
        mapping_file is not None,
        keep_working_files,
        keep_unzipped,
    )
    if any(incompatible_options):
        raise click.UsageError(
            "--qc only accepts --source-dir and cannot be combined with processing options."
        )
    _print_banner(source_dir)
    _print_qc_mode_line()
    qc_started = monotonic()
    try:
        _run_with_elapsed_ticker(qc_started, lambda: _print_subject_audits(source_dir))
    except DichotomiseError as failure:
        error(str(failure))
        raise SystemExit(1) from failure
    total_elapsed = _format_elapsed(monotonic() - qc_started)
    success(f"QC complete in {total_elapsed}; no files or folders were created.")


def _prepare_processing_options(
    sanitise: bool,
    sanitise_policy: str | None,
    subject_id: str | None,
    new_id: str | None,
    random_name: bool,
    mapping: tuple[str, ...],
    mapping_file: Path | None,
) -> ProcessingOptions:
    """Validate and normalise options used by a standard pipeline run."""
    if random_name and sanitise_policy is not None:
        raise click.UsageError("--random-name cannot be combined with --sanitise-policy.")
    sanitise = sanitise or sanitise_policy is not None or random_name
    label_sources = sum(
        (subject_id is not None, new_id is not None, bool(mapping), mapping_file is not None)
    )
    if label_sources > 1:
        raise click.UsageError(
            "Choose only one of --subj-id, --new-id, --mapping, or --mapping-file."
        )
    if not sanitise and label_sources:
        raise click.UsageError("Replacement-label options require --sanitise or --sanitise-policy.")

    effective_policy = sanitise_policy or "minimal"
    subject_mappings = _parse_inline_mappings(mapping) if mapping else None
    if mapping_file is not None:
        subject_mappings = _load_mapping_file(mapping_file)
    label_mode = "random" if random_name else _infer_label_mode(subject_id, new_id)
    if sanitise and label_sources == 0:
        if effective_policy == "minimal":
            label_mode = "random"
        else:
            label_mode = "numerical"
            subject_id = "1"
    return ProcessingOptions(
        sanitise=sanitise,
        sanitise_policy=effective_policy,
        subject_id=subject_id,
        new_id=new_id,
        label_mode=label_mode,
        subject_mappings=subject_mappings,
    )


@click.command(
    cls=DichotomiseCommand,
    help=(
        "A command-line tool for auditing, fixing, and archiving DICOM exports from Siemens "
        "XA60+ systems."
    ),
    epilog=(
        "[bold]Basic run[/]: dichotomise --source-dir /path/to/scanner_export "
        "--out-dir /path/to/fixed_export\n\n"
        "[bold]Relabelled run[/]: dichotomise --source-dir /path/to/scanner_export "
        "--out-dir /path/to/fixed_export "
        "--sanitise --subj-id 1\n\n"
        "The audit table is shown before fixing and classification. Failed series are copied to "
        "review/ rather than be quietly discarded."
    ),
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--source-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    required=True,
    panel="Input (required)",
    help="Input directory to be processed. Can be single- or multi-subject directory.",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path, file_okay=False),
    panel="Output (standard runs)",
    help="Output directory for dichotomised results; required unless --qc is used.",
)
@click.option(
    "--qc",
    is_flag=True,
    panel="QC mode",
    help=(
        "Audit source DICOMs in place and print the audit table and NOTES only; never write files."
    ),
)
@click.option(
    "--sanitise",
    is_flag=True,
    panel="Optional flags",
    help="Do some anonymisation before final archiving.",
)
@click.option(
    "--sanitise-policy",
    "sanitise_policy",
    default=None,
    panel="Optional flags",
    help=(
        "Use JSON policy presets: minimal (default), standard, full, retain.\n\n"
        "Custom policy names may be given with or without .json."
    ),
)
@click.option(
    "--subj-id",
    "subject_id",
    default=None,
    panel="Optional flags",
    help="Numerical replacement ID; 6 produces sub-0006.",
)
@click.option(
    "--new-id",
    default=None,
    panel="Optional flags",
    help="Replacement ID for one study; ADNC0751 becomes sub-ADNC0751.",
)
@click.option(
    "--random-name",
    is_flag=True,
    panel="Optional flags",
    help="Generate a random replacement name using the minimal sanitisation policy.",
)
@click.option(
    "--mapping",
    multiple=True,
    panel="Optional flags",
    help="PatientID:replacement_id pair; repeat the flag or separate pairs with commas.",
)
@click.option(
    "--mapping-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    panel="Optional flags",
    help="JSON file mapping PatientID to replacement ID; .json may be omitted.",
)
@click.option(
    "--keep-working-files",
    is_flag=True,
    panel="Optional flags",
    help=(
        "Keep the rectified (and sanitised) DICOM files under working/, instead of "
        "deleting them once archived. Warning: this uses a lot of disk space -- a full "
        "second copy of every subject's files, on top of the archive."
    ),
)
@click.option(
    "--keep-unzipped",
    is_flag=True,
    panel="Optional flags",
    help=(
        "Also keep each study's final (optionally sanitised) files unarchived, in "
        "final/ -- e.g. for a later BIDS conversion that wants plain files, not a tarball."
    ),
)
def cli(
    source_dir: Path,
    out_dir: Path | None,
    qc: bool,
    sanitise: bool,
    sanitise_policy: str | None,
    subject_id: str | None,
    new_id: str | None,
    random_name: bool,
    mapping: tuple[str, ...],
    mapping_file: Path | None,
    keep_working_files: bool,
    keep_unzipped: bool,
) -> None:
    """Run the standard, end-to-end dichotomise pipeline."""
    if qc:
        _run_qc(
            source_dir,
            out_dir,
            sanitise,
            sanitise_policy,
            subject_id,
            new_id,
            random_name,
            mapping,
            mapping_file,
            keep_working_files,
            keep_unzipped,
        )
        return

    if out_dir is None:
        raise click.UsageError("--out-dir is required unless --qc is used.")
    options = _prepare_processing_options(
        sanitise,
        sanitise_policy,
        subject_id,
        new_id,
        random_name,
        mapping,
        mapping_file,
    )

    _print_banner(source_dir)
    if pigz_path() is not None:
        _log_info("pigz found, using it.")
    if keep_working_files:
        _log_warning(
            "--keep-working-files uses a lot of disk space: a full second copy of "
            "every subject's files, on top of the archive."
        )
    run_started = monotonic()
    try:
        run = _run_with_elapsed_ticker(
            run_started,
            lambda: run_pipeline(
                source_dir,
                out_dir,
                sanitise_requested=options.sanitise,
                sanitise_level=options.sanitise_policy,
                label_mode=options.label_mode,
                subject_id=options.subject_id,
                new_id=options.new_id,
                subject_mappings=options.subject_mappings,
                keep_working_files=keep_working_files,
                keep_unzipped=keep_unzipped,
                on_run_start=_print_output_line,
                on_audit=_print_audit_table,
                on_stage=_log_stage,
                on_warning=_log_warning,
                on_info=_log_info,
                on_archive_progress=_log_archive_progress,
            ),
        )
    except DichotomiseError as failure:
        error(str(failure))
        raise SystemExit(1) from failure

    total_elapsed = _format_elapsed(monotonic() - run_started)
    success(f"Finished: {run.root} (total time {total_elapsed})")
    _print_run_summary(run.reports_dir)
    for archive in sorted(run.archives_dir.glob("*.tar.gz")):
        console.print(f"  [green]✓[/green] {archive.name}")
