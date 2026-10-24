#!/usr/bin/env python3
"""check-autoinstall.py - validate the VM edition's autoinstall file.

Usage: check-autoinstall.py AUTOINSTALL_YAML [--schema SUBIQUITY_SCHEMA_JSON]

Fails (exit 1, one line per problem) unless the file is what an unattended
BrainSyncOS VM install needs:
  - one top-level key `autoinstall` (24.04 form; subiquity rejects others
    beside it) holding version 1
  - nothing interactive: no interactive-sections, no identity (the account
    comes from install.d/30-user.sh), user-data with users: [] (so neither
    subiquity nor cloud-init creates a user)
  - ssh: install-server true, allow-pw false; packages include
    openssh-server, avahi-daemon, libnss-mdns
  - storage with a layout; shutdown poweroff or reboot
  - late-commands: plain strings, the first one exactly
    `/bin/bash /cdrom/brainsyncos/hook.sh`, and only once; the last one prints
    the success marker (BRAINSYNCOS-INSTALL-OK) on the serial consoles and
    error-commands print the failure marker (BRAINSYNCOS-INSTALL-FAILED):
    scripts/utm-vm.sh install reads them from the console, so the markers
    must be built at run time (not literally in the command text)
  - network: every ethernet uses DHCP with dhcp-identifier: mac (macOS's DHCP
    server then records the VM's MAC, which utm-vm.sh looks up)
With --schema (autoinstall-schema.json from the subiquity snap on the ISO),
every top-level key must be one the installer knows, and the file must
validate against the schema (the JSON-schema subset subiquity's schema uses:
type, properties, additionalProperties, required, enum, const, items,
minimum/maximum, anyOf/oneOf).
"""

import argparse
import json
import sys

import yaml

HOOK = "/bin/bash /cdrom/brainsyncos/hook.sh"
REQUIRED_PACKAGES = ("openssh-server", "avahi-daemon", "libnss-mdns")
# scripts/utm-vm.sh install greps the serial console log for these.
MARKER_PREFIX = "BRAINSYNCOS-"
OK_MARKER = MARKER_PREFIX + "INSTALL-OK"
FAILED_MARKER = MARKER_PREFIX + "INSTALL-FAILED"


def prints_marker(cmd, marker):
    """True if cmd is the console-marker command for marker (assembled at run time)."""
    suffix = marker[len(MARKER_PREFIX):]
    return (isinstance(cmd, str) and marker not in cmd and ("m=%s;" % suffix) in cmd
            and ("echo %s$m" % MARKER_PREFIX) in cmd and "/dev/hvc0" in cmd and "/dev/ttyAMA0" in cmd)
TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def type_ok(value, t):
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, TYPES[t])


