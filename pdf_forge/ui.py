from __future__ import annotations

import os
import shutil
import sys
from typing import Optional

from .constants import *  # noqa: F401,F403

__all__ = ['Color', 'enable_ansi_colors', 'colorize', 'print_success', 'print_warning', 'print_error', 'print_heading', 'print_info', 'print_note', 'print_kv', 'back_text', 'reset_questions', 'set_operation_prompt', 'question_prompt', 'Prompt', 'guidance_text', 'print_banner', '_print_progress']

class Color:
    """ANSI color codes used for readable terminal output.

    Only bright (high-intensity) foreground colors are used for content so the
    text stays readable on dark terminal themes. Dark/standard-intensity colors
    (30-37) are intentionally avoided because they render poorly on some
    consoles.
    """

    RESET = "\033[0m"
    BOLD = "\033[1m"

    # Curated palette of bright, readable colors for a consistent look.
    RED = "\033[91m"                  # errors
    GREEN = "\033[92m"                # success / default-option marker
    YELLOW = "\033[93m"               # warnings
    BLUE = "\033[38;5;117m"           # info / progress (light sky blue)
    MAGENTA = "\033[38;5;219m"        # accents (light pink-magenta)
    CYAN = "\033[38;5;123m"           # prompts (light cyan)
    WHITE = "\033[97m"                # high-contrast detail text
    GRAY = "\033[38;5;252m"           # field labels
    DIM = "\033[38;5;250m"            # subtle separators
    ORANGE = "\033[38;5;222m"         # accents
    PINK = "\033[38;5;218m"           # accents
    LIME = "\033[38;5;118m"           # accents
    LIGHT_BLUE = "\033[38;5;117m"     # menu headings / option numbers
    NOTE_YELLOW = "\033[38;5;227m"    # informational notes (e.g. "Logging to")

    # Title banner color (truecolor hot pink).
    WIZARD_TITLE = "\033[38;2;255;50;115m"

    # Back/quit prompt accents used by the {back=0, quit=exit} hint.
    BACK_PROMPT = "\033[38;5;166m"    # orange for back=0
    EXIT_PROMPT = "\033[38;5;32m"     # blue for quit=exit

    # Extra accent colors to give the UI a varied ~20-color palette.
    AQUA = "\033[38;5;159m"           # pale aqua
    VIOLET = "\033[38;5;141m"         # soft violet
    TEAL = "\033[38;5;37m"            # teal
    CORAL = "\033[38;5;209m"          # coral
    GOLD = "\033[38;5;220m"           # gold
    SKY = "\033[38;5;75m"             # sky blue
    HINT_YELLOW = "\033[38;5;221m"    # yellow used for (y/n) hints


_COLOR_ENABLED = False


def enable_ansi_colors() -> None:
    """Enable ANSI escape sequence processing on the current terminal.

    On Windows 10+ the virtual terminal mode must be enabled explicitly for
    legacy consoles. On other platforms ANSI is assumed available when the
    stream is a TTY. Failures are non-fatal; colors are simply disabled.
    """
    global _COLOR_ENABLED

    if not sys.stdout.isatty():
        _COLOR_ENABLED = False
        return

    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
                _COLOR_ENABLED = True
            else:
                _COLOR_ENABLED = False
        except Exception:  # pragma: no cover - depends on host console
            _COLOR_ENABLED = False
    else:
        _COLOR_ENABLED = True


def colorize(text: str, color: str) -> str:
    """Wrap text in a color code when colors are enabled."""
    if _COLOR_ENABLED:
        return f"{color}{text}{Color.RESET}"
    return text


def print_success(message: str) -> None:
    print(colorize(message, Color.GREEN))


def print_warning(message: str) -> None:
    print(colorize(message, Color.YELLOW))


def print_error(message: str) -> None:
    print(colorize(message, Color.RED))


def print_heading(message: str) -> None:
    print(colorize(message, Color.BOLD + Color.LIGHT_BLUE))


def print_info(message: str) -> None:
    print(colorize(message, Color.BLUE))


def print_note(message: str) -> None:
    """Informational note printed in note-yellow."""
    print(colorize(message, Color.NOTE_YELLOW))


def print_kv(label: str, value: str, value_color: str = None) -> None:
    """Print a 'label: value' line: gray label, colored value."""
    if value_color is None:
        value_color = Color.WHITE
    print(
        "  "
        + colorize(f"{label + ':':<19}", Color.GRAY)
        + colorize(str(value), value_color)
    )


def back_text(text: str = "back=0, quit=exit") -> str:
    """Return a colored '{back=0, quit=exit}' control hint.

    'back' parts are orange, 'exit' parts are blue, braces/commas white.
    No trailing colon (question_prompt appends it).
    """
    parts = []
    for part in text.split(", "):
        lowered = part.lower()
        if "back" in lowered:
            parts.append(colorize(part, Color.BACK_PROMPT))
        elif "exit" in lowered:
            parts.append(colorize(part, Color.EXIT_PROMPT))
        else:
            parts.append(colorize(part, Color.WHITE))
    joined = colorize(", ", Color.WHITE).join(parts)
    return colorize("{", Color.WHITE) + joined + colorize("}", Color.WHITE)


