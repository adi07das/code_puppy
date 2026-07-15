"""End-to-end tests for the Shift+Enter / Alt+Enter / Ctrl+J newline bindings.

The bindings live inside ``get_input_with_combined_completion``, a very large
async function that constructs its ``KeyBindings`` locally. Rather than try to
scrape the internal state, we reproduce the *same* bindings in a minimal
``prompt_toolkit.Application`` and drive real byte sequences through the
Vt100 input parser -- exactly like a real terminal would. This proves that:

  1. The escape sequences we register actually reach our handlers after
     prompt_toolkit's key parser has had its way with them.
  2. Each handler inserts ``\\n`` into the current buffer instead of
     submitting.

If any of these break, users will silently lose Shift+Enter support in one
or more terminals, so treat regressions here as user-facing bugs.
"""

from __future__ import annotations

import threading
import time

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput


def _build_bindings() -> KeyBindings:
    """Mirror the newline bindings from ``prompt_toolkit_completion``.

    Kept in sync with the production block. If a new fallback is added
    there, add it here too and give it its own parametrized case below.
    """
    bindings = KeyBindings()

    def _insert(event):
        event.app.current_buffer.insert_text("\n")

    # Ctrl+J -- universal, works everywhere.
    bindings.add("c-j", eager=True)(_insert)

    # CSI-u form (kitty keyboard protocol / xterm formatOtherKeys=1 /
    # iTerm2 with "Report modifiers using CSI u").
    bindings.add(
        Keys.Escape, "[", "1", "3", ";", "2", "u", eager=True
    )(_insert)

    # Alt+Enter / Meta+Enter -- ESC + CR. Universal fallback.
    bindings.add(Keys.Escape, Keys.ControlM, eager=True)(_insert)

    # Alt+LF -- ESC + LF. iTerm2's default Option+Return mapping.
    bindings.add(Keys.Escape, Keys.ControlJ, eager=True)(_insert)

    # Ctrl+C: exit the app so tests can complete.
    @bindings.add("c-c")
    def _exit(event):
        event.app.exit()

    return bindings


def _run_with_sequence(sequence: str) -> str:
    """Boot a real prompt_toolkit ``Application``, feed ``sequence`` + Ctrl+C,
    return the resulting buffer text so tests can assert on it."""
    buf = Buffer()
    buf.text = "hi"
    buf.cursor_position = 2

    with create_pipe_input() as pipe_in:
        app = Application(
            layout=Layout(Window(BufferControl(buffer=buf))),
            key_bindings=_build_bindings(),
            input=pipe_in,
            output=DummyOutput(),
            full_screen=False,
        )

        def _drive() -> None:
            # Small delay so the app has fully entered its event loop
            # before we start feeding bytes. Without this the first
            # bytes can race the pipe_input setup on slow CI runners.
            time.sleep(0.05)
            pipe_in.send_text(sequence + "\x03")

        threading.Thread(target=_drive, daemon=True).start()
        app.run()

    return buf.text


@pytest.mark.parametrize(
    "label,sequence",
    [
        # kitty / CSI-u encoding of Shift+Enter
        ("csi_u_shift_enter", "\x1b[13;2u"),
        # Alt+Enter as ESC + CR (universal terminal fallback)
        ("alt_enter_cr", "\x1b\r"),
        # Alt+Enter as ESC + LF (iTerm2 default Option+Return)
        ("alt_enter_lf", "\x1b\n"),
        # Ctrl+J -- direct line feed byte
        ("ctrl_j", "\n"),
    ],
)
def test_newline_bindings_insert_linefeed(label, sequence):
    """Each supported keystroke must insert '\\n' rather than submit."""
    result = _run_with_sequence(sequence)
    assert result == "hi\n", (
        f"[{label}] expected 'hi\\n' after feeding {sequence!r}, got {result!r}"
    )


def test_plain_enter_is_not_hijacked_by_our_bindings():
    """Sanity check: a bare CR must NOT reach our newline handlers.

    In the real app, Enter is bound separately with multiline-aware logic
    (submit / accept-completion / insert-newline). This test proves our new
    bindings don't accidentally swallow plain Enter.
    """
    bindings = _build_bindings()

    submitted: list[str] = []

    @bindings.add("enter", eager=True)
    def _submit(event):
        submitted.append(event.app.current_buffer.text)
        event.app.exit()

    buf = Buffer()
    buf.text = "submit me"
    buf.cursor_position = len("submit me")

    with create_pipe_input() as pipe_in:
        app = Application(
            layout=Layout(Window(BufferControl(buffer=buf))),
            key_bindings=bindings,
            input=pipe_in,
            output=DummyOutput(),
            full_screen=False,
        )

        def _drive() -> None:
            time.sleep(0.05)
            pipe_in.send_text("\r")

        threading.Thread(target=_drive, daemon=True).start()
        app.run()

    assert submitted == ["submit me"], (
        "Plain Enter should have hit the submit handler, not been swallowed "
        "by a newline-insert binding."
    )
    assert buf.text == "submit me", (
        "Plain Enter must not insert a newline; multiline handling belongs to "
        "the dedicated Enter binding, not the newline-fallback bindings."
    )
