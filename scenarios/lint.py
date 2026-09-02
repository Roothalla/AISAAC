"""
Scenario linter.

Run:  python scenarios/lint.py

Checks the invariants the three-layer scenario model depends on, and prints the
acceptance block for SPEC.md step 3.

WHAT THIS FILE IS ACTUALLY PROVING

  The project's contamination argument rests on a claim -- that a binding is
  strategically inert, so two bindings of one skeleton are the same instrument, while
  a mutation of that skeleton is a different one. A claim like that is worth very
  little as prose in a README. Here it is a check that either passes or does not:

    1. Bind the same skeleton twice. Strip the display layer. The two must hash
       identically. This proves the RESOLVER does not leak surface into strategy.
    2. Lint each binding for numeric literals and for identifiers that appear as
       predicate arguments. This proves the BINDING carries no strategy to leak.
    3. Apply the mutation overlay. The hash must differ, and the diff must contain
       exactly the fields the overlay declares it changes -- no more.

  (1) and (2) are independent and both are needed. (1) alone would pass trivially if
  the resolver simply ignored bindings; (2) alone would not catch a resolver that
  merged a label into a strategic field.
"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent
TAX = HERE.parent / "taxonomies"

BINDING_ALLOWED_KEYS = {"binds", "instance", "labels", "prose"}
REQUIRED_FIXTURES = {"SCRIPTED_COERCION", "CAPABILITY_TEST",
                     "EXOGENOUS_SHOCK", "RED_LINE_PRESSURE"}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_taxonomies():
    t = {n: load(TAX / f"{n}.yaml") for n in
         ("actions", "stances", "bargaining", "resources",
          "capabilities", "predicates", "archetypes")}
    return {
        "action_codes": {a["code"] for a in t["actions"]["actions"]},
        "actions_by_code": {a["code"]: a for a in t["actions"]["actions"]},
        "item_ids": {i["id"] for i in t["bargaining"]["items"]},
        "items_by_id": {i["id"]: i for i in t["bargaining"]["items"]},
        "pool_ids": {r["id"] for r in t["resources"]["resources"]},
        "welfare_ids": {w["id"] for w in t["resources"]["welfare_indicators"]},
        "capability_ids": {c["id"] for c in t["capabilities"]["capabilities"]},
        "capabilities_by_id": {c["id"]: c for c in t["capabilities"]["capabilities"]},
        "state_predicates": t["predicates"]["state_predicates"],
        "raw": t,
    }


# ---------------------------------------------------------------------------
# resolution:  template (+ overlay)  x  binding  ->  {strategic, display}
# ---------------------------------------------------------------------------

def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_overlay(parent, mutation):
    """Overlay a mutation onto its parent. Seats are keyed by id, not position."""
    resolved = json.loads(json.dumps(parent))
    ov = mutation.get("overlay", {})
    for key, val in ov.items():
        if key == "seats":
            by_id = {s["id"]: s for s in resolved["seats"]}
            for seat_id, patch in val.items():
                by_id[seat_id].update(deep_merge(by_id[seat_id], patch))
            resolved["seats"] = [by_id[s["id"]] for s in resolved["seats"]]
        else:
            resolved[key] = deep_merge(resolved.get(key, {}), val)
    resolved["meta"] = deep_merge(resolved["meta"], mutation["meta"])
    return resolved


def resolve(template, binding):
    """
    Bind a template. Labels and prose go into a SEPARATE display namespace and are
    never merged into strategic fields. That separation is the whole mechanism; if a
    future resolver merges them, check (1) below starts failing, which is the point.
    """
    return {
        "strategic": json.loads(json.dumps(template)),
        "display": {"labels": binding.get("labels", {}),
                    "prose": binding.get("prose", {})},
    }


def strategic_core(scenario):
    core = json.loads(json.dumps(scenario["strategic"]))
    # meta.id and the mutation bookkeeping are identity, not strategy.
    for k in ("id", "mutation_of", "mutated_fields", "notes", "hold_out"):
        core.get("meta", {}).pop(k, None)
    return core


def core_hash(scenario):
    return hashlib.sha256(
        json.dumps(strategic_core(scenario), sort_keys=True,
                   separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def flatten(obj, prefix=""):
    """Flatten to dotted paths. Lists of {id: ...} are keyed by id, not index."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if obj and all(isinstance(e, dict) and "id" in e for e in obj):
            for e in obj:
                out.update(flatten(e, f"{prefix}[{e['id']}]"))
        else:
            out[prefix] = json.dumps(obj, sort_keys=True)
    else:
        out[prefix] = obj
    return out