def validate(value, schema, path, errs):
    if not isinstance(schema, dict):
        return
    if "anyOf" in schema or "oneOf" in schema:
        alts = schema.get("anyOf", []) + schema.get("oneOf", [])
        if not any(not _errors(value, alt, path) for alt in alts):
            errs.append("%s: matches none of the allowed forms" % path)
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(type_ok(value, x) for x in ts):
            errs.append("%s: %r is not of type %s" % (path, value, t))
            return
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r not in %r" % (path, value, schema["enum"]))
    if "const" in schema and value != schema["const"]:
        errs.append("%s: %r is not %r" % (path, value, schema["const"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("%s: %r < minimum %r" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errs.append("%s: %r > maximum %r" % (path, value, schema["maximum"]))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append("%s: missing required %r" % (path, k))
        for k, v in value.items():
            if k in props:
                validate(v, props[k], "%s.%s" % (path, k), errs)
            elif schema.get("additionalProperties") is False:
                errs.append("%s: unknown key %r" % (path, k))
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(v, schema["additionalProperties"], "%s.%s" % (path, k), errs)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            validate(v, schema["items"], "%s[%d]" % (path, i), errs)


def _errors(value, schema, path):
    e = []
    validate(value, schema, path, e)
    return e


def check(doc, schema):
    errs = []
    if not isinstance(doc, dict) or list(doc) != ["autoinstall"]:
        return ["the file must have exactly one top-level key, autoinstall (got %r)" % (list(doc) if isinstance(doc, dict) else doc)]
    ai = doc["autoinstall"]
    if not isinstance(ai, dict):
        return ["autoinstall is not a mapping"]
    if ai.get("version") != 1:
        errs.append("version must be 1, got %r" % ai.get("version"))
    for k in ("interactive-sections", "identity"):
        if k in ai:
            errs.append("%s must not be set (unattended install; 30-user.sh makes the account)" % k)
    ud = ai.get("user-data")
    if not isinstance(ud, dict) or ud.get("users") != []:
        errs.append("user-data must be a mapping with users: [] (no installer or cloud-init user)")
    ssh = ai.get("ssh") or {}
    if ssh.get("install-server") is not True or ssh.get("allow-pw") is not False:
        errs.append("ssh needs install-server: true and allow-pw: false, got %r" % ssh)
    pkgs = ai.get("packages") or []
    for p in REQUIRED_PACKAGES:
        if p not in pkgs:
            errs.append("packages lacks %s" % p)
    if not isinstance(ai.get("storage"), dict) or "layout" not in ai["storage"]:
        errs.append("storage needs a layout (the VM's single disk)")
    if ai.get("shutdown") not in ("poweroff", "reboot"):
        errs.append("shutdown must be poweroff or reboot, got %r" % ai.get("shutdown"))
    late = ai.get("late-commands")
    if not isinstance(late, list) or not late:
        errs.append("late-commands must be a non-empty list")
    else:
        bad = [c for c in late if not isinstance(c, str)]
        if bad:
            errs.append("late-commands must be plain strings (a ': ' makes YAML a mapping): %r" % bad)
        if late[0] != HOOK:
            errs.append("the first late-command must be %r, got %r" % (HOOK, late[0]))
        if late.count(HOOK) != 1:
            errs.append("the hook must run exactly once, found %d" % late.count(HOOK))
        if not prints_marker(late[-1], OK_MARKER):
            errs.append("the last late-command must print %s on the serial consoles (built at run time), got %r"
                        % (OK_MARKER, late[-1]))
    errc = ai.get("error-commands")
    if not isinstance(errc, list) or not any(prints_marker(c, FAILED_MARKER) for c in errc):
        errs.append("error-commands must print %s on the serial consoles (built at run time)" % FAILED_MARKER)
    net = ai.get("network")
    eths = net.get("ethernets") if isinstance(net, dict) else None
    if not isinstance(eths, dict) or not eths:
        errs.append("network needs ethernets (DHCP with dhcp-identifier: mac)")
    else:
        for k, e in eths.items():
            if not isinstance(e, dict) or e.get("dhcp4") is not True or e.get("dhcp-identifier") != "mac":
                errs.append("network.ethernets.%s needs dhcp4: true and dhcp-identifier: mac "
                            "(utm-vm.sh finds the VM by its MAC in macOS's DHCP leases), got %r" % (k, e))
    if schema is not None:
        known = set(schema.get("properties", {}))
        for k in ai:
            if k not in known:
                errs.append("top-level key %r is unknown to this subiquity" % k)
        validate(ai, schema, "autoinstall", errs)
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--schema")
    a = ap.parse_args()
    with open(a.file) as f:
        try:
            doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            sys.exit("check-autoinstall.py: %s: not YAML: %s" % (a.file, e))
    schema = None
    if a.schema:
        with open(a.schema) as f:
            schema = json.load(f)
    errs = check(doc, schema)
    if errs:
        for e in errs:
            print("check-autoinstall.py: %s: %s" % (a.file, e), file=sys.stderr)
        sys.exit(1)
    ai = doc["autoinstall"]
    print("check-autoinstall.py: %s OK: %d keys, %d late-commands, shutdown %s%s" % (
        a.file, len(ai), len(ai["late-commands"]), ai["shutdown"],
        ", valid against the subiquity schema" if schema is not None else ""))


if __name__ == "__main__":
    main()
