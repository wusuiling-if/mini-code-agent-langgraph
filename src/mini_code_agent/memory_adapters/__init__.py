"""MCA-specific adapters for the host-neutral memory core.

Submodules are intentionally not imported here: the transaction adapter itself
depends on the transaction runtime, while that runtime uses the project adapter.
Keeping this package initializer empty prevents an import cycle.
"""
