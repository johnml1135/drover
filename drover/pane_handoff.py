"""A director hands off its session before it fills. The hook is here; Task 12 fills it."""


def maintain(service, run) -> None:
    """Called for a live run of the current generation whose pane shows the recorded agent."""
