import asyncio
import inspect
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Coroutine,
    Dict,
    Generic,
    Iterable,
    Literal,
    Optional,
    TypeVar,
    cast,
)

import anyio
import anyio._backends._asyncio
from sniffio import AsyncLibraryNotFoundError
from typing_extensions import ParamSpec

from prefect import Flow, Task, get_client
from prefect.client.orchestration import PrefectClient
from prefect.client.schemas import FlowRun, TaskRun
from prefect.context import FlowRunContext
from prefect.futures import PrefectFuture
from prefect.results import ResultFactory
from prefect.server.schemas.states import State
from prefect.states import (
    Completed,
    Pending,
    Running,
    exception_to_failed_state,
)
from prefect.utilities.asyncutils import A, Async, run_sync
from prefect.utilities.engine import (
    _dynamic_key_for_task_run,
    _resolve_custom_flow_run_name,
    collect_task_run_inputs,
    propose_state,
)

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class FlowRunEngine(Generic[P, R]):
    flow: Flow[P, Coroutine[Any, Any, R]]
    parameters: Optional[Dict[str, Any]] = None
    flow_run: Optional[FlowRun] = None
    _is_started: bool = False
    _client: Optional[PrefectClient] = None
    short_circuit: bool = False

    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}

    @property
    def client(self) -> PrefectClient:
        if not self._is_started or self._client is None:
            raise RuntimeError("Engine has not started.")
        return self._client

    @property
    def state(self) -> State:
        return self.flow_run.state  # type: ignore

    async def begin_run(self) -> State:
        state = Running()
        return await self.set_state(state)

    async def set_subflow_state(self, state: State) -> State:
        """This appears to not be necessary"""
        pass

    async def set_state(self, state: State) -> State:
        """ """
        # prevents any state-setting activity
        if self.short_circuit:
            return self.state

        state = await propose_state(self.client, state, flow_run_id=self.flow_run.id)  # type: ignore
        self.flow_run.state = state  # type: ignore
        self.flow_run.state_name = state.name  # type: ignore
        self.flow_run.state_type = state.type  # type: ignore
        await self.set_subflow_state(state)
        return state

    async def result(self, raise_on_failure: bool = True) -> R | State | None:
        _result = self.state.result(raise_on_failure=raise_on_failure)
        # state.result is a `sync_compatible` function that may or may not return an awaitable
        # depending on the parent frame
        if inspect.isawaitable(_result):
            _result = await _result
        return _result

    async def handle_success(self, result: R) -> R:
        await self.set_state(Completed())
        return result

    async def handle_exception(
        self, exc: Exception, msg: str = None, result_factory: ResultFactory = None
    ) -> State:
        context = FlowRunContext.get()
        state = await exception_to_failed_state(
            exc,
            message=msg or "Flow run encountered an exception:",
            result_factory=result_factory or getattr(context, "result_factory", None),
        )
        return await self.set_state(state)

    async def create_subflow_task_run(
        self, client: PrefectClient, context: FlowRunContext
    ) -> TaskRun:
        dummy_task = Task(
            name=self.flow.name, fn=self.flow.fn, version=self.flow.version
        )
        task_inputs = {
            k: await collect_task_run_inputs(v) for k, v in self.parameters.items()
        }
        parent_task_run = await client.create_task_run(
            task=dummy_task,
            flow_run_id=(
                context.flow_run.id if getattr(context, "flow_run", None) else None
            ),
            dynamic_key=_dynamic_key_for_task_run(context, dummy_task),
            task_inputs=task_inputs,
            state=Pending(),
        )
        return parent_task_run

    async def create_flow_run(self, client: PrefectClient) -> FlowRun:
        flow_run_ctx = FlowRunContext.get()

        # this is a subflow run
        parent_task_run = None
        if flow_run_ctx:
            parent_task_run = await self.create_subflow_task_run(
                client=client, context=flow_run_ctx
            )

        try:
            flow_run_name = _resolve_custom_flow_run_name(
                flow=self.flow, parameters=self.parameters
            )
        except TypeError:
            flow_run_name = None

        flow_run = await client.create_flow_run(
            flow=self.flow,
            name=flow_run_name,
            parameters=self.flow.serialize_parameters(self.parameters),
            state=Pending(),
            parent_task_run_id=getattr(parent_task_run, "id", None),
        )
        return flow_run

    @asynccontextmanager
    async def start(self):
        """
        - sets state to running
        - initialize flow run logger
        """
        async with get_client() as client:
            self._client = client
            self._is_started = True

            if not self.flow_run:
                self.flow_run = await self.create_flow_run(client)

            # validate prior to context so that context receives validated params
            if self.flow.should_validate_parameters:
                try:
                    self.parameters = self.flow.validate_parameters(self.parameters)
                except Exception as exc:
                    await self.handle_exception(
                        exc,
                        msg="Validation of flow parameters failed with error",
                        result_factory=await ResultFactory.from_flow(self.flow),
                    )
                    self.short_circuit = True

            with FlowRunContext(
                flow=self.flow,
                log_prints=self.flow.log_prints or False,
                flow_run=self.flow_run,
                parameters=self.parameters,
                client=client,
                background_tasks=anyio.create_task_group(),
                result_factory=await ResultFactory.from_flow(self.flow),
                task_runner=self.flow.task_runner,
            ):
                yield self

        self._is_started = False
        self._client = None

    @contextmanager
    def start_sync(self):
        """
        - sets state to running
        - initialize flow run logger
        """
        try:
            client = get_client()
            run_sync(client.__aenter__())
            self._client = client
            self._is_started = True

            if not self.flow_run:
                self.flow_run = run_sync(self.create_flow_run(client))

            # validate prior to context so that context receives validated params
            if self.flow.should_validate_parameters:
                try:
                    self.parameters = self.flow.validate_parameters(self.parameters)
                except Exception as exc:
                    run_sync(
                        self.handle_exception(
                            exc,
                            msg="Validation of flow parameters failed with error",
                            result_factory=run_sync(ResultFactory.from_flow(self.flow)),
                        )
                    )
                    self.short_circuit = True

            # if running in a completely synchronous frame, anyio will not detect the
            # backend to use for the task group
            try:
                task_group = anyio.create_task_group()
            except AsyncLibraryNotFoundError:
                task_group = anyio._backends._asyncio.TaskGroup()

            with FlowRunContext(
                flow=self.flow,
                log_prints=self.flow.log_prints or False,
                flow_run=self.flow_run,
                parameters=self.parameters,
                client=client,
                background_tasks=task_group,
                result_factory=run_sync(ResultFactory.from_flow(self.flow)),
                task_runner=self.flow.task_runner,
            ):
                yield self
        except Exception:
            # run_sync(client.__aexit__(type(exc), exc, None))
            raise
        else:
            run_sync(client.__aexit__(None, None, None))
        finally:
            self._is_started = False
            self._client = None

    async def get_client(self):
        if not self._is_started:
            raise RuntimeError("Engine has not started.")
        else:
            return self._client

    def is_running(self) -> bool:
        if getattr(self, "flow_run", None) is None:
            return False
        return getattr(self, "flow_run").state.is_running()

    def is_pending(self) -> bool:
        if getattr(self, "flow_run", None) is None:
            return False  # TODO: handle this differently?
        return getattr(self, "flow_run").state.is_pending()


