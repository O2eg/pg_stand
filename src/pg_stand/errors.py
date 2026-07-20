class PgStandError(RuntimeError):
    """Base error shown to a pg-stand operator."""


class ConfigError(PgStandError):
    """The declarative stand configuration is invalid."""


class DockerRuntimeError(PgStandError):
    """Docker could not reach the requested stand state."""


class PreconditionError(PgStandError):
    """A caller-provided plan or state precondition is no longer true."""
