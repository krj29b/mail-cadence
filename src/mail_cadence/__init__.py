import importlib.metadata

try:
    __version__ = importlib.metadata.version("mail-cadence")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.5.0"

__all__ = ["__version__"]
