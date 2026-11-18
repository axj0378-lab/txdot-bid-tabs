# Cost indices

One CSV per index, loaded with `scripts/load_cost_index.py` into `cost_indices`.

```
date,value
2024-01-01,100.0
2024-04-01,103.2
```

- `date`: `YYYY-MM-DD`, or `YYYY-MM` (first day of the month): the period the value applies to.
- `value`: index value, > 0.

Which index to use (TxDOT HCI, FHWA NHCCI, ENR CCI, ...) is still DEFERRED (SPEC.md decisions
log), so no index is loaded and prices stay nominal. Loading an index does not activate it;
`--activate` sets `cost_settings.active_index_name`.
