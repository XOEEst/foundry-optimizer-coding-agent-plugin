class RuntimeWiringError(RuntimeError):
    """The optimize-job runtime wiring could not be proven safe."""


class RuntimeIntegrationError(RuntimeWiringError):
    """A required runtime integration is missing or inconsistent."""
