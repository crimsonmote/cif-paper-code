import os
import threading
from contextlib import contextmanager
from pathlib import Path

import keyring
import pymongo
from pymongo.errors import PyMongoError

_client_local = threading.local()
_LOG_PATH = Path(__file__).resolve().parents[1] / "log" / "error.log"

try:
    import multiprocessing.util as _mp_util

    def _after_fork_reset(obj):
        setattr(obj, "clients", {})

    _mp_util.register_after_fork(_client_local, _after_fork_reset)
except Exception:
    # Fallback for environments without multiprocessing util support
    pass


def _resolve_mongo_uri() -> str:
    """
    Resolve the mongo URI using environment overrides, then keyring, and finally a local fallback.
    """
    return (
        os.environ.get("MONGO_URI")
        or keyring.get_password("mongo", "uri")
        or "mongodb://localhost:27017/openalex"
    )


def get_mongo_client(appname: str = "science-commercialization"):
    """
    Return a cached MongoClient per process/appname combination.
    """
    clients = getattr(_client_local, "clients", None)
    if clients is None:
        clients = {}
        _client_local.clients = clients
    if appname in clients:
        return clients[appname]
    uri = _resolve_mongo_uri()
    client = pymongo.MongoClient(uri, appname=appname)
    clients[appname] = client
    return client


@contextmanager
def handle_pymongo_errors():
    """
    Context manager to log PyMongo errors while keeping console output informative.
    """

    try:
        yield
    except PyMongoError as exc:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a") as log_file:
            msg = getattr(exc, "_message", str(exc))
            log_file.write(msg + "\n")
            details = getattr(exc, "details", None)
            if details:
                log_file.write("Error Document:\n " + str(details) + "\n")
            log_file.write("Full error: " + str(exc) + "\n")
        print(getattr(exc, "_message", f"Mongo error: {exc}"))
    except Exception as exc:  # noqa: BLE001 - log unexpected errors too
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a") as log_file:
            log_file.write("Full error: " + str(exc) + "\n")
        print(f"❌ An unexpected error occurred: {exc}")
