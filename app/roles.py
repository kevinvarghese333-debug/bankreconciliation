"""Roles and what each may do. Higher roles include everything below them."""
from __future__ import annotations

ROLES = ["viewer", "operator", "manager", "admin"]          # lowest → highest

LABELS = {
    "viewer": "Viewer",
    "operator": "Operator",
    "manager": "Manager",
    "admin": "Admin",
}

DESCRIPTIONS = {
    "viewer": "Read-only: dashboard, runs, open items, history; download Excel.",
    "operator": "Everything a Viewer can, plus upload and reconcile, manual match, notes, reviews, and adding or removing holidays.",
    "manager": "Everything an Operator can, plus acknowledge warnings, unmatch, undo the latest run, "
               "sign off runs as checked, view the audit log, and get inbox alerts "
               "(timing differences and entries booked late that operators' runs adjusted).",
    "admin": "Everything, plus the Admin portal: users and roles, revoking reconciled days, and bank accounts.",
}

# permission -> minimum role
PERMS = {
    "view": "viewer",
    "reconcile": "operator",
    "match": "operator",
    "note": "operator",
    "ack": "manager",
    "unmatch": "manager",
    "undo": "manager",
    "signoff": "manager",
    "holidays": "operator",
    "audit": "manager",
    "inbox": "manager",
    "accounts": "admin",
    "admin": "admin",
}

ACTION_TEXT = {
    "reconcile": "run reconciliations",
    "match": "match items",
    "note": "add notes",
    "ack": "acknowledge warnings",
    "unmatch": "remove matches",
    "undo": "undo runs",
    "signoff": "sign off runs",
    "holidays": "change holidays",
    "audit": "view the audit log",
    "inbox": "open the inbox",
    "accounts": "change bank accounts",
    "admin": "open the Admin portal",
}


def can(role: str | None, perm: str) -> bool:
    if not role or role not in ROLES:
        return False
    need = PERMS.get(perm, "admin")
    return ROLES.index(role) >= ROLES.index(need)
