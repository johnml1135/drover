"""Atomic, snapshot-only conversion of legacy waiting episodes.

All associations are staged before evaluating a resolver. Legacy text is evidence,
never a runtime release predicate. No machine configuration or checkout is read.
"""
import json
import math
import re
import time
from datetime import datetime, timezone

from .. import contract
from . import ops, states
from .db import tx
from .waiting import (Waiting, cause_key, repo_key, TASK_TERMINAL, OWNER_ANSWER,
                      REPLAN, MANUAL, DEADLINE, BASE, OPEN, RESOLVED, AUTHORIZED)


from .waiting_migration_common import (_event, _events, _task, _subject, _stage, _question,
                                       _alert, _diagnostic, _request, _boundary)
from .waiting_migration_holds import _holds, _epoch, _deadlines, _finish
from .waiting_migration_questions import (_qid, _link, _repo_subjects, _attach_owner,
                                          _proposal_identity, _tuner_questions, _questions)
from .waiting_migration_rehearsal import _logical, _fingerprint, _probes


def migrate_waiting(conn) -> dict:
    report = {'converted': {name: 0 for name in ('holds', 'causes', 'questions', 'requests', 'deadlines', 'cache')},
              'retired': [], 'diagnostics': []}
    # Already converted databases must be logically immutable on repeated calls.
    if conn.execute('SELECT 1 FROM schema_version WHERE version=7').fetchone():
        return report
    with tx(conn):
        before_causes = conn.execute('SELECT count(*) FROM wait_causes').fetchone()[0]
        _holds(conn, report)
        _questions(conn, report)
        _deadlines(conn, report)
        _finish(conn)
        Waiting(conn, 'migration').expire(time.time())
        report['converted']['causes'] = conn.execute('SELECT count(*) FROM wait_causes').fetchone()[0] - before_causes
    return report


def main(argv=None) -> int:
    import argparse
    import hashlib
    import os
    import sqlite3
    from pathlib import Path
    from .db import connect, migrate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--copy', required=True)
    parser.add_argument('--verify-copy', action='store_true')
    args = parser.parse_args(argv)
    source, destination = Path(args.source), Path(args.copy)
    src = dest = None
    try:
        if source.resolve() == destination.resolve() or destination.exists() or destination.is_symlink():
            raise ValueError('copy must be a fresh, distinct path')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None)
        src.row_factory = sqlite3.Row
        source_logical = _fingerprint(src)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        dest = sqlite3.connect(destination, isolation_level=None)
        src.backup(dest)
        dest.close()
        dest = None
        src.close()
        src = None
        dest = connect(destination, migrate_db=False)
        reports = []
        # Capture the converter report while retaining the exact migration transaction.
        from unittest.mock import patch
        from . import waiting_migration
        converter = waiting_migration.migrate_waiting
        def capture(conn):
            report = converter(conn)
            reports.append(report)
            return report
        with patch.object(waiting_migration, 'migrate_waiting', side_effect=capture):
            migrate(dest)
        integrity = dest.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = [list(r) for r in dest.execute('PRAGMA foreign_key_check')]
        before = _fingerprint(dest)
        migrate(dest)
        if _fingerprint(dest) != before or integrity != 'ok' or foreign_keys:
            raise ValueError('copy failed integrity or idempotence checks')
        probes = _probes(dest) if args.verify_copy else []
        src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash or _fingerprint(src) != source_logical:
            raise ValueError('source changed during rehearsal; supply an immutable backup')
        print(json.dumps({'source_sha256': source_hash, 'source_fingerprint': source_logical,
                          'copy_fingerprint': before, 'integrity_check': integrity, 'foreign_key_check': foreign_keys,
                          'idempotent': True, 'probes': probes, 'migration': reports}, sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc), 'copy': str(destination)}))
        return 2
    finally:
        if src is not None:
            src.close()
        if dest is not None:
            dest.close()


if __name__ == '__main__':
    raise SystemExit(main())
