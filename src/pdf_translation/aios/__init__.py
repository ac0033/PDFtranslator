"""The AI OS: the part of a job that finishes the work when the workflow alone cannot.

A job runs the deterministic translation workflow first. The decision node
(:mod:`.decision`) then reads the acceptance report: whether the version is
usable is decided by code (the delivery rules of AGENTS.md section 2); Jev
(:mod:`.jev`), when the caller provided a key, triages the remaining warnings
and picks a repair action for each problem. The supervisor
(:mod:`.supervisor`) carries the actions out, calling the workflow's own
nodes, and hands whatever the rules cannot handle to the pi agent
(:mod:`.pi_agent`), which works through this service's tools only
(:mod:`.tools`). Hard limits - rounds, budget, time, progress - are enforced
by the supervisor, never left to a prompt. The optimiser (:mod:`.optimize`)
improves a version that already passed, and keeps it only if it is not worse.
"""
