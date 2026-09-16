#!/usr/bin/env python3
"""Safely upgrade an old supervisor after ordinary, non-forced drive ejection."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)


def command_for(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                            text=True, capture_output=True, timeout=3)
    return result.stdout.strip() if result.returncode == 0 else ""


def restore_intent(turtle, paths, intended):
    # Preserve settings and connections added since the snapshot. Only the
    # temporary maintenance fields are changed under the service's normal lock.
    with turtle.Store(paths).update() as state:
        state["shutdown"] = False
        for connection in state["connections"]:
            if connection["id"] in intended:
                connection["desiredConnected"] = intended[connection["id"]]
                connection["reconnectRequested"] = False
                connection["revision"] = connection.get("revision", 0) + 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-supervisor", required=True, type=int)
    parser.add_argument("--expected-child", type=int, action="append", default=[])
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 180:
        parser.error("Use a timeout between 10 and 180 seconds")
    os.umask(0o077)
    resources = Path.home() / "Applications/Mountain Turtle.app/Contents/Resources"
    source = resources / "service/turtle_service.py"
    spec = importlib.util.spec_from_file_location("installed_turtle", source)
    turtle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(turtle)
    paths, store = turtle.Paths(resources=resources), None
    store = turtle.Store(paths)
    runtime, saved = store.runtime(), store.read()
    expected_pid = args.expected_supervisor
    if runtime.get("pid") != expected_pid or not turtle.service_running(paths):
        raise RuntimeError("Supervisor identity changed; inspect the current service before retrying")
    if str(source) not in command_for(expected_pid) or " serve" not in command_for(expected_pid):
        raise RuntimeError("The expected supervisor is not running the installed service")
    if saved.get("shutdown") or any(c.get("reconnectRequested") for c in saved["connections"]):
        raise RuntimeError("A shutdown or reconnect is already pending; let it finish first")
    mounts = turtle.mount_table()
    live = runtime.get("connections", {})
    owned = {}
    for connection in saved["connections"]:
        pid = live.get(connection["id"], {}).get("pid")
        if not pid or not turtle.process_alive(pid):
            continue
        command = command_for(pid)
        if "rclone nfsmount " not in command or str(paths.remotes / (connection["id"] + ".conf")) not in command:
            raise RuntimeError("A mount process no longer matches this app's saved connection")
        owned[connection["id"]] = pid
        if str(paths.mounts / connection["name"]) not in mounts:
            raise RuntimeError("An owned mount is transitioning; wait before restarting its supervisor")
    if set(owned.values()) != set(args.expected_child):
        raise RuntimeError("The active mount processes changed; inspect them before retrying")
    if args.dry_run:
        emit("validated", supervisor=expected_pid, children=list(owned.values()),
             connections=[dict(id=c["id"], name=c["name"], desiredConnected=c.get("desiredConnected", False))
                          for c in saved["connections"]])
        return

    backup = paths.base / ("service-upgrade-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    intended = {}
    mount_paths = set()
    with store.update() as state:
        if store.runtime().get("pid") != expected_pid:
            raise RuntimeError("Supervisor changed before shutdown; no drive was disconnected")
        turtle.write_json(backup / "connections.json", state)
        turtle.write_json(backup / "runtime.json", store.runtime())
        if paths.plist.exists():
            shutil.copyfile(paths.plist, backup / "service.plist")
            (backup / "service.plist").chmod(0o600)
        for connection in state["connections"]:
            intended[connection["id"]] = connection.get("desiredConnected", False)
            mount_paths.add(str(paths.mounts / connection["name"]))
            connection["desiredConnected"] = False
            connection["reconnectRequested"] = False
            connection["revision"] = connection.get("revision", 0) + 1
        state["shutdown"] = True
    emit("safe_ejection_requested", backup=str(backup))
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            attached = turtle.mount_table() & mount_paths
            active = [pid for pid in owned.values() if turtle.process_alive(pid)]
            live = store.runtime().get("connections", {})
            busy = [identity for identity in intended if "busy" in live.get(identity, {}).get("message", "").lower()]
            if busy:
                raise RuntimeError("A drive is busy; restart aborted without forcing disconnection")
            if not attached and not active and not turtle.service_running(paths) and not turtle.process_alive(expected_pid):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Safe ejection did not finish within the timeout; restart aborted")
        # Start without login defaults and without making any remote connection
        # until the new supervisor is running and the original intent is restored.
        with store.update() as state:
            state["shutdown"] = False
        turtle.ensure_service(paths)
        publish_deadline = time.monotonic() + 5
        while time.monotonic() < publish_deadline:
            new_pid = store.runtime().get("pid")
            if new_pid and new_pid != expected_pid and turtle.process_alive(new_pid):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The new supervisor has not published its identity; check service status")
        restore_intent(turtle, paths, intended)
        emit("new_supervisor_started", supervisor=new_pid, restoredDesired=intended)
    except Exception:
        restore_intent(turtle, paths, intended)
        if not turtle.service_running(paths):
            turtle.ensure_service(paths)
        emit("aborted_intent_restored", supervisor=store.runtime().get("pid"))
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never include provider output or private runtime state.
        emit("error", message=str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__)
        sys.exit(1)
