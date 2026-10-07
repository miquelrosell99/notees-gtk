"""Workspace feature map (RESHAPED per owner directive 2026-10-04)
— the per-workspace feature toggles ARE the core class families:
tasks=task, events=event, meetings=meeting, sources=source, persons=person.

``packages/domain/src/features.ts`` parity for the GTK client. The TS domain
resolves the family set through the STATIC seed knowledge
(``SYSTEM_CLASS_EXTENDS`` in ``packages/domain/src/seeds.ts``) — never through
the store's ``class_extends`` table — so the applier's archival re-derivation
converges on replicas that have not seen the seed envelopes yet. The same
static map is ported here (the GTK client has no seed manifest of its own;
seeding is server-side, and the store only needs the family-relevant subset of
the fixed system-class UUIDs — never rename, never regenerate, never reuse —
plus the #14 follow-up five, owner list 2026-10-06: the deploy catalog's
missing everyday classes, trip extending event so the events cascade reaches
it).

Semantics (F1–F4, owner-confirmed, mirroring the TS record):

- Toggle-off is NEVER deletion (F3): the store applier flips the family
  classes' registry ``active`` bit + class-node ``is_active``,
  leaving ``class_member_set`` untouched — instances keep their class_ids and
  stay in the graph.
- An absent ``workspace_feature`` row means ENABLED (F2 — all families
  default ON; the empty table is the pre-toggle state, zero migration).
- A ``class.delete`` addressed at a family's BASE class is routed to the
  toggle (F4): applied as a feature-disable so the Features setting is the
  single archive path and the lossy plain delete (membership tombstoning)
  never runs on managed classes. Family CHILDREN (book, meeting, birthday,
  …) are NOT F4 bases — a delete on those keeps plain semantics (the owner
  mapped F4 to the five bases exactly).
- The base system stays always-on (F1): journals (year/month/day), assets,
  the structural + admonition classes, and the classes of the DROPPED
  pre-reshape features (readItLater/library/collections/people → highlight,
  weblink, collection, agent, organization, …) — never feature-managed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ALWAYS_ON_SYSTEM_CLASSES",
    "SYSTEM_CLASS_DISPLAY_NAMES",
    "SYSTEM_CLASS_ICONS",
    "TASK_FAMILY_SEED",
    "TASK_PRIORITY_OPTION_UUIDS",
    "TASK_STATUS_OPTION_UUIDS",
    "WORKSPACE_FEATURES",
    "WORKSPACE_FEATURE_MAP",
    "WorkspaceFeature",
    "TaskFamilySeedEntry",
    "TaskSeedOption",
    "family_class_names",
    "feature_for_managed_class",
    "gating_features_for_class",
    "is_always_on_system_class",
    "managed_class_ids",
    "system_class_ancestors",
    "system_class_uuid",
    "task_property_uuid",
]

#: The five core class families (owner directive 2026-10-04) — the
#: strict ``workspace.feature.set`` enum (op-types.ts ``WORKSPACE_FEATURES``).
WorkspaceFeature = Literal["tasks", "events", "meetings", "sources", "persons"]
WORKSPACE_FEATURES: tuple[WorkspaceFeature, ...] = ("tasks", "events", "meetings", "sources", "persons")

#: The family-relevant system-class UUIDs (seeds.ts subset — the applier's
#: flip list and F4 routing resolve through these fixed ids), plus the two
#: ids the property/applier semantics resolve through: ``asset`` (the
#: implicit filter of an asset-typed property schema, M38) and ``weblink``
#: (the source-family child, the 2026-10-07 convergence), plus the #14
#: follow-up five (owner list, 2026-10-06 — the deploy catalog's missing
#: everyday classes, plain seeds per the meeting-system precedent).
_SYSTEM_CLASS_UUIDS: dict[str, str] = {
    "asset": "00000000-0000-0000-0001-000000000009",
    "task": "00000000-0000-0000-0001-000000000012",
    "source": "00000000-0000-0000-0001-000000000023",
    "book": "00000000-0000-0000-0001-000000000024",
    "paper": "00000000-0000-0000-0001-000000000025",
    "article": "00000000-0000-0000-0001-000000000026",
    "thesis": "00000000-0000-0000-0001-000000000027",
    "document": "00000000-0000-0000-0001-000000000028",
    "agent": "00000000-0000-0000-0001-000000000029",
    "person": "00000000-0000-0000-0001-000000000030",
    "organization": "00000000-0000-0000-0001-000000000031",
    "movie": "00000000-0000-0000-0001-000000000035",
    "song": "00000000-0000-0000-0001-000000000036",
    "tv_series": "00000000-0000-0000-0001-000000000037",
    "conference": "00000000-0000-0000-0001-000000000038",
    "meeting": "00000000-0000-0000-0001-000000000039",
    "event": "00000000-0000-0000-0001-000000000040",
    "birthday": "00000000-0000-0000-0001-000000000041",
    "weblink": "00000000-0000-0000-0001-000000000034",
    # The #14 follow-up five (owner list, 2026-10-06): the deploy catalog's
    # missing everyday classes — plain seeds per the meeting-system ruling
    # (zero wire cost, seed convergence only; seeds.ts …0043-…0047).
    "definition": "00000000-0000-0000-0001-000000000043",
    "idea": "00000000-0000-0000-0001-000000000044",
    "place": "00000000-0000-0000-0001-000000000045",
    "project": "00000000-0000-0000-0001-000000000046",
    "trip": "00000000-0000-0000-0001-000000000047",
}

#: The task class icon (seeds.ts ``SYSTEM_CLASS_ICONS`` subset — the
#: enable-path family ensure authors it on the class rows).
SYSTEM_CLASS_ICONS_TASK = "mdiCheckboxMarkedCircleOutline"

#: Static canonical ``extends`` edges between the system classes
#: (seeds.ts ``SYSTEM_CLASS_EXTENDS`` family subset). The family sets and the
#: chrome gating walk resolve through THIS map — the store's ``class_extends``
#: table is authored by ops and may not exist on a fresh replica.
_SYSTEM_CLASS_EXTENDS: dict[str, tuple[str, ...]] = {
    "book": ("source",),
    "paper": ("source",),
    "article": ("source",),
    "thesis": ("source",),
    "document": ("source",),
    "movie": ("source",),
    "song": ("source",),
    "tv_series": ("source",),
    "conference": ("source",),
    "person": ("agent",),
    "organization": ("agent",),
    "meeting": ("event",),
    "birthday": ("event",),
    # The web link IS a source (owner ruling, 2026-10-07 convergence): a
    # bookmarked page is a cited web source — weblink inherits the source
    # family's bibliographic bindings, and disabling `source` hides
    # weblinks with the rest of the family.
    "weblink": ("source",),
    # A trip IS an event (the #14 follow-up, owner list 2026-10-06): a trip
    # is calendar-bound, so the events toggle cascades to it — this map is
    # the cascade authority (seeds.ts ``SYSTEM_CLASS_EXTENDS`` parity).
    "trip": ("event",),
}

#: The #14 follow-up five's seed decorations (seeds.ts ``SYSTEM_CLASS_ICONS``
#: / ``SYSTEM_CLASS_DISPLAY_NAMES`` subset — the GTK map's classes only).
#: The server seed authors the display titles into the class nodes' text
#: content (title-is-content) and the icons onto the class rows.
SYSTEM_CLASS_ICONS: dict[str, str] = {
    "definition": "mdiBookOpenPageVariant",
    "idea": "mdiThoughtBubbleOutline",
    "place": "mdiMapMarkerOutline",
    "project": "mdiBriefcaseOutline",
    "trip": "mdiAirplane",
}

#: The display titles the seed authors for the five (seeds.ts
#: ``SYSTEM_CLASS_DISPLAY_NAMES`` subset; the raw keys stay code-facing
#: vocabulary — these are the human wordings).
SYSTEM_CLASS_DISPLAY_NAMES: dict[str, str] = {
    "definition": "Definition",
    "idea": "Idea",
    "place": "Place",
    "project": "Project",
    "trip": "Trip",
}


def system_class_ancestors(name: str) -> frozenset[str]:
    """The transitive ancestor set over the static extends map."""
    ancestors: set[str] = set()
    stack = list(_SYSTEM_CLASS_EXTENDS.get(name, ()))
    while stack:
        current = stack.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        stack.extend(_SYSTEM_CLASS_EXTENDS.get(current, ()))
    return frozenset(ancestors)


@dataclass(frozen=True)
class WorkspaceFeatureSpec:
    """One family's settings-tab metadata (the TS ``WorkspaceFeatureSpec``)."""

    base_class: str
    label: str
    powers: str


