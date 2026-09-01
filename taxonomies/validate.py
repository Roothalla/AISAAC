"""
Taxonomy validator.

Run:  python taxonomies/validate.py

Checks the invariants the scoring code depends on. If this passes, every scorer
in the project can assume well-formed taxonomies and skip defensive checks.
"""

import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).parent
VALID_ATTRIBUTION = {"OVERT", "DENIABLE", "COVERT"}
VALID_VISIBILITY = {"PUBLIC", "TARGET_ONLY", "HIDDEN"}
VALID_AUDIENCE = {"PUBLIC", "TARGET"}
REQUIRED_ACTION_FIELDS = [
    "code", "family", "label", "escalation", "attribution",
    "visibility", "reversible", "delayed_payoff", "params",
    "costs", "preconditions", "obligation_tags",
]


def load(name):
    with open(HERE / name) as fh:
        return yaml.safe_load(fh)


def check_actions(actions_doc, resources_doc, errors, warnings):
    actions = actions_doc["actions"]
    ladder = set(actions_doc["escalation_ladder"].keys())
    resource_ids = {r["id"] for r in resources_doc["resources"]}
    welfare_ids = {w["id"] for w in resources_doc["welfare_indicators"]}

    codes = [a["code"] for a in actions]
    for code, n in Counter(codes).items():
        if n > 1:
            errors.append(f"duplicate action code: {code}")

    for a in actions:
        code = a.get("code", "<missing>")

        for field in REQUIRED_ACTION_FIELDS:
            if field not in a:
                errors.append(f"{code}: missing required field '{field}'")

        if a.get("escalation") not in ladder:
            errors.append(f"{code}: escalation {a.get('escalation')} not on ladder")

        if a.get("attribution") not in VALID_ATTRIBUTION:
            errors.append(f"{code}: bad attribution {a.get('attribution')}")

        if a.get("visibility") not in VALID_VISIBILITY:
            errors.append(f"{code}: bad visibility {a.get('visibility')}")

        for res in a.get("costs", {}):
            if res not in resource_ids:
                errors.append(f"{code}: cost references unknown resource '{res}'")

        for key in a.get("effects", {}):
            if key not in resource_ids and key not in welfare_ids:
                if key not in {"closes_channel"}:
                    warnings.append(f"{code}: effect key '{key}' is not a known pool")

        # A covert action that is publicly visible is a contradiction unless it is
        # explicitly deniable, e.g. a leak that surfaces without an author.
        if a.get("attribution") == "COVERT" and a.get("visibility") == "PUBLIC":
            errors.append(f"{code}: COVERT action cannot have PUBLIC visibility")

    return actions


def check_escalation_coverage(actions, warnings):
    """
    Every escalation rung needs options in more than one family, otherwise a seat
    that wants to escalate has only one instrument available and the measured
    escalation pattern is forced by the taxonomy rather than chosen by the model.
    """
    by_rung = {}
    for a in actions:
        by_rung.setdefault(a["escalation"], set()).add(a["family"])
    for rung in sorted(by_rung):
        if len(by_rung[rung]) < 2:
            warnings.append(
                f"escalation rung {rung} has options in only one family "
                f"({by_rung[rung]}); seats have no instrument choice at this rung"
            )


def check_deescalation_available(actions, errors):
    """
    If no rung-0 action exists, de-escalation is unrepresentable and every model
    will appear escalatory. This would be a harness artefact, not a finding.
    """
    if not any(a["escalation"] == 0 for a in actions):
        errors.append("no rung-0 actions defined; de-escalation is unrepresentable")


def check_stances(stances_doc, errors):
    stances = stances_doc["stances"]
    for code, n in Counter(s["code"] for s in stances).items():
        if n > 1:
            errors.append(f"duplicate stance code: {code}")

    for s in stances:
        code = s["code"]
        if s.get("audience") not in VALID_AUDIENCE:
            errors.append(f"{code}: bad audience {s.get('audience')}")
        if s.get("binds"):
            if "resolves_on" not in s:
                errors.append(f"{code}: binds=true but no resolves_on")
            if not s.get("requires"):
                errors.append(f"{code}: binds=true but requires no fields to check")
        else:
            if "resolves_on" in s:
                errors.append(f"{code}: binds=false but declares resolves_on")
    return stances


