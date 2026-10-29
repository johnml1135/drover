from drover.ledger import ops
from drover.ledger.ops import NotFound as LedgerNotFound
from drover.ledger.states import IllegalTransition


class NotFound(LookupError):
    pass


class BadRequest(ValueError):
    pass


class Forbidden(PermissionError):
    pass


def _translate(fn):
    """Map ledger exceptions onto the hub's three error types."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except LedgerNotFound as e:
            raise NotFound(str(e)) from e
        except ops.NotApproved as e:
            raise BadRequest(str(e)) from e
        except PermissionError as e:
            raise Forbidden(str(e)) from e
        except (IllegalTransition, ValueError) as e:
            raise BadRequest(str(e)) from e
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _run_id(run_id):
    """A positive integer run id, spelled plainly; anything else is NotFound."""
    if not str(run_id).isascii() or not str(run_id).isdigit() or int(run_id) < 1:
        raise NotFound(f"run {run_id}")
    return int(run_id)


class _Rollback(Exception):
    pass
