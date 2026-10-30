---
schema_version: 1
id: ADR-0011
status: accepted
date: "2026-10-29"
decision-makers: [johnm]
applies_to: ["drover/**", "scripts/deploy_hub.py"]
enforcement: null
supersedes: []
links: []
---

# CI outcome contract

CI outcomes must distinguish passed, code-failed, infrastructure-failed,
termination-unknown and deferred. Each carries the reason, failing modules or
tests, commit, platform, gate kind and available run and nonce identities.

The supervisor establishes infrastructure failure and termination certainty.
A command's output cannot establish either: parsed failures belong to the code
outcome even when output contains an infrastructure-looking message. An unknown
termination holds the checkout and capacity until the runner is confirmed stopped;
it must not cause a relaunch.

Dispatcher, landing and deploy validate receipts against the saved plan, including
commit, platform, kind, version and the required platform set. Missing or mismatched
evidence cannot advance a stage. Landing retains its existing recovery and runner
adoption behavior.

A stable infrastructure failure receives one automatic retry on a healthy host,
then one owner question with the evidence. Code failures return to the worker with
the failing tests named. An explicit owner continue starts a named recovery episode
rather than silently resetting unrelated budgets.

Legacy receipts without the required evidence cannot advance a stage. Previously
issued runners remain eligible for adoption through their saved supervisor association.
