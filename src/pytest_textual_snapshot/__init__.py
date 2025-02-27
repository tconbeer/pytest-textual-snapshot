from __future__ import annotations
import re
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from operator import attrgetter
from os import PathLike
from pathlib import Path, PurePath
from typing import (
    Awaitable,
    Union,
    List,
    Optional,
    Callable,
    Iterable,
    TYPE_CHECKING,
    Dict,
    Tuple,
)

import pytest
from _pytest.config import ExitCode
from _pytest.fixtures import FixtureRequest
from _pytest.main import Session
from _pytest.terminal import TerminalReporter
from jinja2 import Template
from rich.console import Console
from syrupy import SnapshotAssertion
from syrupy.extensions.image import SVGImageSnapshotExtension
import hashlib

from textual.app import App

if TYPE_CHECKING:
    from textual.pilot import Pilot

TEXTUAL_SNAPSHOT_SVG_KEY = pytest.StashKey[str]()
TEXTUAL_ACTUAL_SVG_KEY = pytest.StashKey[str]()
TEXTUAL_SNAPSHOT_PASS = pytest.StashKey[bool]()

SNAPSHOT_RESULTS = pytest.StashKey[Dict[str, Tuple[bool, App, str, str]]]()


def pytest_addoption(parser):
    parser.addoption(
        "--snapshot-report",
        action="store",
        default="snapshot_report.html",
        help="Snapshot test output HTML path.",
    )


def app_stash_key() -> pytest.StashKey:
    try:
        return app_stash_key._key
    except AttributeError:
        from textual.app import App

        app_stash_key._key = pytest.StashKey[App]()
    return app_stash_key()

def _hash(s: str) -> str:
    return hashlib.md5(s.encode('utf-8')).hexdigest()


@pytest.fixture
def snap_compare(
    snapshot: SnapshotAssertion, request: FixtureRequest
) -> Callable[[str | PurePath], bool]:
    """
    This fixture returns a function which can be used to compare the output of a Textual
    app with the output of the same app in the past. This is snapshot testing, and it
    used to catch regressions in output.
    """

    def compare(
        app_path: str | PurePath,
        press: Iterable[str] = (),
        terminal_size: tuple[int, int] = (80, 24),
        run_before: Callable[[Pilot], Awaitable[None] | None] | None = None,
    ) -> bool:
        """
        Compare a current screenshot of the app running at app_path, with
        a previously accepted (validated by human) snapshot stored on disk.
        When the `--snapshot-update` flag is supplied (provided by syrupy),
        the snapshot on disk will be updated to match the current screenshot.

        Args:
            app_path (str): The path of the app. Relative paths are relative to the location of the
                test this function is called from.
            press (Iterable[str]): Key presses to run before taking screenshot. "_" is a short pause.
            terminal_size (tuple[int, int]): A pair of integers (WIDTH, HEIGHT), representing terminal size.
            run_before: An arbitrary callable that runs arbitrary code before taking the
                screenshot. Use this to simulate complex user interactions with the app
                that cannot be simulated by key presses.

        Returns:
            Whether the screenshot matches the snapshot.
        """
        from textual._import_app import import_app

        node = request.node
        path = Path(app_path)
        if path.is_absolute():
            # If the user supplies an absolute path, just use it directly.
            app = import_app(str(path.resolve()))
        else:
            # If a relative path is supplied by the user, it's relative to the location of the pytest node,
            # NOT the location that `pytest` was invoked from.
            node_path = node.path.parent
            resolved = (node_path / app_path).resolve()
            app = import_app(str(resolved))

        from textual._doc import take_svg_screenshot

        actual_screenshot = take_svg_screenshot(
            app=app,
            press=press,
            terminal_size=terminal_size,
            run_before=run_before,
        )
        result = snapshot == actual_screenshot

        if result is False:
            # The split and join below is a mad hack, sorry...
            node.stash[TEXTUAL_SNAPSHOT_SVG_KEY] = "\n".join(
                str(snapshot).splitlines()[1:-1]
            )
            node.stash[TEXTUAL_ACTUAL_SVG_KEY] = actual_screenshot
            node.stash[app_stash_key()] = app
        else:
            node.stash[TEXTUAL_SNAPSHOT_PASS] = True

        return result

    return compare


@pytest.fixture
def app_snapshot(
    snapshot: SnapshotAssertion, request: FixtureRequest
) -> Callable[[App, Optional[str]], Awaitable[bool]]:
    snapshot.use_extension(SVGImageSnapshotExtension)

    async def compare(app: App, name: Optional[str] = None) -> bool:
        if name == "snapshot":
            raise ValueError("cannot name a snapshot 'snapshot'!")
        key = name if name is not None else "snapshot"
        node = request.node
        # take a snapshot; retry twice, with sleeps to prevent false positives
        result = False
        sleeps = [0.5, 0.1, 0]
        while not result and sleeps:
            await asyncio.sleep(sleeps.pop())
            actual_screenshot = app.export_screenshot()
            classname_pattern = r"terminal-\d+"
            classname_placeholder = f"terminal-{_hash(node.nodeid)}-{_hash(key)}"
            normalized_screenshot = re.sub(
                classname_pattern, classname_placeholder, actual_screenshot
            )
            result = normalized_screenshot == snapshot(name=name)

        results = node.stash.get(SNAPSHOT_RESULTS, {})
        if result is False:
            n = snapshot.num_executions
            historical_screenshot = str(snapshot.executions[n - 1].recalled_data)
            results.update(
                {key: (False, app, normalized_screenshot, historical_screenshot)}
            )
        else:
            results.update({key: (True, app, "", "")})
        node.stash[SNAPSHOT_RESULTS] = results
        return result

    return compare