def check_bargaining(bargaining_doc, actions, errors, warnings):
    """
    The load-bearing cross-check. Accord compliance is computed by matching
    obligation tags between signed accords and executed actions, so every tag
    referenced anywhere must exist in the registry, and every obligation an item
    imposes must be reachable by at least one action -- otherwise the accord
    cannot be violated and compliance is vacuously perfect.
    """
    registry = set(bargaining_doc["obligations"])
    items = bargaining_doc["items"]
    action_codes = {a["code"] for a in actions}
    action_tags = set()
    for a in actions:
        action_tags.update(a.get("obligation_tags", []))

    for tag in sorted(action_tags - registry):
        errors.append(f"actions.yaml uses obligation tag '{tag}' absent from registry")

    for code, n in Counter(i["id"] for i in items).items():
        if n > 1:
            errors.append(f"duplicate bargaining item: {code}")

    for item in items:
        iid = item["id"]
        if not 0 <= item.get("base_weight", -1) <= 10:
            errors.append(f"{iid}: base_weight out of range")
        if item.get("verifiability") not in {"HIGH", "MEDIUM", "LOW"}:
            errors.append(f"{iid}: bad verifiability {item.get('verifiability')}")

        for tag in item.get("imposes", []):
            if tag not in registry:
                errors.append(f"{iid}: imposes unknown obligation '{tag}'")
            elif tag not in action_tags:
                warnings.append(
                    f"{iid}: obligation '{tag}' is not carried by any action, "
                    f"so it can never be violated"
                )

        # discharged_by entries are either bare action codes or predicate strings
        for d in item.get("discharged_by", []):
            if "(" not in d and d not in action_codes:
                errors.append(f"{iid}: discharged_by references unknown action '{d}'")
            elif "(" in d:
                inner = d[d.index("(") + 1:d.rindex(")")]
                for ref in [x.strip() for x in inner.split(",")]:
                    if ref.isupper() and "_" in ref and ref not in action_codes:
                        if not any(c.islower() for c in ref):
                            warnings.append(
                                f"{iid}: predicate '{d}' references '{ref}' "
                                f"which is not an action code"
                            )

    item_ids = {i["id"] for i in items}
    for tmpl in bargaining_doc["accord_templates"]:
        for iid in tmpl["items"]:
            if iid not in item_ids:
                errors.append(f"accord {tmpl['id']}: unknown item '{iid}'")

    return items


def main():
    errors, warnings = [], []

    resources_doc = load("resources.yaml")
    actions_doc = load("actions.yaml")
    stances_doc = load("stances.yaml")
    bargaining_doc = load("bargaining.yaml")

    actions = check_actions(actions_doc, resources_doc, errors, warnings)
    check_escalation_coverage(actions, warnings)
    check_deescalation_available(actions, errors)
    stances = check_stances(stances_doc, errors)
    items = check_bargaining(bargaining_doc, actions, errors, warnings)

    print(f"actions:   {len(actions)}")
    print(f"stances:   {len(stances)}")
    print(f"resources: {len(resources_doc['resources'])}")
    print(f"welfare:   {len(resources_doc['welfare_indicators'])}")
    print(f"bargaining items: {len(items)}")
    print(f"obligations: {len(bargaining_doc['obligations'])}")
    print(f"accord templates: {len(bargaining_doc['accord_templates'])}")

    ver = Counter(i["verifiability"] for i in items)
    print("verifiability: " + ", ".join(f"{k}={ver[k]}" for k in ["HIGH", "MEDIUM", "LOW"]))

    fam = Counter(a["family"] for a in actions)
    print("\nby family: " + ", ".join(f"{k}={v}" for k, v in sorted(fam.items())))
    esc = Counter(a["escalation"] for a in actions)
    print("by rung:   " + ", ".join(f"{k}={esc[k]}" for k in sorted(esc)))
    print(f"binding stances: {sum(1 for s in stances if s.get('binds'))}")
    print(f"delayed-payoff actions: {sum(1 for a in actions if a['delayed_payoff'])}")

    for w in warnings:
        print(f"\nWARN  {w}")
    for e in errors:
        print(f"\nERROR {e}")

    if errors:
        print(f"\nFAILED with {len(errors)} error(s)")
        return 1
    print(f"\nOK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
