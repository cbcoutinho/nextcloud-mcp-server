"""Error-formatting helpers for the vector-sync pipeline.

Vector-sync work runs inside anyio task groups, so a failure in a child task
surfaces as a ``BaseExceptionGroup`` whose default ``str()`` is the useless
``"unhandled errors in a TaskGroup (N sub-exception)"`` -- it hides the real
``ConnectError`` / ``APIConnectionError`` that operators need to triage embed
drops (card 309). ``format_exception_group`` flattens the group to the leaf
exceptions so log lines name the actual cause; pair it with ``exc_info=True`` to
keep the full traceback.
"""


def format_exception_group(exc: BaseException) -> str:
    """Return a concise, leaf-naming string for ``exc``.

    For a (possibly nested) ``BaseExceptionGroup`` this joins the ``repr`` of
    each leaf exception; for an ordinary exception it returns its ``repr``. The
    result is meant for the human-readable portion of a log message, not for
    parsing.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return repr(exc)
    leaves = _flatten(exc)
    noun = "sub-exception" if len(leaves) == 1 else "sub-exceptions"
    return f"{len(leaves)} {noun}: " + "; ".join(repr(e) for e in leaves)


def _flatten(exc: BaseException) -> list[BaseException]:
    """Depth-first list of the leaf exceptions within ``exc``."""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_flatten(sub))
        return leaves
    return [exc]
