"""Interactive prompts with a single global auto-yes switch.

``-y/--yes`` on the command line calls ``set_auto_yes(True)``.  It only answers
yes/no questions.  Free-text answers (username, database, dataset) are never
invented: they must come from a flag, the saved config or the keyboard.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_state = {"auto_yes": False}


class PromptError(RuntimeError):
    """Raised when an answer is needed but no input is available."""


def set_auto_yes(value: bool) -> None:
    """Turn the -y behaviour on or off for every later ask_yes_no call."""
    _state["auto_yes"] = bool(value)


def ask_yes_no(question: str, default: bool = True) -> bool:
    """Ask a y/n question.  Enter picks the default; -y always answers yes."""
    if _state["auto_yes"]:
        log.info("%s -> yes (auto, -y)", question)
        return True

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except EOFError:
            raise PromptError(
                f"No input available for: {question!r}. Rerun with -y to answer yes automatically."
            ) from None
        if not answer:
            result = default
        elif answer in ("y", "yes"):
            result = True
        elif answer in ("n", "no"):
            result = False
        else:
            print("Please answer y or n.")
            continue
        log.debug("%s -> %s", question, "yes" if result else "no")
        return result


def ask_choice(question: str, choices: list[str], default: str | None = None) -> str:
    """Ask until the answer is one of choices; a unique beginning such as 'm' is enough."""
    while True:
        answer = ask_text(f"{question} ({'/'.join(choices)})", default=default).lower()
        matches = [c for c in choices if c.startswith(answer)]
        if answer in choices or len(matches) == 1:
            return answer if answer in choices else matches[0]
        print(f"Please answer one of: {', '.join(choices)}.")


def ask_text(question: str, default: str | None = None, flag_hint: str | None = None) -> str:
    """Ask for a non-empty string.  Enter picks the default when there is one."""
    shown = f"{question} [{default}]: " if default else f"{question}: "
    while True:
        try:
            answer = input(shown).strip()
        except EOFError:
            if default:
                log.info("%s -> %s (default, no input available)", question, default)
                return default
            hint = f" Pass it with {flag_hint}." if flag_hint else ""
            raise PromptError(f"No input available for: {question!r}.{hint}") from None
        if answer:
            return answer
        if default:
            return default
        print("A value is required.")
