"""Help overlay: a one-shot modal listing global + screen bindings."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Static


_HELP_TEXT = """\
[b]vezir TUI -- keyboard shortcuts[/b]

[b cyan]Global[/b cyan]
  [b]ctrl+r[/b]  Record tab
  [b]ctrl+s[/b]  Sessions tab
  [b]ctrl+l[/b]  Refresh current screen
  [b]ctrl+q[/b]  Quit
  [b]F1[/b]      This help

[b cyan]Sessions list[/b cyan]
  [b]enter[/b]   Open selected session

[b cyan]Session detail[/b cyan]
  [b]enter[/b]   View highlighted artifact
  [b]l[/b]       Open labeling for this session
  [b]y[/b]       Sync now
  [b]e[/b]       Retry summary
  [b]p[/b]       Share with team (un-personal)
  [b]escape[/b]  Back to sessions

[b cyan]Record screen[/b cyan]
  [b]ctrl+space[/b] Start / stop recording
  [b]ctrl+p[/b]    Pause / resume
  [b]ctrl+u[/b]    Upload last recording
  [b]ctrl+x[/b]    Toggle personal flag

[b cyan]Label screen[/b cyan]
  [b]tab[/b]     Next speaker
  [b]space[/b]   Play / stop clip
  [b]enter[/b]   Submit all labels
  [b]escape[/b]  Cancel
"""


class HelpScreen(ModalScreen):
    """Dismiss with escape or enter."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        width: 60;
        max-width: 90%;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Static(_HELP_TEXT)
        yield Footer()
