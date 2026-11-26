"""A small model of Haste's AccessPolicyV2 rule engine, for offline policy tests.

It follows haste-access-control, haste-fhir-client and haste-fhirpath at
HasteHealth commit 6d3a3ac (2026-11-26; the files involved are unchanged since
bf26bf3), only as far as the seed policy needs. ``request_reflection.rs``:

* ``%request`` has ``type``, ``level``, ``resource_type`` and ``id``; a field
  the request shape lacks is empty.
* Every search is ``level = 'type'``, a system search (``?_type=``) included,
  and a system search has no ``resource_type``.
* ``resource_type`` and ``id`` exist only for create, read, vread, update,
  patch, delete and type search (and a compartment's inner request). History
  and operations (``$everything``) have neither.
* ``%request.parameters`` is always empty. It is a ``ParsedParameters``, whose
  ``flatten()`` returns nothing, and FHIRPath flattens every field it navigates
  to (``url.rs``, ``fhirpath/src/lib.rs`` ``Invocation::Identifier``). So a
  policy cannot see search parameters at all.
* ``%target`` is the attribute read of ``resource_type/id``; empty when the
  resource does not exist.
* A rule's target and condition must each give one boolean, otherwise the
  evaluation is an error, and an error denies the request. A false condition
  with the default ``permit`` effect is a deny; a false target skips the rule
  (undetermined). ``any`` takes the best child result. The policy denies as
  soon as one top-level rule denies, and grants only on an allow.
* FHIRPath: paths, string literals, ``|``, ``=``, ``in``, ``and``, ``or`` with
  three-valued logic, ``exists()``, ``empty()``, ``all()`` and ``$this``.
  An unknown field is empty.

It is a test aid, not shipped: the live ``$evaluate-policy`` check is what proves the policy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

DENY, UNDETERMINED, ALLOW = -1, 0, 1
_REQUEST_FIELDS = ("type", "level", "resource_type", "id")
_TOKEN = re.compile(r"\s*(?:('(?:[^'\\]|\\.)*')|(%?\$?[A-Za-z_][A-Za-z0-9_]*)|(\S))")


class PolicyError(Exception):
    """An evaluation error. Haste denies the request."""


@dataclass
class Request:
    type: str
    level: str
    resource_type: str | None = None
    id: str | None = None


def parse_request(method: str, url: str) -> Request:
    """The request reflection Haste would build for a relative URL."""
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if method == "POST":
        return Request("create", "type", parts[0])
    if method == "DELETE":
        return Request("delete", "instance", parts[0], parts[1])
    if not parts:
        # A system search: Haste calls every search type-level.
        return Request("search", "type")
    if parts == ["metadata"]:
        return Request("capabilities", "system")
    if parts[0] == "_history":
        return Request("history", "system")
    if len(parts) == 1:
        return Request("search", "type", parts[0])
    if parts[1] == "_history":
        return Request("history", "type")
    if parts[1].startswith("$"):
        return Request("invoke", "type")
    if len(parts) == 2:
        return Request("read", "instance", parts[0], parts[1])
    if parts[2] == "_history":
        return Request("history", "instance")
    if parts[2].startswith("$"):
        return Request("invoke", "instance")
    # Patient/{id}/Observation: Haste reflects the inner search, which has no id.
    return Request("compartment", "type", parts[2])


def evaluate_policy(
    policy: Mapping[str, Any],
    request: Request,
    store: Mapping[str, Mapping[str, Any]],
) -> int:
    """ALLOW, UNDETERMINED or DENY; raises PolicyError on an evaluation error."""
    env = _Env(policy, request, store)
    result = DENY
    for rule in policy.get("rule") or []:
        level = _rule(rule, env)
        if level == DENY:
            return DENY
        result = max(result, level)
    return result


def decide(policy: Mapping[str, Any], method: str, url: str, store: Mapping[str, Any]) -> bool:
    """Whether Haste would grant the request: errors and undetermined deny."""
    try:
        return evaluate_policy(policy, parse_request(method, url), store) == ALLOW
    except PolicyError:
        return False


class _Env:
    def __init__(
        self, policy: Mapping[str, Any], request: Request, store: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self.policy = policy
        self.request = request
        self.store = store

    def variable(self, name: str) -> list[Any]:
        if name == "request":
            return [self.request]
        for attribute in self.policy.get("attribute") or []:
            if attribute.get("attributeId") != name:
                continue
            path = attribute["operation"]["path"]["expression"]
            if path != "%request.resource_type + '/' + %request.id":
                raise PolicyError(f"model only reads the request target, not {path}")
            if self.request.resource_type is None or self.request.id is None:
                raise PolicyError("attribute path is not a single value")
            found = self.store.get(f"{self.request.resource_type}/{self.request.id}")
            return [found] if found is not None else []
        raise PolicyError(f"unknown variable %{name}")


def _rule(rule: Mapping[str, Any], env: _Env) -> int:
    target = rule.get("target")
    if target is not None and not _boolean(target["expression"], env, "target"):
        return UNDETERMINED
    combine = rule.get("combineBehavior")
    if combine in ("any", "all-of"):
        if "condition" in rule:
            raise PolicyError("condition is not supported with combineBehavior")
        levels = [_rule(child, env) for child in rule.get("rule") or []]
        if combine == "any":
            return max([DENY, *levels])
        if DENY in levels:
            return DENY
        return min([ALLOW, *levels])
    if combine is not None:
        raise PolicyError("unsupported combineBehavior")
    if rule.get("rule"):
        raise PolicyError("nested rules need combineBehavior")
    if "condition" not in rule:
        raise PolicyError("condition is not specified")
    passed = _boolean(rule["condition"]["expression"], env, "condition")
    effect = rule.get("effect", "permit")
    if effect == "deny":
        return DENY if passed else ALLOW
    return ALLOW if passed else DENY


def _boolean(expression: Mapping[str, Any], env: _Env, what: str) -> bool:
    if expression.get("language") != "text/fhirpath":
        raise PolicyError("model only supports text/fhirpath")
    values = _Parser(str(expression["expression"]), env).parse()
    if len(values) != 1 or not isinstance(values[0], bool):
        raise PolicyError(f"{what} did not evaluate to a single boolean: {values!r}")
    return values[0]


class _Parser:
    """Recursive descent over the FHIRPath subset; evaluates as it parses."""

    def __init__(self, text: str, env: _Env, this: list[Any] | None = None) -> None:
        self.tokens = [m.group(1) or m.group(2) or m.group(3) for m in _TOKEN.finditer(text)]
        self.tokens = [token for token in self.tokens if token]
        self.at = 0
        self.env = env
        self.this = this

    def parse(self) -> list[Any]:
        values = self._or()
        if self.at != len(self.tokens):
            raise PolicyError(f"unexpected {self.tokens[self.at]!r}")
        return values

    def _peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise PolicyError(f"expected {expected!r}, got {token!r}")
        self.at += 1
        return token

    def _or(self) -> list[Any]:
        left = self._and()
        while self._peek() == "or":
            self._take()
            right = self._and()
            left = _logic(left, right, "or")
        return left

    def _and(self) -> list[Any]:
        left = self._in()
        while self._peek() == "and":
            self._take()
            right = self._in()
            left = _logic(left, right, "and")
        return left

    def _in(self) -> list[Any]:
        left = self._equal()
        if self._peek() == "in":
            self._take()
            collection = self._equal()
            if not left:
                return []
            if len(left) != 1:
                raise PolicyError("membership needs a single element")
            return [left[0] in collection]
        return left

    def _equal(self) -> list[Any]:
        left = self._union()
        if self._peek() == "=":
            self._take()
            right = self._union()
            if not left or not right:
                return []
            return [left == right]
        return left

    def _union(self) -> list[Any]:
        left = self._path()
        while self._peek() == "|":
            self._take()
            for value in self._path():
                if value not in left:
                    left = [*left, value]
        return left

    def _path(self) -> list[Any]:
        values = self._primary()
        while self._peek() == ".":
            self._take()
            name = self._take()
            if self._peek() == "(":
                values = self._function(name, values)
            else:
                values = _field(values, name)
        return values

    def _function(self, name: str, values: list[Any]) -> list[Any]:
        self._take("(")
        if name in ("exists", "empty"):
            self._take(")")
            return [bool(values)] if name == "exists" else [not values]
        if name == "all":
            start = self.at
            depth = 0
            while True:
                token = self._take()
                if token == "(":
                    depth += 1
                elif token == ")":
                    if depth == 0:
                        break
                    depth -= 1
            criteria = " ".join(self.tokens[start : self.at - 1])
            for value in values:
                result = _Parser(criteria, self.env, [value]).parse()
                if result != [True]:
                    return [False]
            return [True]
        raise PolicyError(f"model does not support {name}()")

    def _primary(self) -> list[Any]:
        token = self._take()
        if token == "(":
            values = self._or()
            self._take(")")
            return values
        if token.startswith("'"):
            return [token[1:-1]]
        if token == "$this":
            if self.this is None:
                raise PolicyError("$this outside a function")
            return self.this
        if token.startswith("%"):
            return self.env.variable(token[1:])
        raise PolicyError(f"model does not support {token!r} at the start of a path")


def _field(values: list[Any], name: str) -> list[Any]:
    found: list[Any] = []
    for value in values:
        if isinstance(value, Request):
            # `parameters` and anything else the reflection lacks are empty.
            item = getattr(value, name, None) if name in _REQUEST_FIELDS else None
        elif isinstance(value, Mapping):
            item = value.get(name)
        else:
            item = None
        if item is None:
            continue
        if isinstance(item, list):
            found.extend(item)
        else:
            found.append(item)
    return found


def _logic(left: list[Any], right: list[Any], op: str) -> list[Any]:
    def one(values: list[Any]) -> bool | None:
        if not values:
            return None
        if len(values) != 1 or not isinstance(values[0], bool):
            raise PolicyError(f"{op} needs booleans")
        return bool(values[0])

    a, b = one(left), one(right)
    if op == "and":
        if a is False or b is False:
            return [False]
        return [True] if a and b else []
    if a is True or b is True:
        return [True]
    return [False] if a is False and b is False else []
