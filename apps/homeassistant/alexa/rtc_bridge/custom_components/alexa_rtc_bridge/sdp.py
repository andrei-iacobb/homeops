"""Complete Alexa SDP answers without trickle ICE.

Adapted from Home Assistant Core (Apache-2.0), commit
5588f94efe6bde02a4b7cecffcab23fb085c8d55, alexa/handlers.py.
https://github.com/home-assistant/core/pull/182881
"""

import ipaddress

from webrtc_models import RTCIceCandidateInit


def is_ipv4_candidate(candidate: str) -> bool:
    """Alexa accepts IPv4 ICE candidates only."""
    try:
        return ipaddress.ip_address(candidate.split()[4]).version == 4
    except (IndexError, ValueError):
        return False


def embed_candidates(sdp: str, candidates: list[RTCIceCandidateInit]) -> str:
    """Put candidates in their actual media sections, retaining existing IPv4."""
    lines = sdp.replace("\r\n", "\n").rstrip("\n").split("\n")
    media_starts = [i for i, line in enumerate(lines) if line.startswith("m=")]
    if not media_starts:
        return sdp
    mids: dict[str, int] = {}
    for section, start in enumerate(media_starts):
        end = media_starts[section + 1] if section + 1 < len(media_starts) else None
        for line in lines[start:end]:
            if line.startswith("a=mid:"):
                mids[line.removeprefix("a=mid:")] = section
    extra: dict[int, list[str]] = {}
    for candidate in candidates:
        if not is_ipv4_candidate(candidate.candidate):
            continue
        if candidate.sdp_mid is not None and candidate.sdp_mid in mids:
            section = mids[candidate.sdp_mid]
        elif (
            candidate.sdp_m_line_index is not None
            and 0 <= candidate.sdp_m_line_index < len(media_starts)
        ):
            section = candidate.sdp_m_line_index
        else:
            continue
        extra.setdefault(section, []).append(f"a={candidate.candidate}")
    # Session-level IPv4 candidates are retained too, but IPv6 is removed everywhere.
    result = [
        line for line in lines[:media_starts[0]]
        if not line.startswith("a=candidate:")
        or is_ipv4_candidate(line.removeprefix("a="))
    ]
    for section, start in enumerate(media_starts):
        end = media_starts[section + 1] if section + 1 < len(media_starts) else None
        section_lines = [
            line for line in lines[start:end]
            if line != "a=end-of-candidates"
            and (
                not line.startswith("a=candidate:")
                or is_ipv4_candidate(line.removeprefix("a="))
            )
        ]
        result.extend(dict.fromkeys([*section_lines, *extra.get(section, [])]))
        result.append("a=end-of-candidates")
    return "\r\n".join(result) + "\r\n"
