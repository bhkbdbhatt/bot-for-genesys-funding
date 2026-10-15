"""Substack publisher - manual submission.

Substack publishes no public writing API: posts are composed in the web editor
(or sent by email to a secret address), so the honest integration is the same
hand-off CoderLegion and DevDojo use. The publisher shapes the article to
Substack's loose limits, writes ``content_outbox/substack/<id>.md`` and prints
the submission steps; the ledger stays ``queued`` until the live URL is
confirmed.

Substack tags are plain strings on the post form, so the suggested-tags header
is the useful part of the hand-off.
"""

from __future__ import annotations

from publishers.manual import ManualPublisher

DEFAULT_SUBMIT_URL = "https://substack.com/publish"


class SubstackPublisher(ManualPublisher):
    platform = "substack"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run)
        if not self.submit_url:
            self.submit_url = DEFAULT_SUBMIT_URL


def build(config, *, dry_run: bool = False) -> SubstackPublisher:
    """Entry point used by the publisher factory."""
    return SubstackPublisher(config, dry_run=dry_run)
