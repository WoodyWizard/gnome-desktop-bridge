"""Semantic desktop inspection and interaction through GNOME AT-SPI."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .errors import BackendUnavailable, InvalidRequest, NotFound, StaleReference
from .policy import AppIdentity


@dataclass(slots=True)
class SemanticTarget:
    ref: str
    accessible: Any
    app: AppIdentity


class AtspiBackend:
    """A deliberately small wrapper around libatspi's introspection interfaces.

    Element references live for one discovery generation. This prevents an API
    client from accidentally clicking a different widget after the UI changes.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._atspi: Any | None = None
        self._desktop: Any | None = None
        self._generation = 0
        self._next_ref = 1
        self._refs: dict[str, SemanticTarget] = {}

    def _ensure(self) -> Any:
        if self._atspi is not None and self._desktop is not None:
            return self._atspi
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            Atspi.init()
            desktop = Atspi.get_desktop(0)
            if desktop is None:
                raise RuntimeError("AT-SPI returned no desktop root")
        except Exception as exc:
            raise BackendUnavailable(
                "atspi",
                "Cannot connect to the desktop accessibility bus. "
                "Make sure the daemon runs inside the active GNOME user session.",
            ) from exc
        self._atspi = Atspi
        self._desktop = desktop
        return Atspi

    def available(self) -> tuple[bool, str | None]:
        try:
            with self._lock:
                self._ensure()
            return True, None
        except BackendUnavailable as exc:
            return False, exc.message

    def _new_generation(self) -> None:
        self._generation += 1
        self._next_ref = 1
        self._refs.clear()

    def _remember(self, accessible: Any, app: AppIdentity) -> str:
        ref = f"g{self._generation}:n{self._next_ref}"
        self._next_ref += 1
        self._refs[ref] = SemanticTarget(ref, accessible, app)
        return ref

    def resolve(self, ref: str) -> SemanticTarget:
        if not isinstance(ref, str) or not ref:
            raise InvalidRequest("A non-empty semantic ref is required")
        if len(ref) > 100:
            raise InvalidRequest("The semantic ref is too long")
        with self._lock:
            self._ensure()
            target = self._refs.get(ref)
            if target is None:
                if ref.startswith("g") and ":n" in ref:
                    raise StaleReference(ref)
                raise NotFound("Unknown semantic element reference", details={"ref": ref})
            return target

    @staticmethod
    def _safe(callable_value: Any, default: Any = None) -> Any:
        try:
            return callable_value()
        except Exception:
            return default

    def _identity(self, accessible: Any) -> AppIdentity:
        app = self._safe(accessible.get_application)
        if app is None:
            app = accessible
        name = self._safe(app.get_name, "") or ""
        toolkit_name = self._safe(app.get_toolkit_name, "") or ""
        pid = self._safe(app.get_process_id)
        app_id = ""

        attributes = self._safe(app.get_attributes, {}) or {}
        if isinstance(attributes, dict):
            app_id = str(
                attributes.get("application-id")
                or attributes.get("app-id")
                or attributes.get("desktop-entry")
                or ""
            )
        else:
            for item in self._safe(app.get_attributes_as_array, []) or []:
                if not isinstance(item, str) or ":" not in item:
                    continue
                key, value = item.split(":", 1)
                if key in {"application-id", "app-id", "desktop-entry"}:
                    app_id = value
                    break

        try:
            parsed_pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            parsed_pid = None
        return AppIdentity(
            name=self._bounded(name, 2000),
            app_id=self._bounded(app_id, 1000),
            toolkit_name=self._bounded(toolkit_name, 500),
            pid=parsed_pid,
        )

    def list_apps(self) -> dict[str, Any]:
        with self._lock:
            self._ensure()
            self._new_generation()
            child_count = self._safe(self._desktop.get_child_count, 0) or 0
            apps: list[dict[str, Any]] = []
            for index in range(max(0, int(child_count))):
                app = self._safe(lambda index=index: self._desktop.get_child_at_index(index))
                if app is None:
                    continue
                identity = self._identity(app)
                ref = self._remember(app, identity)
                apps.append(
                    {
                        "ref": ref,
                        **identity.as_dict(),
                        "role": self._safe(app.get_role_name, "application"),
                        "childCount": self._safe(app.get_child_count, 0),
                    }
                )
            return {"generation": self._generation, "apps": apps}

    def snapshot(
        self,
        ref: str,
        *,
        depth: int = 8,
        max_nodes: int = 1000,
        include_text: bool = True,
        redact_protected_text: bool = True,
    ) -> dict[str, Any]:
        if not 0 <= depth <= 30:
            raise InvalidRequest("depth must be between 0 and 30")
        if not 1 <= max_nodes <= 5000:
            raise InvalidRequest("maxNodes must be between 1 and 5000")
        with self._lock:
            old_target = self.resolve(ref)
            accessible = old_target.accessible
            identity = old_target.app
            self._new_generation()
            counter = [0]
            truncated = [False]
            text_budget = [256_000]
            visited: set[tuple[str, str]] = set()
            root = self._serialize_node(
                accessible,
                identity,
                remaining_depth=depth,
                max_nodes=max_nodes,
                counter=counter,
                truncated=truncated,
                visited=visited,
                include_text=include_text,
                redact_protected_text=redact_protected_text,
                text_budget=text_budget,
            )
            return {
                "generation": self._generation,
                "app": identity.as_dict(),
                "nodeCount": counter[0],
                "truncated": truncated[0],
                "root": root,
            }

    def _serialize_node(
        self,
        accessible: Any,
        identity: AppIdentity,
        *,
        remaining_depth: int,
        max_nodes: int,
        counter: list[int],
        truncated: list[bool],
        visited: set[tuple[str, str]],
        include_text: bool,
        redact_protected_text: bool,
        text_budget: list[int],
    ) -> dict[str, Any] | None:
        if counter[0] >= max_nodes:
            truncated[0] = True
            return None
        counter[0] += 1
        ref = self._remember(accessible, identity)

        role_name = self._bounded(self._safe(accessible.get_role_name, "unknown") or "unknown", 200)
        role = self._safe(accessible.get_role)
        protected = self._is_protected(role, role_name)
        if protected and redact_protected_text:
            # Do not fetch free-text properties from a password widget at all;
            # some toolkits mirror the secret in Accessible.name.
            name = "[REDACTED]"
            description = ""
        else:
            name = self._bounded(self._safe(accessible.get_name, "") or "", 2000)
            description = self._bounded(self._safe(accessible.get_description, "") or "", 4000)
        interfaces = sorted(
            self._bounded(str(value), 200)
            for value in (self._safe(accessible.get_interfaces, []) or [])[:100]
        )
        state_names = self._state_names(accessible)
        node: dict[str, Any] = {
            "ref": ref,
            "name": str(name),
            "role": str(role_name),
            "description": str(description),
            "states": state_names,
            "interfaces": interfaces,
            "childCount": self._safe(accessible.get_child_count, 0) or 0,
        }

        bounds = self._bounds(accessible)
        if bounds is not None:
            node["bounds"] = bounds

        actions = self._actions(accessible)
        if actions:
            node["actions"] = actions

        if protected:
            node["protected"] = True
        if include_text:
            if protected and redact_protected_text:
                if self._safe(accessible.get_text_iface) is not None:
                    node["text"] = "[REDACTED]"
            elif text_budget[0] > 0:
                text_result = self._text(accessible, max_chars=min(4096, text_budget[0]))
                if text_result is not None:
                    text_value, text_truncated = text_result
                    node["text"] = text_value
                    text_budget[0] -= len(text_value)
                    if text_truncated:
                        node["textTruncated"] = True
            numeric_value = self._numeric_value(accessible)
            if numeric_value is not None and not protected:
                node["value"] = numeric_value

        if remaining_depth <= 0:
            if int(node["childCount"]) > 0:
                node["childrenTruncated"] = True
            return node

        # Accessible proxies can occasionally expose cycles. Every node of one
        # snapshot belongs to one application, so its D-Bus object path is a
        # stable key; Python wrapper identity is not.
        object_path = getattr(accessible, "path", None)
        proxy_key = (str(identity.pid), str(object_path or id(accessible)))
        if proxy_key in visited:
            node["cycle"] = True
            return node
        visited.add(proxy_key)

        children: list[dict[str, Any]] = []
        child_count = min(max(0, int(node["childCount"])), max_nodes)
        for index in range(child_count):
            if counter[0] >= max_nodes:
                truncated[0] = True
                break
            child = self._safe(lambda index=index: accessible.get_child_at_index(index))
            if child is None:
                continue
            child_node = self._serialize_node(
                child,
                identity,
                remaining_depth=remaining_depth - 1,
                max_nodes=max_nodes,
                counter=counter,
                truncated=truncated,
                visited=visited,
                include_text=include_text,
                redact_protected_text=redact_protected_text,
                text_budget=text_budget,
            )
            if child_node is not None:
                children.append(child_node)
        if children:
            node["children"] = children
        return node

    def _state_names(self, accessible: Any) -> list[str]:
        state_set = self._safe(accessible.get_state_set)
        states = self._safe(state_set.get_states, []) if state_set is not None else []
        results: list[str] = []
        for state in states or []:
            value = getattr(state, "value_nick", None)
            if value:
                results.append(str(value))
                continue
            rendered = str(state)
            results.append(rendered.rsplit(".", 1)[-1].lower())
        return sorted(set(results))

    def _bounds(self, accessible: Any) -> dict[str, int] | None:
        atspi = self._atspi
        component = self._safe(accessible.get_component_iface)
        if component is None:
            return None
        rect = self._safe(lambda: component.get_extents(atspi.CoordType.SCREEN))
        if rect is None:
            return None
        try:
            return {
                "x": int(rect.x),
                "y": int(rect.y),
                "width": int(rect.width),
                "height": int(rect.height),
            }
        except (AttributeError, TypeError, ValueError):
            return None

    def _actions(self, accessible: Any) -> list[dict[str, Any]]:
        action_iface = self._safe(accessible.get_action_iface)
        if action_iface is None:
            return []
        count = min(int(self._safe(action_iface.get_n_actions, 0) or 0), 100)
        results: list[dict[str, Any]] = []
        for index in range(max(0, int(count))):
            results.append(
                {
                    "index": index,
                    "name": self._bounded(
                        self._safe(lambda index=index: action_iface.get_action_name(index), "")
                        or "",
                        500,
                    ),
                    "description": self._bounded(
                        self._safe(
                            lambda index=index: action_iface.get_action_description(index), ""
                        )
                        or "",
                        1000,
                    ),
                    "keyBinding": self._bounded(
                        self._safe(lambda index=index: action_iface.get_key_binding(index), "")
                        or "",
                        500,
                    ),
                }
            )
        return results

    @staticmethod
    def _bounded(value: Any, limit: int) -> str:
        rendered = str(value)
        return rendered if len(rendered) <= limit else rendered[:limit] + "…[TRUNCATED]"

    def _text(self, accessible: Any, max_chars: int) -> tuple[str, bool] | None:
        text_iface = self._safe(accessible.get_text_iface)
        if text_iface is None:
            return None
        count = self._safe(text_iface.get_character_count)
        if count is None:
            return None
        try:
            original_count = max(0, int(count))
            count = min(original_count, max_chars)
        except (TypeError, ValueError):
            return None
        value = self._safe(lambda: text_iface.get_text(0, count))
        if value is None:
            return None
        rendered = str(value)
        truncated = original_count > count or len(rendered) > max_chars
        return rendered[:max_chars], truncated

    def _numeric_value(self, accessible: Any) -> float | int | None:
        value_iface = self._safe(accessible.get_value_iface)
        if value_iface is None:
            return None
        value = self._safe(value_iface.get_current_value)
        return value if isinstance(value, (int, float)) else None

    def _is_protected(self, role: Any, role_name: str) -> bool:
        if "password" in str(role_name).casefold():
            return True
        try:
            return role == self._atspi.Role.PASSWORD_TEXT
        except Exception:
            return False

    def identity_for_ref(self, ref: str) -> AppIdentity:
        return self.resolve(ref).app

    def invoke(self, ref: str, *, action: int | str = 0) -> dict[str, Any]:
        with self._lock:
            target = self.resolve(ref)
            iface = self._safe(target.accessible.get_action_iface)
            if iface is None:
                raise InvalidRequest("The target does not expose an AT-SPI Action interface")
            count = min(max(int(self._safe(iface.get_n_actions, 0) or 0), 0), 1000)
            index: int | None = None
            if type(action) is int:
                index = action
            elif isinstance(action, str):
                wanted = action.casefold()
                for candidate in range(count):
                    name = self._safe(
                        lambda candidate=candidate: iface.get_action_name(candidate), ""
                    )
                    if self._bounded(name, 500).casefold() == wanted:
                        index = candidate
                        break
            if index is None or not 0 <= index < count:
                raise InvalidRequest(
                    "No matching action on the target",
                    details={"requested": action, "available": self._actions(target.accessible)},
                )
            name = self._safe(lambda: iface.get_action_name(index), "") or ""
            result = self._safe(lambda: iface.do_action(index), False)
            if not result:
                raise BackendUnavailable("atspi", "The application rejected the requested action")
            return {"ref": ref, "actionIndex": index, "actionName": name, "performed": True}

    def fill(self, ref: str, text: str) -> dict[str, Any]:
        if not isinstance(text, str):
            raise InvalidRequest("text must be a string")
        if len(text) > 100_000:
            raise InvalidRequest("text is too long (maximum 100000 characters)")
        with self._lock:
            target = self.resolve(ref)
            iface = self._safe(target.accessible.get_editable_text_iface)
            if iface is None:
                raise InvalidRequest("The target is not editable through AT-SPI")
            result = self._safe(lambda: iface.set_text_contents(text), False)
            if not result:
                raise BackendUnavailable("atspi", "The application rejected the text update")
            return {"ref": ref, "performed": True, "textLength": len(text)}

    def focus(self, ref: str) -> dict[str, Any]:
        with self._lock:
            target = self.resolve(ref)
            component = self._safe(target.accessible.get_component_iface)
            if component is None:
                raise InvalidRequest("The target does not expose an AT-SPI Component interface")
            result = self._safe(component.grab_focus, False)
            if not result:
                raise BackendUnavailable("atspi", "The application rejected the focus request")
            return {"ref": ref, "performed": True}

    def _toplevel(self, accessible: Any) -> Any | None:
        """Return the window-like ancestor directly below the application."""

        current = accessible
        for _ in range(64):
            parent = self._safe(current.get_parent)
            if parent is None:
                return None
            role_name = str(self._safe(parent.get_role_name, "") or "")
            if role_name == "application":
                return current
            current = parent
        return None

    def describe_ref(self, ref: str) -> dict[str, Any]:
        """Describe an element for pointer targeting and on-screen feedback."""

        with self._lock:
            target = self.resolve(ref)
            accessible = target.accessible
            role_name = self._bounded(
                self._safe(accessible.get_role_name, "unknown") or "unknown", 200
            )
            protected = self._is_protected(self._safe(accessible.get_role), role_name)
            name = "" if protected else self._bounded(self._safe(accessible.get_name, "") or "", 200)
            result: dict[str, Any] = {
                "ref": ref,
                "app": target.app,
                "role": role_name,
                "name": name,
                "bounds": self._bounds(accessible),
                "window": None,
            }
            window = self._toplevel(accessible)
            if window is not None:
                window_bounds = self._bounds(window)
                if window_bounds is not None:
                    result["window"] = {
                        "name": self._bounded(self._safe(window.get_name, "") or "", 1000),
                        "bounds": window_bounds,
                    }
            return result

    def bounds_for_ref(self, ref: str) -> tuple[AppIdentity, dict[str, int]]:
        with self._lock:
            target = self.resolve(ref)
            bounds = self._bounds(target.accessible)
            if bounds is None or bounds["width"] <= 0 or bounds["height"] <= 0:
                raise InvalidRequest("The target has no usable screen coordinates")
            return target.app, bounds
