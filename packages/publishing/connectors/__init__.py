"""Out-of-process publish-platform connectors (M6c).

These connectors drive real desktop publishing apps (e.g. 小V猫 over CDP). They
depend on optional packages (``websockets``) and a running desktop app, and are
NEVER imported on the API request hot path nor exercised by tests. The
``PublishPlatformAdapter`` implementations import them lazily.
"""
