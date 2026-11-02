# Rearchitecture survey: live admin directories and row spellings

Task 2, slice 0. Taken 2026-11-02 at base 75a5717. Read-only and serial. No cleanup ran. `.drover` was not read, and `drover.deploy` was not entered, so the repo list is the directories under `/mnt/h/repos`.

WSL git 2.53.0, Windows git 2.51.0.windows.1. Counts only: no worktree contents. `<admin>` stands for `<common>/worktrees/<id>`.

## Result

- **O3 holds; it does not return to the session lead.** WSL git treats `H:/...` as a relative path and spells the row under the admin directory. Every such row stops at a side check, as section 16 says.
- **Ruling 9 would refuse 0 repos.** No admin directory has an empty or unreadable pointer.
- **Bad `commondir`: 0. Repos using reftable: 0.**
- **`parse`'s `gitdir` rule matched every linked row**, 321 of 321 in the four repos listed, on both sides.
- **One new input for Task 9:** one place holds two rows (section 4).

## 1. Admin directories

45 directories under `/mnt/h/repos`: 29 with a `.git` directory, 15 with none, and `drover.deploy`, which has a `.git` file and was skipped. No bare repos. 20 of the 29 have no `worktrees/` directory. The script took about 10 s.

| Measure | Count |
|---|---|
| Repos with admin directories | 9 |
| Admin directories | 420 |
| Pointer empty, unreadable, undecodable or missing | 0 |
| Repos ruling 9 would refuse | 0 |
| `commondir` missing, unreadable or not resolving to the common directory | 0 |
| `commondir` other than `../..` | 0 |
| `<common>/reftable` present | 0 |
| Stray files under `worktrees/` | 0 |
| `locked` present, with a reason | 371 |
| `locked` present, empty | 45 |
| `locked` absent | 4 |
| Pointers with CR, non-ASCII bytes or a backslash | 0 |
| Pointers without a trailing `/.git` | 0 |
| Places named by two admin directories | 1 |

| Repo | Admin dirs | Drive-letter | `/mnt` | Other absolute (ext4) | Relative |
|---|---:|---:|---:|---:|---:|
| FieldWorks | 3 | 1 | 1 | 1 | 0 |
| PanGloss | 52 | 17 | 0 | 35 | 0 |
| drover | 46 | 9 | 2 | 0 | 35 |
| google-photos-shrink | 3 | 3 | 0 | 0 | 0 |
| linguistic-assistant | 3 | 0 | 0 | 3 | 0 |
| machine | 83 | 1 | 30 | 52 | 0 |
| motif | 220 | 5 | 0 | 214 | 1 |
| my-skills | 9 | 0 | 1 | 8 | 0 |
| vlc-dvd-streamer | 1 | 1 | 0 | 0 | 0 |
| **Total** | **420** | **37** | **34** | **313** | **36** |

The 4 with no `locked` are all drive-letter: google-photos-shrink 3, vlc-dvd-streamer 1. The 45 with an empty `locked` read as no side marker, so `worktree_side` falls back to the path.

## 2. Row spellings against the pointer

`git worktree list --porcelain -z`, run one at a time, on each side. Exit 0 and no stderr on all eight.

| Repo | Rows | WSL s | Windows s |
|---|---:|---:|---:|
| drover | 47 | 6.1 | 0.1 |
| PanGloss | 53 | 4.3 | 0.1 |
| motif | 221 | 11.2 | 0.2 |
| google-photos-shrink | 4 | 0.6 | 0.1 |

The 321 linked rows, by pointer class:

| Pointer | Rows | WSL row | Windows row |
|---|---:|---|---|
| Drive-letter, `H:/x/.git` | 34 | `<admin>/H:/x` | `H:/x` |
| `/mnt/h/x/.git` | 2 | `/mnt/h/x` | `/mnt/h/x`, not translated |
| Other absolute, `/home/...` | 249 | The pointer | The pointer |
| Relative | 36 | Resolved from the admin directory | Resolved from the admin directory; 4 become `H:/home/...` |

