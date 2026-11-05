from __future__ import annotations

from dichotomise.utils import console as console_module


def test_status_is_usable_as_a_context_manager() -> None:
    console_module.console = console_module.Console(force_terminal=False, no_color=True, width=200)
    with console_module.status("Working..."):
        pass
