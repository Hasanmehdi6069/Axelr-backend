"""Verify optional dependency versions match the module's contract."""
import importlib.metadata
import logging
from packaging import version

logger = logging.getLogger("insights.deps")

# TablesDB landed in appwrite 17.x. Pinning 17.0.0 matches requirements.txt.
_APPWRITE_MIN = "17.0.0"
_PYMONGO_MIN = "4.10.0"
_FASTAPI_MIN = "0.100.0"


def check_appwrite_sdk() -> bool:
    try:
        v = importlib.metadata.version("appwrite")
    except importlib.metadata.PackageNotFoundError:
        logger.warning("appwrite SDK not installed (only required for appwrite backend)")
        return True  # not fatal — mongo deployments don't need it
    if version.parse(v) < version.parse(_APPWRITE_MIN):
        logger.critical(
            "appwrite SDK %s < %s — TablesDB signatures will break",
            v, _APPWRITE_MIN)
        return False
    logger.info("appwrite SDK %s: compatible", v)
    return True


def check_pymongo_version() -> bool:
    try:
        v = importlib.metadata.version("pymongo")
    except importlib.metadata.PackageNotFoundError:
        logger.warning("pymongo not installed (only required for mongo backend)")
        return True
    if version.parse(v) < version.parse(_PYMONGO_MIN):
        logger.critical(
            "pymongo %s < %s — AsyncMongoClient.close() unsupported",
            v, _PYMONGO_MIN)
        return False
    logger.info("pymongo %s: compatible", v)
    return True


def check_fastapi_version() -> bool:
    try:
        v = importlib.metadata.version("fastapi")
    except importlib.metadata.PackageNotFoundError:
        logger.critical("fastapi not installed")
        return False
    if version.parse(v) < version.parse(_FASTAPI_MIN):
        logger.critical("fastapi %s < %s", v, _FASTAPI_MIN)
        return False
    logger.info("fastapi %s: compatible", v)
    return True


def run_all_checks() -> bool:
    logger.info("running dependency compatibility checks")
    checks = [check_appwrite_sdk(), check_pymongo_version(), check_fastapi_version()]
    if all(checks):
        logger.info("all dependency checks passed")
        return True
    logger.critical("dependency checks failed")
    return False