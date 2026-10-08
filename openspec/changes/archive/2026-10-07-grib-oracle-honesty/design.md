# Design

- **Why the validate-met tests leave the grib lane instead of getting GRIB fixtures.** The deterministic
  fixture is written by production-closure code (`_deterministic_raw_content`), which also runs on hosts
  without ecCodes. Making it GRIB would add an ecCodes dependency to the tool. The tests never needed ecCodes
  (masking cfgrib/eccodes changes nothing), so the honest marker is none.
- **Why encode at test time.** A checked-in GRIB sample pins one ecCodes version's tables; the lane's stated
  purpose is version-matched decode. Encoding with the runtime's ecCodes makes the match automatic and keeps
  binary files out of the repository. Cost: fidelity of accumulated-product templates, which the converter
  does not read (it filters by shortName).
- **One rule, independent inputs.** The validate-met continuity check failed because it re-derived the row
  times by a rule that differs from the producer's. It now calls the producer's interval rule, but with the
  configured forecast hours rather than the products the producer consumed: feeding it the same products
  would make the check a tautology that passes when a canonical product is missing.
- **Producer guard is fail-closed, not a behaviour change.** A non-increasing interval end has no valid row
  time; today it yields duplicate rows that the database would reject later, further from the cause.
- **Proving cfgrib was used.** The converter has no positive engine indicator; it logs the fallback at
  WARNING. The tests assert no such record, and the evidence includes the counter-proof (forcing the cfgrib
  open to raise makes the assertion fail).
- **node-27 ecCodes was probed before merge.** Encoding had never run on that runtime (production only
  decodes); the probe showed samples resolve without `ECCODES_SAMPLES_PATH` and the shortName concepts hold
  on 2.47.0.
