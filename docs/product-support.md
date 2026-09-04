# Product support contract

The machine-readable authority is
`planning/product-support-contract.json`. The installed package carries an identical,
read-only copy which can be inspected with:

```text
nfi-bte contract support
nfi-bte contract support --json
```

## Current support boundary

- Native execution supports capability-compatible `NostalgiaForInfinityX7` sources.
- `NostalgiaForInfinityNext` and `NostalgiaForInfinityNextGen` are legacy V8/V9
  sources. They are hidden from normal selection and are not Native-certified.
- Spot and isolated USDT-M Futures use official Freqtrade as the zero-tolerance
  authority.
- Linux x86_64/aarch64, macOS arm64, and Windows through WSL2 are supported. Native
  Windows is not supported.
- GitHub Releases is the current stable distribution. PyPI trusted publishing is a
  planned M28 gate and must not be advertised as available before it passes.
- Historical Spot and Futures certificates remain independently version-bound. The
  same-candidate combined status remains `preview` until M29 certification and the
  M30 operating and clean-room gates pass.

Unknown active semantics fail before Native simulation. A strategy name, source hash,
pair, timerange, or expected result never selects runtime behavior.
