"""Each epic is Drover- or Director-dispatched: who picks next, and nothing else."""
import unittest

from drover import dispatch_repos
from drover.ledger import IllegalTransition, ops
from drover.ledger.ops_dispatch import DEFAULT_KEY, DIRECTOR, DROVER
from drover.notifications import Notifications
from tests.test_ledger import Base


class DispatchSetting(Base):
    def epic(self, mode=DROVER, repo="p"):
        e = ops.create_epic(self.conn, repo, "E")
        return ops.set_epic_mode(self.conn, e["id"], mode)["id"] if mode != DROVER else e["id"]

    def claim(self):
        return ops.claim_next(self.conn, self.worker())

    def test_default_is_drover(self):
        e = self.epic()
        self.assertEqual(ops.get_epic(self.conn, e)["dispatch"], DROVER)
        self.assertEqual(ops.mode_of(self.conn, self.task(epic_id=e)), DROVER)
        self.assertEqual(self.claim()["epic_id"], e)

    def test_director_epic_task_is_not_claimed(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        self.assertIsNone(self.claim())
        self.assertEqual(self.status(t["id"]), "ready")
        self.assertEqual(ops.awaiting_pick(self.conn), 1)

    def test_picked_task_is_claimed(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        picked = ops.pick(self.conn, t["id"], "dispatcher")
        self.assertEqual((picked["picked_by"], bool(picked["picked_at"])), ("dispatcher", True))
        self.assertEqual(ops.awaiting_pick(self.conn), 0)
        self.assertEqual(self.claim()["id"], t["id"])

    def test_task_without_epic_follows_global_default(self):
        t = self.task()
        self.assertEqual(ops.mode_of(self.conn, t), DROVER)
        self.assertEqual(ops.set_default_mode(self.conn, DIRECTOR), DIRECTOR)
        self.assertEqual(ops.get_setting(self.conn, DEFAULT_KEY), DIRECTOR)
        self.assertIsNone(self.claim())
        ops.pick(self.conn, t["id"], "owner")
        self.assertEqual(self.claim()["id"], t["id"])

    def test_only_owner_switches(self):
        e = self.epic()
        for who in ("dispatcher", "worker", "director:a"):
            with self.assertRaises(PermissionError):
                ops.set_epic_mode(self.conn, e, DIRECTOR, actor=who)
            with self.assertRaises(PermissionError):
                ops.set_default_mode(self.conn, DIRECTOR, actor=who)
        with self.assertRaises(ValueError):
            ops.set_epic_mode(self.conn, e, "nobody")
        self.assertEqual(ops.get_epic(self.conn, e)["dispatch"], DROVER)

    def test_switch_leaves_claimed_and_running_alone(self):
        e = self.epic()
        a, b = self.task("a", epic_id=e), self.task("b", epic_id=e)
        self.assertEqual(self.claim()["id"], a["id"])
        ops.start_run(self.conn, a["id"], "worker", "local")
        self.assertEqual(self.status(a["id"]), "running")
        ops.set_epic_mode(self.conn, e, DIRECTOR)
        self.assertEqual(self.status(a["id"]), "running")
        self.assertIsNone(self.claim())
        self.assertEqual(self.status(b["id"]), "ready")

    def test_pick_survives_a_retry(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        ops.pick(self.conn, t["id"], "owner")
        self.assertEqual(self.claim()["id"], t["id"])
        ops.set_status(self.conn, t["id"], "failed", actor="dispatcher")
        ops.set_status(self.conn, t["id"], "ready", actor="owner")
        self.assertEqual(self.claim()["id"], t["id"])

    def test_a_second_pick_changes_nothing_and_logs_nothing(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        first = ops.pick(self.conn, t["id"], "dispatcher")
        again = ops.pick(self.conn, t["id"], "owner")
        self.assertEqual((again["picked_by"], again["picked_at"]), (first["picked_by"], first["picked_at"]))
        self.assertEqual(len(ops.events(self.conn, task_id=t["id"], kind="picked")), 1)

    def test_pick_refused_unless_ready(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        self.claim()                       # not picked: stays ready
        d = self.task("draft", status="draft", epic_id=self.epic(DIRECTOR))
        with self.assertRaises(IllegalTransition):
            ops.pick(self.conn, d["id"], "owner")
        ops.pick(self.conn, t["id"], "owner")
        self.claim()
        with self.assertRaises(IllegalTransition):
            ops.pick(self.conn, t["id"], "owner")
        with self.assertRaises(PermissionError):
            ops.pick(self.conn, d["id"], "worker")
        self.assertIsNone(ops.get_task(self.conn, d["id"])["picked_at"])

    def test_dispatch_repos_still_binds_a_picked_task(self):
        t = self.task(repo="p", epic_id=self.epic(DIRECTOR))
        ops.pick(self.conn, t["id"], "owner")
        dispatch_repos.store(self.conn, ("q",))
        self.assertIsNone(self.claim())
        dispatch_repos.clear(self.conn)
        self.assertEqual(self.claim()["id"], t["id"])

    def test_stop_still_binds_a_picked_task(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        ops.pick(self.conn, t["id"], "owner")
        Notifications(self.conn).stop("owner", "stop")
        self.assertIsNone(self.claim())
        self.assertEqual(self.status(t["id"]), "ready")

    def test_ready_listing_still_shows_unpicked(self):
        t = self.task(epic_id=self.epic(DIRECTOR))
        self.assertEqual([r["id"] for r in ops.ready_tasks(self.conn)], [t["id"]])


if __name__ == "__main__":
    unittest.main()
