from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import asyncssh
from fastapi import FastAPI
from key_manager import keys_exist, setup_ssh_keys_interactive
from models import MachineConfig
from monitor import start_monitor, stop_monitor
from ssh_manager import ssh_manager
from web import state
from web.config import (apply_runtime_settings, load_machines_config, load_nodes_config, load_settings, reload_discovery_manager, )

logger = logging.getLogger(__name__)


async def connect_all_machines() -> list[str]:
    auth_failed: list[str] = []
    for machine_id, config in state.machines_config.items():
        if not config.user:
            logger.warning(f"Missing SSH user for {machine_id}")
            auth_failed.append(machine_id)
            continue
        try:
            success = await ssh_manager.connect(machine_id, config)
            if success:
                logger.info(f"Connected to {machine_id}")
            else:
                logger.warning(f"Failed to connect to {machine_id}")
        except asyncssh.PermissionDenied:
            logger.error(f"Auth failed for {machine_id}")
            auth_failed.append(machine_id)
        except Exception as e:
            logger.error(f"Error connecting to {machine_id}: {e}")
    return auth_failed


async def maintain_machine_connections(stop_event: asyncio.Event | None = None) -> None:
    """Reconnect configured SSH sessions after transient network failures."""
    stop_event = stop_event or asyncio.Event()
    retry_counts: dict[str, int] = {}
    retry_at: dict[str, float] = {}

    while not stop_event.is_set():
        loop = asyncio.get_running_loop()
        due: list[tuple[str, MachineConfig]] = []

        for machine_id, config in state.machines_config.items():
            if not config.user or not ssh_manager.reconnect_enabled(machine_id):
                retry_counts.pop(machine_id, None)
                retry_at.pop(machine_id, None)
                continue

            if ssh_manager.is_connected(machine_id):
                retry_counts.pop(machine_id, None)
                retry_at.pop(machine_id, None)
                continue

            if loop.time() >= retry_at.get(machine_id, 0.0):
                due.append((machine_id, config))

        if due:
            results = await asyncio.gather(
                *(ssh_manager.connect(machine_id, config, automatic=True) for machine_id, config in due),
                return_exceptions=True,
            )
            for (machine_id, _), result in zip(due, results):
                if result is True:
                    logger.info("SSH connection restored for %s", machine_id)
                    retry_counts.pop(machine_id, None)
                    retry_at.pop(machine_id, None)
                    continue

                attempts = retry_counts.get(machine_id, 0) + 1
                retry_counts[machine_id] = attempts
                delay = min(30, 2 ** min(attempts, 5))
                retry_at[machine_id] = loop.time() + delay
                if isinstance(result, BaseException):
                    logger.warning("Could not reconnect to %s: %s", machine_id, result)
                logger.warning("Retrying SSH connection to %s in %d seconds", machine_id, delay)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading configurations...")
    state.machines_config = load_machines_config()
    state.nodes_config = load_nodes_config()
    state.settings = load_settings()
    apply_runtime_settings()
    runtime_advertise_host = reload_discovery_manager()
    if runtime_advertise_host:
        logger.info(f"Using discovery advertise host: {runtime_advertise_host}")
    logger.info(f"Loaded {len(state.machines_config)} machines")
    if not keys_exist():
        await setup_ssh_keys_interactive(state.machines_config)
        state.machines_config = load_machines_config()
    auth_failed = await connect_all_machines()
    if auth_failed:
        print()
        print("=" * 60)
        print("  SSH Authentication Failed")
        print("=" * 60)
        print()
        for mid in auth_failed:
            cfg = state.machines_config[mid]
            print(f"  x {mid}: {cfg.name} ({cfg.user}@{cfg.host})")
        print()
        print("SSH keys are invalid or were not deployed correctly.")
        print("Re-running key setup to fix access...")
        print()
        await setup_ssh_keys_interactive(state.machines_config)
        state.machines_config = load_machines_config()
        await connect_all_machines()
    from web.routes.knowledge import jobs as knowledge_jobs
    knowledge_jobs.start()
    start_monitor(state.settings.network_interface or "")
    connection_task = asyncio.create_task(
        maintain_machine_connections(), name="ssh-connection-supervisor"
    )
    state.background_tasks.add(connection_task)
    connection_task.add_done_callback(state.background_tasks.discard)
    yield
    await knowledge_jobs.stop()
    stop_monitor()
    logger.info("Shutting down...")
    try:
        state.discovery_manager.shutdown()
        for task in list(state.background_tasks):
            try:
                task.cancel()
            except Exception:
                pass
        if state.background_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*state.background_tasks, return_exceptions=True), timeout=2.0)
            except asyncio.TimeoutError:
                logger.debug("Some background tasks did not complete in time")
            except asyncio.CancelledError:
                pass
        state.background_tasks.clear()
    except Exception as e:
        logger.debug(f"Error cancelling tasks: {e}")
    try:
        logger.info("Closing SSH connections...")
        await asyncio.wait_for(ssh_manager.disconnect_all(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("SSH disconnect timeout")
    except asyncio.CancelledError:
        logger.debug("Disconnect cancelled")
    except Exception as e:
        logger.debug(f"Error during disconnect: {e}")
