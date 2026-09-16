from __future__ import annotations

from dataclasses import replace
import json
import unittest

from app.services.citation_service import parse_chat_result
from app.ui.citation_widgets import verified_citation_groups


ALIASES = {"P1": 12, "P2": 27}
EVIDENCE = [
    "distinctive evidence from the first paper",
    "distinctive evidence from the second paper",
]


def _response(marker: str, citations: list[dict[str, object]]) -> str:
    payload = {
        "support_status": "supported",
        "citations": citations,
    }
    return (
        f"Shared claim. {marker}\n<!-- RA_RESULT "
        f"{json.dumps(payload)} RA_RESULT -->"
    )


class AtomicMultiPaperCitationTests(unittest.TestCase):
    def _assert_atomic(
        self, marker: str, citations: list[dict[str, object]]
    ) -> None:
        parsed = parse_chat_result(
            _response(marker, citations), paper_aliases=ALIASES
        )
        self.assertNotIn("P1", parsed.content)
        self.assertNotIn("P2", parsed.content)
        self.assertEqual(
            [(value.paper_id, value.page_hint) for value in parsed.citations],
            [(12, 4), (27, 7)],
        )
        clickable = tuple(
            replace(
                value,
                resolved_page=value.page_hint,
                verified=True,
            )
            for value in parsed.citations
        )
        groups = verified_citation_groups(clickable)
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {int(group[0].paper_id) for group in groups}, {12, 27}
        )

    def test_dot_cluster_is_split(self) -> None:
        self._assert_atomic(
            "[P1.P2]",
            [{
                "claim": "Shared claim.",
                "page_hint": [4, 7],
                "evidence": EVIDENCE,
            }],
        )

    def test_comma_cluster_is_split(self) -> None:
        self._assert_atomic(
            "[P1, P2]",
            [{
                "claim": "Shared claim.",
                "page_hint": [4, 7],
                "evidence": EVIDENCE,
            }],
        )

    def test_page_cluster_is_split(self) -> None:
        marker = "[P1 · p.4, P2 · p.7]"
        self._assert_atomic(
            marker,
            [{
                "claim": "Shared claim.",
                "page_hint": [4, 7],
                "evidence": EVIDENCE,
            }],
        )

    def test_already_separate_markers_remain_atomic(self) -> None:
        self._assert_atomic(
            "[P1 · p.4] [P2 · p.7]",
            [
                {
                    "claim": "Shared claim.",
                    "paper_id": "P1",
                    "page_hint": 4,
                    "evidence": EVIDENCE[0],
                },
                {
                    "claim": "Shared claim.",
                    "paper_id": "P2",
                    "page_hint": 7,
                    "evidence": EVIDENCE[1],
                },
            ],
        )

    def test_cluster_distributes_identityless_metadata_rows(self) -> None:
        self._assert_atomic(
            "[P1 · p.4, P2 · p.7]",
            [
                {
                    "claim": "Shared claim.",
                    "evidence": EVIDENCE[0],
                },
                {
                    "claim": "Shared claim.",
                    "evidence": EVIDENCE[1],
                },
            ],
        )

    def test_alias_is_authoritative_over_conflicting_numeric_id(self) -> None:
        parsed = parse_chat_result(
            _response(
                "",
                [
                    {
                        "claim": "Shared claim.",
                        "paper_id": 12,
                        "paper_alias": "P2",
                        "page_hint": 7,
                        "evidence": EVIDENCE[1],
                    }
                ],
            ),
            paper_aliases=ALIASES,
        )
        self.assertEqual(len(parsed.citations), 1)
        self.assertEqual(parsed.citations[0].paper_id, 27)
        self.assertEqual(parsed.citations[0].alias, "P2")

    def test_nonmember_numeric_paper_id_is_rejected(self) -> None:
        parsed = parse_chat_result(
            _response(
                "",
                [
                    {
                        "claim": "Shared claim.",
                        "paper_id": 999,
                        "page_hint": 4,
                        "evidence": EVIDENCE[0],
                    }
                ],
            ),
            paper_aliases=ALIASES,
        )
        self.assertEqual(parsed.citations, ())
        self.assertEqual(parsed.invalid_citation_count, 1)

    def test_alias_keyed_citation_object_is_flattened(self) -> None:
        payload = {
            "support_status": "supported",
            "citations": {
                "P1": {
                    "claim": "Shared claim.",
                    "page_hint": 4,
                    "evidence": EVIDENCE[0],
                },
                "P2": {
                    "claim": "Shared claim.",
                    "page_hint": 7,
                    "evidence": EVIDENCE[1],
                },
            },
        }
        parsed = parse_chat_result(
            "Shared claim.\n<!--RA_RESULT\n"
            f"{json.dumps(payload)}\nRA_RESULT-->",
            paper_aliases=ALIASES,
        )
        self.assertEqual(
            [(item.paper_id, item.alias) for item in parsed.citations],
            [(12, "P1"), (27, "P2")],
        )

    def test_nested_alias_keyed_sources_are_flattened(self) -> None:
        parsed = parse_chat_result(
            _response(
                "",
                [
                    {
                        "claim": "Shared claim.",
                        "sources": {
                            "P1": {
                                "page_hint": 4,
                                "evidence": EVIDENCE[0],
                            },
                            "P2": {
                                "page_hint": 7,
                                "evidence": EVIDENCE[1],
                            },
                        },
                    }
                ],
            ),
            paper_aliases=ALIASES,
        )
        self.assertEqual(
            [(item.paper_id, item.alias) for item in parsed.citations],
            [(12, "P1"), (27, "P2")],
        )

    def test_fenced_metadata_inside_comment_is_accepted(self) -> None:
        payload = {
            "support_status": "supported",
            "citations": [
                {
                    "claim": "Shared claim.",
                    "paper_alias": "P1",
                    "page_hint": 4,
                    "evidence": EVIDENCE[0],
                }
            ],
        }
        parsed = parse_chat_result(
            "Shared claim.\n<!-- RA_RESULT\n```json\n"
            f"{json.dumps(payload)}\n```\nRA_RESULT -->",
            paper_aliases=ALIASES,
        )
        self.assertEqual(parsed.content, "Shared claim.")
        self.assertEqual(parsed.citations[0].alias, "P1")

    def test_plain_sentinel_metadata_trailer_is_accepted(self) -> None:
        payload = {
            "support_status": "supported",
            "citations": [
                {
                    "claim": "Shared claim.",
                    "paper_alias": "P2",
                    "page_hint": 7,
                    "evidence": EVIDENCE[1],
                }
            ],
        }
        parsed = parse_chat_result(
            f"Shared claim.\nRA_RESULT:\n{json.dumps(payload)}",
            paper_aliases=ALIASES,
        )
        self.assertEqual(parsed.content, "Shared claim.")
        self.assertEqual(parsed.citations[0].alias, "P2")


if __name__ == "__main__":
    unittest.main()
