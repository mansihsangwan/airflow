# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Dag sub-commands."""

from __future__ import annotations

import ast
import errno
import json
import logging
import operator
import signal
import subprocess
import sys
import warnings
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from airflow import settings
from airflow.api.client import get_current_api_client
from airflow.api_connexion.schemas.dag_schema import dag_schema
from airflow.cli.simple_table import AirflowConsole
from airflow.configuration import conf
from airflow.exceptions import AirflowException, RemovedInAirflow3Warning
from airflow.jobs.job import Job
from airflow.models import DagBag, DagModel, DagRun, TaskInstance
from airflow.models.dag import DAG
from airflow.models.serialized_dag import SerializedDagModel
from airflow.utils import cli as cli_utils, timezone
from airflow.utils.cli import get_dag, get_dags, process_subdir, sigint_handler, suppress_logs_and_warning
from airflow.utils.dag_parsing_context import _airflow_parsing_context_manager
from airflow.utils.dot_renderer import render_dag, render_dag_dependencies
from airflow.utils.helpers import ask_yesno
from airflow.utils.providers_configuration_loader import providers_configuration_loaded
from airflow.utils.session import NEW_SESSION, create_session, provide_session
from airflow.utils.state import DagRunState

if TYPE_CHECKING:
    from graphviz.dot import Dot
    from sqlalchemy.orm import Session

    from airflow.timetables.base import DataInterval

log = logging.getLogger(__name__)


def _run_dag_backfill(dags: list[DAG], args) -> None:
    # If only one date is passed, using same as start and end
    args.end_date = args.end_date or args.start_date
    args.start_date = args.start_date or args.end_date

    run_conf = None
    if args.conf:
        run_conf = json.loads(args.conf)

    for dag in dags:
        if args.task_regex:
            dag = dag.partial_subset(
                task_ids_or_regex=args.task_regex, include_upstream=not args.ignore_dependencies
            )
            if not dag.task_dict:
                raise AirflowException(
                    f"There are no tasks that match '{args.task_regex}' regex. Nothing to run, exiting..."
                )

        if args.dry_run:
            print(f"Dry run of DAG {dag.dag_id} on {args.start_date}")
            dagrun_infos = dag.iter_dagrun_infos_between(earliest=args.start_date, latest=args.end_date)
            for dagrun_info in dagrun_infos:
                dr = DagRun(
                    dag.dag_id,
                    execution_date=dagrun_info.logical_date,
                    data_interval=dagrun_info.data_interval,
                )

                for task in dag.tasks:
                    print(f"Task {task.task_id} located in DAG {dag.dag_id}")
                    ti = TaskInstance(task, run_id=None)
                    ti.dag_run = dr
                    ti.dry_run()
        else:
            if args.reset_dagruns:
                DAG.clear_dags(
                    [dag],
                    start_date=args.start_date,
                    end_date=args.end_date,
                    confirm_prompt=not args.yes,
                    include_subdags=True,
                    dag_run_state=DagRunState.QUEUED,
                )

            try:
                dag.run(
                    start_date=args.start_date,
                    end_date=args.end_date,
                    mark_success=args.mark_success,
                    local=args.local,
                    donot_pickle=(args.donot_pickle or conf.getboolean("core", "donot_pickle")),
                    ignore_first_depends_on_past=args.ignore_first_depends_on_past,
                    ignore_task_deps=args.ignore_dependencies,
                    pool=args.pool,
                    delay_on_limit_secs=args.delay_on_limit,
                    verbose=args.verbose,
                    conf=run_conf,
                    rerun_failed_tasks=args.rerun_failed_tasks,
                    run_backwards=args.run_backwards,
                    continue_on_failures=args.continue_on_failures,
                    disable_retry=args.disable_retry,
                )
            except ValueError as vr:
                print(str(vr))
                sys.exit(1)