def guidance_text(plain: str, keywords=()) -> str:
    """Colour constant prompt guidance the way FFmWiz colours its own.

    The guidance body stays in the normal hint colour and only the parts you
    can actually *type* are picked out in light blue - the same split FFmWiz
    uses when it highlights the example value inside an otherwise hint-coloured
    detail string. Each highlight restores the hint colour afterwards so the
    rest of the line does not fall back to the terminal default.
    """
    if not _COLOR_ENABLED:
        return plain
    text = plain
    for keyword in keywords:
        text = text.replace(
            keyword,
            f"{Color.LIGHT_BLUE}{keyword}{Color.RESET}{Color.HINT_YELLOW}",
            1,
        )
    return text


_question_no = 0
_prompt_prefix: Optional[str] = None


def reset_questions() -> None:
    """Reset the local per-operation question counter (prefix is preserved).

    Only the local occurrence counter is reset; the fixed operation prefix set
    by :func:`set_operation_prompt` is left in place. Operations may call this
    at the top without disturbing the prefix the launching menu chose.
    """
    global _question_no
    _question_no = 0


def set_operation_prompt(prefix: Optional[object]) -> None:
    """Begin a new hierarchical numbering context.

    ``prefix`` is the menu/submenu number the user selected to launch the
    operation (e.g. ``"1"`` for "Add PDF files one by one"). Prompts then read
    ``prefix-1``, ``prefix-2``, ... where only the local counter advances -
    validation retries advance the counter but never the prefix. Pass ``None``
    for menu- or queue-control prompts, which fall back to plain numbering.
    """
    global _prompt_prefix, _question_no
    _prompt_prefix = None if prefix is None else str(prefix)
    _question_no = 0


def _next_question_label() -> str:
    """Advance the local counter and return the hierarchical prompt label."""
    global _question_no
    _question_no += 1
    if _prompt_prefix:
        return f"{_prompt_prefix}-{_question_no}"
    return str(_question_no)


class Prompt(str):
    """A prompt whose visible number is refreshed every time it is displayed.

    Operations build a prompt once and then re-use it inside a retry loop. With
    a plain string the label is frozen at construction, so after a *nested*
    prompt (custom DPI, a yes/no confirmation, ...) advanced the counter, the
    outer prompt would redisplay a stale, already-used number - the visible
    sequence then repeats or moves backward.

    This behaves exactly like the string it renders to (so existing callers and
    assertions are unaffected), but :meth:`render` - used by the single input
    helper - allocates a fresh number on every display after the first.
    """

    def __new__(cls, build):
        obj = super().__new__(cls, build(_next_question_label()))
        obj._build = build
        obj._first_display = True
        return obj

    def render(self) -> str:
        """Return the text to display now, numbering it at display time."""
        if self._first_display:
            self._first_display = False
            return str(self)
        return self._build(_next_question_label())


def question_prompt(
    title: str,
    details: Optional[str] = None,
    default: Optional[str] = None,
    back: str = "back=0, quit=exit",
) -> "Prompt":
    """Build a numbered prompt string ending with ': '.

    Format: '\\n{prefix}-{n}. {title} ({details}) [{default}] {back}: '
        * title   -> bold (white); the dynamic field name
        * details -> hint-yellow inside white parentheses
        * default -> green [default] marker (the Enter value)
        * back    -> colored {back=0, quit=exit} control hint

    The leading label is ``{prefix}-{n}`` when an operation prefix is active
    (see :func:`set_operation_prompt`), otherwise the plain occurrence number.
    Path prompts pass their guidance through :func:`guidance_text`, which picks
    out the typeable keywords inside this hint-coloured detail string.
    """
    def _build(label: str) -> str:
        text = "\n" + colorize(f"{label}. {title}", Color.BOLD)
        if details:
            text += (
                " "
                + colorize("(", Color.WHITE)
                + colorize(details, Color.HINT_YELLOW)
                + colorize(")", Color.WHITE)
            )
        if default is not None:
            text += " " + colorize(f"[{default}]", Color.GREEN)
        if back:
            text += " " + back_text(back)
        return text + colorize(": ", Color.WHITE)

    return Prompt(_build)


def print_banner(text: str) -> None:
    """Print a centered hot-pink title with a single full-width '=' rule.

    One title line + one rule, printed once at startup.
    """
    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        width = 80
    padding = max(0, (width - len(text)) // 2)
    print(" " * padding + colorize(text, Color.BOLD + Color.WIZARD_TITLE))
    print(colorize("=" * width, Color.WIZARD_TITLE))


def _print_progress(prefix: str, current: int, total: int) -> None:
    """Print a single-line progress indicator without flooding the console."""
    if total <= 0:
        return
    # Limit updates to avoid excessive output on large documents.
    step = max(1, total // 50)
    if current == total or current % step == 0:
        percent = int(current * 100 / total)
        # Color each segment distinctly from the surrounding text.
        line = (
            "\r"
            + colorize(f"{prefix}: ", Color.AQUA)
            + colorize(f"{current}/{total}", Color.GOLD)
            + colorize(" (", Color.DIM)
            + colorize(f"{percent}%", Color.LIME)
            + colorize(")", Color.DIM)
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        if current == total:
            sys.stdout.write("\n")
            sys.stdout.flush()
