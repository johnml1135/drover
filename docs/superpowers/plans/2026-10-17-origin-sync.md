# T-2 origin sync execution record

The task description is the owner-approved design. Work stays on the assigned task branch; Drover owns committing and gating.

- Add validated repo `[landing]` defaults, retain legacy repo keys, and reject reserved PR mode.
- Fetch and synchronize main before task branching and immediately before local landing, using ref updates only.
- Pause only the affected repo on fetch failure; refuse divergent histories with needs-owner.
- Replay and gate again when main moves, with replay conflicts going to needs-owner.
- Keep push off by default; optional push follows landing and failure alerts without reverting landed tasks.
- Show an owner-authenticated POST Push main action when local main is ahead.

Interpretation: ahead-only main is preserved, because successive unpushed local landings must build on each other. Divergence means commits unique to both local and remote history. The owner's checkout files and index remain untouched even when the main ref advances.

Review findings and fixes:

- Keep later tasks ready while their repo is offline, so acknowledging the original retry alert resumes all work.
- Limit unnamed landing calls to tasks whose recorded checkout belongs to the supplied git repo.
- Explicitly map the fetched main tracking ref, including restricted remote fetch configurations; refresh it after a successful push without overwriting a concurrent fetch.
- Cover replay conflicts, remote and owner main movement during the gate, and fetch failure immediately before landing.
- Preserve canonical landing push settings when the setup wizard updates an existing repo.

Verification uses temporary bare origins and fake gates only. The normal WSL suite cannot use this sandbox's temporary directory: it is on DrvFs and the production ledger guard rejects it. A scratch-only test adapter exempts only sandbox temporary database paths from that guard, including in child interpreters. It is not shipped and does not replace the normal Drover gate. Hub dependencies and Windows Python are unavailable here.

Verification results:

- Unmodified baseline: `python3 -m unittest discover -s tests -t .` ran 828 tests, with 401 errors from the sandbox DrvFs ledger guard and 13 skips.
- Supplementary full run with the scratch-only adapter: the same discovery command ran 848 tests in 1009.742 seconds, OK with 14 skips. This is not a production gate receipt.
- Targeted landing, pipeline safety, origin and config run: 52 tests passed with the adapter.
- Review regression run: five tests passed, including cross-repo divergence isolation, offline retry, replay conflict, pre-landing fetch failure and consecutive local landings.
- Final origin run: seven tests passed, including restricted fetch mappings, tracking refresh after push, and configured remote/main.
- Additional owner-main race and offline cross-repo dispatch checks each passed; four repo config/setup tests passed.
- Changed Python files compiled and `git diff --check` passed.

Hub HTTP/auth tests are present but need the existing hub environment; Windows Python and the unmodified WSL gate remain for Drover's gate. No dependency was added. Changes remain uncommitted for Drover; no exact-SHA gate receipt is claimed.
