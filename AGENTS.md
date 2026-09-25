# Working on technocore-close-call

The rules and the canonical fold live in `close-call-game.md`. Edit the fold's
code block there, then run `python3 scripts/build.py` to regenerate
`close_call_fold.py` and `manifest.json`. Do not edit generated files directly.

Validate changes with:

```sh
python3 scripts/build.py --check
python3 scripts/verify.py
python3 -m unittest discover -s tests -v
```

The expected output in `examples/` is a test vector. If a rule change moves it,
regenerate it with the fold, check the difference by hand, and say so in the
change. A rule change during a contest invalidates its frozen package: make it
for a later contest and publish a new package version.

The package is a draft. Publishing repository changes, posting messages, creating
live rooms and sending payments require explicit user authorization. Keep
credentials, signing seeds, referee keys and participant data out of this repo.

Keep operator implementation, monitoring and the referee service in a separate
repository. `sim/close_call_sim.py` is design evidence, not part of the referee;
it needs NumPy and is excluded from the package manifest.
