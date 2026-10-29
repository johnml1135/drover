"""The only module that talks to the ledger (drover.ledger).

`Store` gives the API a small, stable surface and publishes a bus event after
every mutation. `UiView` is the synchronous contract hub.web expects.
SQLite connections are per thread (the ledger's connect() is thread-bound);
the ledger's own BEGIN IMMEDIATE transactions make writes and claims atomic.
"""
import json
import threading
import time

from drover import config, contract, decisions, dispatch_repos, importer, landing, origin, repos, stats, usage
from drover.ledger import board as ledger_board
from drover.ledger import connect, ops, tx
from drover.ledger.ops import NotFound as LedgerNotFound
from drover.ledger.states import IllegalTransition


from .store_common import BadRequest, Forbidden, NotFound, _Rollback, _run_id, _translate
from .store_ledger import Store
from .store_ui import UiView
