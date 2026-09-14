# Local service protocol (version 1)

CLI: python3 turtle_service.py COMMAND [args]. Output one JSON object. Commands never print credentials. Exit nonzero with {"ok":false,"error":"human explanation"}.

status: {"ok":true,"serviceRunning":bool,"launchAtLogin":bool,"dependencies":{"rclone":path-or-null,"aws":path-or-null,"python":path},"profiles":[string],"connections":[{"id":UUID,"name":string,"bucket":string,"profile":string,"region":string,"readOnly":bool,"autoConnect":bool,"desiredConnected":bool,"mounted":bool,"state":"disconnected|connecting|connected|disconnecting|needsLogin|error","message":string,"mountPath":string,"updatedAt":number}]}

add --name NAME --bucket BUCKET --profile PROFILE --region REGION [--read-only] [--auto-connect] -> {"ok":true,"id":UUID}
edit ID with same fields (only while disconnected)
remove ID (only disconnected; only local saved connection removed)
connect ID (starts service if needed)
disconnect ID (native non-forced unmount; pending uploads wait, busy volume remains connected with explanatory message)
login ID (start AWS SSO device authorization in the browser; five-minute timeout; no credentials printed or persisted by Turtle)
autostart on|off (login LaunchAgent registration, preserve running mounts)
serve (foreground supervisor for launchd)
shutdown (gracefully disconnect all, preserve cached data, report busy mounts)

All action commands return {"ok":true,"message":optional}. GUI polls status every 3 seconds. Backend paths: ~/Library/Application Support/Mountain Turtle, ~/Library/Caches/MountainTurtle, ~/Library/Logs/MountainTurtle. Mount root ~/Mountain Turtle; each display name must be unique and safe as one directory component. No mounts of unrelated buckets. No recursive S3 scans or object mutation during connect/validation. Rclone mounts bind127.0.0.1 with unprivileged native macOS NFS options.

CLI accepts --resource-dir PATH before COMMAND to locate icon-overlay if present. Backend can locate its parent Resources by default. Bundled app resources include service/turtle_service.py and icon-overlay. Dependencies discovered from explicit standard paths + PATH. For app installation, LaunchAgent references the stable service script inside the installed .app, not build checkout.
