"""The knowledge policy: which lifecycle actions on facts an agent may take alone.

The warehouse gets hard rules — read-only by parser, fixes applied by humans —
because a warehouse write is irreversible and shared. A fact lives in a git
repo: every write is attributed, every lifecycle action lands as its own
commit, and a revert undoes one. Those deserve different rules, so where the
warehouse has a wall the library has a *policy*: each action names its actor,
``agent`` or ``user``, and the human picks.

Two things stay outside the policy, because they are not permissions:

- **Evidence.** Whoever retires or supersedes a fact must say what falsified
  it — a query id, an intervention id, a dropped column, a changed definition,
  a record. The store enforces that for agents whatever the policy says: an
  agent may act alone, but not on vibes.
- **The confirmed label.** ``user_confirmed`` records that a human vouched;
  letting an agent set it would make the label false rather than the agent
  trusted. Authority for agent facts is the ``trust`` setting instead: which
  statuses a briefing ranks as knowledge rather than hypotheses, and which may
  displace a confirmed fact when a supersession executes.

Blast radius decides where a policy lives. A solo workspace sets it in
grayson.toml. A team library sets one in library.toml, admin-owned and
travelling with the repo, because one analyst's permissive agent writes into
everyone's briefings; a workspace may narrow the team policy, never widen it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Actor = Literal["agent", "user"]

#: lifecycle actions the policy governs, in the order surfaces list them
ACTIONS: tuple[str, ...] = (
    "retire",
    "supersede",
    "dismiss_question",
    "reconcile",
    "resolve_contested",
    "restore",
)

ACTION_HELP: dict[str, str] = {
    "retire": "retire a fact, citing what falsified it",
    "supersede": "execute a supersession when recording a corrected fact (else it waits "
    "for the human's confirm)",
    "dismiss_question": "retire an open question as moot, with a reason",
    "reconcile": "run the reconcile pass and commit its materialized standing",
    "resolve_contested": "mark two contested facts compatible (judgment, no evidence)",
    "restore": "restore a retired, stale or unverified fact to current (judgment)",
}

#: which statuses count as knowledge in a briefing, lowest admitted first.
#: ``data_inferred`` (the default) admits confirmed and data-inferred facts and
#: ranks proposed ones as hypotheses — the stance the knowledge-only server
#: already states in prose.
TRUST_LEVELS: tuple[str, ...] = ("user_confirmed", "data_inferred", "proposed")
STATUS_RANK: dict[str, int] = {"proposed": 0, "data_inferred": 1, "user_confirmed": 2}

#: named presets. ``propose``: the agent proposes everything and a human acts.
#: ``curate``: evidence-backed actions to the agent, judgment-only ones to the
#: human. ``autonomous``: the agent does all of it; the human audits after.
PRESETS: dict[str, dict[str, Actor]] = {
    "propose": dict.fromkeys(ACTIONS, "user"),
    "curate": {
        "retire": "agent",
        "supersede": "agent",
        "dismiss_question": "agent",
        "reconcile": "agent",
        "resolve_contested": "user",
        "restore": "user",
    },
    "autonomous": dict.fromkeys(ACTIONS, "agent"),
}

DEFAULT_SOLO_PRESET = "curate"
#: a team library that has not chosen is one notch stricter than a solo
#: workspace: an admin widens it with `grayson library policy set`
DEFAULT_TEAM_PRESET = "propose"
DEFAULT_TRUST = "data_inferred"
DEFAULT_PROPOSED_HORIZON_DAYS = 90
DEFAULT_BRIEFING_CAP = 12
DEFAULT_AGENT_WINDOW_DAYS = 30


class PolicyError(ValueError):
    """A policy value that does not parse; the message says what to fix."""


class KnowledgePolicy(BaseModel):
    """One side's policy (a workspace's or a library's), fully resolved."""

    preset: str = DEFAULT_SOLO_PRESET
    actions: dict[str, Actor] = Field(default_factory=lambda: dict(PRESETS[DEFAULT_SOLO_PRESET]))
    trust: str = DEFAULT_TRUST
    proposed_horizon_days: int = Field(default=DEFAULT_PROPOSED_HORIZON_DAYS, ge=0)
    briefing_cap: int = Field(default=DEFAULT_BRIEFING_CAP, ge=0)
    agent_window_days: int = Field(default=DEFAULT_AGENT_WINDOW_DAYS, ge=1)
    #: which of the tunables the source actually set (a default is not a choice,
    #: so `meet` knows whether a library's horizon overrides a workspace's)
    explicit: list[str] = Field(default_factory=list)

    @field_validator("preset")
    @classmethod
    def _preset(cls, v: str) -> str:
        if v not in PRESETS:
            raise ValueError(
                f"unknown knowledge policy preset {v!r} (presets: {', '.join(PRESETS)})"
            )
        return v

    @field_validator("trust")
    @classmethod
    def _trust(cls, v: str) -> str:
        if v not in TRUST_LEVELS:
            raise ValueError(f"trust must be one of {', '.join(TRUST_LEVELS)}, got {v!r}")
        return v

    @classmethod
    def from_preset(cls, preset: str, overrides: dict[str, Any] | None = None, **fields: Any):
        """A policy from a preset name plus per-action overrides."""
        if preset not in PRESETS:
            raise PolicyError(
                f"unknown knowledge policy preset {preset!r} (presets: {', '.join(PRESETS)})"
            )
        actions = dict(PRESETS[preset])
        for action, actor in (overrides or {}).items():
            if action not in ACTIONS:
                raise PolicyError(
                    f"unknown knowledge action {action!r} (actions: {', '.join(ACTIONS)})"
                )
            if actor not in ("agent", "user"):
                raise PolicyError(f"{action}: actor must be 'agent' or 'user', got {actor!r}")
            actions[action] = actor
        try:
            return cls(preset=preset, actions=actions, explicit=sorted(fields), **fields)
        except ValueError as e:
            raise PolicyError(str(e)) from e

    @classmethod
    def from_config(cls, section: dict[str, Any], default_preset: str = DEFAULT_SOLO_PRESET):
        """Parse a `[knowledge]` table (grayson.toml) — preset, `[knowledge.agent]`
        overrides, trust, horizon, cap. Missing keys take the defaults."""
        preset = str(section.get("policy") or default_preset)
        raw = section.get("agent") or {}
        if not isinstance(raw, dict):
            raise PolicyError("[knowledge.agent] must be a table of action = 'agent'|'user'")
        fields: dict[str, Any] = {}
        if section.get("trust") is not None:
            fields["trust"] = str(section["trust"])
        for key in ("proposed_horizon_days", "briefing_cap", "agent_window_days"):
            if section.get(key) is not None:
                fields[key] = _int(key, section[key])
        return cls.from_preset(preset, {str(k): str(v) for k, v in raw.items()}, **fields)

    @classmethod
    def from_library_settings(cls, settings: dict[str, Any]) -> KnowledgePolicy | None:
        """The team's policy from library.toml's flat `[library]` table:
        `knowledge_policy` (preset), `knowledge_agent_denied` (actions withheld
        from agents whatever the preset says), `knowledge_trust`,
        `knowledge_proposed_horizon_days`. None when the library says nothing."""
        keys = ("knowledge_policy", "knowledge_agent_denied", "knowledge_trust")
        if not any(settings.get(k) is not None for k in keys) and (
            settings.get("knowledge_proposed_horizon_days") is None
        ):
            return None
        preset = str(settings.get("knowledge_policy") or DEFAULT_TEAM_PRESET)
        denied = settings.get("knowledge_agent_denied") or []
        if not isinstance(denied, list):
            raise PolicyError("knowledge_agent_denied must be a list of action names")
        overrides: dict[str, str] = {}
        for action in denied:
            if str(action) not in ACTIONS:
                raise PolicyError(
                    f"knowledge_agent_denied names an unknown action {action!r} "
                    f"(actions: {', '.join(ACTIONS)})"
                )
            overrides[str(action)] = "user"
        fields: dict[str, Any] = {}
        if settings.get("knowledge_trust") is not None:
            fields["trust"] = str(settings["knowledge_trust"])
        if settings.get("knowledge_proposed_horizon_days") is not None:
            fields["proposed_horizon_days"] = _int(
                "knowledge_proposed_horizon_days", settings["knowledge_proposed_horizon_days"]
            )
        return cls.from_preset(preset, overrides, **fields)

    def actor(self, action: str) -> Actor:
        if action not in ACTIONS:
            raise PolicyError(
                f"unknown knowledge action {action!r} (actions: {', '.join(ACTIONS)})"
            )
        return self.actions[action]

    def admits(self, status: str) -> bool:
        """Whether a fact of this status counts as knowledge (not a hypothesis)."""
        return STATUS_RANK.get(status, -1) >= STATUS_RANK[self.trust]

    def denied_actions(self) -> list[str]:
        return [a for a in ACTIONS if self.actions[a] == "user"]

    def summary(self) -> dict:
        return {
            "preset": self.preset,
            "actions": {a: self.actions[a] for a in ACTIONS},
            "trust": self.trust,
            "proposed_horizon_days": self.proposed_horizon_days,
            "briefing_cap": self.briefing_cap,
            "agent_window_days": self.agent_window_days,
        }


