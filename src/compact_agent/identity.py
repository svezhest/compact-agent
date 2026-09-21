"""Data-layer identity: deterministic pseudonymous keys from transport metadata.

Backlog invariants (B/C):
  - The canonical key for a person is a deterministic ``hash(sender_id)``, assigned
    HERE in code — never inferred by the model from names or handles. One key per
    ``sender_id``; dedup is structural, not an inference.
  - The model never sees real display names. Rendering replaces every sender label
    (and every in-text occurrence of a known display name) with the sender's
    ``@hash`` before the text leaves this process.
  - The ``hash -> identity`` vault is written OUTSIDE the ``.md`` artifact, so a
    human can still read "who is who" while the compaction itself stays de-identified.

Pseudonymization is NOT anonymization: ``@hash`` plus a rich World profile is
re-identifiable. That is a documented limitation, not a guarantee.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .storage import Message

# Length of the hex digest kept in a label. 8 hex chars = 32 bits: collision-safe
# for any realistic single-chat roster while staying short and readable.
_DIGEST_LEN = 8


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def _hash_key(seed: str) -> str:
    """Stable, deterministic pseudonymous label for a transport-identity seed."""
    return f"@u{_digest(seed)}"


def _mention_key(handle: str) -> str:
    """Pseudonym for a @handle that did NOT resolve to a real platform user.

    Mentions of REAL users are folded onto that person's ``@u`` key by ``user_id`` (so a
    tagged-but-silent user shares one uniform key with their speaker identity). Only handles
    that resolve to nothing real (dead/private handles, channel @names) fall here; they are
    still pseudonymized for privacy but use the SAME ``@u`` prefix — there is no separate
    ``@m`` class anymore — and stay out of the routable roster (not a real person)."""
    return f"@u{_digest(f'mention:{handle.lower()}')}"


# Any @handle mention in body text (Telegram usernames, name-handles). These are
# real identifiers and MUST be pseudonymized before the model sees them — the @me /
# @all / @everyone group tokens are expanded separately and skipped here.
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{3,32})")
_MENTION_SPECIAL = {"me", "all", "everyone"}


@dataclass
class Identity:
    """One pseudonymous participant: a stable ``@hash`` key plus the real-world
    names seen for it (kept only in the off-artifact vault)."""

    key: str
    sender_id: int | None
    names: list[str] = field(default_factory=list)
    grounded: bool = True  # False when we had no sender_id and fell back to name
    usernames: list[str] = field(default_factory=list)  # @usernames seen for this speaker


@dataclass
class IdentityMap:
    """``sender_id -> Identity`` plus the name-scrub table, built once per run."""

    by_id: dict[str, Identity] = field(default_factory=dict)
    # Numeric ``sender_id`` (as str) -> @hash, scrubbed from body text. Display names
    # are deliberately NOT in this table (see `_compile_scrub`).
    _name_to_key: dict[str, str] = field(default_factory=dict)
    _scrub_re: re.Pattern | None = None
    _readable: dict[str, str] | None = None  # @hash -> readable @handle label
    # Pseudonym key -> the real @handle it stands for. Mentions of usernames that
    # are NOT tracked speakers; kept ONLY for the off-artifact vault / .named view.
    _mentions: dict[str, str] = field(default_factory=dict)
    # Lowercased @username (no leading @) -> the speaker @hash that owns it, so a
    # mention of a tracked speaker's own handle resolves to their @u key, not a @m.
    _username_to_key: dict[str, str] = field(default_factory=dict)
    ungrounded: int = 0  # senders with no stable id (name-based fallback)

    def label_for(self, sender_id: int | None, sender_name: str | None) -> str:
        ident = self._resolve(sender_id, sender_name)
        return ident.key

    def _resolve(
        self,
        sender_id: int | None,
        sender_name: str | None,
        sender_username: str | None = None,
    ) -> Identity:
        if sender_id is not None:
            sid = str(sender_id)
            ident = self.by_id.get(sid)
            if ident is None:
                ident = self.by_id[sid] = Identity(key=_hash_key(f"id:{sid}"), sender_id=sender_id)
            grounded = True
        else:
            # No stable id (copied text / HTML export): degrade to a name-keyed
            # hash and flag it, rather than silently inventing identity.
            name = (sender_name or "unknown").strip()
            sid = f"name:{name.lower()}"
            ident = self.by_id.get(sid)
            if ident is None:
                ident = self.by_id[sid] = Identity(
                    key=_hash_key(sid), sender_id=None, grounded=False
                )
                self.ungrounded += 1
            grounded = False
        if sender_name and sender_name not in ident.names:
            ident.names.append(sender_name)
        if sender_username:
            handle = sender_username.lstrip("@")
            if handle and handle not in ident.usernames:
                ident.usernames.append(handle)
            self._username_to_key[handle.lower()] = ident.key
        return ident

    def _compile_scrub(self) -> None:
        """Build the replacement table (raw numeric ``sender_id`` -> @hash).

        ONLY the literal numeric sender_id is scrubbed out of body text (e.g. a message
        quoting ``from_id`` or a ``tg://user?id=`` deep link). Display names and @usernames
        are deliberately NOT scrubbed from body text: real display names are short, common
        substrings (single emoji, "hi", "me", two-letter handles) that match inside ordinary
        words and shred the text — and names in message CONTENT are by-design allowed through.
        Speaker identity is still pseudonymized at the label (``@hash:`` prefix), and @handle
        mentions go through ``pseudonymize_mentions``; this method only handles bare ids."""
        table: dict[str, str] = {}
        for ident in self.by_id.values():
            if ident.sender_id is not None:
                table[str(ident.sender_id)] = ident.key
        self._name_to_key = table
        if table:
            # ids get \b boundaries so they don't match inside larger numbers.
            id_alts = [rf"\b{re.escape(s)}\b" for s in sorted(table, key=len, reverse=True)]
            self._scrub_re = re.compile("|".join(id_alts))
        else:
            self._scrub_re = None

    def scrub(self, text: str) -> str:
        """Replace any literal numeric ``sender_id`` in body text with its @hash.

        Display names / @usernames are intentionally left intact (see ``_compile_scrub``):
        names appearing in message content flow through by design, and short names would
        otherwise corrupt ordinary words."""
        if not text:
            return text
        if self._scrub_re is None:
            self._compile_scrub()
        if self._scrub_re is None:
            return text
        return self._scrub_re.sub(lambda m: self._name_to_key[m.group(0)], text)

    def pseudonymize_mentions(self, text: str) -> str:
        """Replace every @handle mention with a deterministic pseudonym.

        Telegram @usernames (and any @name-handle) are real identifiers that must
        never reach the model. A handle that belongs to a TRACKED SPEAKER resolves to
        that speaker's ``@u<hash>`` (so the model never sees the same person as both a
        speaker and a separate mention pseudonym); any other handle maps to a stable
        ``@u<hash>`` derived from the handle itself (see `_mention_key` — there is no
        separate ``@m`` class). The real handle is recorded only
        in the off-artifact vault. The group tokens @me / @all / @everyone are left
        for ``expand_mentions`` to resolve."""
        if not text:
            return text

        def repl(m: re.Match) -> str:
            handle = m.group(1)
            if handle.lower() in _MENTION_SPECIAL:
                return m.group(0)
            # A mention of a known person's username (a speaker OR a real user resolved at
            # fetch time) collapses onto that person's @u key — one uniform key per person.
            known = self._username_to_key.get(handle.lower())
            if known is not None:
                return known
            # Not a real user: still pseudonymize the handle (privacy), uniform @u prefix,
            # but it stays out of the routable roster — not a person to profile.
            key = _mention_key(handle)
            self._mentions.setdefault(key, handle)
            return key

        return _MENTION_RE.sub(repl, text)

    def sanitize(self, text: str, sender_key: str | None = None) -> str:
        """THE one privacy pipeline for any model-facing text (DRY — both past privacy
        regressions were a call site that forgot one of these steps).

        Order matters: pseudonymize raw @handles FIRST (so a known username becomes its
        @u key before the scrub regex could half-match it), then scrub display names /
        raw sender_ids, then (only for a message body, where the speaker is known)
        expand the @me/@all group tokens."""
        if not text:
            return text
        out = self.scrub(self.pseudonymize_mentions(text))
        if sender_key is not None:
            out = self.expand_mentions(out, sender_key)
        return out

    def keys(self) -> set[str]:
        """Every valid @hash person key (the only keys the model may route to)."""
        return {ident.key for ident in self.by_id.values()}

    def readable_labels(self) -> dict[str, str]:
        """``@hash -> readable @handle`` for the HUMAN-facing view only.

        Derived from the display name (slugified to a handle), made unique by
        appending part of the hash on collision. Never fed to the model — the
        model only ever sees the @hash."""
        if self._readable is not None:
            return self._readable
        out: dict[str, str] = {}
        seen: set[str] = set()
        for ident in sorted(self.by_id.values(), key=lambda i: i.key):
            name = ident.names[0] if ident.names else ""
            slug = re.sub(r"\W+", "", name.strip().lower(), flags=re.UNICODE)
            base = f"@{slug}" if slug else ident.key
            label = base
            if label in seen:  # two people slug to the same handle: disambiguate
                label = f"{base}_{ident.key[2:6]}"
            seen.add(label)
            out[ident.key] = label
        self._readable = out
        return out

    def humanize(self, text: str) -> str:
        """Swap every @hash in an assembled report for its readable @handle.

        For the human-facing rendering ONLY; the canonical artifact stays keyed by
        @hash. Unknown hashes (shouldn't occur) are left untouched."""
        labels = dict(self.readable_labels())
        # Mention pseudonyms map back to their real @handle for the human view.
        for key, handle in self._mentions.items():
            labels.setdefault(key, f"@{handle}")
        if not labels:
            return text
        alts = sorted((re.escape(k) for k in labels), key=len, reverse=True)
        return re.compile("|".join(alts)).sub(lambda m: labels[m.group(0)], text)

    def expand_mentions(self, text: str, sender_key: str) -> str:
        """Resolve the special group-mention tokens to @hash references.

        ``@me`` -> the speaker's own @hash; ``@all`` / ``@everyone`` -> every
        participant's @hash, space-separated (so the model can see exactly who is
        addressed, including the ones who never speak). Done per-message (``@me``
        depends on who is speaking). Word-bounded and not preceded by another @ so
        it never fires inside an already-rendered @hash label."""
        if not text:
            return text
        all_label = " ".join(sorted(self.keys()))
        text = re.sub(r"(?i)(?<![\w@])@(?:all|everyone)\b", all_label, text)
        text = re.sub(r"(?i)(?<![\w@])@me\b", sender_key, text)
        return text

    def vault(self) -> dict:
        """The off-artifact ``hash -> identity`` map for human lookup."""
        labels = self.readable_labels()
        out = {
            ident.key: {
                "label": labels.get(ident.key, ident.key),
                "sender_id": ident.sender_id,
                "names": ident.names,
                "usernames": ident.usernames,
                "grounded": ident.grounded,
            }
            for ident in sorted(self.by_id.values(), key=lambda i: i.key)
        }
        # Mention pseudonyms (handles seen in text, not tracked speakers).
        for key, handle in sorted(self._mentions.items()):
            out.setdefault(key, {
                "label": f"@{handle}",
                "sender_id": None,
                "names": [f"@{handle}"],
                "grounded": False,
                "mention_only": True,
            })
        return out


def build_identity(messages: list[Message]) -> IdentityMap:
    """Assign one deterministic @hash per sender_id over the whole message log.

    Done eagerly and up front so the scrub table is complete before any text is
    rendered (a name first seen late in the chat still gets scrubbed early)."""
    idmap = IdentityMap()
    for m in messages:
        idmap._resolve(m.sender_id, m.sender_name, getattr(m, "sender_username", None))
        # Mentions resolved to a REAL user at fetch time become roster members too, keyed by
        # user_id — so a tagged person who never speaks gets one uniform @u key (folded with
        # their speaker identity if they ever do speak) and can be written to.
        for mn in getattr(m, "mentions", None) or []:
            if mn.get("real") and mn.get("user_id") is not None:
                idmap._resolve(mn["user_id"], mn.get("name"), mn.get("handle"))
    idmap._compile_scrub()
    return idmap


def write_vault(path: Path, idmap: IdentityMap) -> None:
    path.write_text(json.dumps(idmap.vault(), ensure_ascii=False, indent=2), encoding="utf-8")
