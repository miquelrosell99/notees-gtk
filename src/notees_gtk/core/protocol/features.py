"""Workspace feature map (RESHAPED)
 the per-workspace feature toggles ARE the core class families:
tasks=task, events=event, meetings=meeting, sources=source, persons=person.

``packages/domain/src/features.ts`` parity for the GTK client. The TS domain
resolves the family set through the STATIC seed knowledge
(``SYSTEM_CLASS_EXTENDS`` in ``packages/domain/src/seeds.ts``) — never through
the store's ``class_extends`` table — so the applier's archival re-derivation
converges on replicas that have not seen the seed envelopes yet. The same
static map is ported here (the GTK client has no seed manifest of its own;
seeding is server-side; the map carries the family-relevant subset of the
fixed system-class UUIDs — never rename, never regenerate, never reuse —
plus the #14 follow-up five: the deploy catalog's
missing everyday classes, trip extending event so the events cascade reaches
it). The system-extends lock additionally needs
EVERY fixed system-class UUID — the seeded extends mapping is frozen
platform structure — so the full seeds.ts ``SYSTEM_CLASS_UUIDS`` block is
carried below and ``SYSTEM_CLASS_EXTENDS_IDS`` / ``is_system_class_id``
derive the lock authority from it.

Semantics (F1–F3 + the class-node delete routing, owner-confirmed,
mirroring the TS record; superseding F4 — the retired ``class.delete`` op):

- Toggle-off is NEVER deletion (F3): the store applier flips the family
  classes' registry ``active`` bit + class-node ``is_active``,
  leaving ``class_member_set`` untouched — instances keep their class_ids and
  stay in the graph.
- An absent ``workspace_feature`` row means ENABLED (F2 — all families
  default ON; the empty table is the pre-toggle state, zero migration).
- Class-node delete routing: ``object.delete`` on
  a LOCKED system class (``LOCKED_SYSTEM_CLASSES`` below) fails loud; on a
  family BASE class it is routed to the toggle (applied as a
  feature-disable — the Features setting is the single archive path for the
  families); on any other non-locked system class it archives
  (``active = false``), never trash; on a user class it is a plain trash
  row. Membership RIDES in every arm — ``class_member_set`` is never
  touched by class removal (archive or trash); stripping a class from nodes
  stays the explicit per-node ``class.unassign``.
- The base system stays always-on (F1): journals (year/month/day), assets,
  the structural + admonition classes, and the classes of the DROPPED
  pre-reshape features (readItLater/library/collections/people → highlight,
  weblink, collection, agent, organization, …) — never feature-managed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeGuard

__all__ = [
    "ALWAYS_ON_SYSTEM_CLASSES",
    "LOCKED_SYSTEM_CLASSES",
    "SYSTEM_CLASS_DISPLAY_NAMES",
    "SYSTEM_CLASS_EXTENDS_IDS",
    "SYSTEM_CLASS_ICONS",
    "TASK_FAMILY_SEED",
    "TASK_PRIORITY_OPTION_UUIDS",
    "TASK_STATUS_OPTION_UUIDS",
    "WORKSPACE_FEATURES",
    "WORKSPACE_FEATURE_MAP",
    "FamilyFeature",
    "WorkspaceFeature",
    "TaskFamilySeedEntry",
    "TaskSeedOption",
    "family_class_names",
    "feature_for_managed_class",
    "gating_features_for_class",
    "is_always_on_system_class",
    "is_family_feature",
    "is_locked_system_class_id",
    "is_system_class_id",
    "managed_class_ids",
    "system_class_ancestors",
    "system_class_name_for_id",
    "system_class_uuid",
    "task_property_uuid",
]

#: The strict ``workspace.feature.set`` enum (op-types.ts ``WORKSPACE_FEATURES``):
#: the five core class families + ``journal`` — the surfaces-only toggle
#: (the journal nav view + Today-page management; the date classes stay
#: always-on structural seeds).
WorkspaceFeature = Literal["tasks", "events", "meetings", "sources", "persons", "journal"]
WORKSPACE_FEATURES: tuple[WorkspaceFeature, ...] = (
    "tasks",
    "events",
    "meetings",
    "sources",
    "persons",
    "journal",
)

#: The family toggles: the five entries that derive class archival bits
#: (features.ts ``FamilyFeature = Exclude<WorkspaceFeature, "journal">``).
FamilyFeature = Literal["tasks", "events", "meetings", "sources", "persons"]

#: The fixed system-class UUIDs (seeds.ts ``SYSTEM_CLASS_UUIDS`` FULL parity —
#: never rename, never regenerate, never reuse): the family-relevant ids the
#: feature map resolves through (the applier's flip list and F4 routing), the
#: two ids the property/applier semantics resolve through (``asset`` — the
#: implicit filter of an asset-typed property schema, M38 — and ``weblink`` —
#: the source-family child, the 2026-10-07 convergence), the #14 follow-up
#: five (the deploy catalog's missing everyday
#: classes, plain seeds per the meeting-system precedent), and the base-system
#: classes. The FULL block is carried (not just the family subset) because the
#: system-extends lock needs every fixed id — a
#: system class's seeded extends mapping is frozen platform structure.
_SYSTEM_CLASS_UUIDS: dict[str, str] = {
    "year": "00000000-0000-0000-0001-000000000003",
    "month": "00000000-0000-0000-0001-000000000004",
    "day": "00000000-0000-0000-0001-000000000005",
    "quote": "00000000-0000-0000-0001-000000000006",
    "query": "00000000-0000-0000-0001-000000000007",
    "code": "00000000-0000-0000-0001-000000000008",
    "asset": "00000000-0000-0000-0001-000000000009",
    "whiteboard": "00000000-0000-0000-0001-000000000010",
    "card": "00000000-0000-0000-0001-000000000011",
    "task": "00000000-0000-0000-0001-000000000012",
    "template": "00000000-0000-0000-0001-000000000013",
    "comment": "00000000-0000-0000-0001-000000000014",
    "table": "00000000-0000-0000-0001-000000000015",
    "warning": "00000000-0000-0000-0001-000000000016",
    "note": "00000000-0000-0000-0001-000000000017",
    "tip": "00000000-0000-0000-0001-000000000018",
    "info": "00000000-0000-0000-0001-000000000019",
    "danger": "00000000-0000-0000-0001-000000000020",
    "success": "00000000-0000-0000-0001-000000000021",
    "cloze": "00000000-0000-0000-0001-000000000022",
    "source": "00000000-0000-0000-0001-000000000023",
    "book": "00000000-0000-0000-0001-000000000024",
    "paper": "00000000-0000-0000-0001-000000000025",
    "article": "00000000-0000-0000-0001-000000000026",
    "thesis": "00000000-0000-0000-0001-000000000027",
    "document": "00000000-0000-0000-0001-000000000028",
    "agent": "00000000-0000-0000-0001-000000000029",
    "person": "00000000-0000-0000-0001-000000000030",
    "organization": "00000000-0000-0000-0001-000000000031",
    "collection": "00000000-0000-0000-0001-000000000032",
    "highlight": "00000000-0000-0000-0001-000000000033",
    "weblink": "00000000-0000-0000-0001-000000000034",
    "movie": "00000000-0000-0000-0001-000000000035",
    "song": "00000000-0000-0000-0001-000000000036",
    "tv_series": "00000000-0000-0000-0001-000000000037",
    "conference": "00000000-0000-0000-0001-000000000038",
    "meeting": "00000000-0000-0000-0001-000000000039",
    "event": "00000000-0000-0000-0001-000000000040",
    "birthday": "00000000-0000-0000-0001-000000000041",
    # The #14 follow-up five: the deploy catalog's
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
    # The web link IS a source (convergence): a
    # bookmarked page is a cited web source — weblink inherits the source
    # family's bibliographic bindings, and disabling `source` hides
    # weblinks with the rest of the family.
    "weblink": ("source",),
    # A trip IS an event (the #14 follow-up): a trip
    # is calendar-bound, so the events toggle cascades to it — this map is
    # the cascade authority (seeds.ts ``SYSTEM_CLASS_EXTENDS`` parity).
    "trip": ("event",),
}

#: The frozen extends parent set per system class UUID (name-keyed
#: ``_SYSTEM_CLASS_EXTENDS`` resolved to ids) — the system-extends lock
#: authority (SCHEMA.md "Class properties";
#: seeds.ts ``SYSTEM_CLASS_EXTENDS_IDS`` parity): a system class's extends
#: mapping is seeded platform structure the feature-toggle cascade
#: (``system_class_ancestors``) and binding inheritance (the store's
#: ``class_extends`` walk) both derive from, so ``class.setExtends`` on a
#: system class only applies when the payload's parent set EQUALS the seeded
#: set (the seed/ensure re-issue convergence path); every deviation fails
#: loud at the applier. Classes absent from ``_SYSTEM_CLASS_EXTENDS`` are
#: frozen to the empty tuple. User classes stay freely extensible, including
#: onto system bases.
SYSTEM_CLASS_EXTENDS_IDS: dict[str, tuple[str, ...]] = {
    class_id: tuple(_SYSTEM_CLASS_UUIDS[parent] for parent in _SYSTEM_CLASS_EXTENDS.get(name, ()))
    for name, class_id in _SYSTEM_CLASS_UUIDS.items()
}


def is_system_class_id(class_id: str) -> bool:
    """True when the id is a fixed system-class seed UUID (seeds.ts
    ``isSystemClassId``)."""
    return class_id in SYSTEM_CLASS_EXTENDS_IDS

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


#: The core class families.
WORKSPACE_FEATURE_MAP: dict[FamilyFeature, WorkspaceFeatureSpec] = {
    "tasks": WorkspaceFeatureSpec("task", "Tasks", "Tasks hub + checkbox gestures"),
    "events": WorkspaceFeatureSpec("event", "Events", "The calendar day/month surfaces"),
    "meetings": WorkspaceFeatureSpec("meeting", "Meetings", "Meeting quick-create + meeting logic"),
    "sources": WorkspaceFeatureSpec("source", "Sources", "The source family + citation import/export"),
    "persons": WorkspaceFeatureSpec("person", "Persons", "The people graph + contact fields"),
}

_BASE_FEATURE_BY_CLASS: dict[str, FamilyFeature] = {
    spec.base_class: feature for feature, spec in WORKSPACE_FEATURE_MAP.items()
}


def system_class_uuid(name: str) -> str:
    """The fixed UUID of a family-relevant system class (never regenerate)."""
    return _SYSTEM_CLASS_UUIDS[name]


def system_class_name_for_id(class_id: str) -> str | None:
    """The seed name for a system-class id, else None (user/unknown classes;
    seeds.ts ``systemClassNameForId``)."""
    for name, seed_id in _SYSTEM_CLASS_UUIDS.items():
        if seed_id == class_id:
            return name
    return None


def is_family_feature(feature: WorkspaceFeature) -> TypeGuard[FamilyFeature]:
    """True when ``feature`` is a family toggle (derives class archival
    bits) — the ``journal`` entry gates surfaces only and is excluded
    (features.ts ``isFamilyFeature``)."""
    return feature in WORKSPACE_FEATURE_MAP


def feature_for_base_class(name: str) -> FamilyFeature | None:
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
#: anchor anymore. The #14 follow-up five are not
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

#: Locked system classes (the classes-gallery
#: batch): the core seeds that can NEVER be archived or deleted —
#: ``object.delete`` fails loud (``LockedSystemClassError``) and an archival
#: write (``object.update {active: false}``) is rejected the same way. This
#: is the always-on set's locked CORE: the date chain + table, asset, query,
#: code, template, comment (features.ts ``LOCKED_SYSTEM_CLASSES`` parity).
#: The other always-on classes (callouts, quote, whiteboard, …) are
#: gallery-toggleable — disabling them archives (``active = false``), it
#: never deletes. Membership rides; nothing here is ever feature-managed.
LOCKED_SYSTEM_CLASSES: tuple[str, ...] = (
    "year",
    "month",
    "day",
    "table",
    "asset",
    "query",
    "code",
    "template",
    "comment",
)

_LOCKED_CLASS_ID_SET: frozenset[str] = frozenset(_SYSTEM_CLASS_UUIDS[name] for name in LOCKED_SYSTEM_CLASSES)


def is_locked_system_class_id(class_id: str) -> bool:
    """True when ``class_id`` is a LOCKED system class (never archivable or
    deletable; features.ts ``isLockedSystemClassId``)."""
    return class_id in _LOCKED_CLASS_ID_SET


def family_class_names(feature: FamilyFeature) -> tuple[str, ...]:
    """The family's full class set: the base class + its transitive
    extends-children (deterministic, sorted after the base). Disabling the
    family archives exactly this set (the applier's per-class re-derivation
    iterates it)."""
    base = WORKSPACE_FEATURE_MAP[feature].base_class
    children = sorted(name for name in _SYSTEM_CLASS_UUIDS if name != base and base in system_class_ancestors(name))
    return (base, *children)


def managed_class_ids(feature: FamilyFeature) -> tuple[str, ...]:
    """Resolved UUIDs of the family's full class set (the applier's flip list)."""
    return tuple(_SYSTEM_CLASS_UUIDS[name] for name in family_class_names(feature))


def feature_for_managed_class(class_id: str) -> FamilyFeature | None:
    """Class-node delete routing (the retired ``class.delete`` op's F4 rule,
    moved into the ``object.delete`` applier): the owning feature when
    ``class_id`` is a family's BASE class, else None. Children (book,
    meeting, birthday, …) deliberately do NOT route — the owner mapped the
    routing to the five bases exactly. The route decision is a pure function
    of the class id (fixed vocabulary), so every replica takes the same
    branch."""
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
    # The unified date property type: the task family's
    # three date bindings (Scheduled/Deadline/Closed) are DATETIME-typed per
    # the amended time-of-day law (SCHEMA.md "Datetime") — values may carry a
    # wall-clock ``time`` beside the day-node anchor; full-day stays default.
    type: Literal["select", "datetime"]
    options: tuple[TaskSeedOption, ...] = ()
    # the value-display position rides
    # the SCHEMA entry the ensure authors — only the Status schema defaults
    # to "bullet" (the status value rides the block bullet as an icon
    # button); the rest stay NULL ("panel", the properties section).
    display: Literal["panel", "bullet", "inline"] | None = None


#: The task-family seed-ensure manifest: six schemas + their task-class
#: bindings, authored idempotently by the store applier when the ``tasks``
#: feature enables (closes the "task property schemas never authored" gap).
#: Fixed ids end to end (schema + option uuids above). Binding rows carry
#: NO order (``sequence`` retired from the binding;
#: class panels render alphabetically by property name). The Status options
#: carry the designed glyphs (seeds.ts ``TASK_STATUS_OPTIONS``): the
#: circle-family MDI icons with a distinct color each, so a task's state
#: reads at a glance from the block bullet. ``display`` lands on
#: the property_schema row, never the binding.
TASK_FAMILY_SEED: tuple[TaskFamilySeedEntry, ...] = (
    TaskFamilySeedEntry(
        "taskStatus",
        "Status",
        "select",
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
    TaskFamilySeedEntry("taskScheduled", "Scheduled", "datetime"),
    TaskFamilySeedEntry("taskDeadline", "Deadline", "datetime"),
    TaskFamilySeedEntry(
        "taskPriority",
        "Priority",
        "select",
        (
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["low"], "Low"),
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["medium"], "Medium"),
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["high"], "High"),
            TaskSeedOption(TASK_PRIORITY_OPTION_UUIDS["urgent"], "Urgent"),
        ),
    ),
    TaskFamilySeedEntry("taskClosedDate", "Closed", "datetime"),
    # Migrated recurrence rides as a plain select (no engine executes it);
    # authored optionless until the recurrence spec lands.
    TaskFamilySeedEntry("taskRecurrence", "Recurrence", "select"),
)


def task_property_uuid(name: str) -> str:
    """The fixed schema id for a task-family property name."""
    return _TASK_PROPERTY_UUIDS[name]


#: The READONLY derivation list: the retired
#: ``readonly`` render contract was never user configuration (ZERO seeds ever
#: carried the flag), so clients DERIVE readonly-ness from the system list
#: instead of storing it (the binding-``sequence`` philosophy). The list is
#: the platform-PROVENANCE properties — values authored by platform flows,
#: not user data: ``generatedFrom`` (templates provenance, written by
#: instantiation) and ``alias`` (name-equivalence metadata). Mirrors
#: ``READONLY_SYSTEM_PROPERTY_IDS`` in the monorepo's packages/domain.
READONLY_SYSTEM_PROPERTY_IDS: tuple[str, ...] = (
    "00000000-0000-0000-0000-000000000027",  # generatedFrom
    "00000000-0000-0000-0000-000000000028",  # alias
)


def is_readonly_property(schema_id: str) -> bool:
    """True when the schema id is a system property whose editors render
    readonly (the retired render contract, now derived — never blocks a
    write, PC1)."""
    return schema_id in READONLY_SYSTEM_PROPERTY_IDS
