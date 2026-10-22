# The crewmate Profile card's first tab is named Workers, and it opens on Workers

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-22
Supersedes: 2026-10-08-crewmate-profile-card-tabs-are-sessions-schedules-goals-profile.md

## Decision

The crewmate Profile card's tab strip reads Workers, Schedules, Goals, Profile, in that order, and the card opens on Workers unless the opener names a tab. Only the first tab's name changes from the superseded entry: the order, the other three names and the default tab are unchanged. The tab's key stays `sessions` and its catalog key stays `profile_sessions_tab`; the decision is about the word a user reads, not the identifiers code reads.

Everything a user meets on that tab uses the same word. The empty state says what a worker is in plain words, and the quiet-chat link that lands on the tab says "workers" too. Neither the tab nor anything inside it says "session", "chat" or "driving" to the user.

## Why

- Reviewing the Profile card on 2026-10-22, the maintainer found the tab's empty state ("Not driving any sessions right now.") unreadable to a first-time user: someone new to Kiro Crew has no model for why a crewmate would drive sessions at all, and the tab's own name, Sessions, carried the same internal word. He asked for the empty state to explain the mechanism and for the tab to be named for what a user would call the thing.
- He picked the word himself. The first draft of the fix used "helpers"; he replaced it: "call it workers". "Workers" is already this card's own word for the sessions a crewmate opens (`profile_worker_same_hint`).
- The superseded entry settled the ORDER of the tabs and the default tab. Both stand; only the first tab's label changes.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23003 -- the report, filed on the maintainer's behalf the same day.
- https://github.com/kirodotdev/KiroCrew/pull/23007 -- the change that applies it (`profile_sessions_tab` in every catalog; the empty state in `MembersPageProfile.tsx`).
- https://github.com/kirodotdev/KiroCrew/pull/23021 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.
