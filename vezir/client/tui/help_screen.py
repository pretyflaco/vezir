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
  [b]ctrl+r[/b]        Record tab
  [b]ctrl+s[/b]        Sessions tab
  [b]ctrl+e[/b]        Teams tab
  [b]ctrl+t[/b]        Switch to next team (cycles all your teams)
  [b]ctrl+l[/b]        Refresh current screen
  [b]ctrl+q[/b]        Quit
  [b]ctrl+shift+q[/b]  Force quit (emergency escape from any screen)
  [b]F1[/b]            This help

[b cyan]Copy to clipboard[/b cyan]
  [b]ctrl+shift+c[/b]  Copy mouse-selected text (drag to select first)
  [b]c[/b]             Copy current item (session id on Sessions/Detail,
                  artifact body on Artifact view)
  Mouse selection: click-and-drag to select; on terminals that
  capture mouse for the app, hold [b]Shift[/b] while dragging to let
  the terminal handle the selection.

[b cyan]Sessions list[/b cyan]
  [b]enter[/b]   Open selected session
  [b]c[/b]       Copy selected session id
  [b]o[/b]       Open selected session in web browser
  [b]f[/b]       Open local artifacts folder (auto-pulls if needed)
  [b]d[/b]       Copy local artifacts path (auto-pulls if needed)

[b cyan]Teams tab[/b cyan]
  Lists every team you belong to (auto-discovered from the server --
  no `vezir team config add` needed).  The [green]●[/green] marks the
  active team.
  [b]enter[/b]   Switch active team to the highlighted row
  [b]ctrl+t[/b]  Cycle to the next team (works from any tab)
  [dim]Switching keeps the same login; only the team scope changes.[/dim]

[b cyan]Session detail[/b cyan]
  [b]enter[/b]   View highlighted artifact
  [b]c[/b]       Copy session id
  [b]o[/b]       Open in web browser
  [b]f[/b]       Open local artifacts folder
  [b]d[/b]       Copy local artifacts path
  [b]l[/b]       Open labeling for this session
  [b]y[/b]       Sync now
  [b]e[/b]       Retry summary
  [b]p[/b]       Share with team (un-personal)
  [b]ctrl+d[/b]  Delete session (admin or uploader; confirms first)
  [b]escape[/b]  Back to sessions

[b cyan]Artifact view[/b cyan]
  [b]c[/b]       Copy artifact body (text) or path (binary)
  [b]s[/b]       Save a copy to ~/Downloads
  [b]escape[/b]  Back to detail

[b cyan]Record screen[/b cyan]
  [b]ctrl+space[/b] Start / stop recording
  [b]ctrl+p[/b]    Pause / resume
  [b]ctrl+u[/b]    Import file for upload
  [b]ctrl+x[/b]    Toggle personal flag
  Audio level bars show live mic + system audio during recording.

[b cyan]CLI: vezir pull[/b cyan]
  Download meeting artifacts from the server into
  ~/vezir-meetings/<team>/.  Enables team-wide sharing.
    [dim]vezir pull                     # recent team meetings[/dim]
    [dim]vezir pull --since 2026-05-20  # since a date[/dim]
    [dim]vezir pull --session 01KSG...  # specific session[/dim]

[b cyan]Label screen[/b cyan]
  [b]tab[/b]     Next field (Textual default focus traversal)
  [b]enter[/b]   Submit all labels (from any handle input)
  [b]click ▶[/b] Play / stop the speaker's audio clip
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
