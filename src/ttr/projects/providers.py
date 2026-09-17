"""Mailbox provider presets - Gmail only, on purpose.

A user should not have to know what IMAP is to create a project. But the only
thing worse than asking them is guessing on their behalf: a wrong host surfaces
much later as a generic authentication failure, and diagnosing that costs an
evening. So this module claims exactly what has been tested and refuses the rest
with the actual reason.

Microsoft personal accounts get their own refusal rather than falling into the
generic one. Microsoft withdrew app-password IMAP for personal Outlook/Hotmail
accounts on 16 September 2024, so those addresses cannot work no matter how the
host is configured - a user who was told only "unsupported provider" would
reasonably try to make it work anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UnsupportedProvider


@dataclass(frozen=True)
class ProviderPreset:
    imap_host: str
    imap_folder: str = "INBOX"
    imap_use_ssl: bool = True


GMAIL = ProviderPreset(imap_host="imap.gmail.com")

_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_MICROSOFT_PERSONAL_DOMAINS = frozenset({
    "outlook.com", "hotmail.com", "live.com", "msn.com",
})

_MICROSOFT_MESSAGE = (
    "Microsoft accounts cannot be used for alert ingestion.\n"
    "\n"
    "Microsoft withdrew app-password IMAP access for personal Outlook, Hotmail,\n"
    "Live and MSN accounts on 16 September 2024. No IMAP host or password will\n"
    "work for {address}, so this is refused at creation rather than surfacing\n"
    "later as an authentication failure you would reasonably try to debug.\n"
    "\n"
    "What to do instead: forward the TrapTracker alerts to a Gmail address and\n"
    "create the project against that."
)

_UNTESTED_MESSAGE = (
    "Unsupported mail provider: {domain}\n"
    "\n"
    "Gmail is the only provider this command configures, because it is the only\n"
    "one tested. Guessing an IMAP host for {domain} would surface later as a\n"
    "generic authentication failure rather than as this message.\n"
    "\n"
    "Two ways forward:\n"
    "  * forward the alerts to a Gmail address and use that; or\n"
    "  * create the project with a Gmail address, then edit [mailbox] in the\n"
    "    project's project.toml to your provider's IMAP host and folder.\n"
    "    Nothing downstream is Gmail-specific - only this preset is."
)


def split_domain(address: str) -> str:
    """The domain of an email address, lowercased."""
    address = (address or "").strip()
    if address.count("@") != 1:
        raise UnsupportedProvider(
            f"{address!r} is not an email address. Give the address TrapTracker "
            f"forwards its detection alerts to.")
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain:
        raise UnsupportedProvider(
            f"{address!r} is not an email address. Give the address TrapTracker "
            f"forwards its detection alerts to.")
    return domain.lower()


def preset_for(address: str) -> ProviderPreset:
    """The IMAP preset for an address, or a refusal naming the real reason."""
    domain = split_domain(address)
    if domain in _GMAIL_DOMAINS:
        return GMAIL
    if domain in _MICROSOFT_PERSONAL_DOMAINS:
        raise UnsupportedProvider(_MICROSOFT_MESSAGE.format(address=address))
    raise UnsupportedProvider(_UNTESTED_MESSAGE.format(domain=domain))
