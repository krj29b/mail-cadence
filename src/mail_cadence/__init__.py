import importlib.metadata

try:
    __version__ = importlib.metadata.version("mail-cadence")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.9.3"

__all__ = ["__version__"]