@cli_utils.action_cli
@providers_configuration_loaded
def dag_backfill(args, dag: list[DAG] | DAG | None = None) -> None:
    """Create backfill job or dry run for a DAG or list of DAGs using regex."""
    logging.basicConfig(level=settings.LOGGING_LEVEL, format=settings.SIMPLE_LOG_FORMAT)
    signal.signal(signal.SIGTERM, sigint_handler)
    if args.ignore_first_depends_on_past:
        warnings.warn(
            "--ignore-first-depends-on-past is deprecated as the value is always set to True",
            category=RemovedInAirflow3Warning,
            stacklevel=4,
        )
    args.ignore_first_depends_on_past = True

    if not args.treat_dag_id_as_regex and args.treat_dag_as_regex:
        warnings.warn(
            "--treat-dag-as-regex is deprecated, use --treat-dag-id-as-regex instead",
            category=RemovedInAirflow3Warning,
            stacklevel=4,
        )
        args.treat_dag_id_as_regex = args.treat_dag_as_regex

    if not args.start_date and not args.end_date:
        raise AirflowException("Provide a start_date and/or end_date")

    if not dag:
        dags = get_dags(args.subdir, dag_id=args.dag_id, use_regex=args.treat_dag_id_as_regex)
    elif isinstance(dag, list):
        dags = dag
    else:
        dags = [dag]
    del dag

    dags.sort(key=lambda d: d.dag_id)
    _run_dag_backfill(dags, args)
    if len(dags) > 1:
        log.info("All of the backfills are done.")


@cli_utils.action_cli
@providers_configuration_loaded
def dag_trigger(args) -> None:
    """Create a dag run for the specified dag."""
    api_client = get_current_api_client()
    try:
        message = api_client.trigger_dag(
            dag_id=args.dag_id,
            run_id=args.run_id,
            conf=args.conf,
            execution_date=args.exec_date,
            replace_microseconds=args.replace_microseconds,
        )
        AirflowConsole().print_as(
            data=[message] if message is not None else [],
            output=args.output,
        )
    except OSError as err:
        raise AirflowException(err)


@cli_utils.action_cli
@providers_configuration_loaded
def dag_delete(args) -> None:
    """Delete all DB records related to the specified dag."""
    api_client = get_current_api_client()
    if (
        args.yes
        or input("This will drop all existing records related to the specified DAG. Proceed? (y/n)").upper()
        == "Y"
    ):
        try:
            message = api_client.delete_dag(dag_id=args.dag_id)
            print(message)
        except OSError as err:
            raise AirflowException(err)
    else:
        print("Cancelled")


@cli_utils.action_cli
@providers_configuration_loaded
def dag_pause(args) -> None:
    """Pauses a DAG."""
    set_is_paused(True, args)


@cli_utils.action_cli
@providers_configuration_loaded
def dag_unpause(args) -> None:
    """Unpauses a DAG."""
    set_is_paused(False, args)


@providers_configuration_loaded
def set_is_paused(is_paused: bool, args) -> None:
    """Set is_paused for DAG by a given dag_id."""
    should_apply = True
    dags = [
        dag
        for dag in get_dags(args.subdir, dag_id=args.dag_id, use_regex=args.treat_dag_id_as_regex)
        if is_paused != dag.get_is_paused()
    ]

    if not dags:
        print(f"No {'un' if is_paused else ''}paused DAGs were found")
        return

    if not args.yes and args.treat_dag_id_as_regex:
        dags_ids = [dag.dag_id for dag in dags]
        question = (
            f"You are about to {'un' if not is_paused else ''}pause {len(dags_ids)} DAGs:\n"
            f"{','.join(dags_ids)}"
            f"\n\nAre you sure? [y/n]"
        )
        should_apply = ask_yesno(question)

    if should_apply:
        dags_models = [DagModel.get_dagmodel(dag.dag_id) for dag in dags]
        for dag_model in dags_models:
            if dag_model is not None:
                dag_model.set_is_paused(is_paused=is_paused)

        AirflowConsole().print_as(
            data=[
                {"dag_id": dag.dag_id, "is_paused": dag.get_is_paused()}
                for dag in dags_models
                if dag is not None
            ],
            output=args.output,
        )
    else:
        print("Operation cancelled by user")


@providers_configuration_loaded
def dag_dependencies_show(args) -> None:
    """Display DAG dependencies, save to file or show as imgcat image."""
    dot = render_dag_dependencies(SerializedDagModel.get_dag_dependencies())
    filename = args.save
    imgcat = args.imgcat

    if filename and imgcat:
        raise SystemExit(
            "Option --save and --imgcat are mutually exclusive. "
            "Please remove one option to execute the command.",
        )
    elif filename:
        _save_dot_to_file(dot, filename)
    elif imgcat:
        _display_dot_via_imgcat(dot)
    else:
        print(dot.source)