def walk_numbers(obj, path=""):
    """Yield (path, value) for every numeric literal. Bools are not numbers here."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_numbers(v, f"{path}[{i}]")


def walk_strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_binding(binding, name, template, tx, errors, warnings):
    extra = set(binding) - BINDING_ALLOWED_KEYS
    if extra:
        errors.append(f"{name}: keys outside the allowed set: {sorted(extra)}")

    # (2a) No numeric literals. A quantity that matters is strategic.
    for path, val in walk_numbers(binding):
        errors.append(f"{name}: numeric literal at '{path}' = {val!r}; "
                      f"quantities belong in the skeleton")

    # (2b) No digits inside prose either -- a number spelled into a brief is still a
    # number, and it would differ between bindings of one skeleton.
    for path, val in walk_strings(binding.get("prose", {})):
        if any(c.isdigit() for c in val):
            warnings.append(f"{name}: digit inside prose at '{path}'")

    # (2c) Label completeness: every strategic identifier a reader must see named.
    wanted = set()
    for group in template["world_objects"].values():
        wanted.update(group)
    wanted.update(s["id"] for s in template["seats"])
    wanted.update(template["capabilities_in_play"])
    labels = set(binding.get("labels", {}))
    for missing in sorted(wanted - labels):
        errors.append(f"{name}: no label for '{missing}'")
    for unknown in sorted(labels - wanted):
        errors.append(f"{name}: label for unknown identifier '{unknown}'")

    if binding.get("binds") != template["meta"]["id"]:
        errors.append(f"{name}: binds '{binding.get('binds')}' "
                      f"but was linted against '{template['meta']['id']}'")


def check_template(t, tx, errors, warnings):
    name = t["meta"]["id"]
    seat_ids = [s["id"] for s in t["seats"]]
    objs = set()
    for group in t["world_objects"].values():
        objs.update(group)
    state_vars = tx["pool_ids"] | tx["welfare_ids"]

    # -- capabilities ------------------------------------------------------
    for cap in t["capabilities_in_play"]:
        if cap not in tx["capability_ids"]:
            errors.append(f"{name}: capabilities_in_play references unknown '{cap}'")
    for s in t["seats"]:
        for cap in s["capability_table"]:
            if cap not in tx["capability_ids"]:
                errors.append(f"{name}/{s['id']}: capability_table has unknown '{cap}'")
        for cap in t["capabilities_in_play"]:
            if cap not in s["capability_table"]:
                errors.append(f"{name}/{s['id']}: no ground truth for '{cap}'")

    # -- bargaining item references ---------------------------------------
    for s in t["seats"]:
        for item in s["valuations"].get("overrides", {}):
            if item not in tx["item_ids"]:
                errors.append(f"{name}/{s['id']}: valuation for unknown item '{item}'")
    for item in t["scoring_config"]["red_line_candidate_items"]:
        if item not in tx["item_ids"]:
            errors.append(f"{name}: red_line_candidate_items has unknown '{item}'")
    for acc in t["accords_t0"]:
        for item in acc["items"]:
            if item not in tx["item_ids"]:
                errors.append(f"{name}: accord {acc['accord_id']} has unknown item '{item}'")
        if acc["accord_id"] not in t["world_objects"]["accords"]:
            errors.append(f"{name}: accord '{acc['accord_id']}' not in world_objects")
        for p in acc["parties"]:
            if p not in seat_ids:
                errors.append(f"{name}: accord {acc['accord_id']} names unknown seat '{p}'")

    # -- action codes referenced by fixtures and scripts -------------------
    refs = []
    for e in t["event_schedule"]:
        if "instrument" in e["detail"]:
            refs.append((f"event t{e['turn']}", e["detail"]["instrument"]))
    for p in t["principal_script"]:
        refs.append((f"principal_script t{p['turn']}", p["demand_action_code"]))
    for cap_id in t["capabilities_in_play"]:
        for a in tx["capabilities_by_id"][cap_id]["exercise_actions"]:
            refs.append((f"capability {cap_id}", a))
    for where, code in refs:
        if code not in tx["action_codes"]:
            errors.append(f"{name}: {where} references unknown action '{code}'")

    # -- state variables ---------------------------------------------------
    for s in t["seats"]:
        for v in s["sensor_grid"]:
            if v not in state_vars:
                errors.append(f"{name}/{s['id']}: sensor_grid has unknown variable '{v}'")
            if v not in t["salience"]["scale"]:
                errors.append(f"{name}/{s['id']}: '{v}' is visible but has no "
                              f"salience.scale entry, so the sensorium index is "
                              f"undefined for it")
        for v in s.get("welfare_coupling", {}):
            if v not in tx["welfare_ids"]:
                errors.append(f"{name}/{s['id']}: welfare_coupling on non-welfare '{v}'")
            for pool in s["welfare_coupling"][v]:
                if pool not in tx["pool_ids"]:
                    errors.append(f"{name}/{s['id']}: welfare_coupling targets "
                                  f"non-pool '{pool}'")
    for v in t["information"]["queryable_variables"]:
        if v not in state_vars:
            errors.append(f"{name}: queryable_variables has unknown '{v}'")

    # -- world object references in initial_state --------------------------
    for seat, zones in t["initial_state"]["forces_deployed"].items():
        for z in zones:
            if z not in t["world_objects"]["zones"]:
                errors.append(f"{name}: forces_deployed names unknown zone '{z}'")

    # -- every state predicate has an initialiser --------------------------
    RUNTIME = {"runtime"}
    for pred in tx["state_predicates"]:
        init = str(pred.get("init", ""))
        if init in RUNTIME:
            continue
        root = init.split(".")[0].split("[")[0]
        if root not in t and root not in ("seats", "initial_state", "accords_t0"):
            errors.append(f"{name}: predicate {pred['name']}() reads '{init}' "
                          f"which the template does not supply")
        if root == "initial_state":
            leaf = init.split(".", 1)[1] if "." in init else None
            if leaf and leaf not in t["initial_state"]:
                errors.append(f"{name}: initial_state has no '{leaf}' for "
                              f"predicate {pred['name']}()")

    # -- engagement --------------------------------------------------------
    for dyad in t["engagement"]["prior"]:
        for s in dyad.split("|"):
            if s not in seat_ids:
                errors.append(f"{name}: engagement prior names unknown seat '{s}'")
    for a, b in t["engagement"]["non_engagement_guarantee"]:
        key = f"{a}|{b}" if f"{a}|{b}" in t["engagement"]["prior"] else f"{b}|{a}"
        if t["engagement"]["prior"].get(key) != "NOT_ENGAGED":
            errors.append(f"{name}: non_engagement_guarantee {a}/{b} is not "
                          f"NOT_ENGAGED in the prior")
    if not t["engagement"]["non_engagement_guarantee"]:
        errors.append(f"{name}: no non_engagement_guarantee; third-party blindness "
                      f"has no denominator")

    # -- fixtures ----------------------------------------------------------
    kinds = {e["kind"] for e in t["event_schedule"]}
    for k in sorted(REQUIRED_FIXTURES - kinds):
        errors.append(f"{name}: no {k} fixture; a metric it serves cannot fire")

    # -- principal script: every irrational demand needs a control ---------
    by_seat = {}
    for p in t["principal_script"]:
        by_seat.setdefault(p["seat"], []).append(p)
    for seat, entries in by_seat.items():
        irr = [e for e in entries if not e["is_control"]]
        ctl = [e for e in entries if e["is_control"]]
        if irr and not ctl:
            errors.append(f"{name}/{seat}: irrational principal demand with no "
                          f"matched control; sycophancy is not separable from obedience")
        for e in entries:
            if not e.get("refusal_cost"):
                errors.append(f"{name}/{seat} t{e['turn']}: refusal_cost is empty; "
                              f"declining is free and dissent measures nothing")
            if e["seat"] not in seat_ids:
                errors.append(f"{name}: principal_script names unknown seat '{e['seat']}'")

    # -- affordability floor at t0 ----------------------------------------
    delayed = [a for a in tx["actions_by_code"].values() if a.get("delayed_payoff")]
    for s in t["seats"]:
        ok = [a["code"] for a in delayed
              if all(s["resources"].get(p, 0) >= c for p, c in a.get("costs", {}).items())]
        if not ok:
            errors.append(f"{name}/{s['id']}: cannot afford ANY delayed-payoff action "
                          f"at t0, so its revealed discount rate is set by its "
                          f"endowment rather than its choice")
        elif len(ok) < 2:
            warnings.append(f"{name}/{s['id']}: only {len(ok)} affordable "
                            f"delayed-payoff action at t0 ({ok[0]}); thin margin for "
                            f"discount fidelity once pools erode")

    # -- commitment maturation --------------------------------------------
    turns = t["meta"]["turns"]
    grace = t["scoring_config"]["grace_window_turns"]
    if turns - grace < 8:
        warnings.append(f"{name}: {turns} turns with a {grace}-turn grace window "
                        f"leaves few turns in which a commitment can both be issued "
                        f"and resolve; declaratory follow-through will have a thin "
                        f"denominator")


def check_mutation(parent, mutation, resolved, errors, warnings):
    name = mutation["meta"]["id"]
    declared = set(mutation["meta"]["mutated_fields"])
    if not declared:
        errors.append(f"{name}: mutation declares no mutated_fields")

    fp, fm = flatten(parent), flatten(resolved)
    actual = {k for k in set(fp) | set(fm)
              if fp.get(k) != fm.get(k) and not k.startswith("meta.")}

    for extra in sorted(actual - declared):
        errors.append(f"{name}: changed '{extra}' without declaring it")
    for missing in sorted(declared - actual):
        errors.append(f"{name}: declares '{missing}' as mutated but it is unchanged")
    return actual


# ---------------------------------------------------------------------------

def main():
    errors, warnings = [], []
    tx = load_taxonomies()

    parent = load(HERE / "skeletons" / "chokepoint.v1.yaml")
    mutation = load(HERE / "skeletons" / "chokepoint.v1+m01.yaml")
    inst01 = load(HERE / "bindings" / "chokepoint.v1.inst01.yaml")
    inst02 = load(HERE / "bindings" / "chokepoint.v1.inst02.yaml")

    check_template(parent, tx, errors, warnings)
    check_binding(inst01, "inst01", parent, tx, errors, warnings)
    check_binding(inst02, "inst02", parent, tx, errors, warnings)

    mutated = apply_overlay(parent, mutation)
    check_template(mutated, tx, errors, warnings)
    changed = check_mutation(parent, mutation, mutated, errors, warnings)

    h01 = core_hash(resolve(parent, inst01))
    h02 = core_hash(resolve(parent, inst02))
    hm1 = core_hash(resolve(mutated, inst01))

    print("=" * 72)
    print(f"  {parent['meta']['id']}  --  {parent['meta']['turns']} turns, "
          f"{len(parent['seats'])} seats")
    print("=" * 72)
    print()
    print("  binding                       strategic_core_hash")
    print(f"  inst01                        {h01[:16]}")
    print(f"  inst02                        {h02[:16]}")
    print(f"  inst01 + mutation m01         {hm1[:16]}")
    print()
    same = h01 == h02
    diff = hm1 != h01
    print(f"  [{'OK ' if same else 'FAIL'}]  two bindings of one skeleton hash identically")
    print(f"  [{'OK ' if diff else 'FAIL'}]  mutation changes the strategic core")
    if not same:
        errors.append("bindings of one skeleton produced different strategic hashes; "
                      "the resolver is leaking surface into strategy")
    if not diff:
        errors.append("mutation did not change the strategic hash")

    print(f"  [{'OK ' if not any('numeric literal' in e for e in errors) else 'FAIL'}]"
          f"  bindings contain no numeric literals")
    print(f"  [{'OK ' if not any('no label for' in e for e in errors) else 'FAIL'}]"
          f"  bindings label every strategic identifier")
    print()
    print(f"  m01 declared mutations: {len(mutation['meta']['mutated_fields'])}")
    for f in mutation["meta"]["mutated_fields"]:
        print(f"    {f}")
    print(f"  m01 actual diff vs parent: {len(changed)} field(s)")
    print()
    print("  fixtures present:")
    for e in parent["event_schedule"]:
        print(f"    t{e['turn']:<3} {e['kind']:<20} serves {e['serves']}")
    print()
    ctl = sum(1 for p in parent["principal_script"] if p["is_control"])
    print(f"  principal script: {len(parent['principal_script'])} demands "
          f"({ctl} control, {len(parent['principal_script']) - ctl} irrational)")
    ne = parent["engagement"]["non_engagement_guarantee"]
    print(f"  non-engagement guarantee: {len(ne)} dyad(s) "
          f"{['/'.join(d) for d in ne]}")
    print()

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print()
    if errors:
        print(f"FAILED with {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"OK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
