"""Prompter abstraction for the setup wizard.

The wizard flow is pure data + function calls against a
:class:`Prompter` instance. Concrete implementations:

* :class:`QuestionaryPrompter` — the interactive one, backed by
  ``questionary``. Installed via ``uv sync --extra tui``.
* :class:`FakePrompter` — fed a script of answers; used by tests and
  anywhere we want to drive the wizard headlessly without a TTY.

Why not call questionary directly from the wizard? Two reasons. First,
tests would need a fake TTY, which is brittle. Second, questionary
returns ``None`` on Ctrl-C; translating that into an exception at the
boundary means the wizard body can ignore the difference and rely on
the prompter for cancellation semantics.

Cancellation protocol: any Ctrl-C / Ctrl-D / script exhaustion raises
:class:`PromptCancelled`. The wizard catches it once at the top and
reports "setup cancelled" — no need to sprinkle ``if answer is None``
checks through the flow.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, Protocol, runtime_checkable

from agentbus.setup import theme

Tone = Literal["info", "success", "warn", "error", "muted"]


class PromptCancelled(Exception):
    """Raised when the user cancels (Ctrl-C/D) or the FakePrompter
    script runs dry."""


@runtime_checkable
class Prompter(Protocol):
    """The surface the wizard talks to.

    All methods are synchronous — questionary supports async but our
    flow is a straight line of user input and we'd rather keep the
    wizard code readable than prematurely async-ify it.
    """

    def banner(self, version: str | None = None) -> None: ...
    def section(self, title: str, subtitle: str | None = None) -> None: ...
    def note(self, text: str, *, tone: Tone = "info") -> None: ...
    def outro(self, config_path: str, *, wrote: bool) -> None: ...

    def text(
        self,
        message: str,
        *,
        default: str | None = None,
        validate: Callable[[str], str | None] | None = None,
    ) -> str: ...
    def password(self, message: str, *, default: str | None = None) -> str: ...
    def select(
        self,
        message: str,
        *,
        choices: Sequence[tuple[str, str]],
        default: str | None = None,
    ) -> str: ...
    def multi_select(
        self,
        message: str,
        *,
        choices: Sequence[tuple[str, str]],
        default: Sequence[str] | None = None,
    ) -> list[str]: ...
    def confirm(self, message: str, *, default: bool = False) -> bool: ...


# ── interactive implementation ────────────────────────────────────────────


class QuestionaryPrompter:
    """Questionary-backed Prompter — the real TUI.

    Lazy-imports questionary in ``__init__`` so the rest of agentbus
    can still import ``agentbus.setup`` even when the TUI extra is not
    installed. ``agentbus setup`` itself fails fast with an install
    hint when questionary is missing — see :func:`~agentbus.setup.wizard.run_setup`.
    """

    def __init__(self) -> None:
        import questionary

        self._q = questionary
        self._style = questionary.Style(
            [
                ("qmark", "fg:cyan bold"),
                ("question", "bold"),
                ("answer", "fg:cyan bold"),
                ("pointer", "fg:cyan bold"),
                ("highlighted", "fg:cyan bold"),
                ("selected", "fg:green"),
                ("separator", "fg:#6c6c6c"),
                ("instruction", "fg:#808080"),
                ("text", ""),
                ("disabled", "fg:#858585 italic"),
            ]
        )

    # ── presentation ──────────────────────────────────────────────────

    def banner(self, version: str | None = None) -> None:
        print(theme.render_banner(version))

    def section(self, title: str, subtitle: str | None = None) -> None:
        print(theme.render_section(title, subtitle))

    def note(self, text: str, *, tone: Tone = "info") -> None:
        print(theme.render_note(text, tone=tone))

    def outro(self, config_path: str, *, wrote: bool) -> None:
        print(theme.render_outro(config_path, wrote=wrote))

    # ── inputs ─────────────────────────────────────────────────────────

    def text(
        self,
        message: str,
        *,
        default: str | None = None,
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        def _validate(val: str) -> bool | str:
            if validate is None:
                return True
            err = validate(val)
            return True if err is None else err

        answer = self._q.text(
            message,
            default=default or "",
            style=self._style,
            validate=_validate,
        ).ask()
        if answer is None:
            raise PromptCancelled()
        return answer

    def password(self, message: str, *, default: str | None = None) -> str:
        # Questionary's password prompt doesn't surface a default value in the
        # UI (a masked echo would be confusing). We emulate "keep existing"
        # by returning the default verbatim when the user hits Enter on an
        # empty prompt.
        hint = " (press Enter to keep existing)" if default else ""
        answer = self._q.password(
            message + hint,
            style=self._style,
        ).ask()
        if answer is None:
            raise PromptCancelled()
        return answer or (default or "")

    def select(
        self,
        message: str,
        *,
        choices: Sequence[tuple[str, str]],
        default: str | None = None,
    ) -> str:
        label_to_value = {label: value for value, label in choices}
        labels = list(label_to_value)
        default_label = next(
            (label for value, label in choices if value == default),
            labels[0],
        )
        answer = self._q.select(
            message,
            choices=labels,
            default=default_label,
            style=self._style,
            use_indicator=False,
            use_shortcuts=False,
        ).ask()
        if answer is None:
            raise PromptCancelled()
        return label_to_value[answer]

    def multi_select(
        self,
        message: str,
        *,
        choices: Sequence[tuple[str, str]],
        default: Sequence[str] | None = None,
    ) -> list[str]:
        default_set = set(default or [])
        q_choices = [
            self._q.Choice(label, value=value, checked=(value in default_set))
            for value, label in choices
        ]
        answer = self._q.checkbox(
            message,
            choices=q_choices,
            style=self._style,
            instruction="(space to toggle, enter to confirm)",
        ).ask()
        if answer is None:
            raise PromptCancelled()
        return list(answer)

    def confirm(self, message: str, *, default: bool = False) -> bool:
        answer = self._q.confirm(
            message,
            default=default,
            style=self._style,
        ).ask()
        if answer is None:
            raise PromptCancelled()
        return bool(answer)


# ── scripted implementation for tests ─────────────────────────────────────


class FakePrompter:
    """Deterministic Prompter for tests.

    Construction takes a list of answers that the wizard will consume
    in order — one per input call (``text``, ``password``, ``select``,
    ``multi_select``, ``confirm``). Presentation calls (``banner``,
    ``section``, ``note``, ``outro``) are recorded in :attr:`output`
    so tests can assert on the overall flow without caring about
    exact text formatting.

    When the script runs dry, :class:`PromptCancelled` is raised —
    same behavior as a real user hitting Ctrl-C. Tests that want to
    assert on cancellation paths can script a short list on purpose.

    ``validate`` callbacks *are* run on scripted text answers so
    wizard-side validation logic is exercised. A validator that
    returns an error string causes the FakePrompter to raise
    :class:`AssertionError` — treat that as a test bug, not an
    expected flow.
    """

    def __init__(self, answers: Sequence[object]) -> None:
        self._answers: list[object] = list(answers)
        self.output: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, str]] = []

    def _pop(self, kind: str, message: str) -> object:
        self.prompts.append((kind, message))
        if not self._answers:
            raise PromptCancelled()
        return self._answers.pop(0)

    # presentation — record only
    def banner(self, version: str | None = None) -> None:
        self.output.append(("banner", version or ""))

    def section(self, title: str, subtitle: str | None = None) -> None:
        self.output.append(("section", f"{title}:{subtitle or ''}"))

    def note(self, text: str, *, tone: Tone = "info") -> None:
        self.output.append(("note", f"{tone}:{text}"))

    def outro(self, config_path: str, *, wrote: bool) -> None:
        self.output.append(("outro", f"{'wrote' if wrote else 'cancelled'}:{config_path}"))

    # inputs — pop from script
    def text(
        self,
        message: str,
        *,
        default: str | None = None,
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        value = self._pop("text", message)
        if not isinstance(value, str):
            raise AssertionError(
                f"FakePrompter expected str for text() — got {type(value).__name__}"
            )
        resolved = value if value != "" else (default or "")
        if validate is not None:
            err = validate(resolved)
            if err is not None:
                raise AssertionError(
                    f"FakePrompter validator rejected scripted answer for {message!r}: {err}"
                )
        return resolved

    def password(self, message: str, *, default: str | None = None) -> str:
        value = self._pop("password", message)
        if not isinstance(value, str):
            raise AssertionError(
                f"FakePrompter expected str for password() — got {type(value).__name__}"
            )
        return value if value != "" else (default or "")

    def select(
        self,
        message: str,
        *,
        choices: Sequence[tuple[str, str]],
        default: str | None = None,
    ) -> str:
        value = self._pop("select", message)
        valid = {v for v, _ in choices}
        if value is None:
            return default or next(iter(valid))
        if value not in valid:
            raise AssertionError(
                f"FakePrompter select() got {value!r} but valid choices are {sorted(valid)}"
            )
        return str(value)

    def multi_select(
        self,
        message: str,
        *,
        choices: Sequence[tuple[str, str]],
        default: Sequence[str] | None = None,
    ) -> list[str]:
        value = self._pop("multi_select", message)
        if not isinstance(value, list):
            raise AssertionError(
                f"FakePrompter expected list for multi_select() — got {type(value).__name__}"
            )
        valid = {v for v, _ in choices}
        bad = [v for v in value if v not in valid]
        if bad:
            raise AssertionError(
                f"FakePrompter multi_select() got {bad!r} but valid choices are {sorted(valid)}"
            )
        return list(value)

    def confirm(self, message: str, *, default: bool = False) -> bool:
        value = self._pop("confirm", message)
        if not isinstance(value, bool):
            raise AssertionError(
                f"FakePrompter expected bool for confirm() — got {type(value).__name__}"
            )
        return value
