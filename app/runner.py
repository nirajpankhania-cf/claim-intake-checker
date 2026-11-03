"""Rule runner.

Runs every rule in ``app.rules`` over a batch of claims and writes the
resulting ``check_runs`` and ``flags`` rows. Flag history is append-only.
"""