@dataclass
class SvgSnapshotDiff:
    """Model representing a diff between current screenshot of an app,
    and the snapshot on disk. This is ultimately intended to be used in
    a Jinja2 template."""

    snapshot: Optional[str]
    actual: Optional[str]
    test_name: str
    path: PathLike
    line_number: int
    app: App
    environment: dict


def pytest_sessionfinish(
    session: Session,
    exitstatus: Union[int, ExitCode],
) -> None:
    """Called after whole test run finished, right before returning the exit status to the system.
    Generates the snapshot report and writes it to disk.
    """
    diffs: List[SvgSnapshotDiff] = []
    num_snapshots_passing = 0

    for item in session.items:
        path, line_index, name = item.reportinfo()
        # Grab the data our fixture attached to the pytest node
        if SNAPSHOT_RESULTS in item.stash:
            for snap_name, result in item.stash[SNAPSHOT_RESULTS].items():
                num_snapshots_passing += int(result[0])
                app = result[1]
                actual_svg = result[2]
                classname_pattern = r"terminal-[0-9a-f]+-[0-9a-f]+"
                adjusted_actual_svg = re.sub(
                    classname_pattern, lambda m: f"{m.group()}-new", actual_svg
                )
                snapshot_svg = result[3]
                if not result[0]:
                    diffs.append(
                        SvgSnapshotDiff(
                            snapshot=snapshot_svg,
                            actual=adjusted_actual_svg,
                            test_name=f"{name} : {snap_name}"
                            if snap_name != "snapshot"
                            else "",
                            path=path,
                            line_number=line_index + 1,
                            app=app,
                            environment=dict(os.environ),
                        )
                    )
        else:
            num_snapshots_passing += int(item.stash.get(TEXTUAL_SNAPSHOT_PASS, False))
            snapshot_svg = item.stash.get(TEXTUAL_SNAPSHOT_SVG_KEY, None)
            actual_svg = item.stash.get(TEXTUAL_ACTUAL_SVG_KEY, None)
            app = item.stash.get(app_stash_key(), None)

            if app:
                diffs.append(
                    SvgSnapshotDiff(
                        snapshot=str(snapshot_svg),
                        actual=str(actual_svg),
                        test_name=name,
                        path=path,
                        line_number=line_index + 1,
                        app=app,
                        environment=dict(os.environ),
                    )
                )

    if diffs:
        diff_sort_key = attrgetter("test_name")
        diffs = sorted(diffs, key=diff_sort_key)

        this_file_path = Path(__file__)
        snapshot_template_path = (
            this_file_path.parent / "snapshot_report_template.jinja2"
        )

        snapshot_report_path = session.config.getoption("--snapshot-report")
        snapshot_report_path = Path(snapshot_report_path)
        snapshot_report_path = Path.cwd() / snapshot_report_path
        snapshot_report_path.parent.mkdir(parents=True, exist_ok=True)
        template = Template(snapshot_template_path.read_text())

        num_fails = len(diffs)
        num_snapshot_tests = len(diffs) + num_snapshots_passing

        rendered_report = template.render(
            diffs=diffs,
            passes=num_snapshots_passing,
            fails=num_fails,
            pass_percentage=100 * (num_snapshots_passing / max(num_snapshot_tests, 1)),
            fail_percentage=100 * (num_fails / max(num_snapshot_tests, 1)),
            num_snapshot_tests=num_snapshot_tests,
            now=datetime.now(timezone.utc),
        )
        with open(snapshot_report_path, "w+", encoding="utf-8") as snapshot_file:
            snapshot_file.write(rendered_report)

        session.config._textual_snapshots = diffs
        session.config._textual_snapshot_html_report = snapshot_report_path


def pytest_terminal_summary(
    terminalreporter: TerminalReporter,
    exitstatus: ExitCode,
    config: pytest.Config,
) -> None:
    """Add a section to terminal summary reporting.
    Displays the link to the snapshot report that was generated in a prior hook.
    """
    diffs = getattr(config, "_textual_snapshots", None)
    console = Console(legacy_windows=False, force_terminal=True)
    if diffs:
        snapshot_report_location = config._textual_snapshot_html_report
        console.print("[b red]Textual Snapshot Report", style="red")
        console.print(
            f"\n[black on red]{len(diffs)} mismatched snapshots[/]\n"
            f"\n[b]View the [link=file://{snapshot_report_location}]failure report[/].\n"
        )
        console.print(f"[dim]{snapshot_report_location}\n")
