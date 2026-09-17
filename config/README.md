# `config/` — your deployment's local configuration

This directory is where **your** configuration lives. It is deliberately near-empty
in the published repository: everything that belongs here is either specific to
one deployment or is third-party material that is not redistributed.

## `species_aliases.yaml` (not committed)

The alias table mapping your detector's raw class tokens to scientific binomials
and queryable common names. `SPECIES_ALIAS_PATH` points here by default, and the
file is gitignored because it describes your model, not this project's.

You do not have to create it to get started. When it is absent the package falls
back to an illustrative table shipped as package data
(`src/ttr/species/data/species_aliases.example.yaml`), and says so on stderr.
Copy that file here and edit it to match your own classes:

```bash
python -c "from ttr.species.aliases import bundled_example_alias_text as t; print(t())" \
    > config/species_aliases.yaml
```

The format, and the reasoning behind the canonical-key scheme, are documented in
the example file's own header.

## What is not here

The class list of the TrapTracker RT deployment this project was built against,
and the alias table derived from it, are **not** in this repository. Their
redistribution terms have not been confirmed.

That is a provenance decision, not a technical limitation: nothing in the code
depends on that particular list. Point `SPECIES_ALIAS_PATH` at a table describing
whatever model you run.