async def run_flow(
    flow: Task[P, Coroutine[Any, Any, R]],
    flow_run: Optional[FlowRun] = None,
    parameters: Optional[Dict[str, Any]] = None,
    wait_for: Optional[Iterable[PrefectFuture[A, Async]]] = None,
    return_type: Literal["state", "result"] = "result",
) -> R | None:
    """
    Runs a flow against the API.

    We will most likely want to use this logic as a wrapper and return a coroutine for type inference.
    """

    engine = FlowRunEngine[P, R](flow, parameters, flow_run)
    # This is a context manager that keeps track of the state of the flow run.
    async with engine.start() as run:
        await run.begin_run()

        while run.is_pending():
            await asyncio.sleep(1)
            await run.begin_run()

        while run.is_running():
            try:
                # This is where the flow is actually run.
                result = cast(R, await flow.fn(**(run.parameters or {})))  # type: ignore

                # If the flow run is successful, finalize it.
                await run.handle_success(result)
                if return_type == "result":
                    return result

            except Exception as exc:
                # If the flow fails, and we have retries left, set the flow to retrying.
                await run.handle_exception(exc)

        if return_type == "state":
            return run.state
        return await run.result()


def run_flow_sync(
    flow: Task[P, Coroutine[Any, Any, R]],
    flow_run: Optional[FlowRun] = None,
    parameters: Optional[Dict[str, Any]] = None,
    wait_for: Optional[Iterable[PrefectFuture[A, Async]]] = None,
    return_type: Literal["state", "result"] = "result",
) -> R | None:
    engine = FlowRunEngine[P, R](flow, parameters, flow_run)
    # This is a context manager that keeps track of the state of the flow run.
    with engine.start_sync() as run:
        run_sync(run.begin_run())

        while run.is_pending():
            time.sleep(1)
            run_sync(run.begin_run())

        while run.is_running():
            try:
                # This is where the flow is actually run.
                result = cast(R, flow.fn(**(run.parameters or {})))  # type: ignore
                # If the flow run is successful, finalize it.
                run_sync(run.handle_success(result))
                if return_type == "result":
                    return result

            except Exception as exc:
                # If the flow fails, and we have retries left, set the flow to retrying.
                run_sync(run.handle_exception(exc))

        if return_type == "state":
            return run.state
        return run_sync(run.result())