Against section 5.3's rule (strip the line ending and one `/.git`; resolve a relative pointer lexically from the admin directory, a drive-letter pointer counting as relative on WSL):

- Every linked row equals the parsed pointer literally, 321 of 321 on each side, with the admin directory spelled as that side spells it, `/mnt/h/repos` or `H:/repos`.
- No row lacks an admin directory, and no admin directory lacks a row.
- Across sides, all 325 rows join by true place, and `HEAD`, `branch`, `detached` and `locked` agree on every one.
- **Row order differs by side** in drover, PanGloss and motif. Nothing may join by position.
- `prunable` appears only on WSL, on the 3 unlocked google-photos-shrink rows. Locked rows never show it, so a locked drive-letter row looks healthy on WSL.

## 3. O3: what WSL git does with `H:/...` pointers

It mis-resolves them. The pointer has no leading `/`, so git treats it as relative, as section 3's strace suggested: 34 of 34 rows, in `<admin>/H:/repos/...` form. Such a row is a path that does not exist. Git still reads `HEAD`, branch and `locked` from the admin directory, so those fields are right.

**Selection, as section 16 states.** The row begins `/mnt/h/`, so:

- `worktree_side` gives `None` with no lock or a note, and `windows` with a `windows` lock. Either differs from `wsl`: `other_side`. Measured: 34 of 34, being 28 `windows` locks, 3 notes and 3 with no lock.
- A `wsl` lock would pass that check, but `cleanup_path_side` gives `windows`, so `cleanup_contains` is false: `other_drive`. This holds whatever the lock, so it also covers the 3 drive-letter pointers in repos not listed (FieldWorks, machine, vlc-dvd-streamer).
- `decide` turns both into `keep` (disk_cleanup_decisions.py:38-39). `disk_paths` rejects at `not_directory`, then `owner_side` or `physical_drive`.
- The inventory and `unlisted` paths skip on `cleanup_contains`.
- `lost` either fails its common-directory test or finds the admin pointer names the path, by `pointer_at`, which translates the drive letter. Both mean no repair.

**Nesting.** `overlaps_worktrees` on WSL does miss the true place of these rows, as section 3 suspected, but no live target can reach it. A `/mnt` target stops at `drive_unproven` first (disk_paths.py:384), and an ext4 target cannot sit inside a drive path. Windows git spells these rows correctly. So `Proof.reach` is safe and only keeps more; slice 1 should not expect a changed trace row from it on live shapes.

## 4. For Task 9 and later

1. **Two admin directories, one place.** In PanGloss, two admin directories point at the same ext4 worktree. Git lists two rows at one path, one on a branch and one detached at the same commit, and their order differs by side. A map keyed by path keeps one and compares the target against the wrong twin. That refuses, which is the safe side, but it is not what a row comparison means, and it would be a false `identity_changed`. Today's `next(...)` at disk_paths.py:290 and disk_worktrees.py:312 picks the first listed.
2. **The mirror of O3 on Windows.** Four drover relative pointers reach ext4 from a WSL admin spelling. Windows resolves them against `H:/`, giving `H:/home/...`, a path that does not exist. All 4 are locked `wsl`, so they stop at `other_side`. Section 16 does not name this case, and `reach` gets no true place for it; with no lock the row would read as a Windows path. It is outside O3 and changes nothing today.
3. **Unlocked drive-letter rows.** The 4 above are `prunable` from WSL. Whether `protect` covers them depends on the configured repos, which this survey could not read.

## Method

Helper scripts are kept out of the repo, in the task workspace: `survey_admin.py`, `survey_rows.py`, `survey_compare.py`, `survey_agree.py`. Serial reads of `gitdir`, `locked`, `commondir` only, plus the one `worktree list` per repo per side. Pointer targets were not stat'ed in the count. The row comparison took `cleanup_parts` and the side functions from `drover/placement_paths.py` at this commit.
