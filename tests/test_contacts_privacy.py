"""Contacts privacy: a recruiter @handle is pseudonymized before the model sees it, and the
real handle is recovered only in the human-facing .named view (via identity.humanize)."""

from __future__ import annotations

import re

from compact_agent.identity import build_identity
import contacts  # example plugin (see conftest)
from compact_agent.sections.base import ViewContext
from compact_agent.sections.store import VStore
from compact_agent.storage import Message


def _post(text):
    return Message(id=1, date="2026-02-20T11:33:00Z", sender_id=900, sender_name="ФКН",
                   text=text, post_id=1)


def test_recruiter_handle_pseudonymized_then_recovered():
    text = "Продуктовый аналитик, Альфа-Банк. Присылать резюме @Vladimir_Lozovoy"
    idmap = build_identity([_post(text)])

    seen = idmap.sanitize(text)  # what the model is shown
    assert "@Vladimir_Lozovoy" not in seen
    assert "Vladimir_Lozovoy" not in seen          # pseudonymized whole, not half-scrubbed
    assert re.search(r"@u[0-9a-f]+", seen)          # replaced with a @u token

    assert "@Vladimir_Lozovoy" in idmap.humanize(seen)  # human view recovers it


def test_email_and_url_contacts_flow_through_verbatim():
    text = "CV: anna@sfdstrategy.com или https://jobs.ashbyhq.com/recraft/abc"
    idmap = build_identity([_post(text)])
    seen = idmap.sanitize(text)
    assert "anna@sfdstrategy.com" in seen           # email local part blocks the mention regex
    assert "https://jobs.ashbyhq.com/recraft/abc" in seen


def test_contact_card_handle_humanizes_in_named_view():
    text = "резюме @Vladimir_Lozovoy"
    idmap = build_identity([_post(text)])
    tok = idmap.pseudonymize_mentions(text).split()[-1]  # the @u token the model would see
    assert tok.startswith("@u")

    st = VStore(sections=[contacts.section()], known_keys=idmap.keys())
    st.patch(f"contacts/{tok}",
             {"handle": tok, "kind": "tg", "role": "recruiter",
              "companies": ["alfa-bank"], "offers": ["1-analyst"]})

    view = contacts.section().render_view(
        st, ViewContext(relative_date="2026-02-20", kind="channel", platform="telegram"), 1000)
    assert tok in view                       # the de-identified artifact carries the pseudonym
    assert "@Vladimir_Lozovoy" not in view
    assert "@Vladimir_Lozovoy" in idmap.humanize(view)  # the .named view is actionable