def _int(key: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise PolicyError(f"{key} must be an integer, got {value!r}") from e


class EffectivePolicy(BaseModel):
    """What actually governs a workspace: the meet of its own policy and the
    team library's. An action is the agent's only when both sides say so; the
    stricter trust wins; the library's horizon, when it sets one, is the shared
    reconcile behaviour and stands. `narrowed_by` names, per action, the side
    that withheld it — so a refusal can say which file to change."""

    workspace: KnowledgePolicy
    library: KnowledgePolicy | None = None
    actions: dict[str, Actor]
    trust: str
    proposed_horizon_days: int
    briefing_cap: int
    agent_window_days: int
    narrowed_by: dict[str, str] = Field(default_factory=dict)
    library_default: bool = False

    def actor(self, action: str) -> Actor:
        if action not in ACTIONS:
            raise PolicyError(
                f"unknown knowledge action {action!r} (actions: {', '.join(ACTIONS)})"
            )
        return self.actions[action]

    def allows_agent(self, action: str) -> bool:
        return self.actor(action) == "agent"

    def admits(self, status: str) -> bool:
        return STATUS_RANK.get(status, -1) >= STATUS_RANK[self.trust]

    def refusal(self, action: str) -> str:
        """Why an agent may not take `action`, naming the setting to change."""
        side = self.narrowed_by.get(action, "workspace")
        if side == "library":
            where = (
                f"the team library's policy (library.toml: knowledge_policy = "
                f"'{self.library.preset if self.library else DEFAULT_TEAM_PRESET}'"
                + (", the default when unset" if self.library_default else "")
                + ") — an admin widens it with `grayson library policy set`"
            )
        else:
            where = (
                f"this workspace's policy (grayson.toml: [knowledge] policy = "
                f"'{self.workspace.preset}') — `grayson library policy set` changes it"
            )
        return (
            f"{ACTION_HELP.get(action, action)} is a user action under {where}. Ask the "
            "user to do it in the console or at their own prompt"
        )

    def summary(self) -> dict:
        return {
            "preset": self.workspace.preset,
            "actions": {a: self.actions[a] for a in ACTIONS},
            "trust": self.trust,
            "proposed_horizon_days": self.proposed_horizon_days,
            "briefing_cap": self.briefing_cap,
            "agent_window_days": self.agent_window_days,
            "narrowed_by": dict(self.narrowed_by),
            "workspace": self.workspace.summary(),
            "library": (
                {**self.library.summary(), "default": self.library_default}
                if self.library
                else None
            ),
            "action_help": dict(ACTION_HELP),
        }


def meet(
    workspace: KnowledgePolicy,
    library: KnowledgePolicy | None,
    library_default: bool = False,
) -> EffectivePolicy:
    """Combine a workspace policy with the library's (None in solo mode)."""
    if library is None:
        return EffectivePolicy(
            workspace=workspace,
            actions=dict(workspace.actions),
            trust=workspace.trust,
            proposed_horizon_days=workspace.proposed_horizon_days,
            briefing_cap=workspace.briefing_cap,
            agent_window_days=workspace.agent_window_days,
        )
    actions: dict[str, Actor] = {}
    narrowed: dict[str, str] = {}
    for action in ACTIONS:
        ws, lib = workspace.actions[action], library.actions[action]
        if ws == "agent" and lib == "agent":
            actions[action] = "agent"
        else:
            actions[action] = "user"
            narrowed[action] = "library" if lib == "user" else "workspace"
    # the stricter trust admits fewer statuses: the higher rank threshold wins
    trust = max((workspace.trust, library.trust), key=lambda t: STATUS_RANK[t])
    horizon = (
        library.proposed_horizon_days
        if "proposed_horizon_days" in library.explicit
        else workspace.proposed_horizon_days
    )
    return EffectivePolicy(
        workspace=workspace,
        library=library,
        actions=actions,
        trust=trust,
        proposed_horizon_days=horizon,
        briefing_cap=workspace.briefing_cap,
        agent_window_days=workspace.agent_window_days,
        narrowed_by=narrowed,
        library_default=library_default,
    )