@providers_configuration_loaded
def dag_show(args) -> None:
    """Display DAG or saves its graphic representation to the file."""
    dag = get_dag(args.subdir, args.dag_id)
    dot = render_dag(dag)
    filename = args.save
    imgcat = args.imgcat

    if filename and imgcat:
        raise SystemExit(
            "Option --save and --imgcat are mutually exclusive. "
            "Please remove one option to execute the command.",
        )
    elif filename:
        _save_dot_to_file(dot, filename)
    elif imgcat:
        _display_dot_via_imgcat(dot)
    else:
        print(dot.source)


def _display_dot_via_imgcat(dot: Dot) -> None:
    data = dot.pipe(format="png")
    try:
        with subprocess.Popen("imgcat", stdout=subprocess.PIPE, stdin=subprocess.PIPE) as proc:
            out, err = proc.communicate(data)
            if out:
                print(out.decode("utf-8"))
            if err:
                print(err.decode("utf-8"))
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise SystemExit("Failed to execute. Make sure the imgcat executables are on your systems 'PATH'")
        else:
            raise


def _save_dot_to_file(dot: Dot, filename: str) -> None:
    filename_without_ext, _, ext = filename.rpartition(".")
    dot.render(filename=filename_without_ext, format=ext, cleanup=True)
    print(f"File {filename} saved")


def _get_dagbag_dag_details(dag: DAG) -> dict:
    """Return a dagbag dag details dict."""
    return {
        "dag_id": dag.dag_id,
        "dag_display_name": dag.dag_display_name,
        "root_dag_id": dag.parent_dag.dag_id if dag.parent_dag else None,
        "is_paused": dag.get_is_paused(),
        "is_active": dag.get_is_active(),
        "is_subdag": dag.is_subdag,
        "last_parsed_time": None,
        "last_pickled": None,
        "last_expired": None,
        "scheduler_lock": None,
        "pickle_id": dag.pickle_id,
        "default_view": dag.default_view,
        "fileloc": dag.fileloc,
        "file_token": None,
        "owners": dag.owner,
        "description": dag.description,
        "schedule_interval": dag.schedule_interval,
        "timetable_description": dag.timetable.description,
        "tags": dag.tags,
        "max_active_tasks": dag.max_active_tasks,
        "max_active_runs": dag.max_active_runs,
        "max_consecutive_failed_dag_runs": dag.max_consecutive_failed_dag_runs,
        "has_task_concurrency_limits": any(
            t.max_active_tis_per_dag is not None or t.max_active_tis_per_dagrun is not None for t in dag.tasks
        ),
        "has_import_errors": False,
        "next_dagrun": None,
        "next_dagrun_data_interval_start": None,
        "next_dagrun_data_interval_end": None,
        "next_dagrun_create_after": None,
    }


@cli_utils.action_cli
@providers_configuration_loaded
@provide_session
def dag_state(args, session: Session = NEW_SESSION) -> None:
    """
    Return the state (and conf if exists) of a DagRun at the command line.

    >>> airflow dags state tutorial 2015-01-01T00:00:00.000000
    running
    >>> airflow dags state a_dag_with_conf_passed 2015-01-01T00:00:00.000000
    failed, {"name": "bob", "age": "42"}
    """
    dag = DagModel.get_dagmodel(args.dag_id, session=session)

    if not dag:
        raise SystemExit(f"DAG: {args.dag_id} does not exist in 'dag' table")
    dr = session.scalar(select(DagRun).filter_by(dag_id=args.dag_id, execution_date=args.execution_date))
    out = dr.state if dr else None
    conf_out = ""
    if out and dr.conf:
        conf_out = ", " + json.dumps(dr.conf)
    print(str(out) + conf_out)