#: The core class families (owner directive 2026-10-04).
WORKSPACE_FEATURE_MAP: dict[WorkspaceFeature, WorkspaceFeatureSpec] = {
    "tasks": WorkspaceFeatureSpec("task", "Tasks", "Tasks hub + checkbox gestures"),
    "events": WorkspaceFeatureSpec("event", "Events", "The calendar day/month surfaces"),
    "meetings": WorkspaceFeatureSpec("meeting", "Meetings", "Meeting quick-create + meeting logic"),
    "sources": WorkspaceFeatureSpec("source", "Sources", "The source family + citation import/export"),
    "persons": WorkspaceFeatureSpec("person", "Persons", "The people graph + contact fields"),
}

_BASE_FEATURE_BY_CLASS: dict[str, WorkspaceFeature] = {
    spec.base_class: feature for feature, spec in WORKSPACE_FEATURE_MAP.items()
}


def system_class_uuid(name: str) -> str:
    """The fixed UUID of a family-relevant system class (never regenerate)."""
    return _SYSTEM_CLASS_UUIDS[name]


def feature_for_base_class(name: str) -> WorkspaceFeature | None:
    """The feature whose BASE class is ``name``, if any."""
    return _BASE_FEATURE_BY_CLASS.get(name)


#: Always-on system classes (F1) — never feature-managed: the base system
#: (journals year/month/day, asset, query, code, card, template, comment,
#: table, cloze, whiteboard, the admonition set) plus the classes of the
#: DROPPED pre-reshape features (highlight, collection, agent, organization).
#: The five family bases and their extends-children are NOT here — they are
#: the toggle set, weblink included since it became a source-family child
#: (2026-10-07). The retired `class` meta class (…0001, withdrawn
#: 2026-10-07) left the manifest with its retirement — it is nobody's
#: anchor anymore. The #14 follow-up five (2026-10-06, owner list) are not
#: here either: trip rides the events family through its extends edge, and
#: the four plain seeds (definition, idea, place, project) are unmanaged —
#: no gating, not always-on (features.ts semantics — they are absent from
#: the TS ``ALWAYS_ON_SYSTEM_CLASSES`` too).
ALWAYS_ON_SYSTEM_CLASSES: tuple[str, ...] = (
    "year",
    "month",
    "day",
    "asset",
    "query",
    "code",
    "card",
    "template",
    "comment",
    "table",
    "cloze",
    "whiteboard",
    "note",
    "tip",
    "info",
    "warning",
    "danger",
    "success",
    "quote",
    "agent",
    "organization",
    "collection",
    "highlight",
)


