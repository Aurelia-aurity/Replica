"""Explicit seams only; Q1 identity resolution and Q2/Q13 generation are pending."""
from typing import Protocol

from inference import Prepared
from persona_contract import PersonaGenerated, PersonaRequest
from persona_guard import GuardResult, PersonaIdentity, check_reply


class PersonaGenerator(Protocol):
    """A generator must return typed data, independent of its future wire encoding."""

    def prepare(self, request: PersonaRequest) -> Prepared: ...

    def generate(self, request: PersonaRequest, prepared: Prepared) -> PersonaGenerated: ...


class GuardIdentityPending(RuntimeError):
    """Q1: the wire contract does not yet supply an agreed guard identity."""


def check_candidate(message: str, identity: PersonaIdentity | None = None) -> GuardResult:
    """Never infer aliases/register from free-text tone, names, or history.

    None is an explicit pending placeholder, not a disabled/pass-through guard.
    Applying replacement, evidence clearing, and finish_reason needs Q14 agreement.
    """
    if identity is None:
        raise GuardIdentityPending
    return check_reply(message, identity)
