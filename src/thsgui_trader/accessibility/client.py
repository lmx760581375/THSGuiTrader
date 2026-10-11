from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
import ctypes.util
import time
from typing import Iterator

from ..errors import AccessibilityUnavailable


_PTR = ctypes.c_void_p
_UTF8 = 0x08000100
_CF_NUMBER_DOUBLE = 13


@dataclass
class AXNode:
    role: str | None = None
    title: str | None = None
    value: str | None = None
    description: str | None = None
    help: str | None = None
    identifier: str | None = None
    enabled: bool = True
    children: list["AXNode"] = field(default_factory=list)
    ref: int | None = field(default=None, repr=False)
    _release_ref: bool = field(default=False, repr=False)

    def walk(self) -> Iterator["AXNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self) -> str:
        for item in (self.value, self.title, self.description):
            if item is not None and str(item).strip():
                return str(item).strip()
        return ""

    def release(self) -> None:
        for child in self.children:
            child.release()
        if self._release_ref and self.ref:
            _CORE_FOUNDATION.CFRelease(_PTR(self.ref))
            self.ref = None


class _NativeLibraries:
    def __init__(self) -> None:
        try:
            self.application_services = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            self.core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as exc:
            raise AccessibilityUnavailable("macOS ApplicationServices is unavailable") from exc

        ax = self.application_services
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
        ax.AXUIElementCreateApplication.restype = _PTR
        ax.AXUIElementCopyAttributeValue.argtypes = [_PTR, _PTR, ctypes.POINTER(_PTR)]
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementPerformAction.argtypes = [_PTR, _PTR]
        ax.AXUIElementPerformAction.restype = ctypes.c_int32
        ax.AXUIElementSetAttributeValue.argtypes = [_PTR, _PTR, _PTR]
        ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementCopyActionNames.argtypes = [_PTR, ctypes.POINTER(_PTR)]
        ax.AXUIElementCopyActionNames.restype = ctypes.c_int32

        cf = self.core_foundation
        cf.CFStringCreateWithCString.argtypes = [_PTR, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = _PTR
        cf.CFStringGetCString.argtypes = [_PTR, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFGetTypeID.argtypes = [_PTR]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetCount.argtypes = [_PTR]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [_PTR, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = _PTR
        cf.CFNumberGetValue.argtypes = [_PTR, ctypes.c_int, _PTR]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [_PTR]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFRetain.argtypes = [_PTR]
        cf.CFRetain.restype = _PTR
        cf.CFRelease.argtypes = [_PTR]


_LIBS: _NativeLibraries | None = None
_CORE_FOUNDATION: ctypes.CDLL


def _libs() -> _NativeLibraries:
    global _LIBS, _CORE_FOUNDATION
    if _LIBS is None:
        _LIBS = _NativeLibraries()
        _CORE_FOUNDATION = _LIBS.core_foundation
    return _LIBS


def _cf_string(value: str) -> _PTR:
    return _CORE_FOUNDATION.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)


def _cf_to_python(value: _PTR) -> str | bool | float | list[object] | None:
    if not value:
        return None
    cf = _CORE_FOUNDATION
    type_id = cf.CFGetTypeID(value)
    if type_id == cf.CFStringGetTypeID():
        buffer = ctypes.create_string_buffer(4096)
        if cf.CFStringGetCString(value, buffer, len(buffer), _UTF8):
            return buffer.value.decode("utf-8", errors="replace")
        return None
    if type_id == cf.CFBooleanGetTypeID():
        return bool(cf.CFBooleanGetValue(value))
    if type_id == cf.CFNumberGetTypeID():
        number = ctypes.c_double()
        if cf.CFNumberGetValue(value, _CF_NUMBER_DOUBLE, ctypes.byref(number)):
            return float(number.value)
        return None
    if type_id == cf.CFArrayGetTypeID():
        return [
            _cf_to_python(cf.CFArrayGetValueAtIndex(value, index))
            for index in range(cf.CFArrayGetCount(value))
        ]
    return None


class MacAccessibilityClient:
    """Small ctypes bridge for the macOS AXUIElement C API."""

    _ATTRIBUTES = {
        name: name
        for name in (
            "AXRole",
            "AXTitle",
            "AXValue",
            "AXDescription",
            "AXHelp",
            "AXIdentifier",
            "AXEnabled",
            "AXChildren",
            "AXWindows",
        )
    }

    def __init__(self, bundle_id: str, max_nodes: int = 6000) -> None:
        self.bundle_id = bundle_id
        self.max_nodes = max_nodes
        self._keys: dict[str, _PTR] = {}
        _libs()

    def _attribute_key(self, name: str) -> _PTR:
        key = self._keys.get(name)
        if key is None:
            key = _cf_string(name)
            self._keys[name] = key
        return key

    def _running_application(self):
        try:
            from AppKit import NSRunningApplication
        except ImportError as exc:
            raise AccessibilityUnavailable("pyobjc-framework-AppKit is required") from exc
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(self.bundle_id)
        if not apps:
            raise AccessibilityUnavailable(f"application is not running: {self.bundle_id}")
        return apps[0]

    def process_id(self) -> int:
        return int(self._running_application().processIdentifier())

    def _copy_attribute(self, ref: int, name: str):
        output = _PTR()
        error = _LIBS.application_services.AXUIElementCopyAttributeValue(
            _PTR(ref), self._attribute_key(name), ctypes.byref(output)
        )
        if error != 0 or not output.value:
            return None
        try:
            return _cf_to_python(output)
        finally:
            _CORE_FOUNDATION.CFRelease(output)

    def _copy_children(self, ref: int) -> list[int]:
        output = _PTR()
        error = _LIBS.application_services.AXUIElementCopyAttributeValue(
            _PTR(ref), self._attribute_key("AXChildren"), ctypes.byref(output)
        )
        if error != 0 or not output.value:
            return []
        try:
            count = _CORE_FOUNDATION.CFArrayGetCount(output)
            children: list[int] = []
            for index in range(count):
                child = _CORE_FOUNDATION.CFArrayGetValueAtIndex(output, index)
                if child:
                    _CORE_FOUNDATION.CFRetain(child)
                    children.append(int(child))
            return children
        finally:
            _CORE_FOUNDATION.CFRelease(output)

    def _copy_array_attribute(self, ref: int, name: str) -> list[int]:
        output = _PTR()
        error = _LIBS.application_services.AXUIElementCopyAttributeValue(
            _PTR(ref), self._attribute_key(name), ctypes.byref(output)
        )
        if error != 0 or not output.value:
            return []
        try:
            count = _CORE_FOUNDATION.CFArrayGetCount(output)
            refs: list[int] = []
            for index in range(count):
                child = _CORE_FOUNDATION.CFArrayGetValueAtIndex(output, index)
                if child:
                    _CORE_FOUNDATION.CFRetain(child)
                    refs.append(int(child))
            return refs
        finally:
            _CORE_FOUNDATION.CFRelease(output)

    def _copy_element_attribute(self, ref: int, name: str) -> int | None:
        output = _PTR()
        error = _LIBS.application_services.AXUIElementCopyAttributeValue(
            _PTR(ref), self._attribute_key(name), ctypes.byref(output)
        )
        if error != 0 or not output.value:
            return None
        return int(output.value)

    def _read_node(self, ref: int, depth: int, state: list[int], owned: bool) -> AXNode:
        state[0] += 1
        node = AXNode(ref=ref, _release_ref=owned)
        node.role = self._copy_attribute(ref, "AXRole")
        node.title = self._copy_attribute(ref, "AXTitle")
        node.value = self._copy_attribute(ref, "AXValue")
        node.description = self._copy_attribute(ref, "AXDescription")
        node.help = self._copy_attribute(ref, "AXHelp")
        node.identifier = self._copy_attribute(ref, "AXIdentifier")
        enabled = self._copy_attribute(ref, "AXEnabled")
        if isinstance(enabled, bool):
            node.enabled = enabled
        if depth >= 30 or state[0] >= self.max_nodes:
            return node
        for child in self._copy_children(ref):
            node.children.append(self._read_node(child, depth + 1, state, owned=True))
        return node

    def snapshot(self, *, prefer_focused: bool = True) -> AXNode:
        app_ref = _LIBS.application_services.AXUIElementCreateApplication(self.process_id())
        if not app_ref:
            raise AccessibilityUnavailable("unable to create Accessibility application element")
        try:
            focused_window = self._copy_element_attribute(int(app_ref), "AXFocusedWindow")
            main_window = self._copy_element_attribute(int(app_ref), "AXMainWindow")
            if prefer_focused:
                windows = [focused_window] if focused_window else []
                if not windows:
                    windows = [main_window] if main_window else []
            else:
                windows = [main_window] if main_window else []
            if not windows:
                windows = self._copy_array_attribute(int(app_ref), "AXWindows")
            root = AXNode(role="AXApplication", title=self.bundle_id, ref=int(app_ref), _release_ref=True)
            for window in windows:
                root.children.append(self._read_node(window, depth=1, state=[0], owned=True))
            return root
        except Exception:
            _CORE_FOUNDATION.CFRelease(app_ref)
            raise

    def read_attribute(self, node: AXNode, name: str):
        """Read one live AX attribute without rebuilding the control tree."""
        if not node.ref:
            return None
        return self._copy_attribute(node.ref, name)

    def press(self, node: AXNode) -> None:
        if not node.ref:
            raise AccessibilityUnavailable("Accessibility node has no native reference")
        action = _cf_string("AXPress")
        try:
            error = _LIBS.application_services.AXUIElementPerformAction(_PTR(node.ref), action)
        finally:
            _CORE_FOUNDATION.CFRelease(action)
        if error != 0:
            raise AccessibilityUnavailable(f"Accessibility action failed: {error}")

    def set_selected(self, node: AXNode, selected: bool) -> None:
        """Set an Accessibility row or control's selected state."""
        if not node.ref:
            raise AccessibilityUnavailable("Accessibility node has no native reference")
        selected_key = self._attribute_key("AXSelected")
        constant = "kCFBooleanTrue" if selected else "kCFBooleanFalse"
        selected_value = ctypes.c_void_p.in_dll(_CORE_FOUNDATION, constant)
        error = _LIBS.application_services.AXUIElementSetAttributeValue(
            _PTR(node.ref), selected_key, selected_value
        )
        if error != 0:
            raise AccessibilityUnavailable(f"Accessibility selection failed: {error}")

    def select(self, node: AXNode) -> None:
        """Select an Accessibility row or control without coordinate input."""
        self.set_selected(node, True)

    def is_selected(self, node: AXNode) -> bool:
        """Read whether an Accessibility row or control is selected."""
        if not node.ref:
            return False
        return bool(self._copy_attribute(node.ref, "AXSelected"))

    def set_value(self, node: AXNode, value: str) -> None:
        """Set a writable AXValue; never falls back to coordinate or keyboard input."""
        if not node.ref:
            raise AccessibilityUnavailable("Accessibility node has no native reference")
        attribute = self._attribute_key("AXValue")
        cf_value = _cf_string(value)
        try:
            error = _LIBS.application_services.AXUIElementSetAttributeValue(
                _PTR(node.ref), attribute, cf_value
            )
        finally:
            _CORE_FOUNDATION.CFRelease(cf_value)
        if error != 0:
            raise AccessibilityUnavailable(f"Accessibility value write failed: {error}")

    def focus(self, node: AXNode) -> None:
        """Make an AX control the focused element of the target application."""
        if not node.ref:
            raise AccessibilityUnavailable("Accessibility node has no native reference")
        # AX focus changes are ignored by AppKit controls while the target app
        # is backgrounded. Bring only the already-running client forward; no
        # window coordinates or mouse events are involved.
        application = self._running_application()
        application.activateWithOptions_(2)  # NSApplicationActivateIgnoringOtherApps
        # AppKit exposes AXFocused as the writable focus attribute on these
        # custom text fields. Setting the application-level focused element
        # alone returns success but does not move keyboard focus.
        focused_key = self._attribute_key("AXFocused")
        true_value = ctypes.c_void_p.in_dll(_CORE_FOUNDATION, "kCFBooleanTrue")
        focus_error = _LIBS.application_services.AXUIElementSetAttributeValue(
            _PTR(node.ref), focused_key, true_value
        )
        if focus_error != 0:
            raise AccessibilityUnavailable(f"Accessibility focus failed: {focus_error}")
        app_ref = _LIBS.application_services.AXUIElementCreateApplication(self.process_id())
        if not app_ref:
            raise AccessibilityUnavailable("unable to create Accessibility application element")
        attribute = self._attribute_key("AXFocusedUIElement")
        try:
            error = _LIBS.application_services.AXUIElementSetAttributeValue(
                _PTR(app_ref), attribute, _PTR(node.ref)
            )
        finally:
            _CORE_FOUNDATION.CFRelease(app_ref)
        if error != 0:
            raise AccessibilityUnavailable(f"Accessibility focus failed: {error}")

    @staticmethod
    def _post_key(
        key_code: int, *, down: bool, command: bool = False, process_id: int | None = None
    ) -> None:
        try:
            import Quartz
        except ImportError as exc:
            raise AccessibilityUnavailable("pyobjc-framework-Quartz is required") from exc
        event = Quartz.CGEventCreateKeyboardEvent(None, key_code, down)
        if command:
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        if process_id is None:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        else:
            Quartz.CGEventPostToPid(process_id, event)

    def type_text(self, node: AXNode, value: str, *, replace: bool = True) -> None:
        """Send real keyboard events to an AX text field.

        Some Tonghuashun fields accept AXValue writes but do not run their
        change handler. Keyboard events are required for the code field to
        refresh quotes and for the price/quantity calculations to update.
        """
        if not node.ref:
            raise AccessibilityUnavailable("Accessibility node has no native reference")
        self.focus(node)
        process_id = self.process_id()
        try:
            import Quartz
            import AppKit
        except ImportError as exc:
            raise AccessibilityUnavailable("pyobjc-framework-Quartz and AppKit are required") from exc

        # AppKit controls in this client ignore synthetic Unicode key events,
        # but accept the same Cmd+A/Cmd+V path as a human typing into them.
        pasteboard = AppKit.NSPasteboard.generalPasteboard()
        paste_type = AppKit.NSPasteboardTypeString
        previous = pasteboard.stringForType_(paste_type)
        pasteboard.clearContents()
        pasteboard.setString_forType_(value, paste_type)
        try:
            if replace:
                self._post_key(0, down=True, command=True, process_id=process_id)
                self._post_key(0, down=False, command=True, process_id=process_id)
            self._post_key(9, down=True, command=True, process_id=process_id)  # Cmd+V
            self._post_key(9, down=False, command=True, process_id=process_id)
            time.sleep(0.08)
        finally:
            pasteboard.clearContents()
            if previous is not None:
                pasteboard.setString_forType_(previous, paste_type)

    def press_return(self) -> None:
        process_id = self.process_id()
        self._post_key(36, down=True, process_id=process_id)
        self._post_key(36, down=False, process_id=process_id)

    def press_tab(self) -> None:
        """Commit the focused field without invoking the form default action."""
        process_id = self.process_id()
        self._post_key(48, down=True, process_id=process_id)
        self._post_key(48, down=False, process_id=process_id)

    def action_names(self, node: AXNode) -> list[str]:
        if not node.ref:
            return []
        output = _PTR()
        error = _LIBS.application_services.AXUIElementCopyActionNames(
            _PTR(node.ref), ctypes.byref(output)
        )
        if error != 0 or not output.value:
            return []
        try:
            return [
                str(_cf_to_python(_CORE_FOUNDATION.CFArrayGetValueAtIndex(output, index)) or "")
                for index in range(_CORE_FOUNDATION.CFArrayGetCount(output))
            ]
        finally:
            _CORE_FOUNDATION.CFRelease(output)

    def perform_action(self, node: AXNode, action_name: str) -> None:
        if action_name not in self.action_names(node):
            raise AccessibilityUnavailable(
                f"Accessibility action is not available: {action_name}"
            )
        action = _cf_string(action_name)
        try:
            error = _LIBS.application_services.AXUIElementPerformAction(
                _PTR(node.ref), action
            )
        finally:
            _CORE_FOUNDATION.CFRelease(action)
        if error != 0:
            raise AccessibilityUnavailable(f"Accessibility action failed: {error}")