def family_class_names(feature: WorkspaceFeature) -> tuple[str, ...]:
    """The family's full class set: the base class + its transitive
    extends-children (deterministic, sorted after the base). Disabling the
    family archives exactly this set (the applier's per-class re-derivation
    iterates it)."""
    base = WORKSPACE_FEATURE_MAP[feature].base_class
    children = sorted(name for name in _SYSTEM_CLASS_UUIDS if name != base and base in system_class_ancestors(name))
    return (base, *children)


def managed_class_ids(feature: WorkspaceFeature) -> tuple[str, ...]:
    """Resolved UUIDs of the family's full class set (the applier's flip list)."""
    return tuple(_SYSTEM_CLASS_UUIDS[name] for name in family_class_names(feature))


def feature_for_managed_class(class_id: str) -> WorkspaceFeature | None:
    """F4 routing: the owning feature when ``class_id`` is a family's BASE
    class, else None. Children (book, meeting, birthday, …) deliberately do
    NOT route — the owner mapped F4 to the five bases exactly. The route
    decision is a pure function of the class id (fixed vocabulary), so every
    replica takes the same branch."""
    for feature, spec in WORKSPACE_FEATURE_MAP.items():
        if _SYSTEM_CLASS_UUIDS[spec.base_class] == class_id:
            return feature
    return None


def gating_features_for_class(name: str) -> tuple[WorkspaceFeature, ...]:
    """Chrome gating: the features whose OFF state hides a class's
    surfaces — its own feature when it is a family base, plus the feature of
    every family-base ANCESTOR (the static extends walk). Empty for
    always-on/unmanaged classes. A class's chrome shows only when EVERY
    listed feature is enabled (a meeting surface hides when MEETINGS or
    EVENTS is off; a birthday surface hides when EVENTS is off; book/paper/…
    hide when SOURCES is off)."""
    gating: list[WorkspaceFeature] = []
    own = feature_for_base_class(name)
    if own is not None:
        gating.append(own)
    for ancestor in sorted(system_class_ancestors(name)):
        feature = feature_for_base_class(ancestor)
        if feature is not None and feature not in gating:
            gating.append(feature)
    return tuple(gating)


def is_always_on_system_class(name: str) -> bool:
    """True when ``name`` is an always-on (not toggleable) system class (F1)."""
    return name in ALWAYS_ON_SYSTEM_CLASSES


# --------------------------------------------------------------------- task seed
#
# Deterministic select-option ids for the APPLIER-side
# task-family seed-ensure (the ``workspace.feature.set {feature:"tasks",
# enabled:true}`` path authors the six schemas at apply time, so its option
# ids must be fixed, not client-random). The select-option namespace
# (``…0004-…``) continues after the role options (``…0001–…0007``); appended,
# never reused (seeds.ts ``TASK_STATUS_OPTION_UUIDS`` /
# ``TASK_PRIORITY_OPTION_UUIDS``).
TASK_STATUS_OPTION_UUIDS: dict[str, str] = {
    "backlog": "00000000-0000-0000-0004-000000000008",
    "pending": "00000000-0000-0000-0004-000000000009",
    "doing": "00000000-0000-0000-0004-00000000000a",
    "reviewing": "00000000-0000-0000-0004-00000000000b",
    "done": "00000000-0000-0000-0004-00000000000c",
    "cancelled": "00000000-0000-0000-0004-00000000000d",
}

