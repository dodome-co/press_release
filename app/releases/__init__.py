from flask import Blueprint

bp = Blueprint("releases", __name__, url_prefix="/releases")

from . import routes  # noqa: E402,F401
