"""
A little text drawing of what somebody has entered so far.

Shown every few additions and on the review screen, so the contributor can
see the shape of what they have built — and spot the sister attached to the
wrong father — before any of it is sent.

    Toufic Sukar ⚭ Cilene Sukar
    ├ Dibeh
    ├ Saide ⚭ Jamil Tarabay
    └ Kalim ⚭ Wadiha
      └ Steven

Phone-width on purpose: one column, short lines. It is a sketch, not the
chart — the real graph lives on the web view.
"""

from __future__ import annotations

import submissions


class _Family:
    """One couple (or lone parent) and the children under them."""

    def __init__(self, parents: tuple[str, ...]):
        self.parents = parents
        self.children: list[str] = []


def build(payloads: list[dict], self_name: str | None = None,
          self_father: str | None = None,
          ids: dict[str, int] | None = None) -> str:
    """Render the payloads (basket and queued alike) as a text sketch."""
    spouses: dict[str, str] = {}
    parent_family: dict[str, _Family] = {}   # child name -> family
    families: list[_Family] = []
    mentioned: list[str] = []

    origins: dict[str, str] = {}

    def _kin(a: str, b: str) -> bool:
        """Whether one name is a bare form of the other: Kalim / Kalim Sukar."""
        return a == b or a.startswith(b + " ") or b.startswith(a + " ")

    def note(name: str, ref: bool = False) -> str:
        """Record a name.

        A REFERENCE ("Kalim's parents are...") may fold onto somebody already
        drawn — that is what lets lines chain. A new person ENTRY never folds
        onto another entry: a brother Toufic and a grandfather Toufic Sukar
        are different men, and merging them scrambles the whole drawing.
        """
        if not name:
            return name
        matches = [seen for seen in mentioned if _kin(seen, name)]

        if ref:
            if matches:
                target = max(matches, key=len)
                if len(name) > len(target):
                    _rename(target, name)
                    return name
                return target
            mentioned.append(name)
            origins[name] = "ref"
            return name

        if name in mentioned and origins.get(name) == "entry":
            return name
        placeholders = [m for m in matches if origins.get(m) == "ref"]
        if placeholders:
            # This entry is the concrete person a reference promised.
            target = max(placeholders, key=len)
            if len(name) > len(target):
                _rename(target, name)
                target = name
            origins[target] = "entry"
            if target not in mentioned:
                mentioned.append(target)
            return target
        if name not in mentioned:
            mentioned.append(name)
        origins.setdefault(name, "entry")
        return name

    def _rename(old: str, new: str) -> None:
        if old in mentioned:
            mentioned[mentioned.index(old)] = new
        origins[new] = origins.pop(old, "ref")
        if old in spouses:
            spouses[new] = spouses.pop(old)
        for key, value in list(spouses.items()):
            if value == old:
                spouses[key] = new
        if old in parent_family:
            parent_family[new] = parent_family.pop(old)
        for family in families:
            family.parents = tuple(new if p == old else p for p in family.parents)
            family.children = [new if c == old else c for c in family.children]

    def family_for(parents: tuple[str, ...]) -> _Family:
        for family in families:
            # The same father with and without a mother yet is one family.
            if set(parents) & set(family.parents):
                # One family node is one couple — a second claim about the
                # same couple can only be the same people, however bare the
                # spelling. Without this, a re-entered bare "Toufic" next to
                # the existing full name drew a three-parent marriage.
                for name in parents:
                    kin_match = next(
                        (p for p in family.parents if _kin(p, name)), None
                    )
                    if kin_match is None:
                        family.parents = family.parents + (name,)
                    elif name != kin_match:
                        if len(name) > len(kin_match):
                            _rename(kin_match, name)
                        else:
                            _rename(name, kin_match)
                return family
        family = _Family(parents)
        families.append(family)
        return family

    def add_child(parents: tuple[str, ...], child: str) -> None:
        family = family_for(parents)
        if child not in family.children:
            family.children.append(child)
        parent_family[child] = family

    if self_name:
        note(self_name, ref=True)
    if self_name and self_father:
        add_child((note(self_father, ref=True),), self_name)

    for payload in payloads:
        kind = payload.get("kind")
        about = (payload.get("about") or {}).get("label") or self_name or "?"
        about = note(about.split(" (")[0], ref=True)
        entries = payload.get("people") or []

        if kind == submissions.IDENTIFY and entries:
            note(submissions.person_label(entries[0]))
            continue

        if kind == submissions.ADD_PARENTS:
            parents = tuple(
                note(submissions.person_label(entry))
                for entry in entries
            )
            if len(parents) == 2:
                spouses[parents[0]] = parents[1]
                spouses[parents[1]] = parents[0]
            if parents:
                add_child(parents, note(about))
            continue

        for entry in entries:
            name = note(submissions.person_label(entry))
            role = entry.get("role")
            if role == submissions.SPOUSE:
                spouses[about] = name
                spouses[name] = about
            elif role == submissions.SIBLING:
                family = parent_family.get(about)
                if family is None:
                    family = family_for((f"parents of {about}",))
                    add_child(family.parents, note(about))
                add_child(family.parents, name)
            elif role == submissions.CHILD:
                parents = (about,)
                if about in spouses:
                    parents = (about, spouses[about])
                add_child(parents, name)

    if not families and not spouses:
        return ""

    # ---- draw -------------------------------------------------------------

    drawn: set[int] = set()
    drawn_names: set[str] = set()
    lines: list[str] = []

    def deco(name: str) -> str:
        """Append the permanent number for people already in the tree."""
        if not ids:
            return name
        number = ids.get(name)
        if number is None:
            first = name.split()[0]
            number = ids.get(first)
        return f"{name} #{number}" if number is not None else name

    def couple_line(name: str, below: "_Family | None" = None) -> str:
        """One person on a child line, with their spouse when it is theirs.

        A marriage that stands as a couple's own family node belongs to
        whoever heads that node. Drawing it beside anyone else married a
        grandson to his grandmother, because two men of one name are one
        name here — so the marriage is shown only when the family about to
        be drawn beneath this line is that couple's own.
        """
        partner = spouses.get(name)
        if partner:
            own = next(
                (
                    family
                    for family in families
                    if len(family.parents) == 2
                    and set(family.parents) == {name, partner}
                ),
                None,
            )
            if own is not None and own is not below:
                partner = None
        drawn_names.add(name)
        if partner:
            drawn_names.add(partner)
            return f"{deco(name)} ⚭ {deco(partner)}"
        return deco(name)

    def draw(family: _Family, indent: str) -> None:
        if id(family) in drawn:
            return
        drawn.add(id(family))
        parents = " ⚭ ".join(deco(name) for name in family.parents)
        drawn_names.update(family.parents)
        for name in family.parents:
            partner = spouses.get(name)
            if partner and partner not in family.parents:
                parents += f" ⚭ {deco(partner)}"
                drawn_names.add(partner)
        lines.append(indent + parents)
        draw_children(family, indent)

    def draw_children(family: _Family, indent: str) -> None:
        for position, child in enumerate(family.children):
            last = position == len(family.children) - 1
            below = next(
                (f for f in families
                 if id(f) not in drawn
                 and (child in f.parents or spouses.get(child) in f.parents)),
                None,
            )
            lines.append(
                indent + ("└ " if last else "├ ") + couple_line(child, below)
            )
            if below is not None:
                drawn.add(id(below))
                draw_children(below, indent + ("  " if last else "│ "))

    roots = [
        family for family in families
        if not any(parent in parent_family for parent in family.parents)
    ]
    for family in roots or families:
        if lines:
            lines.append("")
        draw(family, "")

    # Couples connected to nothing else still deserve a line — decorated
    # like every other name, and only when neither of them is already on
    # the drawing.
    lines.extend(
        f"{deco(name)} ⚭ {deco(partner)}"
        for name, partner in spouses.items()
        if name < partner
        and name not in drawn_names
        and partner not in drawn_names
    )

    return "\n".join(lines).strip()