TASK_PRIORITY_OPTION_UUIDS: dict[str, str] = {
    "low": "00000000-0000-0000-0004-00000000000e",
    "medium": "00000000-0000-0000-0004-00000000000f",
    "high": "00000000-0000-0000-0004-000000000010",
    "urgent": "00000000-0000-0000-0004-000000000011",
}

#: The task-family property-schema UUIDs (seeds.ts ``SYSTEM_PROPERTY_UUIDS``
#: task block ``…0003-…``).
_TASK_PROPERTY_UUIDS: dict[str, str] = {
    "taskStatus": "00000000-0000-0000-0003-000000000001",
    "taskDeadline": "00000000-0000-0000-0003-000000000002",
    "taskScheduled": "00000000-0000-0000-0003-000000000003",
    "taskPriority": "00000000-0000-0000-0003-000000000004",
    "taskClosedDate": "00000000-0000-0000-0003-000000000005",
    "taskRecurrence": "00000000-0000-0000-0003-000000000006",
}


@dataclass(frozen=True)
class TaskSeedOption:
    """One designed select option (TS manifest row): fixed id + label plus
    the decoration (MDI icon name, color token) — absent = the
    key is omitted from the authored options JSON (zod optional parity)."""

    id: str
    label: str
    icon: str | None = None
    color: str | None = None


@dataclass(frozen=True)
class TaskFamilySeedEntry:
    """One schema + binding the enable path authors (TS manifest row)."""

    property: str
    name: str
    type: Literal["select", "date"]
    sequence: int
    options: tuple[TaskSeedOption, ...] = ()
    # Owner review (property-LEVEL): the value-display position rides
    # the SCHEMA entry the ensure authors — only the Status schema defaults
    # to "bullet" (the status value rides the block bullet as an icon
    # button); the rest stay NULL ("panel", the properties section).
    display: Literal["panel", "bullet", "inline"] | None = None


#: The task-family seed-ensure manifest: six schemas + their task-class
#: bindings, authored idempotently by the store applier when the ``tasks``
#: feature enables (closes the "task property schemas never authored" gap).
#: Fixed ids end to end (schema + option uuids
#: above); ``sequence`` is the task-panel display order. The Status options
#: carry the designed glyphs (seeds.ts ``TASK_STATUS_OPTIONS``): the
#: circle-family MDI icons with a distinct color each, so a task's state
#: reads at a glance from the block bullet. ``display`` lands on
#: the property_schema row, never the binding.
TASK_FAMILY_SEED: tuple[TaskFamilySeedEntry, ...] = (
    TaskFamilySeedEntry(
        "taskStatus",
        "Status",
        "select",
        1,
        (
            TaskSeedOption(TASK_STATUS_OPTION_UUIDS["backlog"], "Backlog", "mdiCircleOutline", "gray"),
            TaskSeedOption(TASK_STATUS_OPTION_UUIDS["pending"], "Pending", "mdiCircle", "yellow"),
            TaskSeedOption(TASK_STATUS_OPTION_UUIDS["doing"], "Doing", "mdiCircleHalfFull", "orange"),
            TaskSeedOption(TASK_STATUS_OPTION_UUIDS["reviewing"], "Reviewing", "mdiEyeCircleOutline", "blue"),
            TaskSeedOption(TASK_STATUS_OPTION_UUIDS["done"], "Done", "mdiCheckCircle", "green"),
            TaskSeedOption(TASK_STATUS_OPTION_UUIDS["cancelled"], "Cancelled", "mdiCloseCircle", "red"),
        ),
        display="bullet",
    ),
    TaskFamilySeedEntry("taskScheduled", "Scheduled", "date", 2),
    TaskFamilySeedEntry("taskDeadline", "Deadline", "date", 3),
    TaskFamilySeedEntry(
        "taskPriority",
        "Priority",
        "select",
        4,
        (
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["low"], "Low"),
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["medium"], "Medium"),
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["high"], "High"),
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["urgent"], "Urgent"),
        ),
    ),
    TaskFamilySeedEntry("taskClosedDate", "Closed", "date", 5),
    # Migrated recurrence rides as a plain select (no engine executes it);
    # authored optionless until the recurrence spec lands.
    TaskFamilySeedEntry("taskRecurrence", "Recurrence", "select", 6),
)


def task_property_uuid(name: str) -> str:
    """The fixed schema id for a task-family property name."""
    return _TASK_PROPERTY_UUIDS[name]
