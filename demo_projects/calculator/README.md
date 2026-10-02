# calculator (demo target)

Controlled project used by the SEM7 self-healing pipeline. Scenarios copy this
into a sandbox git repository, introduce a bug on a feature branch, and let the
pipeline detect, diagnose, and repair it.

Run tests: `pytest`

## Conventions
- `divide` must use true division and raise `ValueError` on a zero divisor.
- Public functions must keep their docstrings accurate.