@cli_utils.action_cli
@providers_configuration_loaded
def dag_next_execution(args) -> None:
    """
    Return the next execution datetime of a DAG at the command line.

    >>> airflow dags next-execution tutorial
    2018-08-31 10:38:00
    """
    dag = get_dag(args.subdir, args.dag_id)

    with create_session() as session:
        last_parsed_dag: DagModel = session.scalars(
            select(DagModel).where(DagModel.dag_id == dag.dag_id)
        ).one()

    if last_parsed_dag.get_is_paused():
        print("[INFO] Please be reminded this DAG is PAUSED now.", file=sys.stderr)

    def print_execution_interval(interval: DataInterval | None):
        if interval is None:
            print(
                "[WARN] No following schedule can be found. "
                "This DAG may have schedule interval '@once' or `None`.",
                file=sys.stderr,
            )
            print(None)
            return
        print(interval.start.isoformat())

    next_interval = dag.get_next_data_interval(last_parsed_dag)
    print_execution_interval(next_interval)

    for _ in range(1, args.num_executions):
        next_info = dag.next_dagrun_info(next_interval, restricted=False)
        next_interval = None if next_info is None else next_info.data_interval
        print_execution_interval(next_interval)


@cli_utils.action_cli
@suppress_logs_and_warning
@providers_configuration_loaded
@provide_session
def dag_list_dags(args, session=NEW_SESSION) -> None:
    """Display dags with or without stats at the command line."""
    cols = args.columns if args.columns else []
    invalid_cols = [c for c in cols if c not in dag_schema.fields]
    valid_cols = [c for c in cols if c in dag_schema.fields]
    if invalid_cols:
        from rich import print as rich_print

        rich_print(
            f"[red][bold]Error:[/bold] Ignoring the following invalid columns: {invalid_cols}.  "
            f"List of valid columns: {list(dag_schema.fields.keys())}",
            file=sys.stderr,
        )
    dagbag = DagBag(process_subdir(args.subdir))
    if dagbag.import_errors:
        from rich import print as rich_print

        rich_print(
            "[red][bold]Error:[/bold] Failed to load all files. "
            "For details, run `airflow dags list-import-errors`",
            file=sys.stderr,
        )

    def get_dag_detail(dag: DAG) -> dict:
        dag_model = DagModel.get_dagmodel(dag.dag_id, session=session)
        if dag_model:
            dag_detail = dag_schema.dump(dag_model)
        else:
            dag_detail = _get_dagbag_dag_details(dag)
        return {col: dag_detail[col] for col in valid_cols}

    AirflowConsole().print_as(
        data=sorted(dagbag.dags.values(), key=operator.attrgetter("dag_id")),
        output=args.output,
        mapper=get_dag_detail,
    )


@cli_utils.action_cli
@suppress_logs_and_warning
@providers_configuration_loaded
@provide_session
def dag_details(args, session=NEW_SESSION):
    """Get DAG details given a DAG id."""
    dag = DagModel.get_dagmodel(args.dag_id, session=session)
    if not dag:
        raise SystemExit(f"DAG: {args.dag_id} does not exist in 'dag' table")
    dag_detail = dag_schema.dump(dag)

    if args.output in ["table", "plain"]:
        data = [{"property_name": key, "property_value": value} for key, value in dag_detail.items()]
    else:
        data = [dag_detail]

    AirflowConsole().print_as(
        data=data,
        output=args.output,
    )


@cli_utils.action_cli
@suppress_logs_and_warning
@providers_configuration_loaded
def dag_list_import_errors(args) -> None:
    """Display dags with import errors on the command line."""
    dagbag = DagBag(process_subdir(args.subdir))
    data = []
    for filename, errors in dagbag.import_errors.items():
        data.append({"filepath": filename, "error": errors})
    AirflowConsole().print_as(
        data=data,
        output=args.output,
    )
    if data:
        sys.exit(1)


@cli_utils.action_cli
@suppress_logs_and_warning
@providers_configuration_loaded
def dag_report(args) -> None:
    """Display dagbag stats at the command line."""
    dagbag = DagBag(process_subdir(args.subdir))
    AirflowConsole().print_as(
        data=dagbag.dagbag_stats,
        output=args.output,
        mapper=lambda x: {
            "file": x.file,
            "duration": x.duration,
            "dag_num": x.dag_num,
            "task_num": x.task_num,
            "dags": sorted(ast.literal_eval(x.dags)),
        },
    )


@cli_utils.action_cli
@suppress_logs_and_warning
@providers_configuration_loaded
@provide_session
def dag_list_jobs(args, dag: DAG | None = None, session: Session = NEW_SESSION) -> None:
    """List latest n jobs."""
    queries = []
    if dag:
        args.dag_id = dag.dag_id
    if args.dag_id:
        dag = DagModel.get_dagmodel(args.dag_id, session=session)

        if not dag:
            raise SystemExit(f"DAG: {args.dag_id} does not exist in 'dag' table")
        queries.append(Job.dag_id == args.dag_id)

    if args.state:
        queries.append(Job.state == args.state)

    fields = ["dag_id", "state", "job_type", "start_date", "end_date"]
    all_jobs_iter = session.scalars(
        select(Job).where(*queries).order_by(Job.start_date.desc()).limit(args.limit)
    )
    all_jobs = [{f: str(job.__getattribute__(f)) for f in fields} for job in all_jobs_iter]

    AirflowConsole().print_as(
        data=all_jobs,
        output=args.output,
    )


@cli_utils.action_cli
@suppress_logs_and_warning
@providers_configuration_loaded
@provide_session
def dag_list_dag_runs(args, dag: DAG | None = None, session: Session = NEW_SESSION) -> None:
    """List dag runs for a given DAG."""
    if dag:
        args.dag_id = dag.dag_id
    else:
        dag = DagModel.get_dagmodel(args.dag_id, session=session)

        if not dag:
            raise SystemExit(f"DAG: {args.dag_id} does not exist in 'dag' table")

    state = args.state.lower() if args.state else None
    dag_runs = DagRun.find(
        dag_id=args.dag_id,
        state=state,
        no_backfills=args.no_backfill,
        execution_start_date=args.start_date,
        execution_end_date=args.end_date,
        session=session,
    )

    dag_runs.sort(key=lambda x: x.execution_date, reverse=True)
    AirflowConsole().print_as(
        data=dag_runs,
        output=args.output,
        mapper=lambda dr: {
            "dag_id": dr.dag_id,
            "run_id": dr.run_id,
            "state": dr.state,
            "execution_date": dr.execution_date.isoformat(),
            "start_date": dr.start_date.isoformat() if dr.start_date else "",
            "end_date": dr.end_date.isoformat() if dr.end_date else "",
        },
    )


@cli_utils.action_cli
@providers_configuration_loaded
@provide_session
def dag_test(args, dag: DAG | None = None, session: Session = NEW_SESSION) -> None:
    """Execute one single DagRun for a given DAG and execution date."""
    run_conf = None
    if args.conf:
        try:
            run_conf = json.loads(args.conf)
        except ValueError as e:
            raise SystemExit(f"Configuration {args.conf!r} is not valid JSON. Error: {e}")
    execution_date = args.execution_date or timezone.utcnow()
    with _airflow_parsing_context_manager(dag_id=args.dag_id):
        dag = dag or get_dag(subdir=args.subdir, dag_id=args.dag_id)
    dr: DagRun = dag.test(execution_date=execution_date, run_conf=run_conf, session=session)
    show_dagrun = args.show_dagrun
    imgcat = args.imgcat_dagrun
    filename = args.save_dagrun
    if show_dagrun or imgcat or filename:
        tis = session.scalars(
            select(TaskInstance).where(
                TaskInstance.dag_id == args.dag_id,
                TaskInstance.execution_date == execution_date,
            )
        ).all()

        dot_graph = render_dag(dag, tis=tis)
        print()
        if filename:
            _save_dot_to_file(dot_graph, filename)
        if imgcat:
            _display_dot_via_imgcat(dot_graph)
        if show_dagrun:
            print(dot_graph.source)

    if dr and dr.state == DagRunState.FAILED:
        raise SystemExit("DagRun failed")


@cli_utils.action_cli
@providers_configuration_loaded
@provide_session
def dag_reserialize(args, session: Session = NEW_SESSION) -> None:
    """Serialize a DAG instance."""
    session.execute(delete(SerializedDagModel).execution_options(synchronize_session=False))

    if not args.clear_only:
        dagbag = DagBag(process_subdir(args.subdir))
        dagbag.sync_to_db(session=session)